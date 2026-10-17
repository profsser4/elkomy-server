"""
نقطة تشغيل السيرفر على Vercel.
بتشغّل bot.py زي ما هو من غير تعديل عليه، وبتضيف 3 حاجات:
1) حفظ البيانات في Neon قبل وبعد كل طلب (storage_sync).
2) روابط التشغيل (ref) بتتشفّر بدل ما تتخزّن في الذاكرة، لأن كل طلب ممكن يروح لنسخة تانية.
3) مسار التشغيل الجديد (extras.py).
4) رابط /cron/run بيشغّل المهام الدورية (إشعارات الحلقات ومزامنة Stremio) ويناديه GitHub Actions.
"""
import base64
import hashlib
import hmac
import os

os.environ["DATA_DIR"] = os.environ.get("DATA_DIR") or "/tmp/elkomy"
os.environ["ST_AUTOSYNC"] = "0"      # مفيش خيوط خلفية على Vercel
os.environ["PUSH_WATCHER"] = "0"
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)

import bot  # noqa: E402
import storage_sync  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from flask import jsonify, request  # noqa: E402

app = bot.app

# ---- روابط التشغيل من غير ذاكرة مشتركة
_fernet = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(("refs:" + str(bot.SECRET)).encode()).digest()))


class _Refs:
    def get(self, ref, default=None):
        try:
            return _fernet.decrypt(str(ref).encode(), ttl=6 * 3600).decode()
        except Exception:
            return default


bot.REFS = _Refs()
bot.ref_for = lambda url: _fernet.encrypt(str(url).encode()).decode()


# ---- المهام الدورية
def _cron():
    if request.path != "/cron/run":
        return None
    key = os.environ.get("CRON_KEY", "")
    sent = request.headers.get("X-Cron-Key", "")
    if not key or not hmac.compare_digest(sent.encode(), key.encode()):
        return jsonify(error="غير مصرّح"), 401
    out = {}
    jobs = [("episodes", bot.check_episodes), ("stremio", bot.st_auto_sync)]
    if request.args.get("dub") == "1":
        jobs.append(("dub", bot._dub_scan))
    for name, fn in jobs:
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = "error: " + str(e)[:200]
    return jsonify(out)


app.before_request_funcs.setdefault(None, []).insert(0, _cron)

# مسار التشغيل الجديد (نسخ + بث محوَّل + تحميل)
import extras  # noqa: E402
extras.register(app, bot)

# لازم تتنادى بعد _cron عشان تبقى الأولى في الترتيب (تسحب البيانات قبل أي حاجة)
storage_sync.attach(app, os.environ["DATA_DIR"])
