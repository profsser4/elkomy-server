import os
import io
import re
import json
import uuid
import hmac
import base64
import hashlib
import time
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import quote, unquote, urlparse, urljoin

import requests
from flask import Flask, request, jsonify, Response, g, make_response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024   # حد أقصى لحجم أي طلب
CORS(app)

# ==============================================================
# Variables على Railway: TMDB_KEY, PROWLARR_URL, PROWLARR_KEY,
# SUBDL_KEY, APP_USER, APP_PASS (الافتراضي admin/admin),
# DATA_DIR (اختياري)
# مفاتيح المزوّدين وحساب Stremio بيتضافوا من جوه الموقع مش من الكود
# ==============================================================
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASS = os.environ.get("APP_PASS", "admin")
TMDB_KEY = os.environ.get("TMDB_KEY", "")
SUBDL_KEY = os.environ.get("SUBDL_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_KEY = os.environ.get("PROWLARR_KEY", "")
def _pick_data_dir():
    """أول مجلد قابل للكتابة: DATA_DIR ← /data ← . ← /tmp (Vercel بيسمح بالكتابة في /tmp بس)."""
    cands = [os.environ.get("DATA_DIR", ""), "/data" if os.path.isdir("/data") else "", ".", "/tmp/elkomy"]
    for d in cands:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".w_test")
            with open(t, "w") as f:
                f.write("1")
            os.remove(t)
            return d
        except Exception:
            continue
    return "/tmp"


DATA_DIR = _pick_data_dir()

# ---------------- التخزين الدائم: Upstash Redis (REST) ----------------
# على Vercel الملفات بتتمسح، فكل بيانات JSON بتتحفظ في Redis لو المتغيرات موجودة.
# لو مفيش Redis (تشغيل محلي/سيرفر عادي) بيرجع يستخدم ملفات DATA_DIR زي الأول.
def _env_first(*names):
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


KV_URL = _env_first("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL", "STORAGE_KV_REST_API_URL").rstrip("/")
KV_TOKEN = _env_first("KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN", "STORAGE_KV_REST_API_TOKEN")
KV_ON = bool(KV_URL and KV_TOKEN)
KV_PREFIX = os.environ.get("KV_PREFIX", "elkomy:")
_KV_SES = requests.Session()


def _kv_cmd(*args):
    last = None
    for _ in range(3):
        try:
            r = _KV_SES.post(KV_URL, json=list(args),
                             headers={"Authorization": "Bearer " + KV_TOKEN}, timeout=8)
            d = r.json()
            if r.status_code != 200 or "error" in d:
                raise Exception(str(d.get("error") or r.status_code))
            return d.get("result")
        except Exception as e:
            last = e
            time.sleep(0.15)
    raise last


def kv_get(name):
    """بيرجع الـ object المحفوظ أو None لو مفيش. بيرمي Exception لو Redis مش متاح."""
    v = _kv_cmd("GET", KV_PREFIX + name)
    return None if v is None else json.loads(v)


def kv_set(name, obj):
    _kv_cmd("SET", KV_PREFIX + name, json.dumps(obj, ensure_ascii=False))


def kv_del(name):
    _kv_cmd("DEL", KV_PREFIX + name)


def store_load(path):
    """قراءة JSON: من Redis (الاسم = اسم الملف) أو من الملف. بيرمي Exception لو مفيش بيانات."""
    name = os.path.basename(path)
    if KV_ON:
        v = kv_get(name)
        if v is None:
            raise FileNotFoundError(name)
        return v
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def store_save(path, d):
    name = os.path.basename(path)
    if KV_ON:
        kv_set(name, d)
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


def store_del(path):
    if KV_ON:
        kv_del(os.path.basename(path))
        return
    os.remove(path)

PFILE = os.path.join(DATA_DIR, "providers.json")
UDFILE = os.path.join(DATA_DIR, "userdata.json")
SFILE = os.path.join(DATA_DIR, "stremio.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
HOME_FILE = os.path.join(DATA_DIR, "home.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
ACT_FILE = os.path.join(DATA_DIR, "activity.json")
UD_LOCK = threading.Lock()
ST_LOCK = threading.Lock()
USERS_LOCK = threading.Lock()
REF_LOCK = threading.Lock()
ACT_LOCK = threading.Lock()
LAST = {}
CAT_CACHE = {}
MAX_PROFILES = 10
MAX_ITEMS = 150
def _load_secret():
    s = os.environ.get("SECRET_KEY")
    if s:
        return s
    if KV_ON:
        try:
            v = _kv_cmd("GET", KV_PREFIX + ".secret")
            if not v:
                _kv_cmd("SET", KV_PREFIX + ".secret", uuid.uuid4().hex + uuid.uuid4().hex, "NX")
                v = _kv_cmd("GET", KV_PREFIX + ".secret")
            if v:
                return v
        except Exception:
            pass
    p = os.path.join(DATA_DIR, ".secret")
    try:
        with open(p, encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    v = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
    except OSError:
        return APP_PASS
    return v


SECRET = _load_secret()
if APP_PASS == "admin":
    print("WARNING: APP_PASS الافتراضي (admin). غيّره من Variables فوراً.")
REFS = {}
FAILS = {}
USER_RX = re.compile(r"[a-z0-9_.-]{3,20}")

TYPES = {
    "torbox": "TorBox",
    "alldebrid": "AllDebrid",
    "premiumize": "Premiumize",
    "1fichier": "1Fichier",
    "debridlink": "Debrid-Link",
}
# أنواع بتشتغل على مواقعها بس
DEFAULT_DOMAINS = {
    "1fichier": [
        "1fichier.com", "alterupload.com", "cjoint.net", "desfichiers.com",
        "dfichiers.com", "megadl.fr", "mesfichiers.org", "piecejointe.net",
        "pjointe.com", "tenvoi.com", "dl4free.com",
    ],
}
# ترتيب التجربة للروابط اللي ملهاش دومين محدد (الأسرع الأول)
PRIORITY = {
    "realdebrid": 1, "alldebrid": 2, "debridlink": 3,
    "premiumize": 4, "torbox": 9,
}
ENV_SEED = {
    "debridlink": "DEBRIDLINK_KEY",
    "torbox": "TORBOX_KEY", "1fichier": "ONEFICHIER_KEY",
"alldebrid": "ALLDEBRID_KEY",
    "premiumize": "PREMIUMIZE_KEY",
}

TORBOX_BASE = "https://api.torbox.app/v1/api"
SUBDL_API = "https://api.subdl.com/api/v1/subtitles"
SUBDL_DL = "https://dl.subdl.com"
ST_API = "https://api.strem.io/api"
VIDEO = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v")
TRACKERS = (
    "&tr=udp://tracker.opentrackr.org:1337/announce"
    "&tr=udp://open.stealth.si:80/announce"
    "&tr=udp://tracker.bittor.pw:1337/announce"
)
EP_PATTERNS = (
    re.compile(r"[sS](\d{1,2})[ ._-]*[eE](\d{1,3})"),
    re.compile(r"(?<!\d)(\d{1,2})[xX](\d{2,3})(?!\d)"),
)
EP_LOOSE = (
    re.compile(r"(?<![A-Za-z0-9])(?:Episode|EP|E)[ ._-]?(\d{1,3})(?!\d)", re.I),
    re.compile(r"الحلقة[ ._-]*(\d{1,3})"),
)
CATS = {"movie": "2000", "tv": "5000", "anime": "5070"}
LATIN_RE = re.compile(r"[A-Za-z\u00C0-\u024F]")
NONLAT_RE = re.compile(
    r"[\u0400-\u04FF\u0590-\u06FF\u0750-\u077F\u3040-\u30FF"
    r"\u4E00-\u9FFF\uAC00-\uD7AF]"
)


# ----------------- المزوّدين (محفوظين في ملف) -----------------

def _env_providers():
    out = []
    for t, env in ENV_SEED.items():
        k = os.environ.get(env, "").strip()
        if k:
            out.append({
                "id": "env_" + t, "type": t, "name": TYPES[t],
                "key": k, "domains": DEFAULT_DOMAINS.get(t, []),
            })
    return out


def load():
    """المزوّدين = اللي في الملف + أي مفتاح في Environment Variables (الـ env بيتضاف دايماً لو النوع ده مش موجود).
    كده حتى لو الملف اتمسح (Vercel/ريستارت) المزوّدين الأساسيين يفضلوا شغّالين."""
    ps = []
    try:
        d = store_load(PFILE)
        ps = d if isinstance(d, list) else []
    except Exception:
        ps = []
    ps = [p for p in ps if p.get("type") in TYPES]   # Real-Debrid اتشال من التطبيق: أي مزوّد قديم من نوع مش معروف بيتتجاهل
    have = {p.get("type") for p in ps}
    for e in _env_providers():
        if e["type"] not in have:
            ps.append(e)
    return ps


def save(ps):
    try:
        store_save(PFILE, ps)
    except Exception:
        pass   # نظام الملفات للقراءة فقط: مفاتيح الـ env هي المصدر الأساسي


def dom(x):
    x = x.strip().lower()
    h = urlparse(x if "//" in x else "//" + x).netloc
    return h.replace("www.", "")


def candidates(u):
    host = urlparse(u).netloc.lower().replace("www.", "")
    ps = [p for p in load() if p["type"] != "alldebrid" or AD_SERVER]

    def hit(p):
        return any(
            d and (host == d or host.endswith("." + d))
            for d in p.get("domains", [])
        )

    first = [p for p in ps if hit(p)]
    rest = [
        p for p in ps
        if p not in first and p["type"] not in DEFAULT_DOMAINS
    ]
    rest.sort(key=lambda p: PRIORITY.get(p["type"], 5))
    return first + rest


def jj(r):
    try:
        return r.json()
    except ValueError:
        raise Exception(f"رد غير مفهوم من الموقع (كود {r.status_code})")


# ----------------- المستخدمين والتوكن -----------------

def users_load():
    try:
        d = store_load(USERS_FILE)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def users_save(d):
    store_save(USERS_FILE, d)


def fp_of(s):
    return hashlib.sha256(("fp:" + s).encode()).hexdigest()[:10]


def sign(body):
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def make_token(user, role, fp):
    body = f"{user}|{role}|{int(time.time()) + 30 * 86400}|{fp}"
    return base64.urlsafe_b64encode(body.encode()).decode() + "." + sign(body)


def read_token(tok):
    try:
        b64, sig = str(tok).rsplit(".", 1)
        body = base64.urlsafe_b64decode(b64.encode()).decode()
        if not hmac.compare_digest(sig.encode(), sign(body).encode()):
            return None
        user, role, exp, fp = body.split("|")
        if int(exp) < time.time():
            return None
        if role == "admin":
            if user != APP_USER.lower() or not hmac.compare_digest(fp.encode(), fp_of(APP_PASS).encode()):
                return None
        else:
            rec = users_load().get(user)
            if not rec or fp != fp_of(rec["h"]):
                return None
        return user, role
    except Exception:
        return None


def needs_admin(path, method):
    if path.startswith(("/providers", "/users", "/iptv/admin", "/settings", "/home/", "/activity", "/push/check", "/websearch", "/sitesearch")):
        return True
    if path in ("/stremio/login", "/stremio/sync", "/stremio/logout",
                "/stremio/library"):
        return True
    if path.startswith("/stremio/addon"):
        return not (path == "/stremio/addons" and method == "GET")
    return False


@app.before_request
def guard():
    open_paths = ("/", "/auth", "/ud/beacon")
    if request.method == "OPTIONS" or request.path in open_paths or request.path.startswith("/subx/"):
        return None
    who = read_token(request.headers.get("X-Token", ""))
    if not who:
        return jsonify(error="غير مصرّح"), 401
    g.user, g.role = who
    LAST[g.user] = time.time()
    if g.role != "admin" and needs_admin(request.path, request.method):
        return jsonify(error="للأدمن بس"), 403
    if g.role != "admin" and request.method == "POST" and request.path in ("/play/start", "/generate", "/stremio/resolve", "/tb/stream", "/sub/auto", "/sub/ai"):
        lk, msg = lock_of(g.user)
        if lk >= 2:                                   # تصفح فقط: مفيش مشاهدة ولا تحميل
            return jsonify(error=msg, nodl=True, lock=2), 403
        if lk == 1:                                   # ممنوع التحميل بس
            b = request.get_json(silent=True) or {}
            if (request.path == "/play/start" and b.get("mode") == "download") or \
                    (request.path == "/generate" and (b.get("zip") or b.get("all"))):
                return jsonify(error=msg, nodl=True, lock=1), 403
    return None


@app.get("/")
def home():
    return "Elkomy Server is Active! 🚀"


def too_many(ip, limit=10):
    now = time.time()
    lst = [t for t in FAILS.get(ip, []) if now - t < 600]
    FAILS[ip] = lst
    return len(lst) >= limit


@app.post("/auth")
def auth():
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if too_many(ip):
        return jsonify(ok=False, error="محاولات كتير"), 429
    d = request.get_json(silent=True) or {}
    u = str(d.get("u", "")).strip().lower()
    p = str(d.get("p", "")).strip()
    if len(u) > 64 or len(p) > 200:
        return jsonify(ok=False), 400
    if too_many("u:" + u, 20):
        return jsonify(ok=False, error="محاولات كتير"), 429
    if u == APP_USER.lower() and hmac.compare_digest(p.encode(), APP_PASS.encode()):
        return jsonify(ok=True, role="admin", user=u,
                       token=make_token(u, "admin", fp_of(APP_PASS)))
    rec = users_load().get(u)
    if rec and check_password_hash(rec["h"], p):
        return jsonify(ok=True, role="user", user=u,
                       token=make_token(u, "user", fp_of(rec["h"])))
    FAILS.setdefault(ip, []).append(time.time())
    FAILS.setdefault("u:" + u, []).append(time.time())
    time.sleep(0.5)
    return jsonify(ok=False), 401


# ----------------- اختيار مزوّد المشاهدة والتحميل -----------------
# الإعداد العام (للأدمن وللمستخدمين اللي معندهمش اختيار خاص):
#   watch: "auto" (الأولوية العادية) أو id/نوع المزوّد (torbox, realdebrid, debridlink, alldebrid أو id مزوّد)
#   dl:    "same" (التحميل من نفس مزوّد المشاهدة) أو id/نوع مزوّد تاني للتحميل بس
#   strict: لو true مفيش رجوع لمزوّد تاني لو المختار فشل
# وكل مستخدم ممكن يكون ليه wp (مشاهدة) و dp (تحميل) خاصين في users.json.
SET_DEF = {"watch": "alldebrid", "dl": "same", "strict": False}


def settings_load():
    try:
        d = store_load(SETTINGS_FILE)
        d = d if isinstance(d, dict) else {}
    except Exception:
        d = {}
    out = dict(SET_DEF)
    for k in ("watch", "dl"):
        if isinstance(d.get(k), str) and d[k].strip():
            out[k] = d[k].strip()[:40]
    out["strict"] = bool(d.get("strict"))
    return out


def settings_save(d):
    store_save(SETTINGS_FILE, d)


def prov_pref(user, mode):
    """بيرجّع (المزوّد المفضّل أو None, strict) للمستخدم حسب نوع العملية: watch أو download."""
    st = settings_load()
    rec = users_load().get(user) or {}
    w = str(rec.get("wp") or st["watch"] or "auto")
    if mode == "download":
        d = str(rec.get("dp") or st["dl"] or "same")
        sel = w if d == "same" else d
    else:
        sel = w
    return (None if sel in ("auto", "", "same") else sel), bool(st["strict"])


def prov_ok(v):
    """قيمة صالحة لاختيار مزوّد: فاضي/auto/same أو نوع معروف أو id مزوّد موجود."""
    v = str(v or "").strip()
    if v in ("", "auto", "same") or v in TORRENT_TYPES:
        return True
    return any(p["id"] == v for p in load())


_PROV_WORDS = re.compile(r"(?i)(torbox|all-?debrid|real-?debrid|debrid-?link|stremio|aradeb|1fichier)")


def _hide_accounts(o, names):
    """الأعضاء ما يشوفوش أسماء المزوّدين ولا الحسابات: نشيل provider/stream_note ونبدّل الأسماء في رسائل الخطأ."""
    if isinstance(o, dict):
        for k in list(o.keys()):
            v = o[k]
            if k in ("provider", "stream_note", "stream_notes", "diag"):
                o[k] = "" if isinstance(v, str) else v
            elif k in ("error", "status", "detail", "message") and isinstance(v, str):
                for n in names:
                    v = v.replace(n, "السيرفر")
                o[k] = _PROV_WORDS.sub("السيرفر", v)
            else:
                _hide_accounts(v, names)
    elif isinstance(o, list):
        for x in o:
            _hide_accounts(x, names)


try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    pass


@app.after_request
def sec_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    try:
        if request.path.startswith(("/me", "/users", "/iptv", "/auth", "/settings", "/providers", "/activity")):
            resp.headers["Cache-Control"] = "no-store"
    except Exception:
        pass
    return resp


@app.after_request
def mask_for_members(resp):
    try:
        if getattr(g, "role", "admin") == "admin" or request.method == "OPTIONS":
            return resp
        if resp.mimetype != "application/json" or resp.direct_passthrough:
            return resp
        o = json.loads(resp.get_data(as_text=True))
        names = sorted({str(p.get("name") or "") for p in load() if p.get("name")}, key=len, reverse=True)
        _hide_accounts(o, names)
        resp.set_data(json.dumps(o, ensure_ascii=False))
    except Exception:
        pass
    return resp


IMDBFIX_FILE = os.path.join(DATA_DIR, "imdbfix.json")
IMDBFIX_LOCK = threading.Lock()


@app.get("/imdbfix")
def imdbfix_get():
    """أرقام IMDb اللي اتلقت لأعمال مالهاش رقم على TMDB (مشتركة لكل الأعضاء)"""
    try:
        return jsonify(map=store_load(IMDBFIX_FILE))
    except Exception:
        return jsonify(map={})


@app.post("/imdbfix")
def imdbfix_set():
    d = request.get_json(silent=True) or {}
    ty = "tv" if d.get("type") == "tv" else "movie"
    tid = str(d.get("id") or "").strip()
    im = str(d.get("imdb") or "").strip()
    if not tid.isdigit() or not re.fullmatch(r"tt\d{5,10}", im):
        return jsonify(error="بيانات غلط"), 400
    with IMDBFIX_LOCK:
        try:
            m = store_load(IMDBFIX_FILE)
        except Exception:
            m = {}
        m[ty + ":" + tid] = im
        if len(m) > 5000:
            m = dict(list(m.items())[-5000:])
        store_save(IMDBFIX_FILE, m)
    return jsonify(ok=True)


@app.get("/settings")
def settings_get():
    ps = [{"id": p["id"], "name": p["name"], "type": p["type"]}
          for p in load() if p["type"] in TORRENT_TYPES]
    return jsonify(settings=settings_load(), providers=ps)


@app.post("/settings")
def settings_set():
    d = request.get_json(silent=True) or {}
    cur = settings_load()
    for k, dflt in (("watch", "auto"), ("dl", "same")):
        if k in d:
            v = str(d.get(k) or dflt).strip()
            if not prov_ok(v):
                return jsonify(error="المزوّد ده مش موجود"), 400
            cur[k] = v or dflt
    if "strict" in d:
        cur["strict"] = bool(d["strict"])
    settings_save(cur)
    return jsonify(ok=True, settings=cur)


FREE_MSG = "حسابك مجاني: المشاهدة والتحميل متاحين للأعضاء المميزين بس. كلّمني للاشتراك."
EXPIRED_MSG = "انتهى اشتراكك المميز. كلّمني للتجديد."


def plan_of(rec, now=None):
    """باقة العضو: free (مجاني) / premium (مميز، بمدة أو دائم) / expired (انتهت مدته).
    الأعضاء القدام (من غير حقل plan) بيتعاملوا كمميز دائم، فمفيش حاجة بتتغيّر عندهم."""
    now = now or time.time()
    p = rec.get("plan")
    try:
        until = float(rec.get("until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    if p is None:
        return {"state": "premium", "until": 0, "left": None, "legacy": True}
    if p == "free":
        return {"state": "free", "until": 0, "left": None, "legacy": False}
    if until and until < now:
        return {"state": "expired", "until": int(until), "left": 0, "legacy": False}
    left = int((until - now + 86399) // 86400) if until else None
    return {"state": "premium", "until": int(until), "left": left, "legacy": False}


def lock_of(user):
    """مستوى الصلاحية: 0 عادي، 1 ممنوع التحميل، 2 تصفح فقط (ممنوع المشاهدة والتحميل) + رسالة الأدمن.
    العضو المجاني أو اللي انتهت مدته المميزة = تصفح فقط تلقائيًا."""
    rec = users_load().get(user) or {}
    lk = rec.get("lock")
    if lk is None:
        lk = 1 if rec.get("nodl") else 0
    try:
        lk = int(lk)
    except (TypeError, ValueError):
        lk = 0
    msg = rec.get("nodl_msg") or ("المشاهدة والتحميل مش متاحين لحسابك." if lk >= 2 else "التحميل مش متاح لحسابك.")
    st = plan_of(rec)["state"]
    if st in ("free", "expired"):
        lk = 2
        if not rec.get("nodl_msg"):
            msg = FREE_MSG if st == "free" else EXPIRED_MSG
    return lk, msg


@app.get("/me")
def me():
    out = {"user": g.user, "role": g.role}
    if g.role == "admin":
        out["plan"] = {"state": "admin", "until": 0, "left": None}
    else:
        pl = plan_of(users_load().get(g.user) or {})
        out["plan"] = {"state": pl["state"], "until": pl["until"], "left": pl["left"]}
    if g.role != "admin":
        lk, msg = lock_of(g.user)
        out["lock"] = lk
        out["lock_msg"] = msg
        out["watch_prov"] = prov_pref(g.user, "watch")[0] or "auto"
        out["dl_prov"] = prov_pref(g.user, "download")[0] or "auto"
    return jsonify(out)


@app.get("/users")
def users_list():
    us = users_load()
    items = []
    for n, v in sorted(us.items()):
        pl = plan_of(v)
        items.append({"name": n, "c": v.get("c", 0), "lock": lock_of(n)[0],
                      "msg": v.get("nodl_msg", ""),
                      "wp": v.get("wp", ""), "dp": v.get("dp", ""),
                      "plan": pl["state"], "until": pl["until"], "left": pl["left"],
                      "legacy": pl["legacy"]})
    return jsonify(items=items)


@app.post("/users")
def users_add():
    d = request.get_json(silent=True) or {}
    n = str(d.get("username", "")).strip().lower()
    p = str(d.get("password", "")).strip()
    if not USER_RX.fullmatch(n):
        return jsonify(error="الاسم 3-20 حرف إنجليزي أو أرقام أو _ . -"), 400
    if n == APP_USER.lower():
        return jsonify(error="الاسم ده محجوز للأدمن"), 400
    if len(p) < 6:
        return jsonify(error="الباسورد لازم 6 حروف على الأقل"), 400
    with USERS_LOCK:
        us = users_load()
        if n in us:
            return jsonify(error="المستخدم موجود بالفعل"), 400
        # العضو الجديد مجاني: يتصفح بس (مفيش مشاهدة ولا تحميل ولا IPTV) لحد ما الأدمن يفعّل له باقة مميزة
        us[n] = {"h": generate_password_hash(p), "c": int(time.time()), "plan": "free", "plan_at": int(time.time())}
        users_save(us)
    return jsonify(ok=True)


@app.post("/users/<name>/password")
def users_pass(name):
    p = str((request.get_json(silent=True) or {}).get("password", "")).strip()
    if len(p) < 6:
        return jsonify(error="الباسورد لازم 6 حروف على الأقل"), 400
    with USERS_LOCK:
        us = users_load()
        if name not in us:
            return jsonify(error="المستخدم مش موجود"), 404
        us[name]["h"] = generate_password_hash(p)
        users_save(us)
    return jsonify(ok=True)


@app.post("/users/<name>/perms")
def users_perms(name):
    d = request.get_json(silent=True) or {}
    with USERS_LOCK:
        us = users_load()
        if name not in us:
            return jsonify(error="المستخدم مش موجود"), 404
        try:
            lk = max(0, min(2, int(d.get("lock") or 0)))
        except (TypeError, ValueError):
            lk = 0
        us[name]["lock"] = lk
        us[name].pop("nodl", None)
        us[name]["nodl_msg"] = str(d.get("msg") or "").strip()[:300]
        for k in ("wp", "dp"):
            if k in d:
                v = str(d.get(k) or "").strip()
                if not prov_ok(v):
                    return jsonify(error="المزوّد ده مش موجود"), 400
                if v in ("", "auto", "same"):
                    us[name].pop(k, None)
                else:
                    us[name][k] = v
        users_save(us)
    return jsonify(ok=True)


@app.post("/users/<name>/plan")
def users_plan(name):
    """تحديد باقة العضو: plan = free | premium. للمميز: days/months (0 و0 = دائم). extend=true بيزوّد على المدة الحالية."""
    d = request.get_json(silent=True) or {}
    plan = str(d.get("plan") or "").strip().lower()
    if plan not in ("free", "premium"):
        return jsonify(error="الباقة لازم تكون free أو premium"), 400
    try:
        days = max(0, min(36500, int(d.get("days") or 0)))
        months = max(0, min(1200, int(d.get("months") or 0)))
    except (TypeError, ValueError):
        return jsonify(error="المدة لازم تكون أرقام"), 400
    with USERS_LOCK:
        us = users_load()
        if name not in us:
            return jsonify(error="المستخدم مش موجود"), 404
        rec = us[name]
        now = time.time()
        if plan == "free":
            rec["plan"] = "free"
            rec.pop("until", None)
        else:
            span = (days + months * 30) * 86400
            if span <= 0:
                rec["plan"] = "premium"
                rec["until"] = 0                     # دائم
            else:
                base = now
                if d.get("extend"):
                    cur = plan_of(rec, now)
                    if cur["state"] == "premium" and cur["until"]:
                        base = cur["until"]
                rec["plan"] = "premium"
                rec["until"] = int(base + span)
        rec["plan_at"] = int(now)
        users_save(us)
        pl = plan_of(rec)
    return jsonify(ok=True, plan=pl["state"], until=pl["until"], left=pl["left"])


@app.get("/users/export")
def users_export():
    """نسخة احتياطية للأدمن: كل المستخدمين (بالباقات) + سجل النشاط. بتتنزّل كملف JSON."""
    us = {n: {k: v for k, v in (r or {}).items() if k != "h"} for n, r in users_load().items()}   # من غير هاشات الباسورد
    data = {"users": us, "activity": act_load(), "at": int(time.time())}
    resp = make_response(json.dumps(data, ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=elkomy-members-backup.json"
    return resp


@app.delete("/users/<name>")
def users_del(name):
    with USERS_LOCK:
        us = users_load()
        if name not in us:
            return jsonify(error="المستخدم مش موجود"), 404
        us.pop(name)
        users_save(us)
    try:
        store_del(ud_path(name))
    except Exception:
        pass
    with IPTV_LOCK:
        al = iptv_load()
        if al.pop(name, None) is not None:
            iptv_save(al)
    return jsonify(ok=True)


# ----------------- IPTV: حساب خاص لكل عضو (الأدمن بس هو اللي بيضيفه) -----------------
# السيرفر بيحفظ الهوست/اليوزر/الباس وتاريخ الانتهاء والتثبيتات بس.
# مفيش أي اتصال بالـIPTV من السيرفر: الـIPTV عايز آيبي مصري، فالجهاز نفسه (التطبيق/المتصفح) هو اللي بيكلمه.
IPTV_FILE = os.path.join(DATA_DIR, "iptv.json")
IPTV_LOCK = threading.Lock()


def iptv_load():
    try:
        d = store_load(IPTV_FILE)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def iptv_save(d):
    store_save(IPTV_FILE, d)


def iptv_host(h):
    """بيشيل أي /c/ أو player_api.php أو مسارات زيادة ويسيب http(s)://host:port بس"""
    h = str(h or "").strip()
    if not h:
        return ""
    if not re.match(r"^https?://", h, re.I):
        h = "http://" + h
    try:
        u = urlparse(h)
        if not u.hostname:
            return ""
        return f"{u.scheme.lower()}://{u.netloc}"[:200]
    except Exception:
        return ""


def iptv_pub(rec, with_pass=True):
    if not rec or not rec.get("host"):
        return {"set": False}
    o = {"set": True, "host": rec["host"], "user": rec.get("user", ""),
         "stat": rec.get("stat") or {}}
    if with_pass:
        o["pass"] = rec.get("pass", "")
    return o


IPTV_ALL = "__all__"      # حساب IPTV مشترك لأي عضو ملوش حساب خاص
IPTV_PINS = "__pins__"    # تثبيتات الصفحة الرئيسية اللي الأدمن بيعملها للكل


def member_locked(user=None):
    """مجاني أو انتهى اشتراكه (والأدمن مستثنى)."""
    if getattr(g, "role", "") == "admin":
        return False
    rec = users_load().get(user or g.user) or {}
    return plan_of(rec)["state"] in ("free", "expired")


@app.get("/iptv/cfg")
def iptv_cfg():
    if member_locked():
        return jsonify(set=False, locked=True, error="الـIPTV للأعضاء المميزين بس")
    al = iptv_load()
    rec = al.get(g.user) or {}
    shared = False
    if not rec.get("host"):
        rec = al.get(IPTV_ALL) or {}
        shared = bool(rec.get("host"))
    o = iptv_pub(rec)
    if shared:
        o["shared"] = True
    return jsonify(o)


@app.post("/iptv/stat")
def iptv_stat():
    if member_locked():
        return jsonify(error="الـIPTV للأعضاء المميزين بس", locked=True), 403
    """الجهاز بيبلّغ بحالة الاشتراك (تاريخ الانتهاء/عدد الاتصالات) عشان تظهر للأدمن وللعضو"""
    d = request.get_json(silent=True) or {}

    def num(k):
        try:
            return max(0, int(float(d.get(k) or 0)))
        except (TypeError, ValueError):
            return 0
    with IPTV_LOCK:
        al = iptv_load()
        rec = al.get(g.user)
        if not rec or not rec.get("host"):
            rec = al.get(IPTV_ALL)
        if not rec or not rec.get("host"):
            return jsonify(error="مفيش اشتراك IPTV لحسابك"), 404
        rec["stat"] = {"exp": num("exp"), "maxc": num("maxc"), "active": num("active"),
                       "status": str(d.get("status") or "")[:30],
                       "trial": bool(d.get("trial")), "t": int(time.time())}
        iptv_save(al)
    return jsonify(ok=True)


@app.get("/iptv/pins")
def iptv_pins_get():
    pid = str(request.args.get("pid", ""))[:40]
    al = iptv_load()
    own = ((al.get(g.user) or {}).get("pins") or {}).get(pid) or {}
    gp = al.get(IPTV_PINS) or {}
    gl = [str(x) for x in (gp.get("live") or [])]
    gc = [c for c in (gp.get("cats") or []) if isinstance(c, dict)]
    merged = dict(own)
    merged["live"] = gl + [x for x in (own.get("live") or []) if x not in gl]
    merged["cats"] = gc + [c for c in (own.get("cats") or [])
                           if not any(c.get("k") == x.get("k") and c.get("id") == x.get("id") for x in gc)]
    return jsonify(pins=merged, g={"live": gl, "cats": gc, "ln": gp.get("ln") or {}})


@app.post("/iptv/pins")
def iptv_pins_set():
    """تثبيتات كل بروفايل: {live:[stream_id...], cats:[{k:'live|vod|series', id:'..', n:'اسم'}]}
    ولو global=true (أدمن بس) بتتحفظ للكل وبتظهر في رئيسية كل الأعضاء."""
    d = request.get_json(silent=True) or {}
    p = d.get("pins") if isinstance(d.get("pins"), dict) else {}
    live = [str(x)[:20] for x in (p.get("live") or [])][:200]
    cats = []
    for c in (p.get("cats") or [])[:60]:
        if isinstance(c, dict) and c.get("k") in ("live", "vod", "series"):
            cats.append({"k": c["k"], "id": str(c.get("id", ""))[:20], "n": str(c.get("n", ""))[:80]})
    if d.get("global"):
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        with IPTV_LOCK:
            al = iptv_load()
            ln = {}                      # أسماء القنوات المثبّتة للكل (عشان تتلاقي بالاسم عند مزوّد تاني)
            if isinstance(p.get("ln"), dict):
                for i, n in list(p["ln"].items())[:200]:
                    if str(i)[:20] in live and str(n).strip():
                        ln[str(i)[:20]] = str(n).strip()[:80]
            al[IPTV_PINS] = {"live": live, "cats": cats, "ln": ln}
            iptv_save(al)
        return jsonify(ok=True)
    pid = str(d.get("pid", ""))[:40]
    if not pid:
        return jsonify(error="بروفايل ناقص"), 400

    def idmap(x):                      # ترتيب الأقسام وإخفاؤها لكل تبويب (live / vod / series)
        out = {}
        if isinstance(x, dict):
            for k in ("live", "vod", "series"):
                v = x.get(k)
                if isinstance(v, list):
                    out[k] = [str(i)[:20] for i in v][:800]
        return out
    order, hidden = idmap(p.get("order")), idmap(p.get("hidden"))
    names = {}                           # أسماء مخصّصة للأقسام: {kind: {category_id: "اسم"}}
    nm = p.get("names")
    if isinstance(nm, dict):
        for k in ("live", "vod", "series"):
            v = nm.get(k)
            if isinstance(v, dict):
                d2 = {str(i)[:20]: str(n).strip()[:80] for i, n in list(v.items())[:800] if str(n).strip()}
                if d2:
                    names[k] = d2
    with IPTV_LOCK:
        al = iptv_load()
        rec = al.get(g.user)
        if not rec or not rec.get("host"):
            if not (al.get(IPTV_ALL) or {}).get("host"):
                return jsonify(error="مفيش اشتراك IPTV لحسابك"), 404
            rec = al.setdefault(g.user, {})
        gp = al.get(IPTV_PINS) or {}
        gl = {str(x) for x in (gp.get("live") or [])}
        gc = {(c.get("k"), c.get("id")) for c in (gp.get("cats") or []) if isinstance(c, dict)}
        live = [x for x in live if x not in gl]                      # تثبيتات الكل مابتتنسخش على حساب العضو
        cats = [c for c in cats if (c["k"], c["id"]) not in gc]
        rec.setdefault("pins", {})[pid] = {"live": live, "cats": cats, "order": order, "hidden": hidden, "names": names}
        iptv_save(al)
    return jsonify(ok=True)


IPTV_FEAT = "__feat__"    # ماتشات مميزة + بانرات إعلان + إعلان منبثق (الأدمن بس بيعدّلها)


def _fs(x, n):
    return str(x or "").strip()[:n]


def _furl(x):
    x = _fs(x, 600)
    return x if x.lower().startswith(("http://", "https://")) else ""


def _fdate(x):
    x = _fs(x, 10)
    return x if re.match(r"^\d{4}-\d{2}-\d{2}$", x) else ""


def feat_clean(d):
    out = {"mt": _fs(d.get("mt"), 60), "matches": [], "banners": [], "popup": {"on": False}}
    for m in (d.get("matches") or [])[:12]:
        if isinstance(m, dict) and _fs(m.get("ch"), 20):
            out["matches"].append({"id": _fs(m.get("id"), 20) or uuid.uuid4().hex[:8], "ch": _fs(m.get("ch"), 20),
                                   "n": _fs(m.get("n"), 80), "icon": _furl(m.get("icon")),
                                   "title": _fs(m.get("title"), 80), "time": _fs(m.get("time"), 40),
                                   "until": _fdate(m.get("until"))})
    for b in (d.get("banners") or [])[:10]:
        if isinstance(b, dict) and _fs(b.get("title"), 80):
            k = b.get("k") if b.get("k") in ("vod", "series") else ""
            out["banners"].append({"id": _fs(b.get("id"), 20) or uuid.uuid4().hex[:8], "k": k,
                                   "sid": _fs(b.get("sid"), 20) if k else "", "title": _fs(b.get("title"), 80),
                                   "sub": _fs(b.get("sub"), 160), "img": _furl(b.get("img")),
                                   "tag": _fs(b.get("tag"), 30), "nm": _fs(b.get("nm"), 80), "until": _fdate(b.get("until"))})
    p = d.get("popup")
    if isinstance(p, dict):
        k = p.get("k") if p.get("k") in ("vod", "series") else ""
        out["popup"] = {"on": bool(p.get("on")), "id": _fs(p.get("id"), 20), "title": _fs(p.get("title"), 80),
                        "text": _fs(p.get("text"), 300), "img": _furl(p.get("img")), "k": k,
                        "sid": _fs(p.get("sid"), 20) if k else "", "nm": _fs(p.get("nm"), 80), "until": _fdate(p.get("until"))}
    return out


@app.get("/iptv/feat")
def iptv_feat_get():
    return jsonify((iptv_load().get(IPTV_FEAT) or {}))


@app.post("/iptv/feat")
def iptv_feat_set():
    if g.role != "admin":
        return jsonify(error="للأدمن بس"), 403
    d = request.get_json(silent=True) or {}
    with IPTV_LOCK:
        al = iptv_load()
        al[IPTV_FEAT] = feat_clean(d)
        iptv_save(al)
    return jsonify(ok=True)



@app.get("/iptv/admin")
def iptv_admin_list():
    al = iptv_load()
    names = sorted(set(users_load().keys()) | {APP_USER.lower()})
    out = []
    for n in [IPTV_ALL] + names:
        o = iptv_pub(al.get(n))
        o["name"] = n
        out.append(o)
    return jsonify(items=out)


@app.post("/iptv/admin/<name>")
def iptv_admin_set(name):
    name = name.strip().lower()
    if name != IPTV_ALL and name != APP_USER.lower() and name not in users_load():
        return jsonify(error="المستخدم مش موجود"), 404
    d = request.get_json(silent=True) or {}
    with IPTV_LOCK:
        al = iptv_load()
        if d.get("clear"):
            al.pop(name, None)
            iptv_save(al)
            return jsonify(ok=True)
        host = iptv_host(d.get("host"))
        user = str(d.get("user", "")).strip()[:80]
        pw = str(d.get("pass", "")).strip()[:80]
        if not host or not user or not pw:
            return jsonify(error="الهوست واليوزر والباس كلهم مطلوبين"), 400
        old = al.get(name) or {}
        same = old.get("host") == host and old.get("user") == user
        al[name] = {"host": host, "user": user, "pass": pw,
                    "stat": old.get("stat") if same else {},
                    "pins": old.get("pins") or {}}
        iptv_save(al)
    return jsonify(ok=True, host=host)


@app.get("/providers")
def providers_get():
    items = [{
        "id": p["id"], "name": p["name"], "type": p["type"],
        "domains": p.get("domains", []), "key": "••••" + p["key"][-4:],
    } for p in load()]
    return jsonify(types=TYPES, items=items)


@app.post("/providers")
def providers_add():
    d = request.get_json() or {}
    t = d.get("type")
    k = (d.get("key") or "").strip()
    if t not in TYPES or not k:
        return jsonify(error="النوع أو المفتاح ناقص"), 400
    raw = re.split(r"[,\s]+", d.get("domains") or "")
    doms = [dom(x) for x in raw if x.strip()]
    doms = doms or DEFAULT_DOMAINS.get(t, [])
    ps = load()
    ps.append({
        "id": uuid.uuid4().hex[:8], "type": t,
        "name": (d.get("name") or TYPES[t]).strip(),
        "key": k, "domains": doms,
    })
    save(ps)
    return jsonify(ok=True)


@app.delete("/providers/<pid>")
def providers_del(pid):
    save([p for p in load() if p["id"] != pid])
    return jsonify(ok=True)


@app.get("/providers/export")
def providers_export():
    return jsonify(items=load())


@app.post("/providers/import")
def providers_import():
    if load():
        return jsonify(ok=False)
    got = (request.get_json() or {}).get("items") or []
    items = [
        x for x in got
        if isinstance(x, dict) and x.get("type") in TYPES and x.get("key")
    ]
    for x in items:
        x.setdefault("id", uuid.uuid4().hex[:8])
        x.setdefault("name", TYPES[x["type"]])
        x.setdefault("domains", DEFAULT_DOMAINS.get(x["type"], []))
    save(items)
    return jsonify(ok=True)


@app.post("/providers/<pid>/test")
def providers_test(pid):
    p = next((x for x in load() if x["id"] == pid), None)
    if not p:
        return jsonify(ok=False, error="مش موجود"), 404
    k, t = p["key"], p["type"]
    h = {"Authorization": f"Bearer {k}"}
    try:
        if t == "torbox":
            ok = tb(k, "GET", "/user/me").get("success")
        elif t == "realdebrid":
            r = requests.get("https://api.real-debrid.com/rest/1.0/user",
                             headers=h, timeout=15)
            ok = r.status_code == 200
        elif t == "alldebrid":
            r = requests.get("https://api.alldebrid.com/v4/user",
                             params={"agent": "Elkomy", "apikey": k}, timeout=15)
            ok = jj(r).get("status") == "success"
        elif t == "premiumize":
            r = requests.get("https://www.premiumize.me/api/account/info",
                             params={"apikey": k}, timeout=15)
            ok = jj(r).get("status") == "success"
        elif t == "debridlink":
            r = requests.get("https://debrid-link.com/api/v2/account/infos",
                             headers=h, timeout=15)
            ok = jj(r).get("success") is True
        else:
            r = requests.post("https://api.1fichier.com/v1/user/info.cgi",
                              headers=h, json={}, timeout=15)
            ok = r.status_code == 200 and jj(r).get("status") != "KO"
        return jsonify(ok=bool(ok))
    except Exception as e:
        return jsonify(ok=False, error=str(e))


# ----------------- بيانات المستخدمين (بروفايلات + تقدّم المشاهدة) -----------------

def tnum(x):
    t = x.get("t") if isinstance(x, dict) else 0
    return t if isinstance(t, (int, float)) else 0


def ud_path(user):
    if user == APP_USER.lower():
        return UDFILE
    return os.path.join(DATA_DIR, f"ud_{user}.json")


def ud_load(user):
    try:
        d = store_load(ud_path(user))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("profiles", {})
    d.setdefault("items", {})
    return d


def ud_save(d, user):
    store_save(ud_path(user), d)


def ud_merge(base, inc):
    if not isinstance(inc, dict):
        return base
    profs = inc.get("profiles")
    for pid, p in (profs.items() if isinstance(profs, dict) else []):
        if not isinstance(p, dict) or not isinstance(pid, str) or len(pid) > 24:
            continue
        o = base["profiles"].get(pid)
        if o is None and not p.get("del"):
            live = [x for x in base["profiles"].values() if not x.get("del")]
            if len(live) >= MAX_PROFILES:
                continue
        if o is None or tnum(p) > tnum(o):
            base["profiles"][pid] = p
    allit = inc.get("items")
    for pid, items in (allit.items() if isinstance(allit, dict) else []):
        if pid not in base["profiles"] or not isinstance(items, dict):
            continue
        dst = base["items"].setdefault(pid, {})
        for k, it in items.items():
            if not isinstance(it, dict) or len(str(k)) > 90:
                continue
            if len(json.dumps(it)) > 8000:
                continue
            o = dst.get(k)
            if o is None or tnum(it) > tnum(o):
                dst[k] = it
        live = sorted(
            [(tnum(v), k) for k, v in dst.items() if not v.get("del")],
            reverse=True,
        )
        for _, k in live[MAX_ITEMS:]:
            dst.pop(k, None)
    cutoff = (time.time() - 60 * 86400) * 1000
    for dst in base["items"].values():
        old = [k for k, v in dst.items() if v.get("del") and tnum(v) < cutoff]
        for k in old:
            dst.pop(k, None)
    return base


@app.get("/ud")
def ud_get():
    with UD_LOCK:
        return jsonify(ud_load(g.user))


@app.post("/ud/merge")
def ud_merge_ep():
    inc = request.get_json(silent=True) or {}
    with UD_LOCK:
        base = ud_merge(ud_load(g.user), inc)
        ud_save(base, g.user)
    if request.args.get("lite"):
        return jsonify(ok=True)
    return jsonify(base)


@app.post("/ud/beacon")
def ud_beacon():
    try:
        d = json.loads(request.get_data(as_text=True) or "{}")
    except ValueError:
        return "", 400
    who = read_token(str(d.get("t", "")))
    if not who:
        return "", 401
    with UD_LOCK:
        base = ud_merge(ud_load(who[0]), d.get("data") or {})
        ud_save(base, who[0])
    return "", 204


# ----------------- TorBox -----------------

def tb(key, method, path, **kw):
    r = requests.request(method, TORBOX_BASE + path,
                         headers={"Authorization": f"Bearer {key}"},
                         timeout=20, **kw)
    try:
        return r.json()
    except ValueError:
        return {"success": False, "detail": f"TorBox رد غير مفهوم ({r.status_code})"}


def tb_provider():
    return next((p for p in load() if p["type"] == "torbox"), None)


def as_dict(info):
    if isinstance(info, list):
        info = info[0] if info else {}
    return info if isinstance(info, dict) else {}


def is_ready(info):
    return bool(info) and bool(
        info.get("download_finished") or info.get("download_present")
    )


def prog(info, prov):
    return {
        "provider": prov, "status": info.get("download_state"),
        "progress": info.get("progress"), "speed": info.get("download_speed"),
        "eta": info.get("eta"), "seeds": info.get("seeds"),
        "peers": info.get("peers"), "size": info.get("size"),
        "file": info.get("name"),
    }


def parse_ep(name):
    for rx in EP_PATTERNS:
        m = rx.search(name)
        if m:
            return int(m.group(1)), int(m.group(2))
    base = name.rsplit("/", 1)[-1]
    for rx in EP_LOOSE:
        m = rx.search(base)
        if m:
            return None, int(m.group(1))
    return None, None


def video_files(info):
    out = []
    for f in info.get("files") or []:
        full = f.get("name") or ""
        short = f.get("short_name") or full.rsplit("/", 1)[-1]
        if not short.lower().endswith(VIDEO):
            continue
        s, e = parse_ep(full)
        out.append({
            "id": f["id"], "name": short, "size": f.get("size") or 0,
            "season": s, "episode": e,
        })
    out.sort(key=lambda x: (
        x["season"] is None, x["season"] or 0,
        x["episode"] is None, x["episode"] or 0, x["name"].lower(),
    ))
    return out


def main_file(vids, info):
    eps = [v for v in vids if v["episode"] is not None]
    if len(eps) > 1:
        return eps[0]
    if vids:
        return max(vids, key=lambda v: v["size"])
    allf = info.get("files") or []
    if not allf:
        return None
    f = max(allf, key=lambda x: x.get("size") or 0)
    name = f.get("short_name") or (f.get("name") or "").rsplit("/", 1)[-1]
    return {"id": f["id"], "name": name, "size": f.get("size") or 0,
            "season": None, "episode": None}


def tb_link(k, tid, file_id=None, zip_=False):
    params = {"token": k, "torrent_id": tid}
    if zip_:
        params["zip_link"] = "true"
    else:
        params["file_id"] = file_id
    dl = tb(k, "GET", "/torrents/requestdl", params=params)
    if dl.get("success") and dl.get("data"):
        return dl["data"]
    raise Exception(dl.get("detail") or dl.get("error") or "TorBox رفض إنشاء الرابط")


def tb_hls(k, tid, file_id):
    """لينك المشاهدة (HLS) من TorBox. بيختلف عن لينك التحميل الأصلي (direct_link)."""
    note = ""
    for i in range(2):
        try:
            r = tb(k, "GET", "/stream/createstream",
                   params={"id": tid, "file_id": file_id, "type": "torrent"})
            d = r.get("data") if isinstance(r, dict) else None
            u = d.get("hls_url") or d.get("hlsUrl") if isinstance(d, dict) else ""
            if u:
                return u, ""
            note = str((r or {}).get("detail") or (r or {}).get("error") or "TorBox رجّع رد من غير رابط HLS")
        except Exception as e:
            note = f"createstream: {str(e)[:100]}"
        if i == 0:
            time.sleep(1.5)
    return "", note


def find_existing(k, q):
    m = re.search(r"btih:([a-zA-Z0-9]+)", q, re.I)
    if not m:
        return None
    h = m.group(1).lower()
    items = tb(k, "GET", "/torrents/mylist",
               params={"bypass_cache": "true"}).get("data") or []
    for x in (items if isinstance(items, list) else []):
        if str(x.get("hash", "")).lower() == h:
            return x.get("id")
    return None


def clean_torbox(k):
    items = tb(k, "GET", "/webdl/mylist").get("data") or []
    for x in (items if isinstance(items, list) else []):
        if not (x.get("download_finished") or x.get("download_present")):
            tb(k, "POST", "/webdl/controlwebdownload",
               json={"webdl_id": x["id"], "operation": "delete"})


def torbox_web(p, q, web_id=None):
    k = p["key"]
    if not web_id:
        add = tb(k, "POST", "/webdl/createwebdownload", data={"link": q})
        msg = str(add.get("detail") or add.get("error") or "").lower()
        if not add.get("success") and "limit" in msg:
            clean_torbox(k)
            add = tb(k, "POST", "/webdl/createwebdownload", data={"link": q})
        if not add.get("success"):
            raise Exception(add.get("detail") or add.get("error") or "الرابط غير مدعوم")
        d = add.get("data") or {}
        web_id = d.get("webdownload_id") or d.get("id")
    raw = tb(k, "GET", "/webdl/mylist",
             params={"id": web_id, "bypass_cache": "true"}).get("data")
    info = as_dict(raw)
    tid = f"web:{p['id']}:{web_id}"
    st = str(info.get("download_state", "")).lower()
    if st in ("error", "failed", "expired"):
        raise Exception(f"فشل التحميل ({st})")
    if info and not is_ready(info):
        return {"error": "جاري التجهيز...", "torrent_id": tid,
                **prog(info, p["name"])}, 202
    dl = tb(k, "GET", "/webdl/requestdl",
            params={"token": k, "web_id": web_id, "file_id": 0})
    if dl.get("success"):
        return {"direct_link": dl["data"], "name": info.get("name") or "ملف",
                "size": info.get("size"), "provider": p["name"]}, 200
    return {"error": "جاري التجهيز...", "torrent_id": tid,
            "provider": p["name"]}, 202


def torrent_flow(q, tid, file_id=None, want_zip=False, want_all=False):
    p = tb_provider()
    if not p:
        return jsonify(error="ضيف TorBox من إعدادات المزوّدين الأول"), 400
    k = p["key"]
    if not tid and q:
        tid = find_existing(k, q)
    if not tid:
        if "&tr=" not in q:
            q += TRACKERS
        add = tb(k, "POST", "/torrents/createtorrent",
                 data={"magnet": q, "seed": 1})
        if not add.get("success"):
            msg = add.get("detail") or add.get("error") or "فشل إضافة التورنت"
            return jsonify(error=msg), 400
        tid = (add.get("data") or {}).get("torrent_id")
        if not tid:
            return jsonify(
                error="TorBox حطّ التورنت في الطابور (وصلت للحد). جرّب بعد شوية"
            ), 400
    raw = tb(k, "GET", "/torrents/mylist",
             params={"id": tid, "bypass_cache": "true"}).get("data")
    info = as_dict(raw)
    if not is_ready(info):
        return jsonify(error="جاري التجهيز...", torrent_id=tid,
                       **prog(info, p["name"])), 202

    vids = video_files(info)
    try:
        # رابط ZIP للتورنت كله (الموسم كله)
        if want_zip:
            url = tb_link(k, tid, zip_=True)
            name = (info.get("name") or "torrent") + ".zip"
            return jsonify(direct_link=url, name=name, size=info.get("size"),
                           provider=p["name"], zip=True, torrent_id=tid)

        # كل روابط الحلقات مرة واحدة
        if want_all:
            if not vids:
                return jsonify(error="مفيش ملفات فيديو في التورنت"), 400

            def one(v):
                try:
                    return {**v, "url": tb_link(k, tid, v["id"])}
                except Exception as e:
                    return {**v, "url": None, "error": str(e)}

            with ThreadPoolExecutor(max_workers=5) as ex:
                links = list(ex.map(one, vids))
            return jsonify(links=links, provider=p["name"],
                           name=info.get("name"), torrent_id=tid)

        # ملف معيّن (حلقة)
        if file_id is not None:
            f = next((x for x in info.get("files") or []
                      if str(x.get("id")) == str(file_id)), None)
            if not f:
                return jsonify(error="الملف مش موجود في التورنت"), 400
            url = tb_link(k, tid, f["id"])
            name = f.get("short_name") or (f.get("name") or "").rsplit("/", 1)[-1]
            resp = {"direct_link": url, "name": name, "size": f.get("size"),
                    "provider": p["name"], "torrent_id": tid, "file_id": f["id"]}
            hl, hn = tb_hls(k, tid, f["id"])
            if hl:
                resp["hls"] = hl
            elif hn:
                resp["stream_note"] = hn
            if len(vids) > 1:
                resp["pack"] = True
                resp["files"] = vids
                resp["total_size"] = info.get("size")
            return jsonify(resp)

        # الوضع العادي + قايمة الحلقات لو باك
        main = main_file(vids, info)
        if not main:
            return jsonify(error="التورنت فاضي مفيهوش ملفات"), 400
        url = tb_link(k, tid, main["id"])
        resp = {"direct_link": url, "name": main["name"], "size": main["size"],
                "provider": p["name"], "torrent_id": tid, "file_id": main["id"]}
        hl, hn = tb_hls(k, tid, main["id"])
        if hl:
            resp["hls"] = hl
        elif hn:
            resp["stream_note"] = hn
        if len(vids) > 1:
            resp["pack"] = True
            resp["files"] = vids
            resp["total_size"] = info.get("size")
        return jsonify(resp)
    except Exception as e:
        return jsonify(error=f"TorBox: {e}"), 400


# ===== إضافة: بث TorBox باختيار مسار الصوت / الجودة / الترجمة (إضافة فقط، ما بتغيّر أي كود قديم) =====
def tb_hls_opts(k, tid, file_id, audio=None, resolution=None, subtitle=None, typ="torrent"):
    """نفس createstream بس مع chosen_audio_index / chosen_resolution_index / chosen_subtitle_index."""
    params = {"id": tid, "file_id": file_id, "type": typ}
    if audio is not None:
        params["chosen_audio_index"] = int(audio)
    if resolution is not None:
        params["chosen_resolution_index"] = int(resolution)
    if subtitle is not None:
        params["chosen_subtitle_index"] = int(subtitle)
    r = tb(k, "GET", "/stream/createstream", params=params)
    d = r.get("data") if isinstance(r, dict) else None
    d = d if isinstance(d, dict) else {}
    url = d.get("hls_url") or d.get("hlsUrl") or ""
    note = "" if url else str((r or {}).get("detail") or (r or {}).get("error") or "TorBox رجّع رد من غير رابط HLS")
    return url, d, note


def tb_stream_info(k, d):
    """محاولة (best effort) نجيب قايمة المسارات (صوت/ترجمة/جودة). لو فشلت بترجع None من غير ما توقّف حاجة."""
    try:
        pt, tk = d.get("presigned_token"), d.get("token")
        if not (pt and tk):
            return None
        r = tb(k, "GET", "/stream/getstreamdata", params={"presigned_token": pt, "token": tk})
        return r.get("data") if isinstance(r, dict) else None
    except Exception:
        return None


@app.post("/tb/stream")
def tb_stream_route():
    b = request.get_json(silent=True) or {}
    p = tb_provider()
    if not p:
        return jsonify(error="ضيف TorBox من إعدادات المزوّدين الأول"), 400
    tid, fid = b.get("torrent_id"), b.get("file_id")
    if tid in (None, "") or fid in (None, ""):
        return jsonify(error="torrent_id و file_id مطلوبين"), 400
    k = p["key"]
    try:
        url, d, note = tb_hls_opts(k, tid, fid, b.get("audio"), b.get("resolution"), b.get("subtitle"))
        if not url:
            time.sleep(1.5)
            url, d, note = tb_hls_opts(k, tid, fid, b.get("audio"), b.get("resolution"), b.get("subtitle"))
        out = {"hls": url, "note": note}
        if b.get("info"):
            out["info"] = tb_stream_info(k, d)
        return jsonify(out), (200 if url else 502)
    except Exception as e:
        return jsonify(error=f"TorBox: {str(e)[:120]}"), 400
# ===== نهاية الإضافة =====


# ----------------- تورنت على TorBox / Real-Debrid / Debrid-Link -----------------

# 1 = الأعضاء العاديين يقدروا يشغّلوا الروابط اللي فيها مفتاح الدابريد (للعيلة بس). 0 = للأدمن بس
# AllDebrid بيرفض طلبات السيرفرات وممكن يوقف الحساب، فالسيرفر مايكلّمهوش (التشغيل بيتم من Stremio).
# لو السيرفر بقى على جهاز في البيت: ALLDEBRID_SERVER=1
AD_SERVER = os.environ.get("ALLDEBRID_SERVER", "0") == "1"
# 1 (افتراضي) = روابط إضافات Stremio المربوطة بالدابريد تتشغّل مباشرة. 0 = السيرفر يسحبها على حسابه
ADDON_DIRECT = os.environ.get("ADDON_DIRECT", "1") == "1"
SHARE_KEYS = True   # الأعضاء دايماً يشوفوا نسخ Stremio ويشغّلوها (متجاهل متغير SHARE_KEYS في البيئة)
TORRENT_TYPES = ("torbox", "debridlink", "alldebrid")  # الترتيب = الأولوية
CHECKABLE = ("torbox", "debridlink", "alldebrid")         # بيدعموا فحص "فوري"
AD_API = "https://api.alldebrid.com/v4"
AD_API41 = "https://api.alldebrid.com/v4.1"
RD_API = "https://api.real-debrid.com/rest/1.0"
DL_API = "https://debrid-link.com/api/v2"


def torrent_providers():
    ps = [p for p in load() if p["type"] in TORRENT_TYPES
          and (p["type"] != "alldebrid" or AD_SERVER)]
    ps.sort(key=lambda p: TORRENT_TYPES.index(p["type"]))
    return ps


def magnet_hash(q):
    m = re.search(r"btih:([a-zA-Z0-9]+)", q or "", re.I)
    return m.group(1).lower() if m else ""


def tb_cached(k, hs):
    r = tb(k, "GET", "/torrents/checkcached",
           params={"hash": ",".join(hs), "format": "list"})
    d = r.get("data") or []
    if isinstance(d, dict):
        found = list(d.keys())
    else:
        found = [x.get("hash") if isinstance(x, dict) else x for x in d]
    return {str(x).lower() for x in found if x}


# ---- Real-Debrid
def rd(k, method, path, **kw):
    r = requests.request(method, RD_API + path,
                         headers={"Authorization": f"Bearer {k}"},
                         timeout=25, **kw)
    if r.status_code == 204 or not r.content:
        return r.status_code, {}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def rd_err(code, d, default):
    if isinstance(d, dict) and d.get("error"):
        return str(d["error"])
    return f"{default} ({code})"


def rd_find(k, q):
    h = magnet_hash(q)
    if not h:
        return None
    for page in (1, 2):
        code, d = rd(k, "GET", "/torrents", params={"limit": 100, "page": page})
        if code != 200 or not isinstance(d, list):
            return None
        for x in d:
            if str(x.get("hash", "")).lower() == h:
                return x.get("id")
        if len(d) < 100:
            break
    return None


def rd_add(k, q):
    code, d = rd(k, "POST", "/torrents/addMagnet", data={"magnet": q})
    if code not in (200, 201) or not d.get("id"):
        raise Exception(rd_err(code, d, "فشل إضافة التورنت"))
    return d["id"]


def rd_info(k, rid):
    code, d = rd(k, "GET", f"/torrents/info/{rid}")
    if code != 200 or not isinstance(d, dict) or not d.get("id"):
        raise Exception(rd_err(code, d, "التورنت مش موجود"))
    st = str(d.get("status", ""))
    out = {"ready": False, "status": st,
           "progress": (d.get("progress") or 0) / 100,
           "speed": d.get("speed"), "seeds": d.get("seeders"), "peers": None,
           "size": d.get("bytes"), "name": d.get("filename"), "files": []}
    if st in ("magnet_error", "error", "virus", "dead"):
        raise Exception(f"التورنت فشل ({st})")
    files = d.get("files") or []
    if st == "waiting_files_selection":
        vids = [f["id"] for f in files
                if str(f.get("path", "")).lower().endswith(VIDEO)]
        ids = vids or [f["id"] for f in files]
        c2, d2 = rd(k, "POST", f"/torrents/selectFiles/{rid}",
                    data={"files": ",".join(str(i) for i in ids)})
        if c2 not in (200, 201, 204):
            raise Exception(rd_err(c2, d2, "فشل اختيار الملفات"))
        out["status"] = "selecting"
        return out
    if st == "downloaded":
        sel = [f for f in files if f.get("selected")]
        links = d.get("links") or []
        res = []
        for i, f in enumerate(sel):
            if i >= len(links):
                break
            path = str(f.get("path", "")).lstrip("/")
            res.append({"id": len(res), "name": path,
                        "short_name": path.rsplit("/", 1)[-1],
                        "size": f.get("bytes") or 0, "link": links[i]})
        if not res:
            raise Exception("مفيش ملفات جاهزة في التورنت")
        out.update(ready=True, files=res, progress=1)
    return out


_RD_IDS = {}   # رابط التحميل -> id الفك (محتاجينه لرابط البث المحوَّل)


def rd_link(k, f):
    code, d = rd(k, "POST", "/unrestrict/link", data={"link": f["link"]})
    if d.get("download"):
        if d.get("id"):
            if len(_RD_IDS) > 200:
                _RD_IDS.clear()
            _RD_IDS[d["download"]] = d["id"]
        return d["download"]
    raise Exception(rd_err(code, d, "فشل فك الرابط"))


# ---- Debrid-Link
def dl(k, method, path, **kw):
    r = requests.request(method, DL_API + path,
                         headers={"Authorization": f"Bearer {k}"},
                         timeout=25, **kw)
    try:
        return r.json()
    except ValueError:
        return {"success": False, "error": f"Debrid-Link رد غير مفهوم ({r.status_code})"}


def dl_list(k, **params):
    d = dl(k, "GET", "/seedbox/list", params=params)
    if not d.get("success"):
        raise Exception(str(d.get("error") or "فشل"))
    v = d.get("value")
    return v if isinstance(v, list) else []


def dl_find(k, q):
    h = magnet_hash(q)
    if not h:
        return None
    for x in dl_list(k, perPage=50):
        if str(x.get("hashString", "")).lower() == h:
            return x.get("id")
    return None


def dl_add(k, q):
    d = dl(k, "POST", "/seedbox/add", data={"url": q, "async": "true"})
    v = d.get("value")
    if d.get("success") and isinstance(v, dict) and v.get("id"):
        return v["id"]
    if "already" in str(d.get("error", "")).lower():
        rid = dl_find(k, q)
        if rid:
            return rid
    raise Exception(str(d.get("error") or "فشل إضافة التورنت"))


def dl_info(k, rid):
    lst = dl_list(k, ids=rid)
    d = lst[0] if lst else None
    if not d:
        raise Exception("التورنت مش موجود")
    if d.get("errorString"):
        raise Exception(str(d["errorString"]))
    pct = float(d.get("downloadPercent") or 0)
    res = []
    for f in d.get("files") or []:
        if not f.get("downloadUrl"):
            continue
        name = str(f.get("name") or "")
        res.append({"id": len(res), "name": name,
                    "short_name": name.rsplit("/", 1)[-1],
                    "size": f.get("size") or 0, "link": f["downloadUrl"]})
    ready = pct >= 100 and bool(res)
    return {"ready": ready, "status": "ready" if ready else "downloading",
            "progress": pct / 100, "speed": d.get("downloadSpeed"),
            "seeds": None, "peers": d.get("peersConnected"),
            "size": d.get("totalSize"), "name": d.get("name"),
            "files": res if ready else []}


def dl_cached(k, hs):
    found = set()
    for i in range(0, len(hs), 40):
        d = dl(k, "GET", "/seedbox/cached", params={"url": ",".join(hs[i:i + 40])})
        v = d.get("value") if d.get("success") else None
        if isinstance(v, dict):
            found |= {str(x).lower() for x in v.keys()}
    return found


# ---- AllDebrid
def ad(k, method, path, base=AD_API, **kw):
    params = kw.pop("params", {}) or {}
    params.update({"agent": "Elkomy", "apikey": k})
    r = requests.request(method, base + path, params=params, timeout=25, **kw)
    try:
        j = r.json()
    except ValueError:
        raise Exception(f"AllDebrid رد غير مفهوم ({r.status_code})")
    if j.get("status") != "success":
        e = j.get("error") or {}
        raise Exception(str(e.get("message") or e.get("code") or "فشل"))
    return j.get("data") or {}


def ad_magnets(d):
    m = d.get("magnets")
    if isinstance(m, dict):
        return [m]
    return m if isinstance(m, list) else []


def ad_find(k, q):
    h = magnet_hash(q)
    if not h:
        return None
    for x in ad_magnets(ad(k, "GET", "/magnet/status", AD_API41)):
        if str(x.get("hash", "")).lower() == h:
            return x.get("id")
    return None


def ad_add(k, q):
    d = ad(k, "POST", "/magnet/upload", data={"magnets[]": q})
    lst = ad_magnets(d)
    if not lst:
        raise Exception("فشل إضافة التورنت")
    first = lst[0]
    if first.get("error"):
        e = first["error"]
        raise Exception(str(e.get("message") if isinstance(e, dict) else e))
    return first.get("id")


def ad_info(k, rid):
    lst = ad_magnets(ad(k, "GET", "/magnet/status", AD_API41, params={"id": rid}))
    d = lst[0] if lst else None
    if not d:
        raise Exception("التورنت مش موجود")
    code = int(d.get("statusCode") or 0)
    if code >= 5:
        raise Exception(f"التورنت فشل ({d.get('status') or code})")
    size = d.get("size") or 0
    done = d.get("downloaded") or 0
    res = []
    for x in d.get("links") or []:
        if not x.get("link"):
            continue
        name = str(x.get("filename") or "")
        res.append({"id": len(res), "name": name,
                    "short_name": name.rsplit("/", 1)[-1],
                    "size": x.get("size") or 0, "link": x["link"]})
    ready = code == 4 and bool(res)
    return {"ready": ready, "status": str(d.get("status") or "processing"),
            "progress": 1 if ready else (done / size if size else 0),
            "speed": d.get("downloadSpeed"), "seeds": d.get("seeders"),
            "peers": None, "size": size, "name": d.get("filename"),
            "files": res if ready else []}


def ad_link(k, f):
    d = ad(k, "GET", "/link/unlock", params={"link": f["link"]})
    if d.get("link"):
        return d["link"]
    raise Exception("فشل فك الرابط")


def ad_cached(k, hs):
    found = set()
    for i in range(0, len(hs), 40):
        d = ad(k, "POST", "/magnet/instant",
               data=[("magnets[]", h) for h in hs[i:i + 40]])
        for m in ad_magnets(d):
            if m.get("instant") and m.get("hash"):
                found.add(str(m["hash"]).lower())
    return found


ADAPT = {
    "alldebrid": {"find": ad_find, "add": ad_add, "info": ad_info, "link": ad_link},
    "realdebrid": {"find": rd_find, "add": rd_add, "info": rd_info, "link": rd_link},
    "debridlink": {"find": dl_find, "add": dl_add, "info": dl_info,
                   "link": lambda k, f: f["link"]},
}


def cached_for(p, hs):
    if p["type"] == "torbox":
        return tb_cached(p["key"], hs)
    if p["type"] == "debridlink":
        return dl_cached(p["key"], hs)
    if p["type"] == "alldebrid":
        return ad_cached(p["key"], hs)
    return set()


def cached_hashes(hs):
    out = set()
    if not hs:
        return out
    for p in torrent_providers():
        if p["type"] in CHECKABLE:
            try:
                out |= cached_for(p, hs)
            except Exception:
                pass
    return out


def _cached_first(ps, h):
    first = []
    for p in ps:
        if h and p["type"] in CHECKABLE:
            try:
                if h in cached_for(p, [h]):
                    first.append(p)
            except Exception:
                pass
    return first + [p for p in ps if p not in first]


def ordered_tprovs(h, prefer=None, strict=False):
    """ترتيب المزوّدين: المفضّل أولاً (لو محدد)، وجوّه كل مجموعة اللي عنده الملف فوري يتقدّم."""
    ps = torrent_providers()
    if prefer:
        m = [p for p in ps if prefer in (p["id"], p["type"])]
        rest = [p for p in ps if p not in m]
        if m:
            return _cached_first(m, h) + ([] if strict else _cached_first(rest, h))
        if strict:
            return []
    return _cached_first(ps, h)


def generic_flow(p, q, rid, file_id=None, want_zip=False, want_all=False):
    ad = ADAPT[p["type"]]
    k = p["key"]
    try:
        if not rid:
            rid = ad["find"](k, q)
            if not rid:
                if "&tr=" not in q:
                    q += TRACKERS
                rid = ad["add"](k, q)
        tid = f"x:{p['type']}:{p['id']}:{rid}"
        info = ad["info"](k, rid)
    except Exception as e:
        return jsonify(error=f"{p['name']}: {e}"), 400
    if not info["ready"]:
        return jsonify(
            error="جاري التجهيز...", torrent_id=tid, provider=p["name"],
            status=info.get("status"), progress=info.get("progress"),
            speed=info.get("speed"), eta=None, seeds=info.get("seeds"),
            peers=info.get("peers"), size=info.get("size"),
            file=info.get("name")), 202

    files = info["files"]
    vids = []
    for f in files:
        if not f["short_name"].lower().endswith(VIDEO):
            continue
        s, e = parse_ep(f["name"])
        vids.append({"id": f["id"], "name": f["short_name"], "size": f["size"],
                     "season": s, "episode": e})
    vids.sort(key=lambda x: (
        x["season"] is None, x["season"] or 0,
        x["episode"] is None, x["episode"] or 0, x["name"].lower()))
    total = sum(f["size"] for f in files) or info.get("size")
    by_id = {f["id"]: f for f in files}

    def pack_info(resp):
        if len(vids) > 1:
            resp["pack"] = True
            resp["files"] = vids
            resp["total_size"] = total
        return resp

    try:
        if want_zip:
            return jsonify(
                error="ZIP مش متاح على المزوّد ده. استخدم 📋 كل الروابط."), 400

        if want_all:
            if not vids:
                return jsonify(error="مفيش ملفات فيديو في التورنت"), 400

            def one(v):
                try:
                    return {**v, "url": ad["link"](k, by_id[v["id"]])}
                except Exception as ex:
                    return {**v, "url": None, "error": str(ex)}

            with ThreadPoolExecutor(max_workers=5) as ex:
                links = list(ex.map(one, vids))
            return jsonify(links=links, provider=p["name"],
                           name=info.get("name"), torrent_id=tid)

        if file_id is not None:
            f = next((x for x in files if str(x["id"]) == str(file_id)), None)
            if not f:
                return jsonify(error="الملف مش موجود في التورنت"), 400
        else:
            main = main_file(vids, {"files": files})
            if not main:
                return jsonify(error="التورنت فاضي مفيهوش ملفات"), 400
            f = by_id[main["id"]]
        url = ad["link"](k, f)
        return jsonify(pack_info({
            "direct_link": url, "name": f["short_name"], "size": f["size"],
            "provider": p["name"], "torrent_id": tid, "file_id": f["id"]}))
    except Exception as e:
        return jsonify(error=f"{p['name']}: {e}"), 400


def torrent_dispatch(q, tid, file_id=None, want_zip=False, want_all=False, prefer=None, strict=False):
    t = str(tid) if tid else ""
    if t.startswith("x:"):
        _, _, pid, rid = t.split(":", 3)
        p = next((x for x in load() if x["id"] == pid), None)
        if not p:
            return jsonify(error="المزوّد اتحذف"), 400
        return generic_flow(p, q, rid, file_id, want_zip, want_all)
    if t:
        return torrent_flow(q, tid, file_id, want_zip, want_all)
    ps = ordered_tprovs(magnet_hash(q), prefer, strict)
    if not ps and prefer and strict:
        return jsonify(error="المزوّد المختار مش متاح أو مش مضاف"), 400
    if not ps:
        return jsonify(
            error="ضيف مزوّد (TorBox أو AllDebrid) من الإعدادات الأول"), 400
    last = None
    errs = []
    for p in ps:
        if p["type"] == "torbox":
            r = torrent_flow(q, None, file_id, want_zip, want_all)
        else:
            r = generic_flow(p, q, None, file_id, want_zip, want_all)
        code = r[1] if isinstance(r, tuple) else r.status_code
        if code in (200, 202):
            return r
        last = r  # المزوّد ده فشل (اشتراك منتهي مثلاً)، نجرّب اللي بعده
        try:
            resp = r[0] if isinstance(r, tuple) else r
            msg = (resp.get_json(silent=True) or {}).get("error")
            if msg:
                errs.append(str(msg))
        except Exception:
            pass
    if len(errs) > 1:
        return jsonify(error=" | ".join(errs)), 400
    return last


@app.get("/cached")
def cached():
    hs = [h for h in request.args.get("h", "").lower().split(",") if h][:100]
    ps = torrent_providers()
    nocheck = bool(ps) and not any(p["type"] in CHECKABLE for p in ps)
    try:
        return jsonify(cached=sorted(cached_hashes(hs)), nocheck=nocheck)
    except Exception:
        return jsonify(cached=[], nocheck=nocheck)



# ----------------- بحث الويب عن روابط مدعومة (Serper + فتح الصفحات) -----------------
# المفتاح في Railway Variables باسم SERPER_KEY. للأدمن بس. السيرفر بيدوّر، يفتح الصفحات، ويطلّع الروابط
# اللي دومينها مدعوم عند مزوّديك (TorBox / AllDebrid / 1Fichier ...). ما بيحمّلش أي ملف.
import ipaddress
import socket
import html as _html
from concurrent.futures import as_completed

SERPER_KEY = os.environ.get("SERPER_KEY", "").strip()
WS_BUILTIN = (
    "1fichier.com", "mega.nz", "mega.io", "mediafire.com", "rapidgator.net", "katfile.com",
    "ddownload.com", "nitroflare.com", "turbobit.net", "filefactory.com", "send.cm",
    "hexload.com", "wupfile.com", "uploadgig.com", "gofile.io", "pixeldrain.com",
)
WS_PREFER = ("1fichier.com", "mega.nz", "rapidgator.net", "mediafire.com",
             "katfile.com", "ddownload.com", "nitroflare.com", "turbobit.net")
WS_SKIP_PAGES = ("facebook.com", "instagram.com", "youtube.com", "youtu.be", "tiktok.com",
                 "twitter.com", "x.com", "wikipedia.org", "watchit.com", "elcinema.com",
                 "imdb.com", "google.com", "t.me", "reddit.com")
WS_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/124.0 Mobile Safari/537.36")
_DOM_RX = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
_URL_RX = re.compile(r"https?://[^\s\"'<>\\)\]}]+", re.I)
_HOSTS_CACHE = {"t": 0, "hosts": set(), "info": {}}


def _walk_domains(o, out):
    if isinstance(o, dict):
        for k, v in o.items():
            if any(x in str(k).lower() for x in ("icon", "logo", "image", "img", "regex")):
                continue
            _walk_domains(v, out)
    elif isinstance(o, (list, tuple)):
        for v in o:
            _walk_domains(v, out)
    elif isinstance(o, str):
        x = o.strip().lower()
        h = (urlparse(x).netloc if "//" in x else x.split("/")[0]).replace("www.", "")
        if _DOM_RX.match(h):
            out.add(h)


def _host_ok(host, hosts):
    host = (host or "").lower().replace("www.", "")
    return next((d for d in hosts if host == d or host.endswith("." + d)), None)


_BAD_VALS = ("disabled", "offline", "down", "inactive", "false", "0", "maintenance", "unavailable", "off")


def _entry_ok(e):
    """موقع معطّل مؤقتاً عند المزوّد؟ بنقرأ أي حقل حالة معروف في بيانات الموقع."""
    if not isinstance(e, dict):
        return True
    for k in ("status", "active", "enabled", "working", "online", "available", "is_active", "is_enabled"):
        if k in e:
            v = e[k]
            if v is False:
                return False
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0:
                return False
            if isinstance(v, str) and v.strip().lower() in _BAD_VALS:
                return False
    return True


def _split_entries(data):
    """(دومينات شغّالة, دومينات معطّلة) من قايمة مواقع المزوّد."""
    items = data if isinstance(data, list) else (list(data.values()) if isinstance(data, dict) else [])
    good, bad = set(), set()
    for e in items:
        n = set()
        _walk_domains(e, n)
        (good if _entry_ok(e) else bad).update(n)
    return good, bad


def ws_hosts(force=False):
    """كل الدومينات المدعومة (بتتحدّث كل 24 ساعة): مزوّدينك + قايمة TorBox + قايمة AllDebrid العامة + قايمة جاهزة.
    المواقع اللي معطّلة مؤقتاً عند المزوّدين (ومش شغّالة عند غيرهم) بتتشال."""
    c = _HOSTS_CACHE
    if not force and c["hosts"] and time.time() - c["t"] < 86400:
        return c
    hosts, info = set(WS_BUILTIN), {"جاهزة": len(WS_BUILTIN)}
    good, bad = set(), set()
    for pv in load():
        n = set()
        for d in pv.get("domains", []) or []:
            _walk_domains(str(d), n)
        if n:
            info[pv["name"] if pv.get("name") else pv["type"]] = len(n)
            hosts |= n
            good |= n
    tbp = tb_provider()
    if tbp:
        try:
            g, b = _split_entries((tb(tbp["key"], "GET", "/webdl/hosters") or {}).get("data"))
            info["TorBox"] = len(g) + len(b)
            hosts |= g | b
            good |= g
            bad |= b
        except Exception as ex:
            info["TorBox"] = "فشل: " + str(ex)[:60]
    if any(pv["type"] == "alldebrid" for pv in load()):
        try:    # قايمة عامة من غير مفتاح الحساب، فمفيش خطر على الحساب
            r = requests.get(AD_API + "/hosts", params={"agent": "Elkomy"}, timeout=15)
            g, b = _split_entries((jj(r).get("data") or {}).get("hosts") or {})
            info["AllDebrid"] = len(g) + len(b)
            hosts |= g | b
            good |= g
            bad |= b
        except Exception as ex:
            info["AllDebrid"] = "فشل: " + str(ex)[:60]
    off = bad - good
    hosts -= off
    # سوشيال ميديا ومواقع مش مواقع ملفات: بتظهر في قوايم المزوّدين (تحميل فيديوهات) بس مش روابط تحميل للأعمال
    _soc = set(WS_SKIP_PAGES) | {"fb.com", "fb.watch", "fb.me", "t.co", "x.com", "linkedin.com", "pinterest.com",
                                  "snapchat.com", "whatsapp.com", "wa.me", "telegram.org", "telegram.me", "twimg.com"}
    hosts = {h for h in hosts if not any(h == d or h.endswith("." + d) for d in _soc)}
    info["معطّل ومستبعد"] = len(off)
    c.update(t=time.time(), hosts=hosts, info=info)
    return c


def _public_url(u):
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for ai in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(ai[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return False
        return True
    except Exception:
        return False


def _resolve_goto(u):
    """روابط جوجل google.com/goto → الرابط الحقيقي من هيدر Location."""
    if not re.match(r"https?://(www\.)?google\.[a-z.]+/(goto|url)\b", u or "", re.I):
        return u
    try:
        r = requests.get(u, headers={"User-Agent": WS_UA}, timeout=6, allow_redirects=False)
        loc = r.headers.get("Location")
        return urljoin(u, loc) if loc else ""
    except Exception:
        return ""


def _fetch_html(u, limit=1_500_000):
    """بيفتح الصفحة (حد أقصى 3 تحويلات، وبيرفض العناوين الداخلية). بيرجّع (نص, الرابط النهائي)."""
    hd = {"User-Agent": WS_UA, "Accept-Language": "ar,en;q=0.8"}
    for _ in range(4):
        if not _public_url(u):
            return None, u
        r = requests.get(u, headers=hd, timeout=7, allow_redirects=False, stream=True)
        loc = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            r.close()
            u = urljoin(u, loc)
            continue
        ct = (r.headers.get("Content-Type") or "").lower()
        if r.status_code != 200 or not any(x in ct for x in ("html", "text", "json")):
            r.close()
            return None, u
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > limit:
                break
        r.close()
        try:
            return data.decode("utf-8"), u
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="ignore"), u
    return None, u


def ws_extract(html_text, page_url, hosts):
    """كل الروابط المدعومة في كود الصفحة (بما فيها اللي جوه سكريبت أو مكتوبة بـ \\/)."""
    t = _html.unescape((html_text or "").replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "="))
    ph = urlparse(page_url).netloc.lower().replace("www.", "")
    out = []
    for m in _URL_RX.findall(t):
        u = m.rstrip(".,;:!?'\"")
        h = urlparse(u).netloc.lower().replace("www.", "")
        d = _host_ok(h, hosts)
        if d and h != ph and u not in out:
            out.append(u)
    return out


def ws_title(html_text):
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", re.I | re.S)
    return _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:120] if m else ""



_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_TASHKEEL = re.compile(r"[\u064B-\u0652\u0640]")


def _norm(t):
    return _TASHKEEL.sub("", str(t or "").lower()).translate(_AR_MAP)


def ws_rel(query, text):
    """نسبة كلمات اسم العمل اللي موجودة في النص (بتسامح في اختلاف الكتابة)."""
    toks = [w for w in re.split(r"\W+", _norm(query)) if len(w) >= 3]
    toks = [w for w in toks if w not in ("الحلقه", "حلقه", "مسلسل", "فيلم", "episode")]
    if not toks:
        return 1.0
    tx = _norm(text)
    hit = sum(1 for w in toks if w in tx or (len(w) >= 5 and w[:4] in tx))
    return hit / len(toks)


def ws_queries(title, hosts, deep):
    pref = [h for h in WS_PREFER if _host_ok(h, hosts)][:6] or list(WS_PREFER[:4])
    qs = [f"{title} ({' OR '.join(pref)})", f"{title} تحميل مباشر"]
    if deep:
        qs.append(f"{title} download")
    return qs


def serper(q):
    r = requests.post("https://google.serper.dev/search",
                      headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
                      json={"q": q, "num": 10}, timeout=20)
    if r.status_code in (401, 403):
        raise Exception("مفتاح Serper مرفوض (راجع SERPER_KEY)")
    if r.status_code == 429 or r.status_code == 402:
        raise Exception("رصيد Serper خلص")
    return (r.json() or {}).get("organic") or []


@app.get("/websearch/hosts")
def websearch_hosts():
    c = ws_hosts(request.args.get("refresh") == "1")
    return jsonify(count=len(c["hosts"]), info=c["info"], hosts=sorted(c["hosts"]))


@app.post("/websearch")
def websearch():
    b = request.get_json(silent=True) or {}
    title = str(b.get("q") or "").strip()[:120]
    if not title:
        return jsonify(error="اكتب اسم العمل"), 400
    if not SERPER_KEY:
        return jsonify(error="ضيف SERPER_KEY في Variables بتاعة Railway وأعد التشغيل"), 400
    hosts = ws_hosts()["hosts"]
    notes, organic = [], []
    for q in ws_queries(title, hosts, bool(b.get("deep"))):
        try:
            organic += serper(q)
        except Exception as ex:
            notes.append(str(ex)[:120])
            if "مفتاح" in str(ex) or "رصيد" in str(ex):
                break
    raw = []
    seen = set()
    for o in organic:
        u = o.get("link") or ""
        if u and u not in seen:
            seen.add(u)
            raw.append((u, o.get("title") or "", o.get("snippet") or ""))
    with ThreadPoolExecutor(max_workers=8) as ex:
        real = list(ex.map(lambda x: _resolve_goto(x[0]), raw))
    items, direct, dropped = [], [], 0
    for (u0, ttl, snip), u in zip(raw, real):
        if not u:
            continue
        h = urlparse(u).netloc.lower().replace("www.", "")
        rel = ws_rel(title, ttl + " " + snip + " " + unquote(u))
        if _host_ok(h, hosts):
            if rel > 0:
                direct.append({"url": u, "title": ttl, "page": u, "rel": rel})
            else:
                dropped += 1       # رابط موقع مدعوم بس مالوش علاقة باسم العمل
        elif not _host_ok(h, WS_SKIP_PAGES) and _public_url(u):
            items.append((u, ttl, rel))
    items.sort(key=lambda it: -it[2])
    items = items[:10]

    pages, found = [], list(direct)

    def work(it):
        u, ttl, rel0 = it
        try:
            txt, fu = _fetch_html(u)
            if txt is None:
                return {"url": u, "title": ttl, "links": [], "status": "مش بيفتح"}
            return {"url": u, "title": ws_title(txt) or ttl, "links": ws_extract(txt, fu, hosts), "status": "تمام", "rel": rel0}
        except Exception as e:
            return {"url": u, "title": ttl, "links": [], "status": "فشل: " + str(e)[:50]}

    ex = ThreadPoolExecutor(max_workers=6)
    futs = [ex.submit(work, it) for it in items]
    try:
        for f in as_completed(futs, timeout=28):
            r = f.result()
            pages.append({"url": r["url"], "title": r["title"], "found": len(r["links"]), "status": r["status"]})
            for lk in r["links"]:
                found.append({"url": lk, "title": r["title"], "page": r["url"], "rel": r.get("rel", 0)})
    except Exception:
        notes.append("بعض الصفحات اتأخرت وسبناها")
    ex.shutdown(wait=False)

    groups, seen_u = {}, set()
    for f_ in found:
        if f_["url"] in seen_u:
            continue
        seen_u.add(f_["url"])
        h = urlparse(f_["url"]).netloc.lower().replace("www.", "")
        key = _host_ok(h, hosts) or h
        groups.setdefault(key, []).append(f_)
    order = {h: i for i, h in enumerate(WS_PREFER)}
    for v in groups.values():
        v.sort(key=lambda x: -x.get("rel", 0))
    out = [{"host": h, "links": v[:30]} for h, v in
           sorted(groups.items(), key=lambda kv: (order.get(kv[0], 99), -len(kv[1])))]
    return jsonify(groups=out, pages=pages, total=len(seen_u), notes=notes,
                   searched=len(raw), hosts=len(hosts), dropped=dropped,
                   off=ws_hosts()["info"].get("معطّل ومستبعد", 0))



# ----------------- بحث مباشر في موقع MovizLand (من غير جوجل ومن غير رصيد) -----------------
MOVIZ = "https://www.moviz.land"
_MZ_SKIP = ("/ar/", "/free/", "/series/", "/movies/", "/sports/", "/browse/", "/live/", "/kids/",
            "/login/", "/register/", "/category/", "/tag/", "/page/", "/feed", "/privacy", "/anime/", "/wp-")
_AR_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _mz_clean(t):
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t or ""))).strip()


_SZ_RX = re.compile(r"(\d+(?:[.,]\d+)?)\s*(TB|GB|MB|KB|ترابايت|جيجا(?:بايت)?|ميجا(?:بايت)?|ج\.?\s?ب|م\.?\s?ب)(?![A-Za-z])", re.I)
_SZ_UNIT = {"TB": "TB", "ترابايت": "TB", "GB": "GB", "MB": "MB", "KB": "KB"}


def _size_txt(t):
    """أول مساحة مكتوبة في النص (مثلاً 1.4 GB أو 700 ميجا) بصيغة موحّدة، أو فاضي."""
    m = _SZ_RX.search((t or "").translate(_AR_DIG))
    if not m:
        return ""
    u = m.group(2).upper().replace(" ", "")
    if u.startswith("جيجا") or u.startswith("ج"):
        u = "GB"
    elif u.startswith("ميجا") or u.startswith("م"):
        u = "MB"
    else:
        u = _SZ_UNIT.get(u, u)
    return m.group(1).replace(",", ".") + " " + u


def _q_guess(*texts):
    """جودة من النص أو الرابط لو الصفحة ماكتبتهاش (2160/1080/720/480/360 أو 4K/HD/CAM)."""
    t = " ".join(x or "" for x in texts)
    m = re.search(r"(2160|1440|1080|720|576|480|360)\s*[pP]?(?!\d)", t)
    if m:
        return m.group(1) + "P"
    for k, v in (("4k", "4K"), ("uhd", "4K"), ("bluray", "BluRay"), ("web-dl", "WEB-DL"), ("webrip", "WEBRip"),
                 ("hdrip", "HDRip"), ("hdcam", "CAM"), ("cam", "CAM")):
        if re.search(r"(?<![a-z])" + k + r"(?![a-z])", t, re.I):
            return v
    return ""


def moviz_links(page_html, hosts):
    """روابط التحميل من صفحة /watch/ (عناصر download-item متقسمة حسب الجودة)."""
    out = []
    for grp in re.split(r'class="download-group"', page_html or "")[1:]:
        qm = re.search(r"download-group-heading.*?<strong>(.*?)</strong>", grp, re.S)
        q = _mz_clean(qm.group(1)) if qm else ""
        for am in re.finditer(r"<a\b([^>]*download-item[^>]*)>(.*?)</a>", grp, re.S):
            hm = re.search(r'href="([^"]+)"', am.group(1))
            if not hm:
                continue
            u = _html.unescape(hm.group(1)).strip()
            h = urlparse(u).netloc.lower().replace("www.", "")
            d = _host_ok(h, hosts)
            inner = _mz_clean(am.group(2))
            out.append({"url": u, "host": d or h, "quality": q or _q_guess(inner, u),
                        "size": _size_txt(inner) or _size_txt(q), "ok": bool(d)})
    return out


def _mz_get(u):
    try:
        t, _ = _fetch_html(u)
        return t
    except Exception:
        return None


def _mz_title(page_html):
    m = re.search(r'class="watch-title"[^>]*>(.*?)</h1>', page_html or "", re.S)
    return _mz_clean(m.group(1)) if m else ws_title(page_html)


def _mz_expand(post_url, want_nums):
    """رابط عمل → قايمة (عنوان، رابط /watch/). المسلسل بيتفتح على حلقاته، والفيلم على صفحته."""
    page = _mz_get(post_url)
    if page is None:
        return []
    eps = []
    for m in re.finditer(r"<a\b([^>]*episode-card[^>]*)>(.*?)</a>", page, re.S):
        hm = re.search(r'href="([^"]+)"', m.group(1))
        if not hm:
            continue
        href = _html.unescape(hm.group(1))
        nm = re.search(r"episode-number[^>]*>\s*([\d٠-٩]+)", m.group(2))
        no = int(nm.group(1).translate(_AR_DIG)) if nm else None
        eps.append((no, href, _mz_clean(m.group(2))))
    if eps:
        if want_nums:
            sel = [e for e in eps if e[0] in want_nums] or eps
        else:
            sel = eps
        return [(t, h.rstrip("/") + "/watch/" if not h.rstrip("/").endswith("/watch") else h) for _, h, t in sel[:15]]
    return [(_mz_title(page) or ws_title(page), post_url.rstrip("/") + "/watch/")]



# ----------------- بحث مباشر في موقع Ahwak (ahwaktv) (روابط التحميل المدعومة بس) -----------------
CIMA = "https://yam.ahwaktv.net"
_CL_STOP = re.compile(r"(الحلقة|الحلقه|حلقة|حلقه|episode|ep)\s*", re.I)


_CL_SESS = requests.Session()
_CL_HD = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Referer": CIMA + "/",
    "Upgrade-Insecure-Requests": "1",
}
_CL_ERR = {"last": ""}


def _cl_get(u):
    """زي _mz_get بس بهيدرز متصفح كاملة وكوكيز، وبيسجّل سبب الفشل."""
    try:
        px = os.environ.get("CIMA_PROXY", "").strip()   # مثال: http://user:pass@host:port أو socks5://host:port
        r = _CL_SESS.get(u, headers=_CL_HD, timeout=20, allow_redirects=True,
                         proxies=({"http": px, "https": px} if px else None))
        if r.status_code != 200:
            _CL_ERR["last"] = "كود %d" % r.status_code
            return None
        r.encoding = r.encoding or "utf-8"
        t = r.content.decode("utf-8", "ignore")
        if len(t) < 500 or "cf-browser-verification" in t or "Just a moment" in t[:3000]:
            _CL_ERR["last"] = "صفحة تحقق (Cloudflare)"
            return None
        return t
    except requests.Timeout:
        _CL_ERR["last"] = "انتهى الوقت (Timeout)"
    except Exception as e:
        _CL_ERR["last"] = str(e)[:80]
    return None


def _cl_ep(t):
    m = re.search(r"(?:الحلقة|الحلقه|حلقة|حلقه|episode)\s*(\d+)", (t or "").translate(_AR_DIG), re.I)
    return int(m.group(1)) if m else None


def _cl_vid(u):
    m = re.search(r"watch\.php\?vid=([0-9a-zA-Z]+)", u or "")
    return m.group(1) if m else None


def _cl_anchors(page, base):
    """(رابط كامل، نص) لكل لينك داخل سيما لايت."""
    res = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page or "", re.S | re.I):
        hm = re.search(r'href="([^"]+)"', m.group(1))
        if not hm:
            continue
        u = urljoin(base, _html.unescape(hm.group(1))).split("#")[0]
        if not u.startswith(CIMA + "/"):
            continue
        txt = _mz_clean(m.group(2)) + " " + " ".join(re.findall(r'(?:alt|title)="([^"]*)"', m.group(0)))
        res.append((u, txt))
    return res


def _cl_title(page_html):
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page_html or "", re.S)
    return _mz_clean(m.group(1)) if m else ws_title(page_html)


def cima_links(page_html, hosts):
    """أقسام 'سيرفرات التحميل 1080P/720P/480P' (download-sec) وفيها روابط downloadlist."""
    out, seen = [], set()
    for sec in re.split(r'class="[^"]*download-sec', page_html or "")[1:]:
        qm = re.search(r"<h4[^>]*>(.*?)</h4>", sec, re.S)
        q = re.sub(r"^\s*سيرفرات\s+التحميل\s*", "", _mz_clean(qm.group(1))) if qm else ""
        for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', sec, re.S):
            u = _html.unescape(m.group(1)).strip()
            if not u.startswith("http") or u.startswith(CIMA) or (u, q) in seen:
                continue
            seen.add((u, q))
            h = urlparse(u).netloc.lower().replace("www.", "")
            d = _host_ok(h, hosts)
            inner = _mz_clean(m.group(2))
            out.append({"url": u, "host": d or h, "quality": q or _q_guess(inner, u),
                        "size": _size_txt(inner) or _size_txt(q), "ok": bool(d)})
    return out


def _cl_loose_links(page, base, hosts):
    """احتياطي: أي رابط لموقع مدعوم في كود الصفحة (حتى جوه سكريبت)، والجودة من أقرب 720p/1080p قبله."""
    out, seen = [], set()
    t = _html.unescape((page or "").replace("\\/", "/"))
    bh = urlparse(base).netloc.lower().replace("www.", "")
    for m in _URL_RX.finditer(t):
        u = m.group(0).rstrip(".,;:!?'\"")
        h = urlparse(u).netloc.lower().replace("www.", "")
        d = _host_ok(h, hosts)
        if not d or h == bh or u in seen:
            continue
        seen.add(u)
        qm = re.findall(r"(\d{3,4})\s*[pP]\b", t[max(0, m.start() - 800):m.start()])
        near = re.sub(r"<[^>]+>", " ", t[max(0, m.start() - 300):m.end() + 300])
        out.append({"url": u, "host": d, "quality": (qm[-1] + "P") if qm else _q_guess(u),
                    "size": _size_txt(near), "ok": True})
    return out


def _cl_expand(url, want_nums):
    """رابط watch.php → قايمة صفحات الحلقات المطلوبة (من قايمة حلقات الموسم) أو الصفحة نفسها."""
    page = _cl_get(url)
    if not page:
        return []
    eps = {}
    for u, t in _cl_anchors(page, url):
        if _cl_vid(u):
            n = _cl_ep(t)
            if n is not None:
                eps.setdefault(n, u)
    here = _cl_ep(_cl_title(page))
    if want_nums and eps:
        sel = [eps[n] for n in sorted(want_nums) if n in eps]
        if sel:
            return sel[:8]
        if here in want_nums:
            return [url]
        return []
    return [url]


def _cima_search(q):
    hosts = ws_hosts()["hosts"]
    notes = []
    nums = {int(x) for x in re.findall(r"\d+", q.translate(_AR_DIG)) if len(x) <= 4}
    sq = re.sub(r"\d+", " ", _CL_STOP.sub(" ", q.translate(_AR_DIG))).strip() or q
    sres = _cl_get(CIMA + "/search.php?keywords=" + quote(sq))
    if not sres:
        return jsonify(error="موقع Ahwak رفض السيرفر (%s). غالباً بيحظر عناوين السيرفرات، جرّب تشغّل البوت من IP تاني أو بروكسي." % (_CL_ERR["last"] or "مجهول")), 502
    cands, seen = [], set()
    for u, txt in _cl_anchors(sres, CIMA + "/search.php"):
        vid = _cl_vid(u)
        if not vid or vid in seen or len(txt) > 300:
            continue
        rel = ws_rel(sq, txt)
        if rel > 0:
            seen.add(vid)
            ep = _cl_ep(txt)
            bonus = 0.5 if (nums and ep in nums) else 0
            cands.append((rel + bonus, CIMA + "/watch.php?vid=" + vid))
    cands.sort(key=lambda x: -x[0])
    cands = cands[:5]
    if not cands:
        return jsonify(items=[], notes=["مفيش نتايج ليها علاقة بالاسم في Ahwak"], found=0)

    targets = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for lst in ex.map(lambda c: _cl_expand(c[1], nums), cands):
            for u in lst:
                if u not in targets:
                    targets.append(u)
    targets = targets[:20]

    def work(u):
        page = _cl_get(u)
        if not page:
            return None
        lks = cima_links(page, hosts) or _cl_loose_links(page, u, hosts)
        if not lks:   # لو الروابط في صفحة تانية (زرار تحميل)
            for u2, t2 in _cl_anchors(page, u):
                if u2 != u and "download" in u2.lower():
                    pg2 = _cl_get(u2)
                    if pg2:
                        lks = cima_links(pg2, hosts) or _cl_loose_links(pg2, u2, hosts)
                        if lks:
                            break
        return {"title": _cl_title(page), "page": u, "links": lks}

    items, skipped = [], 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(work, targets):
            if not r:
                continue
            good = [l for l in r["links"] if l["ok"]]
            skipped += len(r["links"]) - len(good)
            if good:
                r["links"] = good
                items.append(r)
    if skipped:
        notes.append("استبعدت %d رابط على مواقع مش مدعومة عندك" % skipped)
    items = _title_guard(sq, items, notes)
    return jsonify(items=items, notes=notes, found=sum(len(i["links"]) for i in items))


def _title_guard(q, items, notes):
    """يستبعد الصفحات اللي اسمها مالوش علاقة بكلمة البحث (الموقع ساعات بيرجّع نتايج مقترحة مش مطابقة)."""
    toks = [w for w in re.split(r"\W+", _norm(q)) if len(w) >= 3]
    if not toks:
        return items
    keep = [i for i in items if ws_rel(q, (i.get("title") or "") + " " + unquote(i.get("page") or "")) >= 0.5]
    if len(keep) < len(items):
        notes.append("استبعدت %d صفحة اسمها مش مطابق لـ «%s»" % (len(items) - len(keep), q))
    return keep

@app.post("/sitesearch")
def sitesearch():
    b = request.get_json(silent=True) or {}
    q = str(b.get("q") or "").strip()[:120]
    if not q:
        return jsonify(error="اكتب اسم العمل"), 400
    if str(b.get("site") or "") in ("ahwak", "cimalight", "cima"):
        return _cima_search(q)
    hosts = ws_hosts()["hosts"]
    notes = []
    sres = _mz_get(MOVIZ + "/?s=" + quote(q))
    if not sres:
        return jsonify(error="موقع MovizLand ما ردّش (ممكن بيحظر السيرفر أو واقع)"), 502
    cands, seen = [], set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", sres, re.S | re.I):
        hm = re.search(r'href="([^"]+)"', m.group(1))
        if not hm:
            continue
        u = _html.unescape(hm.group(1)).split("#")[0]
        if not u.startswith(MOVIZ + "/") or u.rstrip("/") == MOVIZ or any(x in u for x in _MZ_SKIP):
            continue
        txt = _mz_clean(m.group(2)) + " " + " ".join(re.findall(r'(?:alt|title)="([^"]*)"', m.group(0))) + " " + unquote(u)
        rel = ws_rel(q, txt)
        if rel > 0 and u not in seen:
            seen.add(u)
            cands.append((rel, u))
    cands.sort(key=lambda x: -x[0])
    cands = cands[:5]
    if not cands:
        return jsonify(items=[], notes=["مفيش نتايج ليها علاقة بالاسم في الموقع"], found=0)

    nums = {int(x) for x in re.findall(r"\d+", q.translate(_AR_DIG)) if len(x) <= 4}
    targets = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for lst in ex.map(lambda c: _mz_expand(c[1], nums), cands):
            targets += lst
    targets = targets[:20]

    def work(t):
        title, wurl = t
        page = _mz_get(wurl)
        if not page:
            return None
        lks = moviz_links(page, hosts)
        return {"title": _mz_title(page) or title, "page": wurl, "links": lks}

    items, skipped = [], 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(work, targets):
            if not r:
                continue
            good = [l for l in r["links"] if l["ok"]]
            skipped += len(r["links"]) - len(good)
            if good:
                r["links"] = good
                items.append(r)
    if skipped:
        notes.append("استبعدت %d رابط على مواقع مش مدعومة عندك" % skipped)
    return jsonify(items=items, notes=notes, found=sum(len(i["links"]) for i in items))


# ----------------- باقي المزوّدين (روابط فورية) -----------------

def r_1fichier(k, u):
    r = requests.post("https://api.1fichier.com/v1/download/get_token.cgi",
                      json={"url": u},
                      headers={"Authorization": f"Bearer {k}"}, timeout=20)
    res = jj(r)
    if res.get("status") == "OK" and res.get("url"):
        return res["url"]
    raise Exception(res.get("message") or "فشل")


def r_realdebrid(k, u):
    r = requests.post("https://api.real-debrid.com/rest/1.0/unrestrict/link",
                      headers={"Authorization": f"Bearer {k}"},
                      data={"link": u}, timeout=20)
    res = jj(r)
    if res.get("download"):
        return res["download"]
    raise Exception(str(res.get("error", "فشل")))


def r_alldebrid(k, u):
    r = requests.get("https://api.alldebrid.com/v4/link/unlock",
                     params={"agent": "Elkomy", "apikey": k, "link": u},
                     timeout=20)
    res = jj(r)
    if res.get("status") == "success" and res.get("data", {}).get("link"):
        return res["data"]["link"]
    raise Exception(str(res.get("error", {}).get("message", "فشل")))


def r_premiumize(k, u):
    r = requests.post("https://www.premiumize.me/api/transfer/directdl",
                      data={"src": u, "apikey": k}, timeout=20)
    res = jj(r)
    if res.get("status") == "success":
        links = [c for c in (res.get("content") or []) if c.get("link")]
        if links:
            return max(links, key=lambda c: c.get("size", 0))["link"]
        if res.get("location"):
            return res["location"]
    raise Exception(str(res.get("message", "الرابط غير مدعوم")))


def r_debridlink(k, u):
    r = requests.post("https://debrid-link.com/api/v2/downloader/add",
                      headers={"Authorization": f"Bearer {k}"},
                      data={"url": u}, timeout=20)
    res = jj(r)
    if res.get("success") and (res.get("value") or {}).get("downloadUrl"):
        return res["value"]["downloadUrl"]
    raise Exception(str(res.get("error", "فشل")))


INSTANT = {
    "debridlink": r_debridlink, "1fichier": r_1fichier,
    "realdebrid": r_realdebrid, "alldebrid": r_alldebrid,
    "premiumize": r_premiumize,
}


def _generate():
    data = request.get_json() or {}
    q = (data.get("query") or data.get("magnet") or data.get("link") or "").strip()
    tid = data.get("torrent_id")
    if not q and not tid:
        return jsonify(error="مفيش رابط مبعوت"), 400
    try:
        # متابعة رابط ويب كان بيجهز
        if tid and str(tid).startswith("web:"):
            _, pid, wid = str(tid).split(":", 2)
            p = next((x for x in load() if x["id"] == pid), None)
            if not p:
                return jsonify(error="المزوّد اتحذف"), 400
            try:
                body, code = torbox_web(p, q, wid)
            except Exception as e:
                return jsonify(error=f"{p['name']}: {e}"), 400
            return jsonify(body), code

        # تورنت
        if tid or q.startswith("magnet:"):
            pf, stc = prov_pref(g.user, "download" if (data.get("zip") or data.get("all")) else "watch")
            return torrent_dispatch(
                q, tid,
                file_id=data.get("file_id"),
                want_zip=bool(data.get("zip")),
                want_all=bool(data.get("all")),
                prefer=pf, strict=stc,
            )

        # رابط عادي
        if not q.startswith(("http://", "https://")):
            return jsonify(error="الرابط غير صالح"), 400
        cands = candidates(q)
        if not cands:
            return jsonify(error="مفيش مزوّدين مناسبين. ضيف مزوّد من الإعدادات."), 400
        tried = []
        for p in cands:
            try:
                if p["type"] == "torbox":
                    body, code = torbox_web(p, q)
                    body["tried"] = tried
                    return jsonify(body), code
                url = INSTANT[p["type"]](p["key"], q)
                name = unquote(url.split("?")[0].rsplit("/", 1)[-1]) or "ملف"
                return jsonify(direct_link=url, name=name,
                               provider=p["name"], tried=tried)
            except Exception as e:
                tried.append(f"{p['name']}: {e}")
        return jsonify(error="فشل السحب من كل المزوّدين ❌", tried=tried), 400
    except requests.Timeout:
        return jsonify(error="السيرفرات بطيئة حالياً، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في السيرفر: {e}"), 500


# ----------------- سجل النشاط -----------------

def act_load():
    try:
        d = store_load(ACT_FILE)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def act_log(user, kind, name, size, prov):
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    name = str(name or "")[:160]
    now = int(time.time() * 1000)
    with ACT_LOCK:
        d = act_load()
        u = d.setdefault(user, {"bytes": 0, "count": 0, "ev": []})
        for e in u["ev"][-5:]:
            if e.get("n") == name and e.get("k") == kind and now - e.get("t", 0) < 300000:
                return
        u["bytes"] += max(size, 0)
        u["count"] += 1
        u["ev"].append({"t": now, "k": kind, "n": name, "s": size,
                        "p": str(prov or "")[:40]})
        del u["ev"][:-300]
        store_save(ACT_FILE, d)


@app.post("/generate")
def generate():
    resp = make_response(_generate())
    if resp.status_code == 200:
        try:
            d = resp.get_json(silent=True) or {}
            if d.get("direct_link"):
                kind = "zip" if d.get("zip") else ("torrent" if d.get("torrent_id") else "link")
                act_log(g.user, kind, d.get("name"), d.get("size"), d.get("provider"))
            elif d.get("links"):
                act_log(g.user, "all", f"{d.get('name') or ''} ({len(d['links'])} ملف)",
                        sum((x.get("size") or 0) for x in d["links"]), d.get("provider"))
        except Exception:
            pass
    return resp


# ----------------- Stremio (حساب + إضافات + مكتبة) -----------------

DEBRID_RX = re.compile(
    r"(?:^|[/|,;])(realdebrid|alldebrid|premiumize|debridlink|torbox)="
    r"([^/|,;&\s]+)", re.I)
HASH_RX = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")
TBOFF_RX = re.compile(r"stremio\.torbox\.app/([0-9a-fA-F-]{20,})", re.I)
SEEDS_RX = re.compile(r"(?:👤|🌱|seeders?|seeds?)\s*[:=]?\s*(\d+)", re.I)
SIZE_RX = re.compile(r"(\d+(?:[.,]\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB)\b", re.I)
UNITS = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size(text):
    """أول مساحة في النص بأي شكل: 💾 4.2 GB / Size: 4.2 GiB / 📦 4,2GB ... (0 لو مفيش)."""
    m = SIZE_RX.search(str(text or ""))
    if not m:
        return 0
    unit = m.group(2).upper().replace("I", "")
    try:
        return int(float(m.group(1).replace(",", ".")) * UNITS[unit])
    except Exception:
        return 0


def st_load():
    try:
        d = store_load(SFILE)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("addons", [])
    d.setdefault("ignored", [])
    return d


def st_save(d):
    store_save(SFILE, d)


def st_post(method, body):
    r = requests.post(f"{ST_API}/{method}", json=body, timeout=20)
    j = jj(r)
    err = j.get("error")
    if err:
        raise Exception(err.get("message") if isinstance(err, dict) else str(err))
    return j.get("result")


def st_base(u):
    u = (u or "").strip().replace("stremio://", "https://")
    return re.sub(r"/manifest\.json.*$", "", u).rstrip("/")


def st_info(m):
    """بترجّع (الأنواع, البادئات) لو الإضافة بتدي streams، وإلا None"""
    for r in m.get("resources") or []:
        name = r if isinstance(r, str) else (r or {}).get("name")
        if name != "stream":
            continue
        rd = r if isinstance(r, dict) else {}
        return {
            "types": rd.get("types") or m.get("types") or [],
            "prefixes": rd.get("idPrefixes") or m.get("idPrefixes") or [],
        }
    return None


def st_manifest(base):
    r = requests.get(base + "/manifest.json", timeout=15)
    return jj(r)


def st_catalogs(m):
    """الكتالوجات اللي تنفع تتعرض كصفوف في الرئيسية (من غير بحث إجباري)"""
    out = []
    for c in (m or {}).get("catalogs") or []:
        if not isinstance(c, dict) or not c.get("id") or c.get("type") not in ("movie", "series"):
            continue
        extra = c.get("extra") or []
        if c.get("extraRequired") or any(
                isinstance(x, dict) and x.get("isRequired") for x in extra):
            continue
        genres = []
        for x in extra:
            if isinstance(x, dict) and x.get("name") == "genre":
                genres = [str(o) for o in (x.get("options") or [])][:40]
        out.append({"type": c["type"], "id": str(c["id"]),
                    "name": str(c.get("name") or c["id"])[:50], "genres": genres})
    return out


def st_add(d, base, man):
    ex = next((a for a in d["addons"] if a["base"] == base), None)
    if ex:
        if man:
            ex["catalogs"] = st_catalogs(man)
        return False
    info = st_info(man)
    d["addons"].append({
        "id": uuid.uuid4().hex[:8], "base": base,
        "name": man.get("name") or "Addon",
        "stream": info is not None,
        "types": (info or {}).get("types", []),
        "prefixes": (info or {}).get("prefixes", []),
        "catalogs": st_catalogs(man),
    })
    return True


def st_import_keys(base):
    """لو رابط الإضافة فيه مفتاح دابريد (زي Torrentio) بنضيفه كمزوّد"""
    path = unquote(urlparse(base).path)
    ps = load()
    added = 0
    for m in DEBRID_RX.finditer(path):
        t, k = m.group(1).lower(), m.group(2).strip()
        if not k or any(p["type"] == t for p in ps):
            continue
        ps.append({"id": uuid.uuid4().hex[:8], "type": t, "name": TYPES[t],
                   "key": k, "domains": []})
        added += 1
    mt = TBOFF_RX.search(base)
    if mt and not any(p["type"] == "torbox" for p in ps):
        ps.append({"id": uuid.uuid4().hex[:8], "type": "torbox", "name": TYPES["torbox"],
                   "key": mt.group(1), "domains": []})
        added += 1
    if added:
        save(ps)
    return added


def st_import(d):
    col = st_post("addonCollectionGet", {
        "type": "AddonCollectionGet", "authKey": d["authKey"], "update": True})
    ign = set(d.get("ignored", []))
    added = keys = 0
    for a in (col or {}).get("addons", []):
        u = a.get("transportUrl") or ""
        if not u.startswith(("http://", "https://")):
            continue
        base = st_base(u)
        if base in ign:
            continue
        man = a.get("manifest") or {}
        if not man:
            try:
                man = st_manifest(base)
            except Exception:
                man = {}
        if st_add(d, base, man):
            added += 1
        keys += st_import_keys(base)
    return added, keys


@app.post("/stremio/login")
def st_login():
    b = request.get_json() or {}
    email = str(b.get("email", "")).strip()
    pw = str(b.get("password", ""))
    if not email or not pw:
        return jsonify(error="اكتب الإيميل والباسورد"), 400
    try:
        res = st_post("login", {"type": "Login", "email": email,
                                "password": pw, "facebook": False})
        key = (res or {}).get("authKey")
        if not key:
            return jsonify(error="Stremio: تسجيل الدخول فشل"), 400
        with ST_LOCK:
            d = st_load()
            d["authKey"] = key
            d["email"] = email
            d["ignored"] = []   # تسجيل الدخول من جديد = رجّع كل إضافات الحساب حتى المحذوفة
            added, keys = st_import(d)
            st_save(d)
        return jsonify(ok=True, added=added, keys=keys)
    except Exception as e:
        return jsonify(error=f"Stremio: {e}"), 400


@app.post("/stremio/sync")
def st_sync():
    with ST_LOCK:
        d = st_load()
        if not d.get("authKey"):
            return jsonify(error="سجّل دخول Stremio الأول"), 400
        try:
            added, keys = st_import(d)
        except Exception as e:
            return jsonify(error=f"Stremio: {e} (جرّب تسجّل دخول تاني)"), 400
        st_save(d)
    return jsonify(ok=True, added=added, keys=keys)


@app.post("/stremio/logout")
def st_logout():
    with ST_LOCK:
        d = st_load()
        d.pop("authKey", None)
        d.pop("email", None)
        st_save(d)
    return jsonify(ok=True)


@app.post("/stremio/addon")
def st_add_ep():
    base = st_base((request.get_json() or {}).get("url", ""))
    if not base.startswith(("http://", "https://")):
        return jsonify(error="الرابط لازم يبدأ بـ https:// أو stremio://"), 400
    try:
        man = st_manifest(base)
    except Exception as e:
        return jsonify(error=f"الإضافة مش شغالة: {e}"), 400
    if not isinstance(man, dict) or not man.get("name"):
        return jsonify(error="ده مش manifest إضافة صحيح"), 400
    with ST_LOCK:
        d = st_load()
        d["ignored"] = [x for x in d["ignored"] if x != base]
        added = st_add(d, base, man)
        st_save(d)
    keys = st_import_keys(base)
    return jsonify(ok=True, added=added, keys=keys)


@app.get("/stremio/addons")
def st_list():
    d = st_load()
    n = sum(1 for a in d["addons"] if a.get("stream"))
    if g.role != "admin":
        return jsonify(logged=False, email="", items=[], n=n)
    return jsonify(
        logged=bool(d.get("authKey")), email=d.get("email", ""), n=n,
        items=[{"id": a["id"], "name": a["name"], "stream": a["stream"]}
               for a in d["addons"]])


@app.delete("/stremio/addons/<aid>")
def st_del(aid):
    with ST_LOCK:
        d = st_load()
        gone = next((a for a in d["addons"] if a["id"] == aid), None)
        d["addons"] = [a for a in d["addons"] if a["id"] != aid]
        if gone and gone["base"] not in d["ignored"]:
            d["ignored"].append(gone["base"])
        st_save(d)
    return jsonify(ok=True)


@app.get("/stremio/library")
def st_library():
    d = st_load()
    key = d.get("authKey")
    if not key:
        return jsonify(items=[])
    try:
        res = st_post("datastoreGet", {
            "type": "DatastoreGet", "authKey": key,
            "collection": "libraryItem", "ids": [], "all": True})
    except Exception as e:
        return jsonify(items=[], error=f"Stremio: {e}")
    items = []
    for x in res or []:
        if not isinstance(x, dict) or x.get("removed") or x.get("temp"):
            continue
        iid = str(x.get("_id", ""))
        typ = x.get("type")
        if not re.fullmatch(r"tt\d+", iid) or typ not in ("movie", "series"):
            continue
        items.append({
            "imdb": iid, "type": "tv" if typ == "series" else "movie",
            "title": x.get("name") or "", "poster": x.get("poster"),
            "year": str(x.get("year") or "")[:4],
            "t": str(x.get("_mtime") or ""),
        })
    items.sort(key=lambda i: i["t"], reverse=True)
    return jsonify(items=items[:60])


@app.get("/stremio/find")
def st_find():
    imdb = request.args.get("imdb", "")
    typ = request.args.get("type", "")
    if not re.fullmatch(r"tt\d+", imdb):
        return jsonify(error="IMDb id غير صالح"), 400
    try:
        r = tmdb_get(f"/find/{imdb}", external_source="imdb_id")
        mv = r.get("movie_results") or []
        tv = r.get("tv_results") or []
        order = [("tv", tv), ("movie", mv)] if typ == "tv" else [("movie", mv), ("tv", tv)]
        for t, lst in order:
            if lst:
                return jsonify(type=t, id=lst[0]["id"])
        return jsonify(error="مش لاقي العمل ده على TMDB")
    except Exception as e:
        return jsonify(error=str(e)), 500


DEB_KEYS = ("realdebrid", "alldebrid", "premiumize", "debridlink", "torbox",
            "putio", "offcc", "orion", "debridoptions")


def st_plain_base(base):
    """بيشيل مفتاح الدابريد من رابط إضافة زي Torrentio، فترجّع magnets بدل روابط"""
    u = urlparse(base)
    segs = [x for x in u.path.split("/") if x]
    if not segs:
        return base
    last = unquote(segs[-1])
    if "=" not in last:
        return base
    parts = last.split("|")
    keep = [x for x in parts if x.split("=", 1)[0].lower() not in DEB_KEYS]
    if len(keep) == len(parts):
        return base
    segs = segs[:-1] + (["|".join(keep)] if keep else [])
    path = "/".join(quote(x, safe="|=,") for x in segs)
    return f"{u.scheme}://{u.netloc}" + ("/" + path if path else "")


def short_err(ex):
    s = str(ex)
    if "CERTIFICATE_VERIFY_FAILED" in s:
        return "شهادة SSL مش متوثّقة"
    if "Name or service not known" in s or "NameResolution" in s or "No address" in s:
        return "الدومين مش موجود"
    if isinstance(ex, requests.Timeout):
        return "انتهى الوقت"
    return type(ex).__name__


REFS_FILE = os.path.join(DATA_DIR, "refs.jsonl")


def _refs_compact():
    try:
        with open(REFS_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-1500:]
        tmp = REFS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, REFS_FILE)
    except Exception:
        pass


def ref_for(url):
    rid = uuid.uuid4().hex[:16]
    with REF_LOCK:
        REFS[rid] = url
        while len(REFS) > 800:
            REFS.pop(next(iter(REFS)))
        try:
            with open(REFS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps([rid, url], ensure_ascii=False) + "\n")
            if os.path.getsize(REFS_FILE) > 3_000_000:
                _refs_compact()
        except Exception:
            pass        # لو الدسك مش متاح: بنكمّل بالذاكرة بس
    return rid


def ref_get(rid):
    url = REFS.get(rid)
    if url:
        return url
    try:
        with open(REFS_FILE, encoding="utf-8") as f:
            for line in reversed(f.readlines()[-2500:]):
                try:
                    k, u = json.loads(line)
                except Exception:
                    continue
                if k == rid:
                    with REF_LOCK:
                        REFS[rid] = u
                    return u
    except Exception:
        pass
    return None


SIZE_PROBE = os.environ.get("SIZE_PROBE", "1") != "0"


def _probe_one(url):
    """مساحة الملف من الرابط نفسه (Content-Length) من غير ما نحمّله."""
    try:
        h = {"User-Agent": "Mozilla/5.0"}
        r = requests.head(url, headers=h, timeout=4, allow_redirects=True)
        n = int(r.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return n
        h["Range"] = "bytes=0-0"
        r = requests.get(url, headers=h, timeout=4, allow_redirects=True, stream=True)
        try:
            cr = r.headers.get("Content-Range") or ""
            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                return int(cr.rsplit("/", 1)[1])
        finally:
            r.close()
    except Exception:
        pass
    return 0


def probe_sizes(items, cap=10):
    """النتايج اللي ليها رابط مباشر ومساحتها مجهولة: نجيب المساحة (المدبلج أولاً، بحد أقصى cap)."""
    if not SIZE_PROBE:
        return
    todo = [i for i in items if i.get("ref") and not i.get("size")]
    todo.sort(key=lambda i: (not i.get("eg"), not i.get("dub")))
    todo = todo[:cap]
    if not todo:
        return
    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            urls = [ref_get(i["ref"]) or "" for i in todo]
            sizes = list(ex.map(lambda u: _probe_one(u) if u else 0, urls))
        for i, n in zip(todo, sizes):
            if n:
                i["size"] = n
    except Exception:
        pass


_NOISE_RX = re.compile(
    r"(\(.*?\)|\[.*?\]|\b(19|20)\d{2}\b|مدبلج\w*|دبلج\w*|مترجم\w*|مصري\w*|كلاسيك\w*|كامل\w*|"
    r"\b(4k|uhd|hd|fhd|1080p|720p|dubbed|dub|arabic)\b)", re.I)


def _lookup_variants(q):
    """نفس الاسم بصيغ مختلفة: الأصلي، من غير الزوايد (سنة/دبلجة/جودة)، وبهمزات موحّدة"""
    out = []
    def add(x):
        x = re.sub(r"\s+", " ", x or "").strip(" -–:|")
        if x and x not in out:
            out.append(x)
    add(q)
    add(_NOISE_RX.sub(" ", q))
    add(re.split(r"\s[-–|:]\s", q)[0])
    for v in list(out):
        add(v.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا"))
        add(re.sub(r"^ال", "", v))
    return out[:6]


@app.get("/stremio/lookup")
def st_lookup():
    q = request.args.get("q", "").strip()[:100]
    typ = "tv" if request.args.get("type") == "tv" else "movie"
    year = request.args.get("year", "")
    if not q:
        return jsonify(error="الاسم ناقص"), 400

    def yr(x):
        return (x.get("release_date") or x.get("first_air_date") or "")[:4]

    def pick(res, t):
        if year.isdigit():
            for x in res:
                if yr(x) == year:
                    return jsonify(type=t, id=x["id"])
            for x in res:                      # سنة قريبة (±1) لأن الإضافات بتختلف أحياناً
                if yr(x).isdigit() and abs(int(yr(x)) - int(year)) <= 1:
                    return jsonify(type=t, id=x["id"])
        return jsonify(type=t, id=res[0]["id"])

    other = "movie" if typ == "tv" else "tv"
    try:
        for v in _lookup_variants(q):
            for t in (typ, other):
                for lang in ("ar", "en-US"):
                    res = tmdb_get(f"/search/{t}", query=v, language=lang,
                                   include_adult="false").get("results", [])
                    if res:
                        # النوع التاني بنقبله بس لو السنة بتطابق
                        if t == other and not (year.isdigit() and any(yr(x) == year for x in res)):
                            continue
                        return pick(res, t)
        return jsonify(error="مش لاقي العمل ده على TMDB", q=q)
    except Exception as ex:
        return jsonify(error=str(ex)), 500


@app.get("/stremio/streams")
def st_streams():
    is_admin = g.role == "admin"
    typ = "series" if request.args.get("type") == "tv" else "movie"
    imdb = request.args.get("imdb", "")
    s, e = request.args.get("s", ""), request.args.get("e", "")
    if not re.fullmatch(r"tt\d+", imdb):
        return jsonify(error="العمل ده ملوش IMDb id"), 400
    sid = f"{imdb}:{s}:{e}" if typ == "series" and s.isdigit() and e.isdigit() else imdb

    # AllDebrid بيرفض طلبات السيرفر (NO_SERVER)، فمايتحسبش مزوّد يسحب من عندنا
    own = (not ADDON_DIRECT) and any(p["type"] != "alldebrid" for p in torrent_providers())
    all_keys = {p["key"] for p in load() if p.get("key")}
    for a_ in st_load()["addons"]:
        all_keys |= {m.group(2) for m in DEBRID_RX.finditer(
            unquote(urlparse(a_["base"]).path))}
    low_keys = {k.lower() for k in all_keys}

    def usable(a):
        if not a.get("stream"):
            return False
        if a.get("types") and typ not in a["types"]:
            return False
        if own and st_plain_base(a["base"]) == a["base"] and any(
                k in unquote(a["base"]) for k in all_keys):
            return False
        pre = a.get("prefixes")
        return not pre or any(imdb.startswith(p) for p in pre)

    def one(a):
        qb = st_plain_base(a["base"]) if own else a["base"]
        url = f"{qb}/stream/{typ}/{sid}.json"
        try:
            r = requests.get(url, timeout=25,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return [], f"{a['name']}: رد {r.status_code}"
            out = []
            for x in (r.json().get("streams") or []):
                h = str(x.get("infoHash") or "").lower()
                bh = x.get("behaviorHints") or {}
                txt = x.get("title") or x.get("description") or ""
                nm = (x.get("name") or "").replace("\n", " ").strip()
                title = (txt.split("\n")[0] or nm or "stream").strip()
                sm = SEEDS_RX.search(txt)
                size = bh.get("videoSize") or parse_size(
                    " ".join([txt, nm, str(bh.get("filename") or "")]))
                if not sm:
                    sm = SEEDS_RX.search(nm)
                u = x.get("url") or ""
                direct = u if u.startswith(("http://", "https://")) else ""
                keyed = bool(direct) and any(k in unquote(direct) for k in all_keys)
                if keyed:
                    if not h:
                        cand = [m for m in HASH_RX.findall(unquote(direct))
                                if m.lower() not in low_keys]
                        if cand:
                            h = cand[0].lower()
                    if h and own:
                        direct = ""
                    elif own or (not is_admin and not SHARE_KEYS):
                        continue
                if keyed and direct and ADDON_DIRECT:
                    h = ""   # الرابط المباشر الجاهز من الإضافة أولى من سحب التورنت على حسابك (زي 1DM)
                if not direct and not h:
                    continue
                fl_eg, fl_dub = dub_flags(" ".join(
                    [txt, nm, str(bh.get("filename") or "")]))
                out.append({
                    "dub": fl_dub, "eg": fl_eg,
                    "title": title, "size": size,
                    "seeders": int(sm.group(1)) if sm else 0,
                    "indexer": (a["name"] + " " + nm).strip() if is_admin else "Stremio",
                    "hash": h,
                    "magnet": f"magnet:?xt=urn:btih:{h}&dn={quote(title)}" if h else "",
                    "direct": "",
                    "ref": ref_for(direct) if direct else "",
                    "cached": bool(direct),
                })
            return out, (None if out else f"{a['name']}: رد فاضي (0 نتيجة)")
        except Exception as ex:
            return [], f"{a['name']}: {short_err(ex)}"

    addons = [a for a in st_load()["addons"] if usable(a)]
    if not addons:
        return jsonify(error="مفيش مصادر متاحة للنوع ده حالياً.")
    with ThreadPoolExecutor(max_workers=6) as ex:
        parts = list(ex.map(one, addons))
    res = [i for p, _ in parts for i in p]
    probe_sizes(res)
    notes, skipped = [], 0
    for _, n in parts:
        if not n:
            continue
        if n.endswith("رد 404"):
            skipped += 1
        else:
            notes.append(n)
    if skipped:
        notes.append(f"{skipped} إضافة مش بتدعم العمل ده (404)")
    if not is_admin:
        notes = []
    return jsonify(results=res[:150], notes=notes)


@app.post("/stremio/resolve")
def st_resolve():
    req = request.get_json(silent=True) or {}
    ref = str(req.get("ref", ""))
    url = ref_get(ref)
    if not url:
        return jsonify(error="الرابط انتهى، دوّر على النتايج تاني"), 404
    try:
        cur = url
        for _ in range(6):
            r = requests.get(cur, allow_redirects=False, stream=True, timeout=25,
                             headers={"User-Agent": "Mozilla/5.0"})
            loc = r.headers.get("Location")
            code = r.status_code
            body = ""
            if code >= 400:
                try:
                    body = r.raw.read(3000, decode_content=True).decode("utf-8", "ignore")
                except Exception:
                    body = ""
            r.close()
            if code in (301, 302, 303, 307, 308) and loc:
                cur = urljoin(cur, loc)
                continue
            if code >= 400:
                # AllDebrid بيرفض فحص السيرفر للرابط (IP سيرفر)، لكن الموبايل بيشغّله عادي.
                # فنسيب المتصفح هو اللي يجرّب الرابط من IP صاحبه بدل ما نفشل هنا.
                if code >= 500 and "alldebrid" in body.lower() and cur != url:
                    return jsonify(url=cur, probe_failed=code)
                msg = f"المصدر رد {code}"
                if g.role == "admin" and body.strip():
                    tm = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
                    if tm:
                        txt = tm.group(1)
                    elif "<" in body[:50]:
                        txt = re.sub(r"<[^>]+>", " ", body)
                    else:
                        txt = body
                    txt = re.sub(r"\s+", " ", txt).strip()
                    if txt:
                        msg += " (" + urlparse(cur).netloc + "): " + txt[:200]
                return jsonify(error=msg), 502
            break
        if g.role != "admin" and not SHARE_KEYS and any(p.get("key") and p["key"] in cur for p in load()):
            return jsonify(error="المصدر ده مش متاح للمستخدمين العاديين"), 403
        act_log(g.user, "stremio", req.get("name"), req.get("size"), "Stremio")
        return jsonify(url=cur)
    except Exception as ex:
        return jsonify(error=f"فشل تجهيز الرابط: {short_err(ex)}"), 502


# ----------------- الصفحة الرئيسية (كتالوجات Stremio) + نشاط المستخدمين -----------------

def ensure_catalogs():
    with ST_LOCK:
        d = st_load()
        todo = [a for a in d["addons"] if "catalogs" not in a]
        if not todo:
            return d

        def f(a):
            try:
                return st_catalogs(st_manifest(a["base"]))
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(f, todo))
        for a, c in zip(todo, res):
            a["catalogs"] = c
        st_save(d)
        return d


def type_ar(t):
    return "أفلام" if t == "movie" else "مسلسلات"


DUB_ENTRY = {"k": "dubeg", "a": "_dub", "ty": "movie", "c": "dub-family", "g": "",
             "t": "🇪🇬 كرتون وعائلي مدبلج بالمصري", "on": True}
CAT_STATS = {}
DUB_STATE = {"running": False}
DUB_FILE = os.path.join(DATA_DIR, "dubbed.json")
EG_RX = re.compile(r"(مصري|مصرى|المصرية|egypt)", re.I)
DUB_RX = re.compile(r"(مدبلج|دبلج|dubbed|\bdub\b)", re.I)


def dub_flags(text):
    """(فيه كلمة مصري؟, فيه كلمة مدبلج؟)"""
    t = str(text or "")
    return bool(EG_RX.search(t)), bool(DUB_RX.search(t))


def cat_key(a, ty, c):
    return f"{a}|{ty}|{c}"


def new_entry(a, c, on=True):
    return {"k": hashlib.md5(cat_key(a["id"], c["type"], c["id"]).encode()).hexdigest()[:8],
            "a": a["id"], "ty": c["type"], "c": c["id"], "g": "",
            "t": f"{c['name']} · {type_ar(c['type'])}", "on": on}


def home_default(d):
    out = [dict(DUB_ENTRY)]
    for a in d["addons"]:
        for c in a.get("catalogs") or []:
            out.append(new_entry(a, c, len(out) < 11))
    return out


def home_layout(d):
    try:
        saved = store_load(HOME_FILE)
    except Exception:
        saved = None
    if not (isinstance(saved, dict) and isinstance(saved.get("rows"), list)):
        return home_default(d)
    rows_ = list(saved["rows"])
    seen = set(saved.get("seen") or [])
    if "dubeg" not in seen and not any(r.get("k") == "dubeg" for r in rows_):
        rows_.insert(0, dict(DUB_ENTRY))
    present = {cat_key(r.get("a"), r.get("ty"), r.get("c")) for r in rows_}
    for a in d["addons"]:          # إضافات جديدة بتظهر لوحدها
        for c in a.get("catalogs") or []:
            k = cat_key(a["id"], c["type"], c["id"])
            if k not in present and k not in seen:
                rows_.append(new_entry(a, c))
                present.add(k)
    return rows_


def dub_load():
    try:
        d = store_load(DUB_FILE)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def dub_status():
    d = dub_load()
    return {"t": d.get("t", 0), "n": len(d.get("items") or []),
            "running": DUB_STATE["running"]}


@app.get("/home/catalogs")
def home_catalogs():
    d = ensure_catalogs()
    items = [{"a": "_dub", "an": "خاص", "ty": "movie", "c": "dub-family",
              "name": "مدبلج بالمصري (كرتون وعائلي)", "genres": []}]
    for a in d["addons"]:
        for c in a.get("catalogs") or []:
            items.append({"a": a["id"], "an": a["name"], "ty": c["type"], "c": c["id"],
                          "name": c["name"], "genres": c.get("genres", [])})
    return jsonify(items=items)


@app.get("/home/layout")
def home_layout_get():
    d = ensure_catalogs()
    ids = {a["id"] for a in d["addons"]}
    out = []
    for r in home_layout(d):
        if r.get("a") not in ids and r.get("a") != "_dub":
            continue
        r = dict(r)
        st = CAT_STATS.get((r.get("a"), r.get("ty"), r.get("c"), r.get("g", "")))
        if st:
            r["stat"] = st
        out.append(r)
    src, err = "saved", ""
    try:
        store_load(HOME_FILE)
    except Exception as ex:
        src, err = "default", short_err(ex)
    return jsonify(rows=out, dub=dub_status(), persist=KV_ON, src=src, err=err)


@app.post("/home/layout")
def home_layout_set():
    d = ensure_catalogs()
    ids = {a["id"] for a in d["addons"]} | {"_dub"}
    rows_in = (request.get_json(silent=True) or {}).get("rows")
    if not isinstance(rows_in, list):
        return jsonify(error="بيانات ناقصة"), 400
    out, used = [], set()
    for r in rows_in[:300]:
        if not isinstance(r, dict) or r.get("a") not in ids or r.get("ty") not in ("movie", "series"):
            continue
        k = re.sub(r"[^a-z0-9]", "", str(r.get("k", "")).lower())[:16] or uuid.uuid4().hex[:8]
        if k in used:
            k = uuid.uuid4().hex[:8]
        used.add(k)
        out.append({"k": k, "a": r["a"], "ty": r["ty"], "c": str(r.get("c", ""))[:80],
                    "g": str(r.get("g", ""))[:40], "t": str(r.get("t", ""))[:60] or "صف",
                    "on": bool(r.get("on"))})
        subs = []
        for m in (r.get("m") or [])[:8]:
            if isinstance(m, dict) and m.get("a") in ids and m.get("ty") in ("movie", "series"):
                subs.append({"a": m["a"], "ty": m["ty"], "c": str(m.get("c", ""))[:80], "g": str(m.get("g", ""))[:40]})
        if subs:
            out[-1]["m"] = subs
    seen = {"dubeg"}
    for a in d["addons"]:
        for c in a.get("catalogs") or []:
            seen.add(cat_key(a["id"], c["type"], c["id"]))
    try:
        store_save(HOME_FILE, {"rows": out, "seen": sorted(seen)})
        back = store_load(HOME_FILE)
        if not isinstance(back, dict) or len(back.get("rows") or []) != len(out):
            return jsonify(error="التخزين رجّع بيانات مختلفة بعد الحفظ"), 500
    except Exception as ex:
        return jsonify(error="فشل الحفظ في التخزين: " + short_err(ex)), 500
    return jsonify(ok=True, n=len(out), kv=KV_ON)


@app.get("/home/ping")
def home_ping():
    return jsonify(v="layout-v6", kv=KV_ON, dir=DATA_DIR)


app.add_url_rule("/home/layout2", "layout2_get", home_layout_get, methods=["GET"])
app.add_url_rule("/home/layout2", "layout2_set", home_layout_set, methods=["POST"])


def cat_items(a, e):
    key = (a["id"], e["ty"], e["c"], e.get("g", ""))
    hit = CAT_CACHE.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    path = f"/catalog/{e['ty']}/{quote(e['c'], safe='')}"
    if e.get("g"):
        path += f"/genre={quote(e['g'], safe='')}"
    r = requests.get(a["base"] + path + ".json", timeout=20,
                     headers={"User-Agent": "Mozilla/5.0"})
    items, skipped, ex_id = [], 0, ""
    for m in (r.json().get("metas") or []):
        iid = str(m.get("id", ""))
        if not m.get("poster"):
            continue
        it = {"type": "tv" if (m.get("type") or e["ty"]) == "series" else "movie",
              "title": m.get("name") or "", "poster": m["poster"],
              "year": str(m.get("year") or m.get("releaseInfo") or "")[:4]}
        if re.fullmatch(r"tt\d+", iid):
            it["imdb"] = iid
        elif iid.startswith("tmdb:") and iid[5:].isdigit():
            it["id"] = int(iid[5:])
        elif it["title"]:
            it["lk"] = True        # id خاص بالإضافة: هنلاقيه بالاسم على TMDB وقت الفتح
            skipped += 1
            ex_id = ex_id or iid.split(":")[0][:12]
        else:
            continue
        items.append(it)
    items = items[:20]
    CAT_STATS[key] = {"n": len(items), "lk": skipped, "ex": ex_id}
    if items:
        CAT_CACHE[key] = (time.time(), items)
    return items


# ---- قسم المدبلج بالمصري: بنفحص إضافاتك على أفلام الكرتون والعائلي المشهورة
def dub_hit(a, imdb):
    try:
        r = requests.get(f"{a['base']}/stream/movie/{imdb}.json", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return False
        for x in (r.json().get("streams") or []):
            bh = x.get("behaviorHints") or {}
            text = " ".join(str(x.get(k) or "") for k in ("name", "title", "description"))
            eg, dub = dub_flags(text + " " + str(bh.get("filename") or ""))
            if eg and dub:
                return True
    except Exception:
        pass
    return False


def _dub_scan():
    d = st_load()
    addons = [a for a in d["addons"] if a.get("stream")
              and (not a.get("types") or "movie" in a["types"])
              and (not a.get("prefixes") or any(str(p).startswith("tt") for p in a["prefixes"]))]
    items = []
    if addons and TMDB_KEY:
        cands = []
        for page in (1, 2, 3):
            try:
                res = tmdb_get("/discover/movie", language="ar", sort_by="popularity.desc",
                               with_genres="16|10751", include_adult="false", page=page,
                               **{"vote_count.gte": "50"}).get("results", [])
            except Exception:
                break
            cands += [x for x in res if x.get("poster_path")]
        cands = cands[:50]

        def check(c):
            try:
                imdb = (tmdb_get(f"/movie/{c['id']}/external_ids").get("imdb_id") or "")
            except Exception:
                return None
            if not re.fullmatch(r"tt\d+", imdb):
                return None
            if any(dub_hit(a, imdb) for a in addons):
                return {"id": c["id"], "type": "movie", "title": c.get("title") or "",
                        "poster": img(c.get("poster_path"), "w342"),
                        "year": (c.get("release_date") or "")[:4]}
            return None

        with ThreadPoolExecutor(max_workers=4) as ex:
            items = [x for x in ex.map(check, cands) if x][:24]
    store_save(DUB_FILE, {"t": time.time(), "items": items})


def start_dub_scan():
    if DUB_STATE["running"]:
        return False
    DUB_STATE["running"] = True

    def run():
        try:
            run_exclusive(_dub_scan, "dub")
        except Exception:
            pass
        finally:
            DUB_STATE["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def dub_items():
    d = dub_load()
    if time.time() - d.get("t", 0) > 24 * 3600:
        start_dub_scan()
    return d.get("items") or []


@app.post("/home/dub-refresh")
def dub_refresh():
    return jsonify(started=start_dub_scan(), running=DUB_STATE["running"])


@app.get("/rows")
def rows():
    d = ensure_catalogs()
    amap = {a["id"]: a for a in d["addons"]}
    ents = [e for e in home_layout(d)
            if e.get("on") and (e.get("a") in amap or e.get("a") == "_dub")]
    if not ents:
        return tmdb_rows()

    def one(e):
        if e["a"] == "_dub":
            items = dub_items()
            return {"id": e["k"], "title": e["t"], "items": items} if items else None
        items, seen_k = [], set()
        for s_ in [e] + [dict(e, a=m["a"], ty=m["ty"], c=m["c"], g=m.get("g", "")) for m in (e.get("m") or [])]:
            try:
                if s_["a"] == "_dub":
                    part = dub_items()
                elif s_["a"] in amap:
                    part = cat_items(amap[s_["a"]], s_)
                else:
                    continue
            except Exception as ex:
                CAT_STATS[(s_["a"], s_["ty"], s_["c"], s_.get("g", ""))] = {"err": short_err(ex)}
                continue
            for x in part:
                kk = x.get("imdb") or x.get("id") or x.get("title")
                if kk in seen_k:
                    continue
                seen_k.add(kk)
                items.append(x)
        return {"id": e["k"], "title": e["t"], "items": items} if items else None

    with ThreadPoolExecutor(max_workers=6) as ex:
        got = list(ex.map(one, ents))
    data = [x for x in got if x]
    if not data:
        return tmdb_rows()
    return jsonify(rows=data)


def watch_stats(user):
    ud = ud_load(user)
    profs = ud.get("profiles", {})
    secs = titles = last = 0
    rows_ = []
    for pid, its in (ud.get("items") or {}).items():
        pr = profs.get(pid) or {}
        for k, it in (its or {}).items():
            if not isinstance(it, dict) or it.get("del") or str(k).startswith("l") or not it.get("title"):
                continue
            dur = it.get("dur") or 0
            pos = it.get("pos") or 0
            secs += dur if it.get("done") else min(pos, dur or pos)
            titles += 1
            last = max(last, tnum(it))
            rows_.append({"profile": pr.get("name", ""), "title": it.get("title"),
                          "type": it.get("type"), "s": it.get("s"), "e": it.get("e"),
                          "pos": pos, "dur": dur, "done": bool(it.get("done")),
                          "t": tnum(it)})
    rows_.sort(key=lambda x: x["t"], reverse=True)
    return secs, titles, last, rows_


@app.post("/usage")
def usage_report():
    """الصفحة بتبعت كل دقيقة تقريبًا كام ثانية العضو شاهد فيها (بتتجمّع يوميًا). حد أقصى 10 دقايق في المرة."""
    d = request.get_json(silent=True) or {}
    try:
        w = int(float(d.get("w") or 0))
    except (TypeError, ValueError):
        return jsonify(error="w لازم رقم"), 400
    w = max(0, min(w, 600))
    try:
        db = int(float(d.get("d") or 0))          # بايتات اتحمّلت فعليًا على الجهاز (من تطبيق الأندرويد)
    except (TypeError, ValueError):
        db = 0
    db = max(0, min(db, 1 << 40))
    if not w and not db:
        return jsonify(ok=True)
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 2 * 3600))   # بتوقيت مصر تقريبًا
    with ACT_LOCK:
        a = act_load()
        u = a.setdefault(g.user, {"bytes": 0, "count": 0, "ev": []})
        u["w"] = int(u.get("w", 0)) + w
        days = u.setdefault("days", {})
        days[day] = int(days.get(day, 0)) + w
        if len(days) > 90:
            for k in sorted(days)[:-90]:
                days.pop(k, None)
        if db:
            u["dl"] = int(u.get("dl", 0)) + db
            dd = u.setdefault("ddl", {})
            dd[day] = int(dd.get(day, 0)) + db
            if len(dd) > 90:
                for k in sorted(dd)[:-90]:
                    dd.pop(k, None)
        store_save(ACT_FILE, a)
    return jsonify(ok=True)


@app.get("/activity")
def activity_list():
    admin = APP_USER.lower()
    us = users_load()
    names = [(admin, "admin")] + [(n, "user") for n in sorted(us)]
    act = act_load()
    out = []
    for n, role in names:
        a = act.get(n, {})
        secs, titles, last_w, _ = watch_stats(n)
        ev = a.get("ev") or []
        last = max(last_w, ev[-1]["t"] if ev else 0, int(LAST.get(n, 0) * 1000))
        rec = us.get(n) or {}
        pl = plan_of(rec) if role == "user" else {"state": "admin", "until": 0, "left": None}
        out.append({"name": n, "role": role, "bytes": a.get("bytes", 0),
                    "count": a.get("count", 0), "watch": secs, "titles": titles,
                    "last": last, "tw": int(a.get("w", 0)), "dl": int(a.get("dl", 0)),
                    "plan": pl["state"], "until": pl["until"], "left": pl["left"],
                    "c": rec.get("c", 0)})
    return jsonify(items=out)


@app.get("/activity/<name>")
def activity_user(name):
    name = name.lower()
    if name != IPTV_ALL and name != APP_USER.lower() and name not in users_load():
        return jsonify(error="المستخدم مش موجود"), 404
    a = act_load().get(name, {})
    _, _, _, watched = watch_stats(name)
    # آخر 14 يوم: وقت المشاهدة المسجَّل + حجم السحب
    days = {}
    for i in range(13, -1, -1):
        k = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 2 * 3600 - i * 86400))
        days[k] = {"w": int((a.get("days") or {}).get(k, 0)), "b": 0, "n": 0,
                   "dl": int((a.get("ddl") or {}).get(k, 0))}
    for e in (a.get("ev") or []):
        k = time.strftime("%Y-%m-%d", time.gmtime(e.get("t", 0) / 1000 + 2 * 3600))
        if k in days:
            days[k]["b"] += int(e.get("s") or 0)
            days[k]["n"] += 1
    rec = users_load().get(name) or {}
    pl = plan_of(rec) if name != APP_USER.lower() else {"state": "admin", "until": 0, "left": None}
    return jsonify(pulls=list(reversed(a.get("ev") or []))[:100], watched=watched[:100],
                   bytes=a.get("bytes", 0), count=a.get("count", 0),
                   tw=int(a.get("w", 0)), dl=int(a.get("dl", 0)),
                   days=[{"d": k, **v} for k, v in days.items()],
                   plan=pl["state"], until=pl["until"], left=pl["left"], c=rec.get("c", 0))


# ----------------- TMDB -----------------

_AR_RE = re.compile(r"[\u0600-\u06FF]")


def _ar_variants(q):
    """صيغ مختلفة للاسم العربي (همزة / ة-ه / ي-ى) عشان البحث ما يفرقش"""
    q = (q or "").strip()
    out = [q]
    if not _AR_RE.search(q):
        return out

    def add(x):
        x = re.sub(r"\s+", " ", x).strip()
        if x and x not in out:
            out.append(x)
    f = _norm(q)
    ws = f.split(" ")
    add(f)
    add(" ".join(("\u0623" + w[1:]) if w[:1] == "\u0627" and len(w) > 2 else w for w in ws))
    add(" ".join(("\u0625" + w[1:]) if w[:1] == "\u0627" and len(w) > 2 else w for w in ws))
    add(f.replace("\u0627", "\u0623"))
    add(re.sub(r"\u0647(?=\s|$)", "\u0629", f))
    add(re.sub(r"\u064a(?=\s|$)", "\u0649", f))
    return out[:7]


def _tmdb_multi(q, lang):
    return [x for x in tmdb_get("/search/multi", query=q, language=lang,
                                include_adult="false").get("results", [])
            if x.get("media_type") in ("movie", "tv")]


def tmdb_get(path, **params):
    if not TMDB_KEY:
        raise RuntimeError("TMDB_KEY مش متضاف في Variables على Railway")
    params["api_key"] = TMDB_KEY
    r = requests.get("https://api.themoviedb.org/3" + path,
                     params=params, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"TMDB رجّع {r.status_code}: تأكد من المفتاح (v3)")
    return r.json()


def img(p, size):
    return f"https://image.tmdb.org/t/p/{size}{p}" if p else None


def is_latin(s):
    return bool(s) and bool(LATIN_RE.search(s)) and not NONLAT_RE.search(s)


def latin_titles(a, e):
    cands = [
        e.get("title") or e.get("name"),
        a.get("original_title") or a.get("original_name"),
    ]
    alt = a.get("alternative_titles") or {}
    for x in (alt.get("titles") or alt.get("results") or []):
        cands.append(x.get("title"))
    out, seen = [], set()
    for c in cands:
        c = (c or "").strip()
        if is_latin(c) and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out[:8]


@app.get("/tmdb")
def tmdb():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    try:
        used, ar = q, []
        for qq in _ar_variants(q):
            ar = _tmdb_multi(qq, "ar")
            if ar:
                used = qq
                break
        en_list = tmdb_get("/search/multi", query=used, language="en-US",
                           include_adult="false").get("results", [])
        en = {(i.get("media_type"), i["id"]): i for i in en_list}
        out = []
        for x in ar:
            if x.get("media_type") not in ("movie", "tv"):
                continue
            e = en.get((x["media_type"], x["id"]), x)
            date_s = x.get("release_date") or x.get("first_air_date") or ""
            out.append({
                "id": x["id"], "type": x["media_type"], "year": date_s[:4],
                "title": x.get("title") or x.get("name") or "",
                "search_title": e.get("title") or e.get("name")
                or x.get("original_title") or x.get("original_name") or "",
                "poster": img(x.get("poster_path"), "w342"),
            })
        return jsonify(results=out[:30])
    except Exception as e:
        return jsonify(error=str(e)), 500


# ----------------- صفوف الجديد (من TMDB) -----------------

ROWS_CACHE = {"t": 0, "data": None}

ROWS_DEF = [
    {"id": "trend", "title": "🔥 الأكثر مشاهدة", "type": "movie",
     "path": "/trending/all/week", "q": {}},
    {"id": "new_movies", "title": "🎬 أحدث الأفلام", "type": "movie",
     "path": "/discover/movie",
     "q": {"sort_by": "popularity.desc", "vote_count.gte": "3",
           "primary_release_date.gte": "d:-60",
           "primary_release_date.lte": "d:0"}},
    {"id": "new_tv", "title": "📺 أحدث المسلسلات", "type": "tv",
     "path": "/discover/tv",
     "q": {"sort_by": "popularity.desc",
           "first_air_date.gte": "d:-120", "first_air_date.lte": "d:0"}},
    {"id": "on_air", "title": "🗓 مسلسلات بتتعرض دلوقتي", "type": "tv",
     "path": "/tv/on_the_air", "q": {}},
    {"id": "hi_movies", "title": "🇮🇳 أحدث الأفلام الهندي", "type": "movie",
     "path": "/discover/movie",
     "q": {"sort_by": "popularity.desc", "with_original_language": "hi",
           "primary_release_date.gte": "d:-240",
           "primary_release_date.lte": "d:0"}},
    {"id": "hi_tv", "title": "🇮🇳 مسلسلات هندي", "type": "tv",
     "path": "/discover/tv",
     "q": {"sort_by": "popularity.desc", "with_original_language": "hi",
           "air_date.gte": "d:-90", "air_date.lte": "d:0"}},
    {"id": "ar_movies", "title": "🇪🇬 أفلام عربي", "type": "movie",
     "path": "/discover/movie",
     "q": {"sort_by": "popularity.desc", "with_original_language": "ar",
           "primary_release_date.gte": "d:-365",
           "primary_release_date.lte": "d:0"}},
    {"id": "ar_tv", "title": "🇪🇬 مسلسلات عربي", "type": "tv",
     "path": "/discover/tv",
     "q": {"sort_by": "popularity.desc", "with_original_language": "ar",
           "air_date.gte": "d:-90", "air_date.lte": "d:0"}},
    {"id": "tr_tv", "title": "🇹🇷 مسلسلات تركي", "type": "tv",
     "path": "/discover/tv",
     "q": {"sort_by": "popularity.desc", "with_original_language": "tr",
           "air_date.gte": "d:-60", "air_date.lte": "d:0"}},
    {"id": "ko_tv", "title": "🇰🇷 مسلسلات كوري", "type": "tv",
     "path": "/discover/tv",
     "q": {"sort_by": "popularity.desc", "with_original_language": "ko",
           "air_date.gte": "d:-60", "air_date.lte": "d:0"}},
    {"id": "anime", "title": "🎌 أنمي", "type": "tv",
     "path": "/discover/tv",
     "q": {"sort_by": "popularity.desc", "with_genres": "16",
           "with_original_language": "ja",
           "air_date.gte": "d:-45", "air_date.lte": "d:0"}},
    {"id": "upcoming", "title": "⏳ قريباً", "type": "movie",
     "path": "/movie/upcoming", "q": {}},
    {"id": "top", "title": "⭐ الأعلى تقييماً", "type": "movie",
     "path": "/movie/top_rated", "q": {}},
]


def resolve_q(q):
    out = {}
    for k, v in q.items():
        if isinstance(v, str) and v.startswith("d:"):
            out[k] = (date.today() + timedelta(days=int(v[2:]))).isoformat()
        else:
            out[k] = v
    return out


def row_items(row):
    q = {"language": "ar", "include_adult": "false", "page": 1}
    q.update(resolve_q(row.get("q", {})))
    res = tmdb_get(row["path"], **q).get("results", [])
    out = []
    for x in res:
        mt = x.get("media_type") or row["type"]
        if mt not in ("movie", "tv") or not x.get("poster_path"):
            continue
        d_s = x.get("release_date") or x.get("first_air_date") or ""
        out.append({
            "id": x["id"], "type": mt, "year": d_s[:4],
            "title": x.get("title") or x.get("name") or "",
            "poster": img(x.get("poster_path"), "w342"),
        })
    return out[:20]


def tmdb_rows():
    if not TMDB_KEY:
        return jsonify(error="TMDB_KEY مش متضاف في Variables على Railway"), 500
    now = time.time()
    if ROWS_CACHE["data"] and now - ROWS_CACHE["t"] < 1800:
        return jsonify(rows=ROWS_CACHE["data"])

    def one(r):
        try:
            items = row_items(r)
        except Exception:
            return None
        if len(items) < 3:
            return None
        return {"id": r["id"], "title": r["title"], "items": items}

    with ThreadPoolExecutor(max_workers=6) as ex:
        got = list(ex.map(one, ROWS_DEF))
    data = [g for g in got if g]
    if data:
        ROWS_CACHE["t"] = now
        ROWS_CACHE["data"] = data
    return jsonify(rows=data)


@app.get("/details")
def details():
    t, i = request.args.get("type"), request.args.get("id")
    if t not in ("movie", "tv") or not i:
        return jsonify(error="بيانات ناقصة"), 400
    try:
        a = tmdb_get(f"/{t}/{i}", language="ar",
                     append_to_response="credits,alternative_titles,external_ids")
        e = tmdb_get(f"/{t}/{i}", language="en-US")
        date_s = a.get("release_date") or a.get("first_air_date") or ""
        rt = a.get("runtime") or (a.get("episode_run_time") or [None])[0]
        cast = (a.get("credits") or {}).get("cast", [])[:10]
        lat = latin_titles(a, e)
        imdb = (a.get("external_ids") or {}).get("imdb_id") or a.get("imdb_id") or ""
        return jsonify(
            id=int(i), type=t, year=date_s[:4], runtime=rt,
            title=a.get("title") or a.get("name") or "",
            search_title=lat[0] if lat else "",
            alt=lat, imdb=imdb,
            rating=round(a.get("vote_average") or 0, 1),
            overview=a.get("overview") or e.get("overview") or "",
            genres=[g["name"] for g in a.get("genres", [])],
            cast=[c["name"] for c in cast],
            poster=img(a.get("poster_path"), "w342"),
            backdrop=img(a.get("backdrop_path"), "w780"),
            seasons=[{"n": s["season_number"], "eps": s["episode_count"]}
                     for s in a.get("seasons", []) if s["season_number"] > 0],
        )
    except Exception as ex:
        return jsonify(error=str(ex)), 500


@app.get("/related")
def related():
    """أجزاء السلسلة (collection) + أعمال مشابهة من TMDB"""
    t, i = request.args.get("type"), request.args.get("id")
    if t not in ("movie", "tv") or not i:
        return jsonify(error="بيانات ناقصة"), 400

    def card(x, mt):
        if not x.get("poster_path"):
            return None
        d_s = x.get("release_date") or x.get("first_air_date") or ""
        return {"id": x["id"], "type": mt, "year": d_s[:4],
                "title": x.get("title") or x.get("name") or "",
                "poster": img(x.get("poster_path"), "w342")}

    out = {"collection": [], "collection_name": "", "similar": []}
    try:
        if t == "movie":
            a = tmdb_get(f"/movie/{i}", language="ar")
            col = a.get("belongs_to_collection")
            if col and col.get("id"):
                c = tmdb_get(f"/collection/{col['id']}", language="ar")
                out["collection_name"] = c.get("name") or col.get("name") or ""
                parts = sorted(c.get("parts", []),
                               key=lambda x: x.get("release_date") or "9999")
                out["collection"] = [y for y in (card(p, "movie") for p in parts) if y]
        seen = {int(i)} | {x["id"] for x in out["collection"]}
        res = tmdb_get(f"/{t}/{i}/recommendations", language="ar", page=1).get("results", [])
        if len(res) < 6:
            res += tmdb_get(f"/{t}/{i}/similar", language="ar", page=1).get("results", [])
        for x in res:
            if x["id"] in seen:
                continue
            seen.add(x["id"])
            y = card(x, t)
            if y:
                out["similar"].append(y)
        out["similar"] = out["similar"][:18]
        return jsonify(**out)
    except Exception as ex:
        return jsonify(error=str(ex)), 500


@app.get("/season")
def season():
    i, n = request.args.get("id"), request.args.get("n")
    try:
        s = tmdb_get(f"/tv/{i}/season/{n}", language="ar")
        return jsonify(episodes=[{
            "n": x["episode_number"], "name": x.get("name") or "",
            "date": x.get("air_date") or "", "runtime": x.get("runtime"),
            "still": img(x.get("still_path"), "w300"),
        } for x in s.get("episodes", [])])
    except Exception as ex:
        return jsonify(error=str(ex)), 500


# ----------------- بحث التورنت (Prowlarr) -----------------

def prowlarr_search(q, cat=None, timeout=45):
    if not (PROWLARR_URL and PROWLARR_KEY):
        raise Exception("PROWLARR_URL و PROWLARR_KEY مش متضافين في Variables")
    params = {"query": q, "type": "search", "limit": 100}
    if cat:
        params["categories"] = cat
    r = requests.get(f"{PROWLARR_URL}/api/v1/search",
                     headers={"X-Api-Key": PROWLARR_KEY},
                     params=params, timeout=timeout)
    if r.status_code != 200:
        raise Exception(f"Prowlarr رجّع {r.status_code}: تأكد من الرابط والمفتاح")
    out = []
    for x in r.json():
        mag = x.get("magnetUrl") or ""
        if not mag.startswith("magnet:"):
            g = x.get("guid") or ""
            if g.startswith("magnet:"):
                mag = g
            elif x.get("infoHash"):
                name = quote(x.get("title", ""))
                mag = f"magnet:?xt=urn:btih:{x['infoHash']}&dn={name}"
            else:
                continue
        m = re.search(r"btih:([a-zA-Z0-9]+)", mag, re.I)
        h = (x.get("infoHash") or (m.group(1) if m else "")).lower()
        out.append({
            "title": x.get("title", ""), "size": x.get("size") or 0,
            "seeders": x.get("seeders") or 0,
            "indexer": x.get("indexer", ""), "magnet": mag, "hash": h,
            "date": x.get("publishDate") or "",   # تاريخ نشر التورنت (ISO) من Prowlarr
        })
    out.sort(key=lambda i: i["seeders"], reverse=True)
    return out


@app.get("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    try:
        cat = CATS.get(request.args.get("cat", ""))
        out = prowlarr_search(q, cat)
        if not out and _AR_RE.search(q):
            # اسم عربي ومفيش نتايج: هات الاسم الإنجليزي من TMDB وابحث بيه
            tried = set()
            for qq in _ar_variants(q)[:5]:
                try:
                    res = _tmdb_multi(qq, "en-US")
                except Exception:
                    break
                if not res:
                    continue
                for x in res[:2]:
                    nm = x.get("title") or x.get("name") or ""
                    if not is_latin(nm):
                        nm = x.get("original_title") or x.get("original_name") or ""
                    if is_latin(nm) and nm.lower() not in tried:
                        tried.add(nm.lower())
                        out = prowlarr_search(nm, cat)
                        if out:
                            break
                break
        return jsonify(results=out[:60])
    except requests.Timeout:
        return jsonify(error="البحث أخد وقت طويل، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في البحث: {e}"), 500


# ----------------- بحث Dailymotion (API عام من غير مفتاح) -----------------

DM_API = "https://api.dailymotion.com/videos"


@app.get("/dailymotion/search")
def dm_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        r = requests.get(DM_API, params={
            "search": q, "page": page, "limit": 24, "sort": "relevance",
            "fields": "id,title,url,duration,thumbnail_240_url,owner.screenname",
        }, timeout=15)
        if r.status_code != 200:
            return jsonify(error=f"Dailymotion رجّع {r.status_code}"), 502
        d = r.json()
        out = []
        for x in d.get("list") or []:
            vid = x.get("id") or ""
            if not vid:
                continue
            out.append({
                "id": vid,
                "title": x.get("title") or "",
                "url": x.get("url") or f"https://www.dailymotion.com/video/{vid}",
                "duration": x.get("duration") or 0,
                "thumb": x.get("thumbnail_240_url") or "",
                "owner": x.get("owner.screenname") or "",
            })
        return jsonify(results=out, has_more=bool(d.get("has_more")), page=page)
    except requests.Timeout:
        return jsonify(error="Dailymotion بطيء حالياً، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في البحث: {e}"), 500


# ----------------- الترجمة (SubDL) -----------------

ZIP_CACHE = {}
TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def ts_secs(m):
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def fmt_secs(t):
    ms = int(round(max(0.0, t) * 1000))
    return "%02d:%02d:%02d.%03d" % (
        ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000
    )


RLE, PDF = "\u202b", "\u202c"


def rtl_wrap(line):
    """بيلفّ سطر الترجمة العربي عشان علامات الترقيم متتقلبش"""
    t = line.strip()
    if not t or re.fullmatch(r"\d+", t):
        return line
    return RLE + t + PDF


def to_vtt(text, shift=0.0, scale=1.0, rtl=False):
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\{\\[^}]*\}", "", text).strip()
    if text.startswith("WEBVTT"):
        text = text[6:]
    lines = []
    for line in text.split("\n"):
        if "-->" in line:
            line = TS_RE.sub(lambda m: fmt_secs(ts_secs(m) * scale + shift), line)
        elif rtl:
            line = rtl_wrap(line)
        lines.append(line)
    return "WEBVTT\n\n" + "\n".join(lines).strip() + "\n"


def ass_secs(t):
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def ass_to_vtt(text, shift=0.0, scale=1.0, rtl=False):
    cols, in_ev, cues = [], False, []
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw in body.split("\n"):
        line = raw.strip()
        if line.startswith("["):
            in_ev = line.lower() == "[events]"
            continue
        if not in_ev:
            continue
        low = line.lower()
        if low.startswith("format:"):
            cols = [c.strip().lower() for c in line.split(":", 1)[1].split(",")]
        elif low.startswith("dialogue:") and cols:
            rest = line.split(":", 1)[1].lstrip()
            parts = rest.split(",", len(cols) - 1)
            if len(parts) < len(cols):
                continue
            row = dict(zip(cols, parts))
            try:
                st, en = ass_secs(row["start"]), ass_secs(row["end"])
            except (KeyError, ValueError):
                continue
            txt = re.sub(r"\{[^}]*\}", "", row.get("text", ""))
            txt = txt.replace("\\N", "\n").replace("\\n", "\n")
            txt = txt.replace("\\h", " ").strip()
            if txt:
                cues.append((st, en, txt))
    cues.sort(key=lambda c: c[0])
    out = ["WEBVTT", ""]
    for st, en, txt in cues:
        out.append(fmt_secs(st * scale + shift) + " --> " + fmt_secs(en * scale + shift))
        out.append("\n".join(rtl_wrap(x) for x in txt.split("\n")) if rtl else txt)
        out.append("")
    return "\n".join(out)


def vtt_to_srt(vtt):
    blocks = re.split(r"\n\s*\n", vtt.strip())
    out, n = [], 0
    for b in blocks:
        lines = b.split("\n")
        k = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if k is None:
            continue
        n += 1
        lines[k] = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", lines[k])
        out.append(str(n) + "\n" + "\n".join(lines[k:]))
    return "\n\n".join(out) + "\n"


def decode_sub(raw, ar):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1256" if ar else "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def ok_sub_path(u):
    return (
        u.startswith("/subtitle/") and len(u) < 300
        and ".." not in u and not re.search(r"[\s\\]", u)
    )


def get_zip(u):
    if u in ZIP_CACHE:
        return ZIP_CACHE[u]
    r = requests.get(SUBDL_DL + u, timeout=30)
    if r.status_code != 200:
        raise Exception(f"تحميل الترجمة فشل ({r.status_code})")
    if len(r.content) > 6000000:
        raise Exception("ملف الترجمة كبير زيادة")
    if len(ZIP_CACHE) >= 20:
        ZIP_CACHE.pop(next(iter(ZIP_CACHE)))
    ZIP_CACHE[u] = r.content
    return r.content


def pick_sub(z, season, episode):
    exts = (".srt", ".vtt", ".ass", ".ssa")
    names = [
        i for i in z.infolist()
        if not i.is_dir() and i.filename.lower().endswith(exts)
        and i.file_size < 3000000
    ]
    if not names:
        return None
    if episode is not None:
        hits = []
        for i in names:
            s, e = parse_ep(i.filename)
            if e == episode and (season is None or s is None or s == season):
                hits.append(i)
        if not hits:
            pat = re.compile(r"(?<!\d)0*%d(?!\d)" % episode)
            hits = [i for i in names if pat.search(os.path.basename(i.filename))]
        if hits:
            names = hits
    pref = {".srt": 0, ".vtt": 1, ".ass": 2, ".ssa": 3}
    names.sort(key=lambda i: (
        pref.get(os.path.splitext(i.filename.lower())[1], 9), -i.file_size
    ))
    return names[0]


def build_sub(u, season, episode, ar, fmt="vtt", shift=0.0, scale=1.0, ai=None):
    data = get_zip(u)
    if data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        info = pick_sub(z, season, episode)
        if not info:
            raise LookupError("مفيش ملف ترجمة مدعوم جوه الأرشيف")
        raw = z.read(info)
        ext = os.path.splitext(info.filename.lower())[1]
    else:
        raw = data
        ext = ".ass" if b"[Script Info]" in raw[:3000] else ".srt"
    text = decode_sub(raw, ar)
    rtl = bool(ar) and fmt != "srt"
    if ext in (".ass", ".ssa"):
        vtt = ass_to_vtt(text, shift, scale, rtl)
    else:
        vtt = to_vtt(text, shift, scale, rtl)
    if ai:                                           # ترجمة الذكاء الاصطناعي: نفس التوقيت ونص عربي
        vtt = _ai_apply(vtt, ai, fmt != "srt")
    if fmt == "srt":
        return vtt_to_srt(vtt), "application/x-subrip"
    return vtt, "text/vtt"


def subdl_call(params):
    r = requests.get(SUBDL_API, params=params, timeout=25)
    res = jj(r)
    if res.get("status") is False and not res.get("subtitles"):
        msg = str(res.get("error") or "")
        if "key" in msg.lower() or "api" in msg.lower():
            raise Exception(msg)
        return {}
    return res


def sub_items(res):
    out = []
    for x in res.get("subtitles") or []:
        u = x.get("url") or ""
        if not u.startswith("/subtitle/"):
            continue
        out.append({
            "u": u,
            "name": x.get("release_name") or x.get("name") or "ترجمة",
            "lang": x.get("language") or x.get("lang") or "",
            "author": x.get("author") or "",
            "season": x.get("season"), "episode": x.get("episode"),
            "full": bool(x.get("full_season")),
        })
    return out


def subdl_search(base, s, e, is_tv):
    attempts = []
    if is_tv and s:
        se = dict(base, season_number=s)
        if e:
            attempts.append(("exact", dict(se, episode_number=e)))
        attempts.append(("season", dict(se, full_season=1)))
    attempts.append(("loose" if is_tv else "exact", base))
    for mode, params in attempts:
        items = sub_items(subdl_call(params))
        if items:
            return items, mode
    return [], "none"


@app.get("/subs")
def subs():
    if not SUBDL_KEY:
        return jsonify(error="SUBDL_KEY مش متضاف في Variables على Railway"), 500
    lang = re.sub(r"[^A-Za-z,]", "", request.args.get("lang", "AR")).upper()
    lang = lang or "AR"
    typ = request.args.get("type", "")
    tid = request.args.get("id", "")
    q = request.args.get("q", "").strip()
    s = request.args.get("s", "")
    e = request.args.get("e", "")
    base = {"api_key": SUBDL_KEY, "languages": lang}
    if tid.isdigit() and typ in ("movie", "tv"):
        base["tmdb_id"] = tid
        base["type"] = typ
    elif q:
        base["film_name"] = q
        if typ in ("movie", "tv"):
            base["type"] = typ
    else:
        return jsonify(error="بيانات ناقصة"), 400
    is_tv = base.get("type") == "tv"
    s_i = int(s) if s.isdigit() else None
    e_i = int(e) if e.isdigit() else None
    try:
        items, mode = subdl_search(base, s_i, e_i, is_tv)
        return jsonify(items=items[:40], mode=mode)
    except requests.Timeout:
        return jsonify(error="SubDL بطيء، جرّب تاني"), 504
    except Exception as ex:
        return jsonify(error=f"الترجمة: {ex}"), 500


SUBX_TTL = 12 * 3600


def _b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(t):
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


@app.get("/sub/link")
def sub_link():
    """رابط ترجمة SRT موقّع (12 ساعة) ينتهي بـ /sub.srt عشان VLC وأي مشغّل خارجي يتعرّف عليه ويحمّله من غير تسجيل دخول"""
    u = request.args.get("u", "")
    if not ok_sub_path(u):
        return jsonify(error="رابط ترجمة غير صالح"), 400
    try:
        shift = float(request.args.get("shift") or 0)
        scale = float(request.args.get("scale") or 1)
    except ValueError:
        return jsonify(error="قيم غير صالحة"), 400
    p = {"u": u, "shift": shift, "scale": scale, "s": request.args.get("s", "")[:6],
         "e": request.args.get("e", "")[:6], "lang": request.args.get("lang", "")[:20],
         "ai": re.sub(r"[^a-f0-9]", "", request.args.get("ai", ""))[:40],
         "exp": int(time.time()) + SUBX_TTL,
         "f": "vtt" if request.args.get("fmt") == "vtt" else "srt"}
    tok = _b64e(json.dumps(p, separators=(",", ":")).encode())
    proto = request.headers.get("X-Forwarded-Proto", "https").split(",")[0].strip()
    base = (os.environ.get("PUBLIC_URL") or f"{proto}://{request.host}").rstrip("/")
    return jsonify(url=f"{base}/subx/{tok}.{sign('subx|' + tok)[:40]}/sub.{p['f']}")


@app.get("/subx/<tk>/sub.<ext>")
def subx_get(tk, ext):
    try:
        tok, sig = tk.rsplit(".", 1)
        if not hmac.compare_digest(sig, sign("subx|" + tok)[:40]):
            raise ValueError
        p = json.loads(_b64d(tok))
        if int(p["exp"]) < time.time() or not ok_sub_path(p["u"]):
            raise ValueError
    except Exception:
        return Response("forbidden", status=403, mimetype="text/plain")
    try:
        shift = max(-600.0, min(600.0, float(p.get("shift") or 0)))
        scale = max(0.8, min(1.25, float(p.get("scale") or 1)))
        se, ep = str(p.get("s", "")), str(p.get("e", ""))
        text, _ = build_sub(p["u"], int(se) if se.isdigit() else None, int(ep) if ep.isdigit() else None,
                            "ar" in str(p.get("lang", "")).lower(), p.get("f", "srt"), shift, scale,
                            p.get("ai") or None)
    except LookupError as ex:
        return Response(str(ex), status=404, mimetype="text/plain")
    except Exception as ex:
        return Response("subtitle error: " + str(ex)[:120], status=502, mimetype="text/plain")
    ctype = "text/vtt" if p.get("f") == "vtt" else "application/x-subrip"
    return Response(text.encode("utf-8-sig"), content_type=ctype + "; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/sub")
def sub_get():
    u = request.args.get("u", "")
    if not ok_sub_path(u):
        return jsonify(error="رابط ترجمة غير صالح"), 400
    fmt = request.args.get("fmt", "vtt")
    try:
        shift = float(request.args.get("shift", 0))
    except ValueError:
        shift = 0.0
    shift = max(-600.0, min(600.0, shift))
    try:
        scale = float(request.args.get("scale", 1))
    except ValueError:
        scale = 1.0
    scale = max(0.8, min(1.25, scale))
    se, ep = request.args.get("s", ""), request.args.get("e", "")
    season = int(se) if se.isdigit() else None
    episode = int(ep) if ep.isdigit() else None
    ar = "ar" in request.args.get("lang", "").lower()
    ai = re.sub(r"[^a-f0-9]", "", request.args.get("ai", ""))[:40] or None
    try:
        text, mt = build_sub(u, season, episode, ar, fmt, shift, scale, ai)
        return Response(text, mimetype=mt)
    except LookupError as ex:
        return jsonify(error=str(ex)), 404
    except zipfile.BadZipFile:
        return jsonify(error="ملف الترجمة تالف"), 502
    except requests.Timeout:
        return jsonify(error="تحميل الترجمة أخد وقت طويل"), 504
    except Exception as ex:
        return jsonify(error=f"الترجمة: {ex}"), 500


# ===================== ضبط الترجمة تلقائياً بالصوت (على السيرفر) =====================
# بيسمع عينات من صوت الفيديو الفعلي ويطابقها مع أوقات الجُمل في ملف الترجمة، ويطلّع (shift, scale).
# النتيجة بتتطبّق على ملف الترجمة نفسه، فبتشتغل مع أي مشغّل: متصفح / أندرويد / تلفزيون / VLC.
# محتاج ffmpeg على السيرفر (على Railway: nixpacks.toml فيه aptPkgs = ["ffmpeg"]).
# لو مش موجود بيرجع reason="noffmpeg" والموقع يكمّل بالضبط القديم من غير ما يتأثر.
import shutil
import subprocess
import socket
import ipaddress
import bisect
import math
import array
import sys

AS_FF = shutil.which("ffmpeg")
if not AS_FF:                       # Vercel: مفيش ffmpeg في النظام، فنستخدم النسخة الجاهزة من حزمة imageio-ffmpeg
    try:
        import imageio_ffmpeg
        AS_FF = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        AS_FF = None
AS_FP = shutil.which("ffprobe")
AS_HYP = (1.0, 0.95904, 1.04271, 1.04167, 0.96, 0.999, 1.001)   # نسب الفريمات المعروفة 23.976/24/25
AS_RATIOS = AS_HYP[1:]
AS_WIN_SEC = 80                    # طول كل عينة صوت
AS_FRACS = (0.12, 0.38, 0.63, 0.88, 0.25, 0.75)
AS_SEM = threading.BoundedSemaphore(2)
AS_RUN = {}
AS_LOCK = threading.Lock()
AS_NET = "http,https,tcp,tls,crypto"


def _as_safe_url(u):
    """بنقبل http/https لعناوين عامة بس (حماية من SSRF)."""
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        port = p.port or (443 if p.scheme == "https" else 80)
        for i in socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP):
            if not ipaddress.ip_address(i[4][0]).is_global:
                return False
        return True
    except Exception:
        return False


def _as_duration(url):
    if not AS_FP:
        return None
    try:
        out = subprocess.run(
            [AS_FP, "-v", "error", "-protocol_whitelist", AS_NET, "-show_entries",
             "format=duration", "-of", "csv=p=0", url],
            capture_output=True, timeout=25).stdout.decode("utf-8", "replace").strip()
        d = float(out.split()[0])
        return d if 120 < d < 86400 else None
    except Exception:
        return None


def _as_pcm(url, t0, dur):
    cmd = [AS_FF, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-protocol_whitelist", AS_NET,
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
           "-ss", "%.2f" % t0, "-t", "%d" % dur, "-i", url,
           "-map", "0:a:0?", "-vn", "-sn", "-dn",
           "-af", "highpass=f=250,lowpass=f=3400",
           "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    return subprocess.run(cmd, capture_output=True, timeout=150).stdout


def _as_energy(raw):
    """طاقة الصوت بالـ dB لكل 0.1 ثانية."""
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    out = []
    for i in range(len(a) // 800):
        seg = a[i * 800:(i + 1) * 800]
        out.append(20.0 * math.log10(sum(map(abs, seg)) / 800.0 + 1.0))
    return out


def _as_norm(x):
    """بيحوّل dB لاحتمال كلام 0..1. بيرجع None لو الصوت ساكت/مفيش إشارة."""
    if len(x) < 600:
        return None
    v = sorted(x)
    lo, hi = v[int(0.2 * (len(v) - 1))], v[int(0.9 * (len(v) - 1))]
    if hi - lo < 3.0:
        return None
    return [min(1.0, max(0.0, (y - lo) / (hi - lo))) for y in x]


def _as_cues(srt):
    raw = []
    for line in srt.split("\n"):
        if "-->" in line:
            m = list(TS_RE.finditer(line))
            if len(m) >= 2:
                s, e = ts_secs(m[0]), ts_secs(m[1])
                if e - s >= 0.15:
                    raw.append((s, e))
    raw.sort()
    out = []
    for s, e in raw:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


class _AsWin:
    def __init__(self, t0, A):
        self.t0, self.n = t0, len(A)
        P, acc = [0.0], 0.0
        for x in A:
            acc += x
            P.append(acc)
        self.P, self.ma = P, acc / self.n
        var = sum(x * x for x in A) / self.n - self.ma * self.ma
        self.sda = math.sqrt(var) if var > 1e-4 else 0.0


def _as_r(w, cs, ce, h, b):
    """ارتباط بين نشاط الكلام في الصوت ونشاط الترجمة لو أوقاتها = h*وقت + b."""
    if not w.sda:
        return 0.0
    n, t0 = w.n, w.t0
    lo, hi = (t0 - b) / h, (t0 + n / 10.0 - b) / h
    i, j = bisect.bisect_left(ce, lo), bisect.bisect_right(cs, hi)
    if j - i < 4:
        return 0.0
    cover, sas, P = 0, 0.0, w.P
    for k in range(i, j):
        a = int(round((h * cs[k] + b - t0) * 10))
        z = int(round((h * ce[k] + b - t0) * 10))
        if a < 0:
            a = 0
        if z > n:
            z = n
        if z > a:
            cover += z - a
            sas += P[z] - P[a]
    ms = cover / n
    vs = ms - ms * ms
    if vs < 1e-4:
        return 0.0
    return (sas / n - w.ma * ms) / (w.sda * math.sqrt(vs))


def _as_joint(wins, cs, ce, h, b):
    return sum(_as_r(w, cs, ce, h, b) for w in wins) / len(wins)


def _as_search(wins, cs, ce, maxoff=180.0):
    """بيدوّر على أحسن (h, b) لكل العينات مع بعض (لازم نفس الإزاحة تنفع في كل الفيلم)."""
    M = int(maxoff / 0.5)
    cands = []
    for h in AS_HYP:
        sc = [_as_joint(wins, cs, ce, h, k * 0.5) for k in range(-M, M + 1)]
        for i, v in enumerate(sc):
            if (i == 0 or v >= sc[i - 1]) and (i == len(sc) - 1 or v >= sc[i + 1]):
                cands.append((v, h, (i - M) * 0.5))
    cands.sort(reverse=True)
    top = []
    for v, h, b in cands:
        if all(not (abs(h - x[1]) < 1e-9 and abs(b - x[2]) < 3) for x in top):
            top.append((v, h, b))
        if len(top) >= 8:
            break
    ref = []
    for v, h, b in top:
        bv, bb = v, b
        for k in range(-5, 6):
            vx = _as_joint(wins, cs, ce, h, b + k * 0.1)
            if vx > bv:
                bv, bb = vx, b + k * 0.1
        ref.append((bv, h, round(bb, 2)))
    ref.sort(reverse=True)
    return ref


def _as_refine(wins, cs, ce, h, b):
    """بعد ما اخترنا (h,b): بنقيس الانحراف المتبقي عند كل عينة ونصحّح السرعة والإزاحة بدقة."""
    pts = []
    for w in wins:
        best, bd, r_at0 = -9, 0.0, 0.0
        for k in range(-60, 61):
            d = k * 0.1
            r = _as_r(w, cs, ce, h, b + d)
            if k == 0:
                r_at0 = r
            if r > best:
                best, bd = r, d
        if best >= 0.08:
            tm = w.t0 + w.n / 20.0
            pts.append(((tm - b) / h, bd, best * best, best))
    return pts


def _as_slope(pts):
    """انحراف خطي δ = k*c + m بوزن الثقة، مع شيل القياسات الشاذة."""
    pts = list(pts)
    while True:
        sw = sum(p[2] for p in pts)
        mc = sum(p[2] * p[0] for p in pts) / sw
        md = sum(p[2] * p[1] for p in pts) / sw
        if len(pts) < 3 or max(p[0] for p in pts) - min(p[0] for p in pts) < 900:
            return 0.0, md, pts
        sxx = sum(p[2] * (p[0] - mc) ** 2 for p in pts)
        if sxx < 1:
            return 0.0, md, pts
        k = sum(p[2] * (p[0] - mc) * (p[1] - md) for p in pts) / sxx
        m = md - k * mc
        res = [(abs(p[1] - (k * p[0] + m)), p) for p in pts]
        worst = max(res, key=lambda x: x[0])
        if worst[0] <= 1.0:
            return k, m, pts
        if len(pts) <= 3:
            return 0.0, md, pts
        pts.remove(worst[1])


def _as_equiv(h, b, tms, h0, b0):
    """هل (h,b) بيدّي تقريباً نفس النتيجة بتاعة (h0,b0) عند أوقات العينات؟"""
    for tm in tms:
        cc = (tm - b0) / h0
        if abs(h * cc + b - tm) > 1.5:
            return False
    return True


def _as_solve(cues, wins):
    cs, ce = [c[0] for c in cues], [c[1] for c in cues]
    ref = _as_search(wins, cs, ce)
    if not ref:
        return {"ok": False, "reason": "nosig"}
    J, h, b = ref[0]
    tms = [w.t0 + w.n / 20.0 for w in wins]
    J2 = -1.0
    for v, h2, b2 in ref[1:]:
        if not _as_equiv(h2, b2, tms, h, b):
            J2 = max(J2, v)
    J0 = _as_joint(wins, cs, ce, 1.0, 0.0)
    rs = [_as_r(w, cs, ce, h, b) for w in wins]
    good = sum(1 for r in rs if r >= 0.1)
    # مطابقة دقيقة (انحراف خطي متبقي)
    pts = _as_refine(wins, cs, ce, h, b)
    s_fin, b_fin = h, b
    if pts:
        k, m, kept = _as_slope(pts)
        s2, b2 = h + k, b + m
        near = [r for r in (0.95904, 0.96, 1.04271, 1.04167, 1.0) if abs(s2 - r) < 0.0006]
        if len(near) == 1:
            b2 += (s2 - near[0]) * (sum(p[0] for p in kept) / len(kept))   # نثبّت الخط عند متوسط العينات
            s2 = near[0]
        if 0.9 < s2 < 1.2 and _as_joint(wins, cs, ce, s2, b2) >= J - 0.01:
            s_fin, b_fin = s2, b2
        elif pts:
            sw = sum(p[2] for p in pts)
            b_fin = b + sum(p[2] * p[1] for p in pts) / sw
    Jf = max(_as_joint(wins, cs, ce, s_fin, b_fin), J)
    # لو عينة بعيدة عن الخط النهائي أكتر من ثانية: الترجمة من نسخة تانية (إزاحة بتتغيّر في نص الفيلم)
    # فمنطبّقش حل وسط غلط
    for w in wins:
        br, bd = -9.0, 0.0
        for k in range(-80, 81):
            r = _as_r(w, cs, ce, s_fin, b_fin + k * 0.1)
            if r > br:
                br, bd = r, k * 0.1
        if br >= 0.1 and abs(bd) > 1.0:
            return {"ok": False, "reason": "split", "r": round(Jf, 3)}
    margin = Jf - J2
    if Jf >= 0.15 and margin >= 0.05 and good >= 3:
        conf = "high"
    elif Jf >= 0.10 and margin >= 0.03 and good >= 2:
        conf = "medium"
    else:
        return {"ok": False, "reason": "low", "r": round(Jf, 3), "margin": round(margin, 3)}
    if Jf - J0 < 0.02 and abs(b_fin) < 1.0 and abs(s_fin - 1) < 0.0005:
        return {"ok": True, "synced": True, "shift": 0.0, "scale": 1.0, "conf": conf, "r": round(Jf, 3), "n": good}
    return {"ok": True, "synced": False, "shift": round(b_fin, 2), "scale": round(s_fin, 6),
            "conf": conf, "r": round(Jf, 3), "n": good}


def _as_job(u, src, season, episode, lang, dur_hint):
    ar = "ar" in (lang or "").lower()
    srt, _ = build_sub(u, season, episode, ar, "srt", 0.0, 1.0)
    cues = _as_cues(srt)
    if len(cues) < 40:
        return {"ok": False, "reason": "fewcues"}
    dur = dur_hint if (dur_hint and 120 < dur_hint < 86400) else _as_duration(src)
    if not dur:
        dur = cues[-1][1] * 1.04 + 60
    wins, tried = [], 0

    def grab(f):
        t0 = max(0.0, min(dur * f - AS_WIN_SEC / 2, dur - AS_WIN_SEC - 20))
        raw = _as_pcm(src, t0, AS_WIN_SEC)
        A = _as_norm(_as_energy(raw))
        return _AsWin(t0, A) if A else None

    for fr in (AS_FRACS[:4], AS_FRACS[4:]):
        if len(wins) >= 3:
            break
        with ThreadPoolExecutor(max_workers=4) as ex:
            for r in ex.map(lambda f: _safe(grab, f), fr):
                if r:
                    wins.append(r)
    if len(wins) < 2:
        return {"ok": False, "reason": "noaudio"}
    res = _as_solve(cues, wins)
    if not res.get("ok") and res.get("reason") in ("low", "nosig"):
        # ثقة ضعيفة: نجيب عينات صوت إضافية من أماكن تانية ونحاول تاني قبل ما نستسلم
        with ThreadPoolExecutor(max_workers=4) as ex:
            for w2 in ex.map(lambda f: _safe(grab, f), (0.05, 0.18, 0.50, 0.70, 0.95)):
                if w2:
                    wins.append(w2)
        res = _as_solve(cues, wins)
    return res


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def _as_cpath(key):
    return os.path.join(DATA_DIR, "autosync_%s.json" % key)


def _as_worker(key, u, src, se, ep, lang, dur):
    res = None
    try:
        with AS_SEM:
            res = _as_job(u, src, int(se) if str(se).isdigit() else None,
                          int(ep) if str(ep).isdigit() else None, lang, dur)
        if res.get("ok"):      # بنحفظ النجاح بس، والفشل بيتعاد تاني (عشان التحسينات تشتغل)
            try:
                store_save(_as_cpath(key), res)
            except Exception:
                pass
    except Exception as ex:
        res = {"ok": False, "reason": "error", "msg": str(ex)[:120]}
    finally:
        with AS_LOCK:
            AS_RUN[key] = {"t": time.time(), "res": res}


@app.post("/sub/auto")
def sub_auto_start():
    b = request.get_json(silent=True) or {}
    u, src = str(b.get("u", "")), str(b.get("src", ""))
    if not ok_sub_path(u):
        return jsonify(error="رابط ترجمة غير صالح"), 400
    if not AS_FF:
        return jsonify(done=True, ok=False, reason="noffmpeg")
    if not _as_safe_url(src):
        return jsonify(done=True, ok=False, reason="badsrc")
    se, ep = str(b.get("s", ""))[:6], str(b.get("e", ""))[:6]
    ident = str(b.get("key") or re.sub(r"[?#].*$", "", src))[:300]
    key = hashlib.sha1("|".join((u, se, ep, ident)).encode()).hexdigest()[:32]
    if not b.get("force"):
        try:
            return jsonify(done=True, **store_load(_as_cpath(key)))
        except Exception:
            pass
    try:
        dur = float(b.get("dur") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if os.environ.get("VERCEL"):    # serverless: الـ thread بيتقتل بعد الرد، فنشتغل جوه نفس الطلب ونرد بالنتيجة
        _as_worker(key, u, src, se, ep, str(b.get("lang", ""))[:20], dur)
        return jsonify(done=True, **(AS_RUN.get(key, {}).get("res") or {"ok": False, "reason": "error"}))
    with AS_LOCK:
        for k in [k for k, v in AS_RUN.items() if v.get("res") and time.time() - v["t"] > 600]:
            AS_RUN.pop(k, None)
        st = AS_RUN.get(key)
        if not st or st.get("res") is not None:          # مفيش شغل شغّال (النتايج النهائية اتحفظت في الكاش فوق)
            AS_RUN[key] = {"t": time.time(), "res": None}
            threading.Thread(target=_as_worker, daemon=True,
                             args=(key, u, src, se, ep, str(b.get("lang", ""))[:20], dur)).start()
    return jsonify(done=False, job=key)


@app.get("/sub/auto")
def sub_auto_poll():
    key = re.sub(r"[^a-f0-9]", "", request.args.get("job", ""))[:40]
    with AS_LOCK:
        st = AS_RUN.get(key)
    if st and st.get("res") is not None:
        return jsonify(done=True, **st["res"])
    if st:
        return jsonify(done=False, job=key)
    try:
        return jsonify(done=True, **store_load(_as_cpath(key)))
    except Exception:
        return jsonify(done=True, ok=False, reason="lost")


# ===================== نهاية ضبط الترجمة بالصوت =====================


# ===================== ترجمة بالذكاء الاصطناعي (لما مفيش ترجمة عربي) =====================
# بياخد أحسن ترجمة إنجليزي مطابقة للفيلم من SubDL ويترجمها عربي بـ Claude، سطر مقابل سطر وبنفس التوقيت،
# وبعدها بتعدّي على الضبط التلقائي بالصوت زي أي ترجمة. محتاج متغيّر AI_KEY (مفتاح Anthropic) على السيرفر.
# اختياري: AI_MODEL (الافتراضي claude-sonnet-5-5).
AI_KEY = os.environ.get("AI_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
# لو المفتاح بتاع Gemini (بيبدأ بـ AIza) أو AI_PROVIDER=gemini بيشتغل بـ Gemini، غير كده Claude
AI_GEMINI = os.environ.get("AI_PROVIDER", "").lower() == "gemini" or AI_KEY.startswith("AIza")
AI_MODEL = os.environ.get("AI_MODEL") or ("gemini-2.5-flash" if AI_GEMINI else "claude-sonnet-5-5")
AI_BATCH = 50
AI_RUN = {}
AI_SEM = threading.BoundedSemaphore(2)
AI_SYS = (
    "You are a professional audiovisual translator working on movie and TV subtitles. "
    "Translate each subtitle line into natural, fluent Arabic (clear Modern Standard Arabic that sounds like real speech). "
    "Keep character names, keep the tone and emotion, translate idioms by meaning, keep lines short enough to read on screen. "
    "Translate sound cues in brackets too, e.g. [music] -> [موسيقى]. "
    "Rules: return ONLY a JSON array of strings, exactly the same length and order as the input 'lines'. "
    "Never merge, split, skip or reorder lines. Use 'before' and 'after' only as context, do not translate them. "
    "No explanations, no markdown."
)


def _ai_cpath(key):
    return os.path.join(DATA_DIR, "aitr_%s.json" % key)


def _vtt_text_cues(vtt):
    out = []
    for b in re.split(r"\n\s*\n", vtt.strip()):
        ln = b.split("\n")
        k = next((i for i, x in enumerate(ln) if "-->" in x), None)
        if k is not None:
            out.append(re.sub(r"<[^>]+>", "", "\n".join(ln[k + 1:])).strip())
    return out


def _ai_apply(vtt, key, rtl):
    """بيبدّل نص كل جملة بالترجمة العربي (التوقيت زي ما هو)."""
    texts = store_load(_ai_cpath(key)).get("texts") or []
    parts, n = re.split(r"(\n\s*\n)", vtt.strip()), 0
    for i, b in enumerate(parts):
        ln = b.split("\n")
        k = next((j for j, x in enumerate(ln) if "-->" in x), None)
        if k is None:
            continue
        tx = texts[n] if n < len(texts) and texts[n] else None
        n += 1
        if tx:
            body = [rtl_wrap(x) if rtl else x for x in tx.split("\n")]
            parts[i] = "\n".join(ln[:k + 1] + body)
    return "".join(parts) + "\n"


def _ai_call_gemini(user):
    r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % AI_MODEL,
                      timeout=150, headers={"x-goog-api-key": AI_KEY, "content-type": "application/json"},
                      json={"systemInstruction": {"parts": [{"text": AI_SYS}]},
                            "contents": [{"role": "user", "parts": [{"text": user}]}],
                            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8000,
                                                 "responseMimeType": "application/json",
                                                 "thinkingConfig": {"thinkingBudget": 0}}})
    d = r.json()
    if r.status_code != 200:
        raise Exception(str((d.get("error") or {}).get("message") or r.status_code)[:160])
    cs = d.get("candidates") or [{}]
    return "".join(x.get("text", "") for x in ((cs[0].get("content") or {}).get("parts") or []))


def _ai_call(user):
    if AI_GEMINI:
        return _ai_call_gemini(user)
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=150,
                      headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": AI_MODEL, "max_tokens": 8000, "temperature": 0.2,
                            "system": AI_SYS, "messages": [{"role": "user", "content": user}]})
    d = r.json()
    if r.status_code != 200:
        raise Exception(str((d.get("error") or {}).get("message") or r.status_code)[:160])
    return "".join(x.get("text", "") for x in d.get("content", []) if x.get("type") == "text")


def _ai_batch(lines, before, after, title, depth=0):
    """بيترجم مجموعة سطور. لو العدد ما طابقش بيقسمها نصين، وآخر حاجة بيسيب السطر زي ما هو."""
    if not lines:
        return []
    msg = json.dumps({"title": title, "before": before, "lines": lines, "after": after}, ensure_ascii=False)
    for _ in range(2):
        try:
            t = _ai_call(msg)
            a, z = t.find("["), t.rfind("]")
            res = json.loads(t[a:z + 1])
            if isinstance(res, list) and len(res) == len(lines) and all(isinstance(x, str) for x in res):
                return [x.strip() for x in res]
        except requests.RequestException:
            time.sleep(1.5)
        except ValueError:
            pass
    if len(lines) == 1 or depth > 4:
        return list(lines)
    h = len(lines) // 2
    return (_ai_batch(lines[:h], before, lines[h:h + 3], title, depth + 1) +
            _ai_batch(lines[h:], lines[max(0, h - 3):h], after, title, depth + 1))


def _ai_job(key, u, season, episode, title):
    vtt, _ = build_sub(u, season, episode, False, "vtt", 0.0, 1.0)
    src = _vtt_text_cues(vtt)
    if len(src) < 20:
        return {"ok": False, "reason": "fewcues"}
    jobs = []
    for i in range(0, len(src), AI_BATCH):
        jobs.append((src[i:i + AI_BATCH], src[max(0, i - 4):i], src[i + AI_BATCH:i + AI_BATCH + 4]))
    with ThreadPoolExecutor(max_workers=4) as ex:
        parts = list(ex.map(lambda j: _ai_batch(j[0], j[1], j[2], title), jobs))
    texts = [x for p in parts for x in p]
    done = sum(1 for a, b in zip(src, texts) if a != b)
    if done < len(src) * 0.5:
        return {"ok": False, "reason": "aifail"}
    store_save(_ai_cpath(key), {"texts": texts, "model": AI_MODEL})
    return {"ok": True, "ai": key, "n": len(texts)}


def _ai_worker(key, u, se, ep, title):
    res = None
    try:
        with AI_SEM:
            res = _ai_job(key, u, int(se) if str(se).isdigit() else None,
                          int(ep) if str(ep).isdigit() else None, title)
    except Exception as ex:
        res = {"ok": False, "reason": "error", "msg": str(ex)[:120]}
    finally:
        with AS_LOCK:
            AI_RUN[key] = {"t": time.time(), "res": res}


@app.post("/sub/ai")
def sub_ai_start():
    b = request.get_json(silent=True) or {}
    u = str(b.get("u", ""))
    if not ok_sub_path(u):
        return jsonify(error="رابط ترجمة غير صالح"), 400
    if not AI_KEY:
        return jsonify(done=True, ok=False, reason="noai")
    se, ep = str(b.get("s", ""))[:6], str(b.get("e", ""))[:6]
    key = hashlib.sha1("|".join((u, se, ep, AI_MODEL)).encode()).hexdigest()[:32]
    try:
        store_load(_ai_cpath(key))
        return jsonify(done=True, ok=True, ai=key)
    except Exception:
        pass
    with AS_LOCK:
        st = AI_RUN.get(key)
        if not st or st.get("res") is not None:
            AI_RUN[key] = {"t": time.time(), "res": None}
            threading.Thread(target=_ai_worker, daemon=True,
                             args=(key, u, se, ep, str(b.get("title", ""))[:120])).start()
    return jsonify(done=False, job=key)


@app.get("/sub/ai")
def sub_ai_poll():
    key = re.sub(r"[^a-f0-9]", "", request.args.get("job", ""))[:40]
    with AS_LOCK:
        st = AI_RUN.get(key)
    if st and st.get("res") is not None:
        return jsonify(done=True, **st["res"])
    if st:
        return jsonify(done=False, job=key)
    try:
        store_load(_ai_cpath(key))
        return jsonify(done=True, ok=True, ai=key)
    except Exception:
        return jsonify(done=True, ok=False, reason="lost")


# ===================== نهاية الترجمة بالذكاء الاصطناعي =====================


# ----------------- إشعارات الحلقات الجديدة (Web Push) -----------------

try:
    from pywebpush import webpush, WebPushException
except Exception:  # المكتبة مش متسطّبة على السيرفر
    webpush = None

    class WebPushException(Exception):
        pass

PUSH_FILE = os.path.join(DATA_DIR, "push.json")
PUSH_LOCK = threading.Lock()
PUSH_HOURS = float(os.environ.get("PUSH_CHECK_HOURS", "3") or 3)
VAPID_SUB = os.environ.get("VAPID_EMAIL", "mailto:admin@example.com")


def push_load():
    try:
        d = store_load(PUSH_FILE)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("subs", {})
    d.setdefault("state", {})
    return d


def push_save(d):
    store_save(PUSH_FILE, d)


def vapid_keys():
    """مفاتيح VAPID بتتعمل أول مرة وتتحفظ (Redis لو متاح، وإلا DATA_DIR). الـpem بيتكتب في /tmp للاستخدام."""
    pem = os.path.join(DATA_DIR if not KV_ON else "/tmp", "vapid_private.pem")
    pubf = os.path.join(DATA_DIR, "vapid_public.txt")
    if KV_ON:
        try:
            saved = kv_get("vapid.json")
        except Exception:
            saved = None
        if not (isinstance(saved, dict) and saved.get("pem") and saved.get("pub")):
            from py_vapid import Vapid
            from cryptography.hazmat.primitives import serialization
            v = Vapid()
            v.generate_keys()
            v.save_key(pem)
            raw = v.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint)
            with open(pem, encoding="utf-8") as f:
                pem_txt = f.read()
            saved = {"pem": pem_txt,
                     "pub": base64.urlsafe_b64encode(raw).rstrip(b"=").decode()}
            try:
                _kv_cmd("SET", KV_PREFIX + "vapid.json", json.dumps(saved), "NX")
                saved = kv_get("vapid.json") or saved
            except Exception:
                pass
        if not os.path.exists(pem) or open(pem, encoding="utf-8").read() != saved["pem"]:
            with open(pem, "w", encoding="utf-8") as f:
                f.write(saved["pem"])
        return pem, saved["pub"]
    if not (os.path.exists(pem) and os.path.exists(pubf)):
        from py_vapid import Vapid
        from cryptography.hazmat.primitives import serialization
        v = Vapid()
        v.generate_keys()
        v.save_key(pem)
        raw = v.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        with open(pubf, "w", encoding="utf-8") as f:
            f.write(base64.urlsafe_b64encode(raw).rstrip(b"=").decode())
    with open(pubf, encoding="utf-8") as f:
        return pem, f.read().strip()


def push_send(user, title, body, url="", tag=""):
    if webpush is None:
        return 0
    pem, _ = vapid_keys()
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag},
                         ensure_ascii=False)
    sent = 0
    with PUSH_LOCK:
        d = push_load()
        keep = []
        for s in d["subs"].get(user, []):
            try:
                webpush(subscription_info=s, data=payload, vapid_private_key=pem,
                        vapid_claims={"sub": VAPID_SUB}, ttl=86400)
                sent += 1
                keep.append(s)
            except WebPushException as ex:
                code = getattr(getattr(ex, "response", None), "status_code", 0)
                if code not in (404, 410):   # الاشتراك مات بس لو 404/410
                    keep.append(s)
            except Exception:
                keep.append(s)
        d["subs"][user] = keep
        push_save(d)
    return sent


def follows_of(user):
    """المسلسلات اللي في "قائمتي" عند المستخدم (كل البروفايلات)"""
    ud = ud_load(user)
    profs = ud.get("profiles", {})
    out = {}
    for pid, its in (ud.get("items") or {}).items():
        if (profs.get(pid) or {}).get("del"):
            continue
        for k, it in (its or {}).items():
            if (str(k).startswith("l:tv:") and isinstance(it, dict)
                    and not it.get("del") and str(it.get("tmdb", "")).isdigit()):
                out[int(it["tmdb"])] = it.get("title") or ""
    return out


def check_episodes():
    if not TMDB_KEY:
        return {"error": "TMDB_KEY مش متضاف"}
    names = [APP_USER.lower()] + sorted(users_load())
    cache = {}
    res = {"shows": 0, "sent": 0, "baseline": 0}
    for u in names:
        ids = follows_of(u)
        if not ids:
            continue
        with PUSH_LOCK:
            st = dict(push_load()["state"].get(u, {}))
        dirty = False
        for tid, title in ids.items():
            res["shows"] += 1
            if tid not in cache:
                try:
                    cache[tid] = tmdb_get(f"/tv/{tid}", language="ar")
                except Exception:
                    cache[tid] = None
            info = cache[tid] or {}
            le = info.get("last_episode_to_air") or {}
            s, e = le.get("season_number"), le.get("episode_number")
            if not s or e is None:
                continue
            cur, key = [int(s), int(e)], str(tid)
            prev = st.get(key)
            if prev is None:
                st[key] = cur
                dirty = True
                res["baseline"] += 1
            elif tuple(cur) > tuple(prev):
                body = f"S{cur[0]:02d}E{cur[1]:02d}" + (
                    f" · {le['name']}" if le.get("name") else "")
                res["sent"] += push_send(
                    u, f"🔔 حلقة جديدة: {info.get('name') or title}", body,
                    f"#open=tv:{tid}", f"tv{tid}")
                st[key] = cur
                dirty = True
        if dirty:
            with PUSH_LOCK:
                d = push_load()
                d["state"].setdefault(u, {}).update(st)
                push_save(d)
    return res


def run_exclusive(fn, name="push"):
    """بيمنع أكتر من عملية سيرفر تشتغل الفحص في نفس الوقت"""
    try:
        import fcntl
    except ImportError:
        return fn()
    with open(os.path.join(DATA_DIR, f"{name}.lock"), "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        try:
            return fn()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def push_loop():
    time.sleep(90)
    while True:
        try:
            run_exclusive(check_episodes)
        except Exception:
            pass
        time.sleep(max(PUSH_HOURS, 0.25) * 3600)


@app.get("/push/key")
def push_key():
    if webpush is None:
        return jsonify(enabled=False, error="ضيف pywebpush في requirements.txt وأعد النشر")
    try:
        return jsonify(enabled=True, key=vapid_keys()[1])
    except Exception as ex:
        return jsonify(enabled=False, error=f"مفاتيح الإشعارات: {ex}")


@app.post("/push/subscribe")
def push_subscribe():
    sub = (request.get_json(silent=True) or {}).get("subscription")
    if (not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict)
            or not str(sub.get("endpoint", "")).startswith("https://")):
        return jsonify(error="اشتراك غير صالح"), 400
    clean = {"endpoint": str(sub["endpoint"])[:600],
             "keys": {"p256dh": str(sub["keys"].get("p256dh", ""))[:200],
                      "auth": str(sub["keys"].get("auth", ""))[:100]}}
    with PUSH_LOCK:
        d = push_load()
        lst = [x for x in d["subs"].get(g.user, []) if x["endpoint"] != clean["endpoint"]]
        lst.append(clean)
        d["subs"][g.user] = lst[-5:]
        push_save(d)
    return jsonify(ok=True)


@app.post("/push/unsubscribe")
def push_unsubscribe():
    ep = str((request.get_json(silent=True) or {}).get("endpoint", ""))
    with PUSH_LOCK:
        d = push_load()
        d["subs"][g.user] = [x for x in d["subs"].get(g.user, []) if x["endpoint"] != ep]
        push_save(d)
    return jsonify(ok=True)


@app.post("/push/test")
def push_test():
    if webpush is None:
        return jsonify(error="ضيف pywebpush في requirements.txt وأعد النشر"), 400
    try:
        n = push_send(g.user, "🔔 إشعار تجريبي", "الإشعارات شغالة ✅", "", "test")
    except Exception as ex:
        return jsonify(error=f"فشل الإرسال: {ex}"), 500
    return jsonify(sent=n)


@app.post("/push/check")
def push_check():
    if webpush is None:
        return jsonify(error="ضيف pywebpush في requirements.txt وأعد النشر"), 400
    r = run_exclusive(check_episodes)
    return jsonify(r if r is not None else {"error": "فحص تاني شغال دلوقتي"})


ST_SYNC_MIN = float(os.environ.get("ST_SYNC_MIN", "60") or 60)


def st_auto_sync():
    with ST_LOCK:
        d = st_load()
        if not d.get("authKey"):
            return
        try:
            st_import(d)
            st_save(d)
        except Exception:
            pass


def st_sync_loop():
    time.sleep(120)
    while True:
        try:
            run_exclusive(st_auto_sync, "stsync")
        except Exception:
            pass
        time.sleep(max(ST_SYNC_MIN, 5) * 60)


if os.environ.get("ST_AUTOSYNC", "1") != "0":
    threading.Thread(target=st_sync_loop, daemon=True).start()


# =====================================================================
# ===== مضاف: تخصيص الرئيسية حسب المشاهدة + browse/hero/interest ======
# ===== كل راوت بيتسجّل فقط لو مش موجود (مفيش تعارض مع القديم) ========
# =====================================================================
_LANG_NAMES = {"ko": "الكوري", "hi": "الهندي", "tr": "التركي", "ar": "العربي", "en": "الإنجليزي",
               "ja": "الياباني", "es": "الإسباني", "fr": "الفرنسي", "zh": "الصيني", "ta": "التاميل",
               "te": "التيلجو", "ml": "المالايالام", "th": "التايلاندي", "ru": "الروسي", "de": "الألماني",
               "it": "الإيطالي", "pt": "البرتغالي", "fa": "الفارسي", "ur": "الأردو"}
_GENRE_KEYS = {  # key: (اسم, movie id, tv id)
    "action": ("أكشن", 28, 10759), "drama": ("دراما", 18, 18), "comedy": ("كوميديا", 35, 35),
    "crime": ("جريمة", 80, 80), "scifi": ("خيال علمي", 878, 10765), "horror": ("رعب", 27, 9648),
    "romance": ("رومانسي", 10749, 10749), "cartoon": ("كرتون", 16, 16), "family": ("عائلي", 10751, 10751),
    "doc": ("وثائقي", 99, 99)}
_META_CACHE = {}
_FY_CACHE = {}


def _ext_has(path):
    return any(r.rule == path for r in app.url_map.iter_rules())


def _add_get(path):
    def deco(fn):
        if not _ext_has(path):
            app.add_url_rule(path, "x_" + fn.__name__, fn, methods=["GET"])
        return fn
    return deco


def _norm_ty(t):
    return "tv" if t in ("tv", "series", "show") else "movie"


def _meta(ty, tid):
    k = (ty, tid)
    if k in _META_CACHE:
        return _META_CACHE[k]
    try:
        d = tmdb_get(f"/{ty}/{tid}", language="en-US")
        m = {"lang": d.get("original_language") or "", "genres": [g["id"] for g in d.get("genres", [])]}
    except Exception:
        m = None
    if m is not None:
        _META_CACHE[k] = m
    return m


def _cards(res, default_ty, limit=20, skip_ids=()):
    out = []
    for x in res:
        mt = x.get("media_type") or default_ty
        if mt not in ("movie", "tv") or not x.get("poster_path") or x.get("id") in skip_ids:
            continue
        d_s = x.get("release_date") or x.get("first_air_date") or ""
        out.append({"id": x["id"], "type": mt, "year": d_s[:4],
                    "title": x.get("title") or x.get("name") or "",
                    "rating": round(x.get("vote_average") or 0, 1),
                    "poster": img(x.get("poster_path"), "w342")})
    return out[:limit]


def _watch_profile(user, pid):
    """يجمّع المشاهدات في فئات (نوع + لغة + كرتون/مدبلج) بوزن الأحدث أعلى"""
    with UD_LOCK:
        d = ud_load(user)
    its = [x for x in (d.get("items", {}).get(pid) or {}).values()
           if isinstance(x, dict) and not x.get("del") and x.get("tmdb")]
    its.sort(key=lambda x: tnum(x), reverse=True)
    its = its[:40]
    try:
        dub_ids = {x.get("id") for x in dub_items()}
    except Exception:
        dub_ids = set()
    buckets, seen, recent = {}, set(), []
    for n, x in enumerate(its):
        ty = _norm_ty(x.get("type"))
        tid = x.get("tmdb")
        seen.add(tid)
        m = _meta(ty, tid)
        if not m:
            continue
        w = 1.0 / (1 + n * 0.15)
        if str(x.get("key", ""))[:1] == "l":
            w *= 0.5
        anim = 16 in m["genres"]
        if tid in dub_ids:
            bk = ("dub", "", True)
        else:
            bk = (ty, m["lang"], anim)
        buckets[bk] = buckets.get(bk, 0) + w
        recent.append((x, ty, m))
    return recent, buckets, seen


@_add_get("/for-you")
def x_for_you_impl():
    pid = request.args.get("pid", "")
    lite = bool(request.args.get("lite"))
    if not pid or not TMDB_KEY:
        return jsonify(rows=[])
    try:
        recent, buckets, seen = _watch_profile(g.user, pid)
    except Exception:
        return jsonify(rows=[])
    if not buckets:
        return jsonify(rows=[])
    ck = (g.user, pid, lite, tuple(sorted((str(k), round(v, 1)) for k, v in buckets.items())))
    c = _FY_CACHE.get(ck)
    if c and time.time() - c[0] < 900:
        return jsonify(rows=c[1])
    tot = sum(buckets.values()) or 1
    rows = []
    for (ty, lg, anim), w in sorted(buckets.items(), key=lambda kv: -kv[1])[:(3 if lite else 5)]:
        share = w / tot
        if share < 0.1 and rows:
            continue
        if ty == "dub":
            items = [x for x in dub_items() if x.get("id") not in seen]
            if len(items) >= 3:
                rows.append({"title": "🇪🇬 كرتون مدبلج بالمصري ليك", "items": items[:20]})
            jobs = [("🧸 كرتون وأنيميشن عائلي", "movie", {"with_genres": "16|10751", "with_original_language": "en"})]
        else:
            nm = _LANG_NAMES.get(lg, lg)
            kind = "أفلام" if ty == "movie" else "مسلسلات"
            if anim:
                jobs = [(f"🧸 كرتون {nm} ليك" if lg != "en" else "🧸 أفلام كرتون ليك", ty,
                         {"with_original_language": lg, "with_genres": 16}),
                        ("🇪🇬 كرتون مدبلج بالمصري", None, None)]
            else:
                jobs = [(f"{'🎬' if ty == 'movie' else '📺'} {kind} {nm} ليك", ty,
                         {"with_original_language": lg, "without_genres": 16}),
                        (f"⭐ الأعلى تقييماً — {kind} {nm}", ty,
                         {"with_original_language": lg, "without_genres": 16,
                          "sort_by": "vote_average.desc", "vote_count.gte": "150"})]
                if lite:
                    jobs = jobs[:1]
        for title, t2, extra in jobs:
            if t2 is None:   # صف المدبلج لمحبي الكرتون
                items = [x for x in dub_items() if x.get("id") not in seen]
                if len(items) >= 3 and not any(r["title"].startswith("🇪🇬") for r in rows):
                    rows.append({"title": title, "items": items[:20]})
                continue
            q = {"language": "ar", "include_adult": "false", "sort_by": "popularity.desc"}
            q.update(extra)
            try:
                items = _cards(tmdb_get("/discover/" + t2, **q).get("results", []), t2, 20, seen)
            except Exception:
                continue
            if len(items) >= 3:
                rows.append({"title": title, "items": items})
    for x, ty, m in recent[:2]:
        try:
            res = tmdb_get(f"/{ty}/{x['tmdb']}/recommendations", language="ar").get("results", [])
            items = _cards(res, ty, 20, seen)
        except Exception:
            continue
        if len(items) >= 3:
            rows.insert(0, {"title": "🎯 علشان شفت " + (x.get("title") or ""), "items": items})
    _FY_CACHE[ck] = (time.time(), rows)
    return jsonify(rows=rows)


def _discover_page(ty, extra, skip):
    page = skip // 20 + 1
    q = {"language": "ar", "include_adult": "false", "sort_by": "popularity.desc", "page": page}
    q.update(extra)
    res = tmdb_get("/discover/" + ty, **q)
    return _cards(res.get("results", []), ty, 40), page < min(res.get("total_pages", 1), 25)


def _metas_to_items(metas, ty):
    items = []
    for m in (metas or []):
        iid = str(m.get("id", ""))
        if not m.get("poster"):
            continue
        it = {"type": "tv" if (m.get("type") or ty) == "series" else "movie",
              "title": m.get("name") or "", "poster": m["poster"],
              "year": str(m.get("year") or m.get("releaseInfo") or "")[:4]}
        if re.fullmatch(r"tt\d+", iid):
            it["imdb"] = iid
        elif iid.startswith("tmdb:") and iid[5:].isdigit():
            it["id"] = int(iid[5:])
        elif it["title"]:
            it["lk"] = True
        else:
            continue
        items.append(it)
    return items


def cat_items_page(a, e, skip):
    """صفحة كاملة من كتالوج إضافة (للمزيد). بترجع (items, raw_count)"""
    path = f"/catalog/{e['ty']}/{quote(e['c'], safe='')}"
    extra = []
    if e.get("g"):
        extra.append(f"genre={quote(e['g'], safe='')}")
    if skip:
        extra.append(f"skip={int(skip)}")
    if extra:
        path += "/" + "&".join(extra)
    r = requests.get(a["base"] + path + ".json", timeout=20,
                     headers={"User-Agent": "Mozilla/5.0"})
    metas = r.json().get("metas") or []
    return _metas_to_items(metas, e["ty"]), len(metas)


def browse_row(k, skip):
    """المزيد بتاع صف من الصفحة الرئيسية (الـ id بتاعه هاش مش لغة). None لو مش صف."""
    d = ensure_catalogs()
    ent = next((e for e in home_layout(d) if e.get("k") == k), None)
    if ent is None:
        return None
    if ent.get("a") == "_dub":
        return jsonify(items=dub_items(), skip=skip, more=False)
    amap = {a["id"]: a for a in d["addons"]}
    items, seen, raw_max = [], set(), 0
    for s_ in [ent] + [dict(ent, a=m["a"], ty=m["ty"], c=m["c"], g=m.get("g", "")) for m in (ent.get("m") or [])]:
        if s_["a"] == "_dub":
            part, raw = dub_items(), 0
        elif s_["a"] in amap:
            try:
                part, raw = cat_items_page(amap[s_["a"]], s_, skip)
            except Exception:
                continue
        else:
            continue
        raw_max = max(raw_max, raw)
        for x in part:
            kk = x.get("imdb") or x.get("id") or x.get("title")
            if kk not in seen:
                seen.add(kk)
                items.append(x)
    return jsonify(items=items, skip=skip + raw_max, more=raw_max > 0 and skip < 400)



@_add_get("/browse")
def x_browse_impl():
    a = request.args
    ty = a.get("type", "")
    try:
        skip = int(a.get("skip", "0") or 0)
    except ValueError:
        skip = 0
    gk, lang, k, fresh = a.get("genre", ""), a.get("int", ""), a.get("k", ""), a.get("fresh", "")
    if k.startswith("fav:"):
        try:
            fk, fty = k[4:].rsplit(":", 1)
            q = fav_query(fk, fty)
            if q is not None and fty in ("movie", "tv"):
                its, mr = _discover_page(fty, q, skip)
                return jsonify(items=its, skip=skip + 20, more=mr)
        except Exception as ex:
            return jsonify(error=str(ex))
    if k and k not in _GENRE_KEYS and k not in _LANG_NAMES:
        try:
            got = browse_row(k, skip)
        except Exception as ex:
            return jsonify(error=str(ex))
        if got is not None:
            return got
    if k:
        if k in _GENRE_KEYS:
            gk = k
        else:
            lang = k
    if fresh:
        lang = fresh
    types = [ty] if ty in ("movie", "tv") else ["movie", "tv"]
    if ty in ("series",):
        types = ["tv"]
    items, more = [], False
    try:
        for t in types:
            extra = {}
            if gk in _GENRE_KEYS:
                gid = _GENRE_KEYS[gk][1 if t == "movie" else 2]
                extra["with_genres"] = gid
            if lang:
                extra["with_original_language"] = lang
            if fresh:
                d0 = date.today()
                extra["primary_release_date.gte" if t == "movie" else "first_air_date.gte"] = (d0 - timedelta(days=120)).isoformat()
                extra["primary_release_date.lte" if t == "movie" else "first_air_date.lte"] = d0.isoformat()
            if gk == "cartoon" and t == "tv":
                extra["with_genres"] = 16
            it, mo = _discover_page(t, extra, skip)
            items += it
            more = more or mo
    except Exception as ex:
        return jsonify(error=str(ex))
    # دمج الفيلم/المسلسل بالتبادل
    if len(types) == 2:
        m_ = [x for x in items if x["type"] == "movie"]
        t_ = [x for x in items if x["type"] == "tv"]
        items = [y for pr in zip(m_, t_) for y in pr] + m_[len(t_):] + t_[len(m_):]
    return jsonify(items=items, skip=skip + 20, more=more)


@_add_get("/hero")
def x_hero_impl():
    if not TMDB_KEY:
        return jsonify(items=[])
    out = []
    try:
        for x in tmdb_get("/trending/all/week", language="ar").get("results", []):
            mt = x.get("media_type")
            if mt not in ("movie", "tv") or not x.get("backdrop_path"):
                continue
            d_s = x.get("release_date") or x.get("first_air_date") or ""
            out.append({"id": x["id"], "type": mt, "year": d_s[:4],
                        "title": x.get("title") or x.get("name") or "",
                        "overview": (x.get("overview") or "")[:220],
                        "rating": round(x.get("vote_average") or 0, 1),
                        "backdrop": img(x["backdrop_path"], "w1280"),
                        "poster": img(x.get("poster_path"), "w342")})
    except Exception:
        pass
    return jsonify(items=out[:8])


@_add_get("/interest/list")
def x_interest_list_impl():
    items = [{"k": lg, "name": "🌐 " + nm} for lg, nm in _LANG_NAMES.items()]
    items += [{"k": gk, "name": v[0]} for gk, v in _GENRE_KEYS.items()]
    return jsonify(items=items)


@_add_get("/interest")
def x_interest_impl():
    ks = [k for k in request.args.get("k", "").split(",") if k][:6]
    rows = []
    for k in ks:
        try:
            if k in _GENRE_KEYS:
                it, _ = _discover_page("movie", {"with_genres": _GENRE_KEYS[k][1]}, 0)
                title = "⭐ " + _GENRE_KEYS[k][0]
                rid = k
            else:
                it, _ = _discover_page("movie", {"with_original_language": k}, 0)
                title = "⭐ أفلام " + _LANG_NAMES.get(k, k)
                rid = "int:" + k
        except Exception:
            continue
        if len(it) >= 3:
            rows.append({"id": rid, "title": title, "items": it[:20]})
    return jsonify(rows=rows)


# ================= الجديد فقط + التصنيف العمري =================
NEW_DAYS_MOVIE = 21        # الفيلم يفضل «جديد» كام يوم من نزوله (غيّر الرقم لو عاوز مدة أقل/أكتر)
NEW_DAYS_TV = 21           # المسلسل/الكرتون: كام يوم من أول عرض
NEW_MIN_POP_MOVIE = 6      # حد أدنى للشعبية عشان مايظهرش أفلام مجهولة
NEW_MIN_POP_TV = 4
NEW_ROW_MAX = 20           # أقصى عدد أعمال في الصف
_CERT_CACHE = {}
_NEW_CACHE = {"t": 0, "d": None}
_LV0 = {"G", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "U", "ALL", "AL", "L", "TP"}
_LV1 = {"PG", "TV-PG", "UA"}
_LV2 = {"PG-13", "TV-14", "12A"}
_LV3 = {"R", "TV-MA", "A", "NC-17", "X", "XXX", "R18", "R18+", "R15+", "18+"}
_FLAGS = {"ko": "🇰🇷", "hi": "🇮🇳", "ta": "🇮🇳", "te": "🇮🇳", "ml": "🇮🇳", "ja": "🇯🇵", "tr": "🇹🇷", "zh": "🇨🇳", "th": "🇹🇭"}


def _cert_info(raw, adult=False):
    c = (raw or "").strip().upper().replace(" ", "")
    if adult or c in ("NC-17", "X", "XXX", "R18", "R18+"):
        return "+18", 3
    if c in _LV0:
        return "عائلي", 0
    if c in _LV1:
        return "بإشراف", 1
    if c in _LV2:
        return "+13", 2
    if c in _LV3:
        return ("+17" if c in ("R", "TV-MA") else "+18"), 3
    if c.isdigit():
        n = int(c)
        if n <= 7:
            return "عائلي", 0
        if n <= 10:
            return "بإشراف", 1
        if n <= 14:
            return "+13", 2
        return "+" + str(n), 3
    return "غير مصنّف", -1


def cert_of(ty, tid):
    ty = _norm_ty(ty)
    k = (ty, str(tid))
    h = _CERT_CACHE.get(k)
    if h and time.time() - h[0] < 7 * 86400:
        return h[1]
    raw, adult = "", False
    try:
        if ty == "movie":
            d = tmdb_get(f"/movie/{tid}", language="en-US", append_to_response="release_dates")
            adult = bool(d.get("adult"))
            res = (d.get("release_dates") or {}).get("results", [])
            for cc in ("US", "GB", "KR", "IN", "EG", None):
                for r in res:
                    if cc and r.get("iso_3166_1") != cc:
                        continue
                    for x in r.get("release_dates", []):
                        if (x.get("certification") or "").strip():
                            raw = x["certification"]
                            break
                    if raw:
                        break
                if raw:
                    break
        else:
            d = tmdb_get(f"/tv/{tid}", language="en-US", append_to_response="content_ratings")
            res = (d.get("content_ratings") or {}).get("results", [])
            for cc in ("US", "GB", "KR", "IN", "EG", None):
                for r in res:
                    if cc and r.get("iso_3166_1") != cc:
                        continue
                    if (r.get("rating") or "").strip():
                        raw = r["rating"]
                        break
                if raw:
                    break
    except Exception:
        return {"cert": "", "label": "غير مصنّف", "lvl": -1, "ok": False}
    label, lvl = _cert_info(raw, adult)
    info = {"cert": raw, "label": label, "lvl": lvl, "ok": 0 <= lvl <= 2}
    _CERT_CACHE[k] = (time.time(), info)
    return info


@_add_get("/cert")
def x_cert_impl():
    return jsonify(cert_of(request.args.get("type", "movie"), request.args.get("id", "")))


def _new_discover(path, q):
    out = []
    for pg in (1, 2):
        try:
            res = tmdb_get(path, language="ar", include_adult="false", sort_by="popularity.desc", page=pg, **q).get("results", [])
        except Exception:
            break
        out += res
        if len(res) < 20:
            break
    return out


def _age_txt(n):
    if n <= 0:
        return "النهاردة"
    if n == 1:
        return "امبارح"
    return f"من {n} أيام" if n <= 10 else f"من {n} يوم"


def build_new():
    today = date.today()
    tdy = today.isoformat()
    gm = (today - timedelta(days=NEW_DAYS_MOVIE)).isoformat()
    gt = (today - timedelta(days=NEW_DAYS_TV)).isoformat()
    jobs = [
        ("movie", "us", NEW_MIN_POP_MOVIE, "/discover/movie", {"with_release_type": "4|5", "region": "US", "release_date.gte": gm, "release_date.lte": tdy}),
        ("movie", "kr", NEW_MIN_POP_MOVIE, "/discover/movie", {"with_origin_country": "KR", "primary_release_date.gte": gm, "primary_release_date.lte": tdy}),
        ("movie", "in", NEW_MIN_POP_MOVIE, "/discover/movie", {"with_origin_country": "IN", "primary_release_date.gte": gm, "primary_release_date.lte": tdy}),
        ("tv", "all", NEW_MIN_POP_TV, "/discover/tv", {"first_air_date.gte": gt, "first_air_date.lte": tdy}),
        ("tv", "kr", NEW_MIN_POP_TV, "/discover/tv", {"with_origin_country": "KR", "first_air_date.gte": gt, "first_air_date.lte": tdy}),
        ("tv", "in", NEW_MIN_POP_TV, "/discover/tv", {"with_origin_country": "IN", "first_air_date.gte": gt, "first_air_date.lte": tdy}),
        ("movie", "ar", 1.5, "/discover/movie", {"with_original_language": "ar", "primary_release_date.gte": gm, "primary_release_date.lte": tdy}),
        ("tv", "ar", 1.5, "/discover/tv", {"with_original_language": "ar", "first_air_date.gte": gt, "first_air_date.lte": tdy}),
    ]
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda j: _new_discover(j[3], j[4]), jobs))
    cand = {}
    for j, res in zip(jobs, results):
        for x in res:
            if not x.get("poster_path") or (x.get("popularity") or 0) < j[2]:
                continue
            key = (j[0], x["id"])
            if key not in cand:
                cand[key] = (x, j[1])
    keys = list(cand.keys())
    with ThreadPoolExecutor(max_workers=8) as ex:
        certs = dict(zip(keys, ex.map(lambda k: cert_of(k[0], k[1]), keys)))
    foreign, arabic, toons, hero = [], [], [], []
    for (ty, tid), (x, src) in cand.items():
        c = certs[(ty, tid)]
        d_s = x.get("release_date") or x.get("first_air_date") or ""
        lg = x.get("original_language") or ""
        badge = _FLAGS.get(lg, "")
        if src != "us" and d_s:
            try:
                badge = (badge + " " + _age_txt((today - date.fromisoformat(d_s)).days)).strip()
            except Exception:
                pass
        it = {"id": tid, "type": ty, "year": d_s[:4], "title": x.get("title") or x.get("name") or "",
              "rating": round(x.get("vote_average") or 0, 1), "poster": img(x.get("poster_path"), "w342"),
              "badge": badge, "cl": c["label"], "lv": c["lvl"], "lg": lg, "pop": x.get("popularity") or 0}
        if 16 in (x.get("genre_ids") or []):
            toons.append(it)
        elif lg == "ar":
            arabic.append(it)
        else:
            foreign.append(it)
        if c["ok"] and x.get("backdrop_path") and (x.get("overview") or "").strip():
            hero.append(dict(it, overview=(x.get("overview") or "")[:220], backdrop=img(x["backdrop_path"], "w1280")))
    for lst in (foreign, arabic, toons, hero):
        lst.sort(key=lambda i: -i["pop"])
    rows = []
    for rid, title, lst in (("new-for", "🌍 جديد أجنبي", foreign), ("new-ar", "🇪🇬 جديد عربي", arabic), ("new-toon", "🧸 كرتون وأنمي جديد", toons)):
        if lst:
            rows.append({"id": rid, "title": title, "items": lst[:NEW_ROW_MAX]})
    return {"rows": rows, "hero": hero[:10]}


@_add_get("/new")
def x_new_impl():
    if not TMDB_KEY:
        return jsonify(rows=[], hero=[])
    now = time.time()
    if _NEW_CACHE["d"] is not None and now - _NEW_CACHE["t"] < 1800:
        return jsonify(_NEW_CACHE["d"])
    try:
        d = build_new()
    except Exception as ex:
        if _NEW_CACHE["d"] is not None:
            return jsonify(_NEW_CACHE["d"])
        return jsonify(rows=[], hero=[], error=str(ex))
    _NEW_CACHE.update(t=now, d=d)
    return jsonify(d)


# ================= المفضلة: منصات / لغات / أنواع / أنمي (كل واحد بيطلع له صفين: أفلام + مسلسلات) =================
_LANG_FEM = {"ar": "عربية", "ko": "كورية", "hi": "هندية", "tr": "تركية", "ja": "يابانية", "zh": "صينية", "en": "إنجليزية",
             "es": "إسبانية", "fr": "فرنسية", "th": "تايلاندية", "ta": "تاميلية", "te": "تيلجو", "ml": "مالايالام",
             "ru": "روسية", "de": "ألمانية", "it": "إيطالية", "pt": "برتغالية", "fa": "فارسية", "ur": "أردو"}
_PROV_RE = [(r"^netflix$", "Netflix"), (r"^shahid", "شاهد VIP"), (r"watch\s?it", "Watch IT"), (r"yango", "Yango Play"),
            (r"^disney( plus|\+)$", "Disney+"), (r"^amazon prime video$", "Prime Video"), (r"^(hbo )?max$", "Max"),
            (r"^apple tv( plus|\+)$", "Apple TV+"), (r"^paramount( plus|\+)$", "Paramount+"), (r"^crunchyroll$", "Crunchyroll"),
            (r"^osn", "OSN+"), (r"^starz", "STARZPLAY"), (r"^viki", "Viki"), (r"^tod$", "TOD"), (r"^hulu$", "Hulu")]
_PROV_FALLBACK = [(8, "Netflix"), (9, "Prime Video"), (337, "Disney+"), (350, "Apple TV+"), (1899, "Max"), (531, "Paramount+"), (283, "Crunchyroll")]
_FAV_REGION = "EG"
_PROV_CACHE = {"t": 0, "d": None}
_FAV_CACHE = {}
_FAV_SEEN = {}


def fav_providers():
    if _PROV_CACHE["d"] is not None and time.time() - _PROV_CACHE["t"] < 12 * 3600:
        return _PROV_CACHE["d"]
    found, byid = {}, {}
    try:
        for ty in ("movie", "tv"):
            for pr in tmdb_get(f"/watch/providers/{ty}", watch_region=_FAV_REGION, language="en-US").get("results", []):
                byid.setdefault(pr["provider_id"], pr)
        for pid, pr in sorted(byid.items()):
            nm = (pr.get("provider_name") or "").strip().lower()
            for rx, label in _PROV_RE:
                if label not in found and re.search(rx, nm):
                    found[label] = {"k": f"p:{pid}", "name": label, "logo": img(pr.get("logo_path"), "w92")}
                    break
    except Exception:
        found = {}
    if not found:
        found = {n: {"k": f"p:{i}", "name": n, "logo": None} for i, n in _PROV_FALLBACK}
    order = [l for _, l in _PROV_RE]
    plats = sorted(found.values(), key=lambda x: order.index(x["name"]) if x["name"] in order else 99)
    missing = [l for l in ("Watch IT", "Yango Play") if l not in found]
    d = {"platforms": plats, "missing": missing}
    _PROV_CACHE.update(t=time.time(), d=d)
    return d


@_add_get("/fav/list")
def x_fav_list_impl():
    pv = fav_providers()
    langs = [{"k": "l:" + c, "name": _LANG_FEM[c]} for c in ("ar", "ko", "hi", "tr", "ja", "zh", "en", "es", "fr", "th", "ta", "te", "ml", "ru", "de", "it", "pt", "fa", "ur")]
    genres = [{"k": "g:" + k, "name": v[0]} for k, v in _GENRE_KEYS.items()]
    return jsonify(platforms=pv["platforms"], missing=pv["missing"], langs=langs, genres=genres, special=[{"k": "anime", "name": "أنمي"}])


def fav_query(key, ty):
    if key.startswith("l:"):
        return {"with_original_language": key[2:]}
    if key.startswith("g:"):
        g = _GENRE_KEYS.get(key[2:])
        return {"with_genres": str(g[1] if ty == "movie" else g[2])} if g else None
    if key == "anime":
        return {"with_genres": "16", "with_original_language": "ja"}
    if key.startswith("p:"):
        return {"with_watch_providers": key[2:], "watch_region": _FAV_REGION, "with_watch_monetization_types": "flatrate"}
    return None


def fav_label(key):
    if key.startswith("l:"):
        return _LANG_FEM.get(key[2:], key[2:]), "lang"
    if key.startswith("g:"):
        return (_GENRE_KEYS.get(key[2:]) or (key,))[0], "genre"
    if key == "anime":
        return "أنمي", "anime"
    if key.startswith("p:"):
        for pr in fav_providers()["platforms"]:
            if pr["k"] == key:
                return pr["name"], "plat"
        return key, "plat"
    return key, ""


def _fav_fetch(key, ty):
    q = fav_query(key, ty)
    if q is None:
        return []
    res = tmdb_get("/discover/" + ty, language="ar", include_adult="false", sort_by="popularity.desc", page=1, **q).get("results", [])
    lgm = {x["id"]: x.get("original_language") or "" for x in res}
    items = _cards(res, ty, 20)
    for it in items:
        it["lg"] = lgm.get(it["id"], "")
    return items


@_add_get("/fav/rows")
def x_fav_rows_impl():
    key = request.args.get("k", "")
    if not key:
        return jsonify(rows=[])
    h = _FAV_CACHE.get(key)
    if h and time.time() - h[0] < 1800:
        return jsonify(rows=h[1])
    name, kind = fav_label(key)
    with ThreadPoolExecutor(max_workers=2) as ex:
        try:
            got = list(ex.map(lambda t: _fav_fetch(key, t), ("movie", "tv")))
        except Exception as e:
            return jsonify(rows=[], error=str(e))
    rows = []
    for ty, items in zip(("movie", "tv"), got):
        if not items:
            continue
        noun = "أفلام" if ty == "movie" else "مسلسلات"
        title = f"{noun} {name}" if kind in ("lang", "genre") else f"{name} · {noun}"
        rows.append({"id": f"fav:{key}:{ty}", "title": title, "items": items})
    _FAV_CACHE[key] = (time.time(), rows)
    return jsonify(rows=rows)


@_add_get("/trailer")
def x_trailer_impl():
    t, i = request.args.get("type", "movie"), request.args.get("id", "")
    if request.args.get("auto") == "1" and not cert_of(t, i)["ok"]:
        return jsonify(key="", blocked=True)      # التشغيل التلقائي: بس للأعمال المصنّفة عائلي/13+
    try:
        for lng in ("ar", "en-US"):
            res = tmdb_get(f"/{_norm_ty(t)}/{i}/videos", language=lng).get("results", [])
            for v in res:
                if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser"):
                    return jsonify(key=v["key"])
    except Exception:
        pass
    return jsonify(key="")
