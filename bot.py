import os
import io
import re
import json
import uuid
import hmac
import base64
import hashlib
import secrets
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
if not KV_ON:
    print("WARNING: Redis مش متوصّل — على Vercel الأعضاء هيتمسحوا مع كل تحديث. وصّل Upstash/Vercel KV فورًا.")
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
        # مفيش مكان نحفظ فيه سر عشوائي: نشتق سر ثابت (مش هو الباسورد نفسه) عشان كل النسخ تتفق
        return hashlib.sha256(("elkomy-secret-v2|" + APP_PASS).encode()).hexdigest()
    return v


SECRET = _load_secret()
# دخول الأدمن بيتقفل لو APP_PASS مش متحدد أو لسه "admin" (أي حد كان هيدخل بيه)
ADMIN_OK = bool(APP_PASS) and APP_PASS.lower() not in ("admin", "password", "123456", "12345678")
if not ADMIN_OK:
    print("SECURITY: دخول الأدمن متقفل لأن APP_PASS مش متحدد أو ضعيف. حدّده من Variables بباسورد قوي.")
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

def _fi_split(raw):
    """مفاتيح 1fichier كتير في خانة واحدة (فاصلة/مسافة/سطر). البادئة cdn: بتعني حساب رصيد ترافيك (CDN) مش بريميم."""
    out = []
    for k in re.split(r"[,;\s]+", str(raw or "").strip()):
        k = k.strip()
        if not k:
            continue
        cdn = k.lower().startswith("cdn:")
        if cdn:
            k = k[4:].strip()
        if k:
            out.append((k, cdn))
    return out


def _fi_logins(raw):
    """حسابات يوزر/باسورد: mail:password بينهم مسافة أو سطر. الباسورد ممكن يحتوي على : عادي (بنقسم عند أول : بس)."""
    out = []
    for t in re.split(r"\s+", str(raw or "").strip()):
        if ":" in t:
            m, pw = t.split(":", 1)
            if m.strip() and pw:
                out.append((m.strip(), pw))
    return out


def _fi_short(mail):
    m = str(mail or "")
    i = m.find("@")
    return (m[:2] + "•••" + m[i:]) if i > 2 else (m[:2] + "•••")


def _fi_env():
    """ONEFICHIER_KEY = حسابات بريميم (كتير بفاصلة/مسافة) ، ONEFICHIER_CDN_KEY = حسابات رصيد ترافيك CDN."""
    items = []
    for env, force_cdn in (("ONEFICHIER_KEY", False), ("ONEFICHIER_CDN_KEY", True)):
        for k, cdn in _fi_split(os.environ.get(env, "")):
            items.append((k, cdn or force_cdn))
    out = []
    for i, (k, cdn) in enumerate(items, 1):
        out.append({
            "id": "env_1fichier" if i == 1 else "env_1fichier_%d" % i,
            "type": "1fichier",
            "name": ("1Fichier CDN" if cdn else "1Fichier") + (" %d" % i if len(items) > 1 else ""),
            "key": k, "cdn": cdn, "domains": DEFAULT_DOMAINS.get("1fichier", []),
        })
    for j, (mail, pw) in enumerate(_fi_logins(os.environ.get("ONEFICHIER_LOGIN", "")), 1):
        out.append({
            "id": "env_1fichier_login_%d" % j, "type": "1fichier", "name": "1Fichier 👤 " + _fi_short(mail),
            "key": mail, "pw": pw, "login": True, "domains": DEFAULT_DOMAINS.get("1fichier", []),
        })
    return out


def _env_providers():
    out = list(_fi_env())
    for t, env in ENV_SEED.items():
        if t == "1fichier":
            continue
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


_FI_RR = {"p": 0, "c": 0, "l": 0}
_FI_BAD = {}


def _fi_sort(lst):
    """حسابات 1fichier: البريميم الأول (عشان نوفّر رصيد الـ CDN)، وبعدين حسابات الترافيك.
    جوه كل مجموعة بنبدّل بين الحسابات عشان الحمل يتوزّع، واللي فشل (مفتاح غلط / خلص رصيده) بيتأخر مؤقتاً."""
    now = time.time()
    good = [p for p in lst if _FI_BAD.get(p["id"], 0) <= now]
    bad = [p for p in lst if p not in good]

    def rot(g, k):
        if len(g) > 1:
            i = _FI_RR[k] % len(g)
            _FI_RR[k] += 1
            g = g[i:] + g[:i]
        return g

    return (rot([p for p in good if not p.get("cdn") and not p.get("login")], "p")
            + rot([p for p in good if p.get("cdn") and not p.get("login")], "c")
            + rot([p for p in good if p.get("login")], "l") + bad)


def _fi_mark(p, msg):
    """لو الرد معناه إن الحساب نفسه مش شغّال (مفتاح غلط / مش مشترك / رصيد خلص) نوقفه 30 دقيقة ونجرّب اللي بعده."""
    m = str(msg or "")
    if re.search(r"#887|location", m, re.I):
        return   # الحساب بيحمّل من مكان تاني دلوقتي: نجرّب حساب تاني بس من غير إيقافه
    if re.search(r"customer|premium|cdn|credit|traffic|quota|invalid|bad key|auth|forbidden|expired|subscri|تسجيل الدخول|الرصيد|login", m, re.I):
        _FI_BAD[p["id"]] = time.time() + 1800


def candidates(u):
    host = urlparse(u).netloc.lower().replace("www.", "")
    ps = [p for p in load() if p["type"] != "alldebrid" or AD_SERVER]

    def hit(p):
        return any(
            d and (host == d or host.endswith("." + d))
            for d in p.get("domains", [])
        )

    first = [p for p in ps if hit(p)]
    _it = iter(_fi_sort([p for p in first if p["type"] == "1fichier"]))
    first = [next(_it) if p["type"] == "1fichier" else p for p in first]
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

USERS_BAK = os.path.join(DATA_DIR, "users.bak.json")     # نسخة احتياطية تلقائية بتتحدّث مع كل حفظ


def _users_read(path):
    """(dict, ok): ok=False معناها القراءة فشلت (Redis واقع مثلاً) — ودي حاجة غير إن مفيش بيانات."""
    try:
        d = store_load(path)
        return (d if isinstance(d, dict) else {}), True
    except FileNotFoundError:
        return {}, True
    except Exception:
        return {}, False


def users_load():
    d, ok = _users_read(USERS_FILE)
    if d:
        return d
    b, _bok = _users_read(USERS_BAK)       # الملف الأساسي فاضي/مفقود/مش مقروء: نرجّع من النسخة الاحتياطية
    if b:
        if ok:
            try:
                store_save(USERS_FILE, b)  # رجّع الأعضاء تلقائيًا
            except Exception:
                pass
        return b
    return d


def users_save(d, allow_delete=False):
    """حفظ الأعضاء بأمان: عمره ما بيمسح عضو غير لو حذف صريح من الأدمن (allow_delete=True)،
    ويرفض الحفظ لو القراءة الحالية فشلت (عشان عطل مؤقت في Redis ما يمسحش الكل)."""
    cur, ok = _users_read(USERS_FILE)
    if not ok:
        raise RuntimeError("تعذّر قراءة بيانات الأعضاء، اتلغى الحفظ حمايةً للبيانات")
    if not cur:
        cur, _ = _users_read(USERS_BAK)
    if not allow_delete:
        for n, r in cur.items():
            if n not in d:
                d[n] = r                   # أي عضو موجود ومش في النسخة اللي بتتحفظ بيرجع تلقائيًا
    store_save(USERS_FILE, d)
    try:
        store_save(USERS_BAK, d)
    except Exception:
        pass


def fp_of(s):
    return hashlib.sha256(("fp:" + s).encode()).hexdigest()[:10]


def sign(body):
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def admin_fp():
    """بصمة الأدمن داخل التوكن: HMAC بالسر (مش SHA256 للباسورد) عشان محدّش يقدر يخمّن الباسورد من التوكن."""
    return hmac.new(SECRET.encode(), ("afp:" + APP_PASS).encode(), hashlib.sha256).hexdigest()[:16]


# ----------------- حد الأجهزة لكل عضو (شاشة / موبايل / متصفح) -----------------
SESS_ACTIVE = 180      # ثواني: الجهاز اللي اتحرك في آخر 3 دقايق يعتبر شغّال دلوقتي
MAX_STRIKES = 3
DTYPES = ("tv", "app", "web")
DEF_LIM = {"tv": 1, "app": 1, "web": 1}
TYPE_AR = {"tv": "شاشة", "app": "موبايل/تطبيق", "web": "متصفح"}
SUSP_MSG = "⛔ حسابك اتوقف لأنه اتفتح على أجهزة أكتر من المسموح 3 مرات. كلّم الأدمن عشان يشغّله."
SEEN = {}


def kick_msg(st):
    m = "اتقفلت هنا لأن الحساب اتفتح على جهاز تاني زيادة عن العدد المسموح لحسابك."
    if st:
        m += f"\n⚠️ تحذير {st} من {MAX_STRIKES}: لو اتكرر الحساب هيتوقف."
    return m


def lim_of(rec):
    l = rec.get("lim") if isinstance(rec.get("lim"), dict) else {}
    out = {}
    for t in DTYPES:
        try:
            out[t] = max(0, min(5, int(l.get(t, DEF_LIM[t]))))
        except (TypeError, ValueError):
            out[t] = DEF_LIM[t]
    return out


def sess_of(rec):
    """جلسات العضو {dv: {sid, ty, ls}} (مع ترحيل الشكل القديم sid/dv)"""
    ss = rec.get("ss")
    ss = dict(ss) if isinstance(ss, dict) else {}
    if rec.get("sid") and not any(s.get("sid") == rec["sid"] for s in ss.values()):
        ss[rec.get("dv") or "legacy"] = {"sid": rec["sid"], "ty": "web", "ls": int(rec.get("ls", 0) or 0)}
    return ss


LASTP = {}                  # {(user, sid): (path, time)} آخر حاجة العضو عملها (من غير الطلبات الدورية)
ADMIN_META = {}             # {user: {ip, ua, geo, t}} الأدمن مالوش جلسات فبنحفظ بياناته في الذاكرة
_POLL_PATHS = ("/me", "/usage", "/users", "/activity", "/ud/", "/settings", "/auth", "/home/rev")
_PATH_AR = (("/play", "تشغيل/مشاهدة"), ("/generate", "سحب ملفات"), ("/tb/", "TorBox"), ("/stremio", "Stremio"),
            ("/iptv", "IPTV"), ("/search", "بحث"), ("/tmdb", "تصفح"), ("/home", "الرئيسية"), ("/sub", "ترجمة"),
            ("/dl", "تحميل"), ("/download", "تحميل"))


def path_ar(p):
    for pre, lb in _PATH_AR:
        if p.startswith(pre):
            return lb
    return p[:40]


def ua_label(ua):
    """وصف مختصر للمتصفح/الجهاز من User-Agent"""
    ua = str(ua or "")
    if not ua:
        return ""
    low = ua.lower()
    if "okhttp" in low or "elkomy" in low or "torflix" in low or "; wv)" in low:
        br = "تطبيق أندرويد"
    elif "smart-tv" in low or "smarttv" in low or "tizen" in low or "webos" in low or "android tv" in low or "bravia" in low or " aft" in low:
        br = "تلفزيون"
    else:
        br = ""
        for key, nm in (("edg/", "Edge"), ("opr/", "Opera"), ("samsungbrowser", "Samsung Browser"), ("firefox/", "Firefox"),
                        ("chrome/", "Chrome"), ("safari/", "Safari")):
            if key in low:
                m = re.search(re.escape(key) + r"(\d+)", low)
                br = nm + (" " + m.group(1) if m else "")
                break
    osn = ""
    for key, nm in (("android", "Android"), ("iphone", "iPhone"), ("ipad", "iPad"), ("windows", "Windows"),
                    ("mac os", "macOS"), ("cros", "ChromeOS"), ("linux", "Linux")):
        if key in low:
            osn = nm
            m = re.search(r"android[ /]([\d.]+)", low) if nm == "Android" else None
            if m:
                osn += " " + m.group(1)
            break
    mm = re.search(r";\s*([^;()]+?)\s+build/", ua, re.I)
    model = mm.group(1).strip() if mm and osn.startswith("Android") else ""
    return " · ".join(x for x in (br, osn, model) if x) or ua[:60]


def req_meta():
    try:
        city = unquote(request.headers.get("X-Vercel-IP-City", "") or "")
        ctry = request.headers.get("X-Vercel-IP-Country", "") or ""
        ip = client_ip()
        return {"ip": ip, "ua": (request.headers.get("User-Agent", "") or "")[:220],
                "geo": ", ".join(x for x in (city, ctry) if x)}
    except Exception:
        return {"ip": "", "ua": "", "geo": ""}


def note_open(s, now, meta=None):
    """بيسجّل «فتحة» جديدة للجلسة لو كان فيه خمول أكتر من SESS_ACTIVE"""
    prev = float(s.get("ls", 0) or 0)
    if not s.get("fs"):
        s["fs"] = int(now)
    if not s.get("lo") or now - prev >= SESS_ACTIVE:
        s["lo"] = int(now)
        s["oc"] = int(s.get("oc", 0) or 0) + 1
    s["ls"] = int(now)
    if meta:
        for k in ("ip", "ua", "geo"):
            if meta.get(k):
                s[k] = meta[k]


def touch_seen(u, sid):
    if not sid:
        return
    now = time.time()
    if now - SEEN.get((u, sid), 0) < 60:
        return
    SEEN[(u, sid)] = now
    meta = req_meta()
    lp = LASTP.get((u, sid))
    with USERS_LOCK:
        us = users_load()
        r = us.get(u)
        if r is None:
            return
        ss = sess_of(r)
        for s in ss.values():
            if s.get("sid") == sid:
                note_open(s, now, meta)
                if lp:
                    s["lp"], s["lpt"] = lp[0], int(lp[1])
                r["ss"] = ss
                r.pop("sid", None)
                r.pop("dv", None)
                users_save(us)
                return


def devs_of(rec):
    """أجهزة العضو (جلساته) بتفاصيلها لعرضها للأدمن"""
    now = time.time()
    out = []
    for k, s in sess_of(rec).items():
        ls = float(s.get("ls", 0) or 0)
        out.append({"ty": s.get("ty", "web"), "ip": s.get("ip", ""), "ua": ua_label(s.get("ua", "")),
                    "geo": s.get("geo", ""), "ls": int(ls * 1000), "lo": int(float(s.get("lo", 0) or 0) * 1000),
                    "fs": int(float(s.get("fs", 0) or 0) * 1000), "lg": int(float(s.get("lg", 0) or 0) * 1000),
                    "oc": int(s.get("oc", 0) or 0), "lp": path_ar(s.get("lp", "")) if s.get("lp") else "",
                    "lpt": int(float(s.get("lpt", 0) or 0) * 1000), "on": now - ls < SESS_ACTIVE})
    out.sort(key=lambda x: x["ls"], reverse=True)
    return out


def make_token(user, role, fp, sid=""):
    body = f"{user}|{role}|{int(time.time()) + 30 * 86400}|{fp}" + (f"|{sid}" if sid else "")
    return base64.urlsafe_b64encode(body.encode()).decode() + "." + sign(body)


def read_token_ex(tok):
    """بيرجّع (who, why, rec, sid): why = '' تمام | bad | kicked (اتقفل لإن جهاز تاني دخل) | suspended (موقوف)"""
    try:
        b64, sig = str(tok).rsplit(".", 1)
        body = base64.urlsafe_b64decode(b64.encode()).decode()
        if not hmac.compare_digest(sig.encode(), sign(body).encode()):
            return None, "bad", None, ""
        parts = body.split("|")
        user, role, exp, fp = parts[:4]
        sid = parts[4] if len(parts) > 4 else ""
        if int(exp) < time.time():
            return None, "bad", None, ""
        if role == "admin":
            if not ADMIN_OK or user != APP_USER.lower() or not hmac.compare_digest(fp.encode(), admin_fp().encode()):
                return None, "bad", None, ""
            return (user, role), "", None, ""
        rec = users_load().get(user)
        if not rec or fp != fp_of(rec["h"]):
            return None, "bad", None, ""
        if rec.get("susp"):
            return None, "suspended", rec, sid
        ss = sess_of(rec)
        if ss and sid not in {s.get("sid") for s in ss.values()}:
            return None, "kicked", rec, sid
        return (user, role), "", rec, sid
    except Exception:
        return None, "bad", None, ""


def read_token(tok):
    return read_token_ex(tok)[0]


_ADMIN_PREFIXES = ("/users", "/iptv/admin", "/activity", "/ru")
_ADMIN_WRITE = ("/settings", "/imdbfix", "/providers", "/home/layout")
_ADMIN_ANY = ("/home/dub-refresh", "/providers", "/providers/export", "/play/diag", "/play/reports")
# مسارات ستريميو بتغيّر إعداد مشترك للموقع كله (حساب + إضافات + مفاتيح مزوّدين): للأدمن بس
_ADMIN_POST_EXACT = ("/stremio/login", "/stremio/sync", "/stremio/logout", "/stremio/addon")


def needs_admin(path, method):
    """المسارات اللي للأدمن بس (الباقي مفتوح لأي عضو مسجّل)"""
    if path.startswith(_ADMIN_PREFIXES) or path in _ADMIN_ANY:
        return True
    if method in ("POST", "PUT", "DELETE") and path.startswith(_ADMIN_WRITE):
        return True
    if method == "POST" and path in _ADMIN_POST_EXACT:
        return True
    if method in ("DELETE", "POST", "PUT") and path.startswith("/stremio/addons/"):
        return True
    return False


@app.before_request
def guard():
    open_paths = ("/", "/auth", "/ud/beacon")
    if request.method == "OPTIONS" or request.path in open_paths or request.path.startswith(("/subx/", "/auth/", "/px/", "/cron/")):
        return None
    who, why, _rec, _sid = read_token_ex(request.headers.get("X-Token", ""))
    if not who:
        if why == "kicked":
            st = int((_rec or {}).get("st", 0))
            return jsonify(error=kick_msg(st), kicked=True, strikes=st, max=MAX_STRIKES), 401
        if why == "suspended":
            return jsonify(error=SUSP_MSG, suspended=True), 401
        return jsonify(error="غير مصرّح"), 401
    g.user, g.role = who
    g.sid = _sid or hashlib.sha1(request.headers.get("X-Token", "").encode()).hexdigest()[:16]
    LAST[g.user] = time.time()
    try:
        if request.method != "OPTIONS" and not request.path.startswith(_POLL_PATHS):
            LASTP[(g.user, _sid)] = (request.path, time.time())
            if len(LASTP) > 5000:
                LASTP.clear()
        if g.role == "admin":
            ADMIN_META[g.user] = dict(req_meta(), t=time.time())
    except Exception:
        pass
    if g.role != "admin":
        try:
            touch_seen(g.user, _sid)
        except Exception:
            pass
    if g.role != "admin" and needs_admin(request.path, request.method):
        return jsonify(error="للأدمن بس"), 403
    if g.role != "admin" and request.method == "POST" and request.path in ("/play/start", "/play/hls", "/play/ext", "/generate", "/stremio/resolve", "/tb/stream", "/sub/auto", "/sub/ai"):
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
    return "TorFlix Server is Active! 🚀"


def too_many(ip, limit=10):
    if KV_ON:
        try:
            v = _kv_cmd("GET", KV_PREFIX + "fail:" + ip)
            return int(v or 0) >= limit
        except Exception:
            pass
    now = time.time()
    lst = [t for t in FAILS.get(ip, []) if now - t < 600]
    FAILS[ip] = lst
    return len(lst) >= limit


def fail_add(key, window=600):
    """بيسجّل محاولة فاشلة (Redis لو متاح عشان تتشارك بين كل نسخ السيرفر)."""
    if KV_ON:
        try:
            k = KV_PREFIX + "fail:" + key
            if int(_kv_cmd("INCR", k)) == 1:
                _kv_cmd("EXPIRE", k, int(window))
            return
        except Exception:
            pass
    FAILS.setdefault(key, []).append(time.time())


def _login_user(u, rec, d, ip):
    """بيفتح جلسة لعضو (مع حد الأجهزة والتحذيرات) — مشتركة بين الدخول العادي والتسجيل وكود الإيميل"""
    if rec.get("susp"):
        return jsonify(ok=False, suspended=True, error=SUSP_MSG), 423
    dv = str(d.get("dv", "")).strip()[:40] or ("ip:" + ip)
    ty = str(d.get("ty", "")).strip().lower()
    if ty not in DTYPES:
        ty = "web"
    now = time.time()
    warn = ""
    with USERS_LOCK:
        us = users_load()
        rec = us.get(u) or rec
        lim = lim_of(rec)
        if lim[ty] <= 0:
            return jsonify(ok=False, blocked=True, title="الجهاز مش مسموح",
                           error=f"حسابك مش مسموح له يدخل من {TYPE_AR[ty]}. كلّم الأدمن."), 423
        ss = {k: v for k, v in sess_of(rec).items() if now - float(v.get("ls", 0) or 0) < 30 * 86400}
        rec.pop("sid", None)
        rec.pop("dv", None)
        strikes = int(rec.get("st", 0))
        cur = ss.get(dv)
        meta = req_meta()
        if cur:                                              # نفس الجهاز: مفيش مشكلة
            note_open(cur, now, meta)
            cur["ty"], cur["lg"] = ty, int(now)
            sid = cur["sid"]
        else:
            same = sorted(((k, v) for k, v in ss.items() if v.get("ty") == ty), key=lambda kv: kv[1].get("ls", 0))
            over = len(same) - lim[ty] + 1
            for k, v in same:                                # الخاملين (مش شغّالين) بيتشالوا من غير تحذير
                if over <= 0:
                    break
                if now - float(v.get("ls", 0) or 0) >= SESS_ACTIVE:
                    ss.pop(k)
                    over -= 1
            if over > 0:                                     # لسه فيه أجهزة شغّالة فعلاً من نفس النوع = تجاوز
                for k, v in [(k, v) for k, v in same if k in ss][:over]:
                    ss.pop(k)
                strikes += 1
                rec["st"], rec["sa"] = strikes, int(now)
                if strikes >= MAX_STRIKES:
                    rec["susp"] = True
                    rec["ss"] = {}
                    us[u] = rec
                    users_save(us)
                    return jsonify(ok=False, suspended=True, title="الحساب موقوف", error=SUSP_MSG), 423
                warn = (f"⚠️ تحذير {strikes} من {MAX_STRIKES}: الحساب اتفتح على أكتر من {TYPE_AR[ty]} "
                        f"(المسموح {lim[ty]}) فاتقفل الأقدم. لو اتكرر الحساب هيتوقف.")
            sid = uuid.uuid4().hex[:12]
            ss[dv] = {"sid": sid, "ty": ty, "ls": int(now), "lg": int(now)}
            note_open(ss[dv], now, meta)
            ss[dv]["ls"] = int(now)
        rec["ss"] = ss
        us[u] = rec
        users_save(us)
    return jsonify(ok=True, role="user", user=u, warn=warn, strikes=strikes,
                   token=make_token(u, "user", fp_of(rec["h"]), sid))


_DUMMY_HASH = generate_password_hash(secrets.token_hex(12))


@app.post("/auth")
def auth():
    ip = client_ip()
    if too_many(ip):
        return jsonify(ok=False, error="محاولات كتير"), 429
    d = request.get_json(silent=True) or {}
    u = str(d.get("u", "")).strip().lower()
    p = str(d.get("p", "")).strip()
    if len(u) > 254 or len(p) > 200:
        return jsonify(ok=False), 400
    if too_many("ui:" + ip + "|" + u, 8) or too_many("u:" + u, 60):
        return jsonify(ok=False, error="محاولات كتير"), 429
    if ADMIN_OK and u == APP_USER.lower() and hmac.compare_digest(p.encode(), APP_PASS.encode()):
        return jsonify(ok=True, role="admin", user=u,
                       token=make_token(u, "admin", admin_fp()))
    if "@" in u:
        u = find_by_email(users_load(), u) or u
    rec = users_load().get(u)
    if rec:
        good = check_password_hash(rec["h"], p)
    else:
        check_password_hash(_DUMMY_HASH, p)      # نفس الزمن سواء الاسم موجود أو لأ (منع تخمين الأسماء)
        good = False
    if good:
        return _login_user(u, rec, d, ip)
    fail_add(ip)
    fail_add("u:" + u)
    fail_add("ui:" + ip + "|" + u)
    time.sleep(0.5)
    return jsonify(ok=False), 401


# ----------------- التسجيل الذاتي (يوزر/باسورد، إيميل/باسورد، تسجيل سريع بكود) -----------------
# إعداد الإيميل (واحد من الاتنين) على Vercel Environment Variables:
#   Resend:  RESEND_API_KEY  (+ MAIL_FROM مثلاً  TorFlix <no-reply@yourdomain.com>)
#   SMTP:    SMTP_HOST, SMTP_PORT (465 أو 587), SMTP_USER, SMTP_PASS (+ MAIL_FROM)
# REG_OPEN=0 بيقفل التسجيل نهائيًا من الإعدادات؛ وإلا الأدمن يفتحه ويقفله من جوه الموقع.
EMAIL_RX = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,24}$")
OTP_FILE = os.path.join(DATA_DIR, "otp.json")
OTP_LOCK = threading.Lock()
OTP_TTL = 600        # صلاحية الكود 10 دقايق
OTP_TRIES = 5        # محاولات غلط قبل ما الكود يتلغي
OTP_GAP = 60         # أقل فاصل بين كودين لنفس الإيميل
RL = {}


def rl_hit(key, limit, window):
    """True لو الحد اتعدّى. بيسجّل المحاولة لو لسه تحت الحد. (Redis لو متاح، وإلا الذاكرة)"""
    if KV_ON:
        try:
            k = KV_PREFIX + "rl:" + key
            n = int(_kv_cmd("INCR", k))
            if n == 1:
                _kv_cmd("EXPIRE", k, int(window))
            return n > limit
        except Exception:
            pass
    now = time.time()
    lst = [t for t in RL.get(key, []) if now - t < window]
    if len(lst) >= limit:
        RL[key] = lst
        return True
    lst.append(now)
    RL[key] = lst
    return False


def client_ip():
    """IP العميل الحقيقي. الهيدرات اللي العميل يقدر يكتبها ما بنثقش فيها (كان ممكن تزويرها لتخطّي حد المحاولات):
    على Vercel بنستخدم X-Vercel-Forwarded-For (Vercel هو اللي بيكتبه). على أي استضافة تانية بناخد الـ IP من
    آخر X-Forwarded-For اللي ضافه البروكسي بتاعك (TRUSTED_PROXIES = عدد البروكسيات قدامك، الافتراضي 1)."""
    h = request.headers
    if os.environ.get("VERCEL"):
        v = h.get("X-Vercel-Forwarded-For", "")
        if v.strip():
            return v.split(",")[0].strip()[:64]
    try:
        n = max(1, int(os.environ.get("TRUSTED_PROXIES", "1") or 1))
    except ValueError:
        n = 1
    xf = [x.strip() for x in h.get("X-Forwarded-For", "").split(",") if x.strip()]
    if xf:
        return (xf[-n] if len(xf) >= n else xf[0])[:64]
    return (request.remote_addr or "")[:64]


def mail_on():
    return bool(os.environ.get("RESEND_API_KEY", "").strip() or os.environ.get("SMTP_HOST", "").strip())


def reg_open():
    if os.environ.get("REG_OPEN", "1").strip() in ("0", "false", "no"):
        return False
    return bool(settings_load().get("reg", True))


def norm_email(x):
    e = str(x or "").strip().lower()
    return e if len(e) <= 254 and EMAIL_RX.fullmatch(e) else ""


def find_by_email(us, em):
    """اسم العضو اللي إيميله موثّق = em (الإيميل الغير موثّق ما يتحسبش)"""
    em = str(em or "").strip().lower()
    for n, r in us.items():
        if isinstance(r, dict) and r.get("ev") and r.get("em") == em:
            return n
    return ""


def send_mail(to, subject, text, html):
    frm = os.environ.get("MAIL_FROM", "").strip()
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if key:
        r = requests.post("https://api.resend.com/emails",
                          headers={"Authorization": "Bearer " + key},
                          json={"from": frm or "TorFlix <onboarding@resend.dev>", "to": [to],
                                "subject": subject, "text": text, "html": html}, timeout=15)
        if r.status_code >= 300:
            raise Exception("mail " + str(r.status_code))
        return
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("mail off")
    import smtplib
    import ssl
    from email.message import EmailMessage
    port = int(os.environ.get("SMTP_PORT", "465") or 465)
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, (frm or user), to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    srv = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) if port == 465 else smtplib.SMTP(host, port, timeout=15)
    try:
        if port != 465:
            srv.starttls(context=ctx)
        if user:
            srv.login(user, pw)
        srv.send_message(msg)
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def _mail_html(code):
    return ('<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;background:#0f172a;padding:32px">'
            '<div style="max-width:420px;margin:auto;background:#111c33;border-radius:16px;padding:28px;text-align:center;color:#e5ecff">'
            '<h2 style="margin:0 0 6px;color:#38bdf8">TorFlix</h2>'
            '<p style="color:#9fb0d3;margin:0 0 18px">كود التحقق بتاعك</p>'
            '<div style="font-size:38px;letter-spacing:10px;font-weight:bold;direction:ltr;background:#0b1224;border-radius:12px;padding:14px">'
            + code + '</div>'
            '<p style="color:#9fb0d3;font-size:13px;margin-top:18px">الكود صالح 10 دقايق. لو مش إنت اللي طلبته تجاهل الرسالة.</p>'
            '</div></div>')


def otp_load():
    try:
        d = store_load(OTP_FILE)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def otp_save(d):
    now = time.time()
    d = {k: v for k, v in d.items() if float(v.get("exp", 0)) > now}
    store_save(OTP_FILE, d)


def otp_key(em):
    return hashlib.sha256(("em:" + em).encode()).hexdigest()[:32]


def otp_hash(em, code):
    return hmac.new(SECRET.encode(), (em + "|" + code).encode(), hashlib.sha256).hexdigest()


def gen_username(us, em):
    base = re.sub(r"[^a-z0-9_.-]", "", em.split("@")[0].lower())[:14]
    if len(base) < 3:
        base = (base + "user")[:14]
    for _ in range(40):
        n = base + str(secrets.randbelow(9000) + 1000)
        if USER_RX.fullmatch(n) and n not in us and n != APP_USER.lower():
            return n
    return "u" + secrets.token_hex(7)


def new_member(h, em="", ev=False, rp=False):
    """العضو الجديد مجاني (تصفح بس) لحد ما الأدمن يفعّل له باقة مميزة"""
    now = int(time.time())
    r = {"h": h, "c": now, "plan": "free", "plan_at": now, "src": "self"}
    if em:
        r["em"], r["ev"] = em, bool(ev)
    if rp:
        r["rp"] = True
    return r


@app.get("/auth/config")
def auth_config():
    return jsonify(reg=reg_open(), mail=mail_on())


@app.post("/auth/register")
def auth_register():
    """تسجيل بيوزر وباسورد (والإيميل اختياري ومش بيتوثّق هنا)"""
    ip = client_ip()
    d = request.get_json(silent=True) or {}
    if str(d.get("hp", "")).strip():                       # honeypot للبوتات
        return jsonify(error="طلب مرفوض"), 400
    if not reg_open():
        return jsonify(error="التسجيل مقفول دلوقتي. كلّم الأدمن."), 403
    if rl_hit("reg:" + ip, 6, 3600):
        return jsonify(error="محاولات تسجيل كتير. جرّب بعد شوية."), 429
    n = str(d.get("username", "")).strip().lower()
    p = str(d.get("password", "")).strip()
    em = norm_email(d.get("email")) if str(d.get("email", "")).strip() else ""
    if str(d.get("email", "")).strip() and not em:
        return jsonify(error="الإيميل مش مكتوب صح"), 400
    if not USER_RX.fullmatch(n):
        return jsonify(error="الاسم 3-20 حرف إنجليزي أو أرقام أو _ . -"), 400
    if n == APP_USER.lower():
        return jsonify(error="الاسم ده محجوز"), 400
    if len(p) < 6 or len(p) > 200:
        return jsonify(error="الباسورد لازم 6 حروف على الأقل"), 400
    with USERS_LOCK:
        us = users_load()
        if n in us:
            return jsonify(error="الاسم ده متاخد، اختار اسم تاني"), 400
        us[n] = new_member(generate_password_hash(p), em, False)
        users_save(us)
        rec = us[n]
    return _login_user(n, rec, d, ip)


@app.post("/auth/email/start")
def auth_email_start():
    """بيبعت كود من 6 أرقام على الإيميل (للتسجيل السريع، تسجيل إيميل+باسورد، دخول بالكود، أو استرجاع الباسورد)"""
    ip = client_ip()
    d = request.get_json(silent=True) or {}
    if str(d.get("hp", "")).strip():
        return jsonify(error="طلب مرفوض"), 400
    if not mail_on():
        return jsonify(error="التسجيل بالإيميل مش متاح دلوقتي."), 503
    em = norm_email(d.get("email"))
    if not em:
        return jsonify(error="الإيميل مش مكتوب صح"), 400
    if rl_hit("otp-ip:" + ip, 10, 3600) or rl_hit("otp-em:" + em, 6, 3600):
        return jsonify(error="طلبات كتير. جرّب بعد شوية."), 429
    exists = bool(find_by_email(users_load(), em))
    if not exists and not reg_open():
        return jsonify(error="التسجيل مقفول دلوقتي. كلّم الأدمن."), 403
    code = "%06d" % secrets.randbelow(10 ** 6)
    k = otp_key(em)
    with OTP_LOCK:
        o = otp_load()
        cur = o.get(k)
        if cur and time.time() - float(cur.get("last", 0)) < OTP_GAP:
            wait = int(OTP_GAP - (time.time() - float(cur.get("last", 0)))) + 1
            return jsonify(error="استنى %d ثانية قبل ما تطلب كود جديد." % wait, wait=wait), 429
        o[k] = {"h": otp_hash(em, code), "exp": int(time.time()) + OTP_TTL, "tries": 0, "last": int(time.time())}
        otp_save(o)
    try:
        send_mail(em, "كود التحقق: " + code + " — TorFlix",
                  "كود التحقق بتاعك: " + code + "\nصالح 10 دقايق. لو مش إنت اللي طلبته تجاهل الرسالة.",
                  _mail_html(code))
    except Exception:
        with OTP_LOCK:
            o = otp_load()
            o.pop(k, None)
            otp_save(o)
        return jsonify(error="مقدرتش أبعت الإيميل. اتأكد من العنوان أو جرّب بعد شوية."), 502
    return jsonify(ok=True, ttl=OTP_TTL, gap=OTP_GAP)


@app.post("/auth/email/verify")
def auth_email_verify():
    """بيتحقق من الكود: لو الإيميل ليه حساب بيدخّله (أو يغيّر الباسورد لو reset)، ولو لأ بيعمله حساب جديد"""
    ip = client_ip()
    d = request.get_json(silent=True) or {}
    em = norm_email(d.get("email"))
    code = re.sub(r"\D", "", str(d.get("code", "")))[:6]
    if not em or len(code) != 6:
        return jsonify(error="اكتب الإيميل وكود من 6 أرقام"), 400
    if rl_hit("otpv:" + ip, 30, 3600) or rl_hit("otpv-em:" + em, 15, 3600):
        return jsonify(error="محاولات كتير. جرّب بعد شوية."), 429
    k = otp_key(em)
    with OTP_LOCK:
        o = otp_load()
        cur = o.get(k)
        if not cur or float(cur.get("exp", 0)) < time.time():
            return jsonify(error="الكود انتهى. اطلب كود جديد."), 400
        if int(cur.get("tries", 0)) >= OTP_TRIES:
            o.pop(k, None)
            otp_save(o)
            return jsonify(error="محاولات غلط كتير. اطلب كود جديد."), 429
        if not hmac.compare_digest(str(cur.get("h", "")), otp_hash(em, code)):
            cur["tries"] = int(cur.get("tries", 0)) + 1
            o[k] = cur
            otp_save(o)
            return jsonify(error="الكود غلط"), 400
        o.pop(k, None)
        otp_save(o)
    pw = str(d.get("password", "")).strip()
    with USERS_LOCK:
        us = users_load()
        un = find_by_email(us, em)
        if un:
            rec = us[un]
            if d.get("reset"):
                if len(pw) < 6 or len(pw) > 200:
                    return jsonify(error="الباسورد لازم 6 حروف على الأقل"), 400
                rec["h"] = generate_password_hash(pw)
                rec.pop("rp", None)
                us[un] = rec
                users_save(us)
        else:
            if not reg_open():
                return jsonify(error="التسجيل مقفول دلوقتي. كلّم الأدمن."), 403
            n = str(d.get("username", "")).strip().lower()
            if n:
                if not USER_RX.fullmatch(n) or n == APP_USER.lower():
                    return jsonify(error="الاسم 3-20 حرف إنجليزي أو أرقام أو _ . -"), 400
                if n in us:
                    return jsonify(error="الاسم ده متاخد، اختار اسم تاني"), 400
            else:
                n = gen_username(us, em)
            rp = False
            if pw:
                if len(pw) < 6 or len(pw) > 200:
                    return jsonify(error="الباسورد لازم 6 حروف على الأقل"), 400
            else:
                pw, rp = secrets.token_urlsafe(14), True    # تسجيل سريع: باسورد عشوائي، الدخول بعد كده بكود أو يحدد باسورد من حسابه
            us[n] = new_member(generate_password_hash(pw), em, True, rp)
            users_save(us)
            un, rec = n, us[n]
    return _login_user(un, rec, d, ip)


@app.post("/me/password")
def me_password():
    """العضو يغيّر الباسورد (أو يحدد باسورد لو كان داخل بتسجيل سريع)"""
    if g.role == "admin":
        return jsonify(error="غيّر باسورد الأدمن من متغيرات السيرفر"), 400
    d = request.get_json(silent=True) or {}
    old, new = str(d.get("old", "")).strip(), str(d.get("new", "")).strip()
    if len(new) < 6 or len(new) > 200:
        return jsonify(error="الباسورد الجديد لازم 6 حروف على الأقل"), 400
    _w, _y, _r, sid = read_token_ex(request.headers.get("X-Token", ""))
    with USERS_LOCK:
        us = users_load()
        rec = us.get(g.user)
        if not rec:
            return jsonify(error="المستخدم مش موجود"), 404
        if not rec.get("rp") and not check_password_hash(rec["h"], old):
            return jsonify(error="الباسورد الحالي غلط"), 400
        rec["h"] = generate_password_hash(new)
        rec.pop("rp", None)
        us[g.user] = rec
        users_save(us)
    return jsonify(ok=True, token=make_token(g.user, "user", fp_of(rec["h"]), sid))


# ----------------- اختيار مزوّد المشاهدة والتحميل -----------------
# الإعداد العام (للأدمن وللمستخدمين اللي معندهمش اختيار خاص):
#   watch: "auto" (الأولوية العادية) أو id/نوع المزوّد (torbox, realdebrid, debridlink, alldebrid أو id مزوّد)
#   dl:    "same" (التحميل من نفس مزوّد المشاهدة) أو id/نوع مزوّد تاني للتحميل بس
#   strict: لو true مفيش رجوع لمزوّد تاني لو المختار فشل
# وكل مستخدم ممكن يكون ليه wp (مشاهدة) و dp (تحميل) خاصين في users.json.
SET_DEF = {"watch": "alldebrid", "dl": "same", "strict": False, "reg": True}


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
    out["reg"] = bool(d.get("reg", True))
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


_PROV_WORDS = re.compile(r"(?i)(torbox|all-?debrid|real-?debrid|debrid-?link|premiumize|stremio|aradeb|1fichier|debrid)")
_MASK_KEYS = ("error", "status", "detail", "message", "msg", "note", "reason", "text")


def _hide_accounts(o, names):
    """الأعضاء ما يشوفوش أسماء المزوّدين ولا الحسابات: نشيل provider/stream_note ونبدّل الأسماء في رسائل الخطأ."""
    if isinstance(o, dict):
        for k in list(o.keys()):
            v = o[k]
            if k in ("provider", "stream_note", "stream_notes", "diag"):
                o[k] = "" if isinstance(v, str) else v
            elif k == "tried":
                o[k] = []                                   # قايمة محاولات المزوّدين: للأدمن بس
            elif k in _MASK_KEYS and isinstance(v, str):
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
        if request.path.startswith(("/me", "/users", "/iptv", "/auth", "/settings", "/providers", "/activity", "/ad", "/stremio", "/push")):
            resp.headers["Cache-Control"] = "no-store"
    except Exception:
        pass
    return resp


# ===================== تعتيم روابط المشاهدة/التحميل للأعضاء =====================
# الأدمن بيشوف الروابط الأصلية. العضو بياخد رابط على سيرفرك (/px/...) والسيرفر هو اللي بيجيب الملف.
# HIDE_HOSTS=0 في Variables بيوقّف التعتيم (لو السيرفر ما بيستحملش بث الفيديو).
HIDE_HOSTS = os.environ.get("HIDE_HOSTS", "1").strip() not in ("0", "false", "no")
PX_TTL = 8 * 3600
_PX_KEYS = ("direct_link", "url", "hls", "link", "stream", "direct")
_PX_PATHS = ("/generate", "/tb/stream", "/stremio/resolve", "/play/")
_PX_FWD = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag")


def _px_ks(nonce, n):
    out, i = b"", 0
    while len(out) < n:
        out += hmac.new(SECRET.encode(), b"pxk|" + nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return out[:n]


def _px_enc(b):
    """تشفير الرابط الأصلي جوه التوكن (عشان العضو ما يفكّش base64 ويشوف دومين المزوّد)."""
    nonce = os.urandom(9)
    return nonce + bytes(x ^ y for x, y in zip(b, _px_ks(nonce, len(b))))


def _px_dec(b):
    nonce, ct = b[:9], b[9:]
    return bytes(x ^ y for x, y in zip(ct, _px_ks(nonce, len(ct))))


def px_make(u, base=None):
    p = {"u": u, "exp": int(time.time()) + PX_TTL}
    tok = _b64e(_px_enc(json.dumps(p, separators=(",", ":")).encode()))
    path = unquote(urlparse(u).path)
    fn = re.sub(r"[^\w.\-]+", "_", path.rsplit("/", 1)[-1])[:60]
    try:                                    # اسم الملف الظاهر في الرابط ما يحملش أي سر حتى لو المزوّد حطه في الـ path
        if any(k in fn or k in path for k in _secret_keys_cached()):
            fn = "video.mp4"
    except Exception:
        pass
    if ".m3u8" in path.lower():
        fn = "stream.m3u8"
    elif not re.search(r"\.[A-Za-z0-9]{2,5}$", fn):
        fn = "video.mp4"
    b = (base or _public_base()).rstrip("/")
    return f"{b}/px/{tok}.{sign('px|' + tok)[:40]}/{fn}"


def _px_rewrite(o):
    if isinstance(o, dict):
        if "probe_failed" in o:          # AllDebrid بيرفض IP السيرفر: الجهاز لازم يفتحه بنفسه
            return
        mine = {request.host.lower(), urlparse(_public_base()).netloc.lower()}
        for k, v in list(o.items()):
            if k in _PX_KEYS and isinstance(v, str) and re.match(r"https?://", v, re.I):
                h = urlparse(v).netloc.lower()
                if h and h not in mine and "/px/" not in v:
                    o[k] = px_make(v)
            else:
                _px_rewrite(v)
    elif isinstance(o, list):
        for x in o:
            _px_rewrite(x)


def _px_m3u8(txt, base):
    out = []
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            out.append(ln)
        elif s.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"', lambda m: 'URI="%s"' % px_make(urljoin(base, m.group(1))), ln))
        else:
            out.append(px_make(urljoin(base, s)))
    return "\n".join(out) + "\n"


@app.route("/px/<tk>/<path:fname>", methods=["GET", "HEAD"])
def px_get(tk, fname):
    try:
        tok, sig = tk.rsplit(".", 1)
        if not hmac.compare_digest(sig, sign("px|" + tok)[:40]):
            raise ValueError
        p = json.loads(_px_dec(_b64d(tok)))
        if int(p["exp"]) < time.time():
            raise ValueError
        cur = str(p["u"])
    except Exception:
        return Response("forbidden", status=403, mimetype="text/plain")
    hdr = {"User-Agent": "Mozilla/5.0"}
    if request.headers.get("Range"):
        hdr["Range"] = request.headers["Range"][:100]
    r = None
    try:
        for _ in range(6):
            if not _public_url(cur):
                return Response("forbidden", status=403, mimetype="text/plain")
            with _guard_net():
                r = requests.get(cur, headers=hdr, stream=True, allow_redirects=False, timeout=(10, 60))
            loc = r.headers.get("Location")
            if r.status_code in (301, 302, 303, 307, 308) and loc:
                cur = urljoin(cur, loc)
                r.close()
                continue
            break
    except Exception:
        return Response("unavailable", status=502, mimetype="text/plain")
    if r is None or r.status_code >= 400:
        code = r.status_code if r is not None and r.status_code in (404, 416) else 502
        if r is not None:
            r.close()
        return Response("unavailable", status=code, mimetype="text/plain")
    ct = (r.headers.get("Content-Type") or "").lower()
    if "mpegurl" in ct or urlparse(cur).path.lower().endswith(".m3u8"):
        try:
            raw = b""
            for ch in r.iter_content(65536):
                raw += ch
                if len(raw) > 3_000_000:
                    break
        finally:
            r.close()
        txt = _px_m3u8(raw.decode("utf-8", "ignore"), cur)
        return Response(txt, content_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    heads = {k: r.headers[k] for k in _PX_FWD if r.headers.get(k)}
    heads["Cache-Control"] = "no-store"
    if request.method == "HEAD":
        r.close()
        return Response(status=r.status_code, headers=heads)

    def gen():
        try:
            for chunk in r.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            r.close()
    return Response(gen(), status=r.status_code, headers=heads, direct_passthrough=True)


_SK_CACHE = {"t": 0.0, "v": frozenset()}


def _secret_keys_cached():
    """كل الأسرار (بتتجدد كل 20 ثانية). لو التحميل فشل بنكمّل بآخر قايمة معروفة بدل ما نفتح الباب."""
    now = time.time()
    if now - _SK_CACHE["t"] > 20:
        try:
            _SK_CACHE["v"] = frozenset(_all_secret_keys())
            _SK_CACHE["t"] = now
        except Exception:
            pass
    return _SK_CACHE["v"]


def _json_leaks(o, keys, urlkeys):
    """True لو أي نص جوه الـ JSON فيه سر: الروابط بتتفحص بكل الأسرار، وباقي النصوص بالأسرار الطويلة (16+)."""
    if isinstance(o, str):
        if len(o) < 8:
            return False
        dec = unquote(o)
        ks = keys if ("://" in o or "%" in o) else urlkeys
        return any(k in o or k in dec for k in ks)
    if isinstance(o, dict):
        return any(_json_leaks(k, keys, urlkeys) or _json_leaks(v, keys, urlkeys) for k, v in o.items())
    if isinstance(o, (list, tuple)):
        return any(_json_leaks(x, keys, urlkeys) for x in o)
    return False


@app.after_request
def leak_guard(resp):
    """آخر خط دفاع: أي رد JSON رايح لعضو وفيه مفتاح مزوّد/باسورد/authKey بيتقفل 403 بدل ما يوصل.
    مستقل عن HIDE_HOSTS وعن mask_for_members (اللي أي خطأ جواه كان بيعدّي الرد زي ما هو)."""
    try:
        if getattr(g, "role", None) == "admin" or request.method == "OPTIONS":
            return resp
        if resp.mimetype != "application/json" or resp.direct_passthrough:
            return resp
        keys = _secret_keys_cached()
        if not keys:
            return resp
        urlkeys = {k for k in keys if len(k) >= 16}
        if _json_leaks(json.loads(resp.get_data(as_text=True)), keys, urlkeys):
            try:
                print("[leak_guard] blocked response with secret on", request.path, flush=True)
            except Exception:
                pass
            return jsonify(error="المصدر ده مش متاح دلوقتي، جرّب نسخة تانية"), 403
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
        if HIDE_HOSTS and request.path.startswith(_PX_PATHS):
            _px_rewrite(o)
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
    if "reg" in d:
        cur["reg"] = bool(d["reg"])
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
        out["ad"] = True
    else:
        pl = plan_of(users_load().get(g.user) or {})
        out["ad"] = bool((users_load().get(g.user) or {}).get("ad"))
        out["plan"] = {"state": pl["state"], "until": pl["until"], "left": pl["left"]}
    if g.role != "admin":
        lk, msg = lock_of(g.user)
        out["lock"] = lk
        out["lock_msg"] = msg
        out["watch_prov"] = prov_pref(g.user, "watch")[0] or "auto"
        out["dl_prov"] = prov_pref(g.user, "download")[0] or "auto"
    return jsonify(out)


def _ad_provider():
    return next((p for p in load() if p["type"] == "alldebrid" and p.get("key")), None)


@app.get("/ad/key")
def ad_key():
    """مفتاح AllDebrid للجهاز: الأدمن دايمًا، والعضو بس لو الأدمن فعّله له (بيشتغل من موبايله مش من السيرفر)"""
    if g.role != "admin" and not (users_load().get(g.user) or {}).get("ad"):
        return jsonify(error="مش مسموح"), 403
    p = _ad_provider()
    if not p:
        return jsonify(error="مفيش مزوّد AllDebrid مضاف"), 404
    return jsonify(key=p["key"])


@app.get("/users")
def users_list():
    us = users_load()
    items = []
    for n, v in sorted(us.items()):
        pl = plan_of(v)
        items.append({"name": n, "c": v.get("c", 0), "lock": lock_of(n)[0],
                      "msg": v.get("nodl_msg", ""),
                      "wp": v.get("wp", ""), "dp": v.get("dp", ""), "ad": bool(v.get("ad")),
                      "plan": pl["state"], "until": pl["until"], "left": pl["left"],
                      "legacy": pl["legacy"], "st": int(v.get("st", 0)), "susp": bool(v.get("susp")), "lim": lim_of(v),
                      "on": {t: sum(1 for s in sess_of(v).values() if s.get("ty") == t and time.time() - float(s.get("ls", 0) or 0) < SESS_ACTIVE) for t in DTYPES}})
    return jsonify(items=items)


@app.post("/users/<name>/unlock")
def users_unlock(name):
    """الأدمن بيشغّل الحساب الموقوف ويصفّر التحذيرات"""
    name = name.strip().lower()
    with USERS_LOCK:
        us = users_load()
        r = us.get(name)
        if r is None:
            return jsonify(error="المستخدم مش موجود"), 404
        r.pop("susp", None)
        r.pop("sid", None)
        r.pop("dv", None)
        r.pop("ss", None)
        r["st"] = 0
        users_save(us)
    return jsonify(ok=True)


@app.post("/users/<name>/limits")
def users_limits(name):
    """عدد الأجهزة المسموحة لكل نوع: {tv, app, web} من 0 لـ 5 (0 = النوع ده ممنوع)"""
    d = request.get_json(silent=True) or {}
    name = name.strip().lower()
    with USERS_LOCK:
        us = users_load()
        r = us.get(name)
        if r is None:
            return jsonify(error="المستخدم مش موجود"), 404
        lim = {}
        for t in DTYPES:
            try:
                lim[t] = max(0, min(5, int(d.get(t, DEF_LIM[t]))))
            except (TypeError, ValueError):
                lim[t] = DEF_LIM[t]
        r["lim"] = lim
        users_save(us)
    return jsonify(ok=True, lim=lim)


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
        if "ad" in d:
            if d.get("ad"):
                us[name]["ad"] = 1
            else:
                us[name].pop("ad", None)
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
    resp.headers["Content-Disposition"] = "attachment; filename=torflix-members-backup.json"
    return resp


@app.get("/users/backup")
def users_backup():
    """نسخة كاملة للأدمن (بهاشات الباسورد) عشان لو حصل أي حاجة ترجّع كل الأعضاء من غير ما حد يسجّل تاني"""
    if g.role != "admin":
        return jsonify(error="للأدمن بس"), 403
    data = {"users": users_load(), "at": int(time.time())}
    resp = make_response(json.dumps(data, ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=torflix-users-FULL-backup.json"
    return resp


@app.post("/users/restore")
def users_restore():
    """بيرجّع الأعضاء الناقصين من ملف backup (من غير ما يمسح أو يغيّر الموجودين)"""
    if g.role != "admin":
        return jsonify(error="للأدمن بس"), 403
    inc = (request.get_json(silent=True) or {}).get("users")
    if not isinstance(inc, dict):
        return jsonify(error="ملف النسخة الاحتياطية مش صالح"), 400
    added = 0
    with USERS_LOCK:
        us = users_load()
        for n, r in inc.items():
            if isinstance(r, dict) and r.get("h") and USER_RX.fullmatch(str(n)) and n not in us:
                us[n] = r
                added += 1
        users_save(us)
    return jsonify(ok=True, added=added, total=len(us))


@app.delete("/users/<name>")
def users_del(name):
    with USERS_LOCK:
        us = users_load()
        if name not in us:
            return jsonify(error="المستخدم مش موجود"), 404
        us.pop(name)
        users_save(us, allow_delete=True)
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


# ---- أكتر من حساب IPTV: السيرفر بيوزّع الأجهزة على الحسابات (كل جهاز بيثبت على حساب) ----
IPTV_ASSIGN = {}          # {devkey: (acckey, last_seen)}
IPTV_ASSIGN_TTL = 3 * 3600
IPTV_MAX_EXTRA = 8


def _acc_key(a):
    return f"{a.get('host', '')}|{a.get('user', '')}"


def iptv_accs(rec):
    """الحساب الأساسي + الحسابات الإضافية (كلهم dicts فيهم host/user/pass/stat)."""
    if not rec or not rec.get("host"):
        return []
    out = [rec]
    for x in (rec.get("extra") or []):
        if isinstance(x, dict) and x.get("host") and x.get("user") and x.get("pass"):
            out.append(x)
    return out


def iptv_owner(al, user):
    """صاحب الحسابات: العضو نفسه لو عنده حساب، وإلا الحساب المشترك."""
    rec = al.get(user) or {}
    if rec.get("host"):
        return user, rec
    rec = al.get(IPTV_ALL) or {}
    return (IPTV_ALL, rec) if rec.get("host") else (None, None)


def iptv_pick(accs, devkey, rotate=False):
    """بيختار الحساب الأقل ضغطاً للجهاز ده ويثبّته عليه. بيرجّع الـdict بتاع الحساب."""
    now = time.time()
    for k in [k for k, v in IPTV_ASSIGN.items() if now - v[1] > IPTV_ASSIGN_TTL]:
        IPTV_ASSIGN.pop(k, None)
    cur = (IPTV_ASSIGN.get(devkey) or ("", 0))[0]
    byk = {_acc_key(a): a for a in accs}
    if len(accs) == 1:
        IPTV_ASSIGN[devkey] = (_acc_key(accs[0]), now)
        return accs[0]

    def expired(a):
        e = (a.get("stat") or {}).get("exp") or 0
        return bool(e) and e < now

    if cur in byk and not rotate and not expired(byk[cur]):
        IPTV_ASSIGN[devkey] = (cur, now)
        return byk[cur]
    load = {}
    for dk, (ak, _t) in IPTV_ASSIGN.items():
        if dk != devkey:
            load[ak] = load.get(ak, 0) + 1
    pool = [a for a in accs if not expired(a)] or list(accs)
    if rotate and len(pool) > 1:
        pool = [a for a in pool if _acc_key(a) != cur] or pool

    def score(a):
        mx = (a.get("stat") or {}).get("maxc") or 0
        n = load.get(_acc_key(a), 0)
        return (n / mx) if mx else n
    best = min(pool, key=lambda a: (score(a), accs.index(a)))
    IPTV_ASSIGN[devkey] = (_acc_key(best), now)
    return best


def _dev_key():
    return f"{g.user}:{getattr(g, 'sid', '')}"


def iptv_cm(rec, acc):
    """خريطة القنوات: {رقم القناة عند الحساب الأساسي: رقمها عند الحساب ده}"""
    pk, ak = _acc_key(rec), _acc_key(acc)
    cm = {}
    for m in (rec.get("cmap") or []):
        ids = m.get("ids") if isinstance(m, dict) else None
        if isinstance(ids, dict) and ids.get(pk) and ids.get(ak):
            cm[str(ids[pk])] = str(ids[ak])
    return cm


@app.get("/iptv/cfg")
def iptv_cfg():
    """host/user/pass = الحساب الأساسي (القوائم والأفلام والمسلسلات).
    sa = حساب البث المخصّص للجهاز ده للقنوات المباشرة (لو غير الأساسي) ومعاه cm (ربط القنوات)."""
    if member_locked():
        return jsonify(set=False, locked=True, error="الـIPTV للأعضاء المميزين بس")
    al = iptv_load()
    owner, rec = iptv_owner(al, g.user)
    accs = iptv_accs(rec)
    if not accs:
        return jsonify({"set": False})
    o = iptv_pub(rec)
    if owner == IPTV_ALL:
        o["shared"] = True
    if len(accs) > 1:
        o["n"] = len(accs)
        acc = iptv_pick(accs, _dev_key(), rotate=request.args.get("rotate") == "1")
        if acc is not rec:
            cm = iptv_cm(rec, acc)
            if cm:
                o["sa"] = {"host": acc["host"], "user": acc["user"], "pass": acc["pass"], "cm": cm}
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
        owner, rec = iptv_owner(al, g.user)
        accs = iptv_accs(rec)
        if not accs:
            return jsonify(error="مفيش اشتراك IPTV لحسابك"), 404
        acc = rec
        if g.role != "admin":
            old = acc.get("stat") or {}
            if owner == IPTV_ALL and time.time() - float(old.get("t", 0) or 0) < 300:
                return jsonify(ok=True)                      # الحساب المشترك: تحديث واحد كل 5 دقايق بس
            if num("maxc") > 50 or num("active") > 100 or num("exp") > time.time() + 10 * 365 * 86400:
                return jsonify(error="قيم غير منطقية"), 400
        acc["stat"] = {"exp": num("exp"), "maxc": num("maxc"), "active": num("active"),
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
    gv = [str(x) for x in (gp.get("vod") or [])]
    gs = [str(x) for x in (gp.get("ser") or [])]
    merged["vod"] = gv + [x for x in (own.get("vod") or []) if x not in gv]
    merged["ser"] = gs + [x for x in (own.get("ser") or []) if x not in gs]
    merged["cats"] = gc + [c for c in (own.get("cats") or [])
                           if not any(c.get("k") == x.get("k") and c.get("id") == x.get("id") for x in gc)]
    return jsonify(pins=merged, g={"live": gl, "cats": gc, "vod": gv, "ser": gs, "ln": gp.get("ln") or {}})


@app.post("/iptv/pins")
def iptv_pins_set():
    """تثبيتات كل بروفايل: {live:[stream_id...], cats:[{k:'live|vod|series', id:'..', n:'اسم'}]}
    ولو global=true (أدمن بس) بتتحفظ للكل وبتظهر في رئيسية كل الأعضاء."""
    d = request.get_json(silent=True) or {}
    p = d.get("pins") if isinstance(d.get("pins"), dict) else {}
    live = [str(x)[:20] for x in (p.get("live") or [])][:200]
    vod_p = [str(x)[:20] for x in (p.get("vod") or [])][:200]
    ser_p = [str(x)[:20] for x in (p.get("ser") or [])][:200]
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
            al[IPTV_PINS] = {"live": live, "cats": cats, "vod": vod_p, "ser": ser_p, "ln": ln}
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
        vod_p = [x for x in vod_p if x not in {str(i) for i in (gp.get("vod") or [])}]
        ser_p = [x for x in ser_p if x not in {str(i) for i in (gp.get("ser") or [])}]
        live = [x for x in live if x not in gl]                      # تثبيتات الكل مابتتنسخش على حساب العضو
        cats = [c for c in cats if (c["k"], c["id"]) not in gc]
        rec.setdefault("pins", {})[pid] = {"live": live, "cats": cats, "vod": vod_p, "ser": ser_p, "order": order, "hidden": hidden, "names": names}
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
            k = b.get("k") if b.get("k") in ("vod", "series", "lib") else ""
            out["banners"].append({"id": _fs(b.get("id"), 20) or uuid.uuid4().hex[:8], "k": k,
                                   "sid": _fs(b.get("sid"), 40) if k else "", "title": _fs(b.get("title"), 80),
                                   "sub": _fs(b.get("sub"), 160), "img": _furl(b.get("img")),
                                   "tag": _fs(b.get("tag"), 30), "nm": _fs(b.get("nm"), 80), "until": _fdate(b.get("until"))})
    out["sec"] = {"home": [], "movie": [], "tv": []}      # مثبّت في الرئيسية / الأفلام / المسلسلات
    sc = d.get("sec")
    if isinstance(sc, dict):
        for key in out["sec"]:
            for x in (sc.get(key) or [])[:24]:
                if isinstance(x, dict) and x.get("k") in ("vod", "series", "lib") and _fs(x.get("sid"), 40):
                    out["sec"][key].append({"k": x["k"], "sid": _fs(x.get("sid"), 40), "nm": _fs(x.get("nm"), 80),
                                            "img": _furl(x.get("img"))})
    p = d.get("popup")
    if isinstance(p, dict):
        k = p.get("k") if p.get("k") in ("vod", "series", "lib") else ""
        out["popup"] = {"on": bool(p.get("on")), "id": _fs(p.get("id"), 20), "title": _fs(p.get("title"), 80),
                        "text": _fs(p.get("text"), 300), "img": _furl(p.get("img")), "k": k,
                        "sid": _fs(p.get("sid"), 40) if k else "", "nm": _fs(p.get("nm"), 80), "until": _fdate(p.get("until"))}
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



def _prune_cmap(cm, keys):
    """بيشيل من ربط القنوات أي حساب اتمسح، وأي ربط فضل فيه أقل من حسابين."""
    out = []
    for m in (cm or [])[:1500]:
        if not isinstance(m, dict) or not isinstance(m.get("ids"), dict):
            continue
        ids = {k: str(v)[:20] for k, v in m["ids"].items() if k in keys and str(v).strip()}
        if len(ids) >= 2:
            out.append({"n": str(m.get("n") or "")[:80], "ids": ids})
    return out


@app.get("/iptv/admin")
def iptv_admin_list():
    al = iptv_load()
    names = sorted(set(users_load().keys()) | {APP_USER.lower()})
    out = []
    for n in [IPTV_ALL] + names:
        o = iptv_pub(al.get(n))
        o["name"] = n
        if o.get("set"):
            o["extra"] = [{"host": x.get("host", ""), "user": x.get("user", ""), "pass": x.get("pass", ""),
                           "stat": x.get("stat") or {}} for x in iptv_accs(al.get(n))[1:]]
            o["cmap"] = al.get(n, {}).get("cmap") or []
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
        oldx = {_acc_key(x): x for x in (old.get("extra") or []) if isinstance(x, dict)}
        extra, seen_k = [], {host + "|" + user}
        for x in (d.get("extra") or [])[:IPTV_MAX_EXTRA]:
            if not isinstance(x, dict):
                continue
            xh, xu, xp = iptv_host(x.get("host")), str(x.get("user", "")).strip()[:80], str(x.get("pass", "")).strip()[:80]
            if not xh or not xu or not xp or (xh + "|" + xu) in seen_k:
                continue
            seen_k.add(xh + "|" + xu)
            extra.append({"host": xh, "user": xu, "pass": xp,
                          "stat": (oldx.get(xh + "|" + xu) or {}).get("stat") or {}})
        al[name] = {"host": host, "user": user, "pass": pw,
                    "stat": old.get("stat") if same else {},
                    "extra": extra,
                    "cmap": _prune_cmap(old.get("cmap"), [host + "|" + user] + [_acc_key(x) for x in extra]),
                    "pins": old.get("pins") or {}}
        iptv_save(al)
    return jsonify(ok=True, host=host)


@app.post("/iptv/admin/<name>/cmap")
def iptv_admin_cmap(name):
    """حفظ ربط القنوات بين حسابات نفس العضو: {cmap:[{n:'beIN 1', ids:{'host|user': stream_id}}]}"""
    name = name.strip().lower()
    d = request.get_json(silent=True) or {}
    with IPTV_LOCK:
        al = iptv_load()
        rec = al.get(name)
        if not rec or not rec.get("host"):
            return jsonify(error="مفيش اشتراك للعضو ده"), 404
        keys = [_acc_key(x) for x in iptv_accs(rec)]
        rec["cmap"] = _prune_cmap(d.get("cmap"), keys)
        iptv_save(al)
    return jsonify(ok=True, cmap=rec["cmap"])


# ----------------- IPTV: مخزن حسابات كبير (مئات/آلاف) + فحص + اختيار الأساسي -----------------
# الفحص بيتم من جهاز الأدمن (آيبي مصري) والسيرفر بيحفظ النتايج بس.
IPTV_POOL_FILE = os.path.join(DATA_DIR, "iptv_pool.json")
IPTV_POOL_MAX = 3000


def pool_load():
    try:
        d = store_load(IPTV_POOL_FILE)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def pool_save(d):
    store_save(IPTV_POOL_FILE, d)


def _pool_owner_ok(name):
    return name == IPTV_ALL or name == APP_USER.lower() or name in users_load()


def _pool_pub(a):
    return {"host": a.get("host", ""), "user": a.get("user", ""), "pass": a.get("pass", ""),
            "st": a.get("st", ""), "exp": a.get("exp", 0), "maxc": a.get("maxc", 0),
            "act": a.get("act", 0), "t": a.get("t", 0)}


@app.get("/iptv/admin/<name>/pool")
def iptv_pool_get(name):
    name = name.strip().lower()
    if not _pool_owner_ok(name):
        return jsonify(error="المستخدم مش موجود"), 404
    pool = pool_load().get(name) or []
    al = iptv_load()
    rec = al.get(name) or {}
    prim = None
    if rec.get("host"):
        prim = {"host": rec["host"], "user": rec.get("user", ""), "pass": rec.get("pass", ""),
                "stat": rec.get("stat") or {}}
    return jsonify(items=[_pool_pub(a) for a in pool if isinstance(a, dict)], primary=prim)


@app.post("/iptv/admin/<name>/pool/add")
def iptv_pool_add(name):
    """إضافة حسابات بالجملة: {items:[{host,user,pass}]} — بيتجاهل المكرر."""
    name = name.strip().lower()
    if not _pool_owner_ok(name):
        return jsonify(error="المستخدم مش موجود"), 404
    d = request.get_json(silent=True) or {}
    with IPTV_LOCK:
        pd = pool_load()
        pool = [a for a in (pd.get(name) or []) if isinstance(a, dict)]
        seen = {_acc_key(a) for a in pool}
        rec = iptv_load().get(name) or {}
        if rec.get("host"):
            seen.add(_acc_key(rec))
        added = dup = bad = 0
        for x in (d.get("items") or [])[:IPTV_POOL_MAX]:
            if not isinstance(x, dict):
                bad += 1
                continue
            h = iptv_host(x.get("host"))
            u = str(x.get("user", "")).strip()[:80]
            p = str(x.get("pass", "")).strip()[:80]
            if not h or not u or not p:
                bad += 1
                continue
            k = h + "|" + u
            if k in seen:
                dup += 1
                continue
            if len(pool) >= IPTV_POOL_MAX:
                bad += 1
                continue
            seen.add(k)
            pool.append({"host": h, "user": u, "pass": p})
            added += 1
        pd[name] = pool
        pool_save(pd)
    return jsonify(ok=True, added=added, dup=dup, bad=bad, total=len(pool))


@app.post("/iptv/admin/<name>/pool/stat")
def iptv_pool_stat(name):
    """حفظ نتايج الفحص: {items:[{k:'host|user', st:'ok|dead|down', exp, maxc, act}]}"""
    name = name.strip().lower()
    if not _pool_owner_ok(name):
        return jsonify(error="المستخدم مش موجود"), 404
    d = request.get_json(silent=True) or {}

    def num(v):
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError):
            return 0
    now = int(time.time())
    res = {}
    for x in (d.get("items") or [])[:500]:
        if isinstance(x, dict) and x.get("st") in ("ok", "dead", "down"):
            res[str(x.get("k"))] = x
    with IPTV_LOCK:
        pd = pool_load()
        pool = pd.get(name) or []
        n = 0
        for a in pool:
            x = res.get(_acc_key(a)) if isinstance(a, dict) else None
            if not x:
                continue
            a["st"] = x["st"]
            a["why"] = str(x.get("why") or "")[:40]
            a["t"] = now
            if x["st"] != "down":          # لو مفيش رد، منمسحش آخر بيانات معروفة
                a["exp"], a["maxc"], a["act"] = num(x.get("exp")), num(x.get("maxc")), num(x.get("act"))
            n += 1
        if n:
            pd[name] = pool
            pool_save(pd)
    return jsonify(ok=True, saved=n)


@app.post("/iptv/admin/<name>/pool/del")
def iptv_pool_del(name):
    """مسح: {keys:[...]} أو {status:'dead'|'down'|'all'}"""
    name = name.strip().lower()
    if not _pool_owner_ok(name):
        return jsonify(error="المستخدم مش موجود"), 404
    d = request.get_json(silent=True) or {}
    keys = set(str(k) for k in (d.get("keys") or []))
    status = d.get("status")
    with IPTV_LOCK:
        pd = pool_load()
        pool = pd.get(name) or []
        keep = []
        for a in pool:
            hit = _acc_key(a) in keys or (status == "all") or (status in ("dead", "down") and a.get("st") == status)
            if not hit:
                keep.append(a)
        gone = len(pool) - len(keep)
        pd[name] = keep
        pool_save(pd)
    return jsonify(ok=True, deleted=gone, total=len(keep))


@app.post("/iptv/admin/<name>/pool/primary")
def iptv_pool_primary(name):
    """يخلّي حساب من البول هو الأساسي (الأفلام/المسلسلات/القوائم). القديم بيرجع للبول."""
    name = name.strip().lower()
    if not _pool_owner_ok(name):
        return jsonify(error="المستخدم مش موجود"), 404
    d = request.get_json(silent=True) or {}
    k = str(d.get("k") or "")
    with IPTV_LOCK:
        pd = pool_load()
        pool = pd.get(name) or []
        pick = next((a for a in pool if _acc_key(a) == k), None)
        if not pick:
            return jsonify(error="الحساب مش موجود في المخزن"), 404
        al = iptv_load()
        old = al.get(name) or {}
        if old.get("host") and old.get("user"):
            back = {"host": old["host"], "user": old["user"], "pass": old.get("pass", "")}
            st = old.get("stat") or {}
            if st.get("t"):
                back.update({"st": "ok" if (st.get("status") or "").lower() == "active" else "down",
                             "exp": st.get("exp", 0), "maxc": st.get("maxc", 0), "act": st.get("active", 0),
                             "t": int(st.get("t") or 0)})
            pool = [a for a in pool if _acc_key(a) != k] + [back]
        else:
            pool = [a for a in pool if _acc_key(a) != k]
        extra = [x for x in (old.get("extra") or []) if isinstance(x, dict) and _acc_key(x) != k]
        newrec = {"host": pick["host"], "user": pick["user"], "pass": pick["pass"],
                  "stat": ({"exp": pick.get("exp", 0), "maxc": pick.get("maxc", 0), "active": pick.get("act", 0),
                            "status": "Active", "trial": False, "t": int(pick.get("t") or 0)}
                           if pick.get("t") and pick.get("st") == "ok" else {}),
                  "extra": extra,
                  "pins": old.get("pins") or {}}
        newrec["cmap"] = _prune_cmap(old.get("cmap"), [_acc_key(newrec)] + [_acc_key(x) for x in extra])
        al[name] = newrec
        iptv_save(al)
        pd[name] = pool
        pool_save(pd)
    return jsonify(ok=True, host=newrec["host"], user=newrec["user"])


@app.get("/providers")
def providers_get():
    items = [{
        "id": p["id"], "name": p["name"], "type": p["type"],
        "domains": p.get("domains", []),
        "key": (_fi_short(p["key"]) if p.get("login") else "••••" + p["key"][-4:]),
        "cdn": bool(p.get("cdn")), "login": bool(p.get("login")),
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
    if t == "1fichier" and d.get("login"):
        accs = _fi_logins(k)
        if not accs:
            return jsonify(error="اكتب الحساب كده: email:password (وممكن كذا حساب بمسافة بينهم)"), 400
        base = (d.get("name") or "").strip()
        for i, (mail, pw) in enumerate(accs, 1):
            nm = (base + (" %d" % i if len(accs) > 1 else "")) if base else "1Fichier 👤 " + _fi_short(mail)
            ps.append({"id": uuid.uuid4().hex[:8], "type": t, "name": nm,
                       "key": mail, "pw": pw, "login": True, "domains": doms})
        save(ps)
        return jsonify(ok=True, added=len(accs))
    if t == "1fichier":
        # أكتر من حساب في نفس الخانة (مسافة/فاصلة/سطر). مفتاح الترافيك يبدأ بـ cdn:  أو ابعت cdn=true
        keys = _fi_split(k) or [(k, False)]
        base = (d.get("name") or "").strip()
        for i, (kk, cdn) in enumerate(keys, 1):
            cdn = cdn or bool(d.get("cdn"))
            nm = base or ("1Fichier CDN" if cdn else "1Fichier")
            if len(keys) > 1:
                nm += " %d" % i
            ps.append({"id": uuid.uuid4().hex[:8], "type": t, "name": nm,
                       "key": kk, "cdn": cdn, "domains": doms})
        save(ps)
        return jsonify(ok=True, added=len(keys))
    ps.append({
        "id": uuid.uuid4().hex[:8], "type": t,
        "name": (d.get("name") or TYPES[t]).strip(),
        "key": k, "domains": doms,
    })
    save(ps)
    return jsonify(ok=True)


@app.post("/providers/<pid>/mode")
def providers_mode(pid):
    """تحويل حساب 1fichier بين بريميم وترافيك (CDN) من غير ما تمسحه وتضيفه تاني."""
    cdn = bool((request.get_json() or {}).get("cdn"))
    ps = load()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p or p["type"] != "1fichier" or p.get("login"):
        return jsonify(error="مش موجود أو حساب يوزر/باسورد (مفيش فيه تحويل)"), 404
    p["cdn"] = cdn
    if p.get("name", "") in ("1Fichier", "1Fichier CDN"):
        p["name"] = "1Fichier CDN" if cdn else "1Fichier"
    _FI_BAD.pop(pid, None)
    save(ps)
    return jsonify(ok=True, cdn=cdn)


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
        tu = str((request.get_json(silent=True) or {}).get("url") or "").strip()
        if t == "1fichier" and p.get("login"):
            _FI_SESS.pop(p["id"], None)
            _fi_login(p, force=True)
            if tu.startswith(("http://", "https://")):
                r_1fichier_login(p, tu)
                return jsonify(ok=True, msg="✅ سجّل دخول وسحب رابط تحميل من الرصيد")
            return jsonify(ok=True, msg="✅ تسجيل الدخول نجح (جرّب بلصق رابط عشان تتأكد إنه بياخد من الرصيد)")
        if t == "1fichier" and tu.startswith(("http://", "https://")):
            # تجربة فعلية: نطلب رابط تحميل للرابط ده بنفس طريقة السحب (cdn لو الحساب ترافيك) ونرجّع رد 1fichier زي ما هو
            try:
                r_1fichier(k, tu, bool(p.get("cdn")))
                return jsonify(ok=True, msg="✅ 1fichier رجّع رابط تحميل (الحساب شغّال على الرابط ده)")
            except Exception as e:
                return jsonify(ok=False, error="رد 1fichier: " + str(e))
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


IV_PID_RX = re.compile(r"iv:[A-Za-z0-9_.-]{1,24}")


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
        if not isinstance(items, dict) or not isinstance(pid, str):
            continue
        if pid.startswith("iv:"):
            # كمّل مشاهدة الـIPTV: معرّف 'iv:<بروفايل>' مش بروفايل حقيقي، فلازم يتقبل بشكل مستقل
            if not IV_PID_RX.fullmatch(pid):
                continue
            if pid not in base["items"] and sum(1 for x in base["items"] if x.startswith("iv:")) >= MAX_PROFILES + 2:
                continue
        elif pid not in base["profiles"]:
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


def _web_key(u):
    """مفتاح موحّد للرابط: يوتيوب بالـ video id، والباقي من غير fragment/سلاش آخر"""
    u = str(u or "").strip()
    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/))([\w-]{11})", u)
    if m:
        return "yt:" + m.group(1)
    u = u.split("#", 1)[0].rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", u.lower())


def find_existing_web(k, q):
    """بدور في حساب TorBox على نفس الرابط اتحمّل قبل كده (جاهز أو لسه بيجهز)"""
    want = _web_key(q)
    try:
        items = tb(k, "GET", "/webdl/mylist",
                   params={"bypass_cache": "true"}).get("data") or []
    except Exception:
        return None
    best = None
    for x in (items if isinstance(items, list) else []):
        if _web_key(x.get("original_url") or x.get("url")) != want:
            continue
        st = str(x.get("download_state", "")).lower()
        if st in ("error", "failed", "expired"):
            continue
        if is_ready(x):
            return x.get("id")  # الجاهز أولوية
        best = best or x.get("id")
    return best


def torbox_web(p, q, web_id=None):
    k = p["key"]
    if not web_id:
        web_id = find_existing_web(k, q)
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


def _ip_public(ip):
    """عنوان عام فعلاً (مش داخلي / loopback / link-local / CGNAT / metadata / multicast، وبيفك IPv4-mapped IPv6)."""
    try:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return bool(ip.is_global) and not ip.is_multicast
    except Exception:
        return False


def _public_url(u):
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        infos = socket.getaddrinfo(p.hostname, None)
        if not infos:
            return False
        for ai in infos:
            if not _ip_public(ipaddress.ip_address(ai[4][0].split("%")[0])):
                return False
        return True
    except Exception:
        return False


# فحص العنوان وقت الاتصال نفسه (بيقفل ثغرة DNS rebinding: الدومين يرجّع IP عام وقت الفحص وبعدها داخلي وقت الاتصال).
# بيشتغل بس جوه `with _guard_net():` عشان ما يأثرش على اتصالات السيرفر العادية (Redis / Prowlarr على شبكة داخلية).
_GUARD = threading.local()
try:
    import urllib3.util.connection as _u3c
    _orig_cc = _u3c.create_connection

    def _guarded_cc(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
        if not getattr(_GUARD, "on", False):
            return _orig_cc(address, timeout, source_address, socket_options)
        host, port = address
        host = host.strip("[]")
        err = None
        for af, st, proto, _cn, sa in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            try:
                if not _ip_public(ipaddress.ip_address(sa[0].split("%")[0])):
                    err = OSError("blocked non-public address")
                    continue
            except ValueError:
                continue
            sock = None
            try:
                sock = socket.socket(af, st, proto)
                for o in (socket_options or []):
                    sock.setsockopt(*o)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                return sock
            except OSError as e:
                err = e
                if sock is not None:
                    sock.close()
        raise err or OSError("no usable address")

    _u3c.create_connection = _guarded_cc
except Exception:
    pass


class _guard_net:
    def __enter__(self):
        _GUARD.on = True

    def __exit__(self, *a):
        _GUARD.on = False
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
        with _guard_net():
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


def _ddg_unwrap(u):
    u = _html.unescape(u or "")
    if u.startswith("//"):
        u = "https:" + u
    m = re.search(r"[?&]uddg=([^&]+)", u)
    return unquote(m.group(1)) if m else u


def _bing_unwrap(u):
    u = _html.unescape(u or "")
    m = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", u)
    if m:
        import base64
        b = m.group(1)
        try:
            return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "ignore")
        except Exception:
            return ""
    return u


def search_ddg(q):
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": q}, timeout=15,
                      headers={"User-Agent": WS_UA, "Accept-Language": "ar,en;q=0.8"})
    if r.status_code != 200:
        raise Exception("DuckDuckGo كود %d" % r.status_code)
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)', r.text, re.S):
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', m.group(3), re.S)
        out.append({"link": _ddg_unwrap(m.group(1)), "title": _mz_clean(m.group(2)),
                    "snippet": _mz_clean(sn.group(1)) if sn else ""})
    return out


def search_bing(q):
    r = requests.get("https://www.bing.com/search", params={"q": q, "count": 20, "setlang": "ar"}, timeout=15,
                     headers={"User-Agent": WS_UA, "Accept-Language": "ar,en;q=0.8"})
    if r.status_code != 200:
        raise Exception("Bing كود %d" % r.status_code)
    out = []
    for m in re.finditer(r'<li class="b_algo"[^>]*>(.*?)</li>', r.text, re.S):
        blk = m.group(1)
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        sn = re.search(r"<p[^>]*>(.*?)</p>", blk, re.S)
        out.append({"link": _bing_unwrap(a.group(1)), "title": _mz_clean(a.group(2)),
                    "snippet": _mz_clean(sn.group(1)) if sn else ""})
    return out


def search_any(q):
    """Serper لو متاح وشغّال، وإلا DuckDuckGo ثم Bing من غير مفتاح ولا رصيد."""
    errs = []
    if SERPER_KEY:
        try:
            return serper(q)
        except Exception as e:
            errs.append(str(e))
    for fn in (search_ddg, search_bing):
        try:
            r = fn(q)
            if r:
                return r
            errs.append(fn.__name__ + ": فاضي")
        except Exception as e:
            errs.append(str(e)[:80])
    raise Exception(" / ".join(errs)[:200] or "محرك البحث ما ردّش")


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
    hosts = ws_hosts()["hosts"]
    notes, organic = [], []
    qs = ws_queries(title, hosts, bool(b.get("deep")))
    # استعلامات إضافية موجّهة لكل موقع تحميل مدعوم عشان النتايج تكون روابط مش مجرد صفحات
    qs += [f"{title} {h}" for h in [h for h in WS_PREFER if _host_ok(h, hosts)][:4]]
    def _safe_search(q):
        try:
            return search_any(q)
        except Exception as ex:
            notes.append(str(ex)[:120])
            return []

    with ThreadPoolExecutor(max_workers=4) as exq:
        for res in exq.map(_safe_search, qs):
            organic += res
    notes[:] = list(dict.fromkeys(notes))[:3]
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
        junk = bool(re.search(r"trailer|teaser|promo|تريلر|إعلان|اعلان|برومو|اعلان تشويقي", (ttl + " " + snip).lower())) \
            or "/clck/" in u or h in ("yandex.ru", "ya.ru")
        if _host_ok(h, hosts):
            if junk:
                dropped += 1
            elif rel > 0:
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
            links = ws_extract(txt, fu, hosts)
            status = "تمام"
            if len(links) < 3:
                # روابط التحميل غالباً في صفحة فرعية: بنتبع لحد 4 روابط "تحميل/download/سيرفرات" من نفس الصفحة
                subs = []
                for au, at in _yy_anchors(txt, fu):
                    low = (at + " " + unquote(au)).lower()
                    if au.split("#")[0] == fu or not _public_url(au) or _host_ok(urlparse(au).netloc.lower().replace("www.", ""), WS_SKIP_PAGES):
                        continue
                    if any(k in low for k in ("download", "تحميل", "تنزيل", "حمل", "سيرفر", "/dl/", "get-link", "links")) and len(at) < 80:
                        if au not in subs:
                            subs.append(au)
                    if len(subs) >= 4:
                        break

                def sub(au):
                    try:
                        t2, f2 = _fetch_html(au)
                        return ws_extract(t2, f2, hosts) if t2 else []
                    except Exception:
                        return []

                if subs:
                    with ThreadPoolExecutor(max_workers=4) as exs:
                        for r2 in exs.map(sub, subs):
                            links += [x for x in r2 if x not in links]
                    status = "تمام (+%d صفحة تحميل)" % len(subs)
            return {"url": u, "title": ws_title(txt) or ttl, "links": links, "status": status, "rel": rel0}
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


_MZ_SESS = requests.Session()
_MZ_ERR = {"last": ""}
_MZ_HD = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Referer": MOVIZ + "/",
}


def _mz_get(u):
    """بهيدرز متصفح موبايل ومهلة أطول وإعادة محاولة، وبيسجّل سبب الفشل. بروكسي اختياري من MOVIZ_PROXY (أو CIMA_PROXY)."""
    px = (os.environ.get("MOVIZ_PROXY") or os.environ.get("CIMA_PROXY") or "").strip()
    for _ in range(2):
        try:
            r = _MZ_SESS.get(u, headers=_MZ_HD, timeout=25, allow_redirects=True,
                             proxies=({"http": px, "https": px} if px else None))
            if r.status_code != 200:
                _MZ_ERR["last"] = "كود %d" % r.status_code
                if r.status_code in (403, 429, 503):
                    return None
                continue
            t = r.content.decode("utf-8", "ignore")
            if "Just a moment" in t[:3000] or "cf-browser-verification" in t:
                _MZ_ERR["last"] = "صفحة تحقق (Cloudflare)"
                return None
            return t
        except requests.Timeout:
            _MZ_ERR["last"] = "انتهى الوقت (Timeout)"
        except Exception as e:
            _MZ_ERR["last"] = str(e)[:80]
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

# ----------------- بحث مباشر في موقع Yoyo Cinema (روابط التحميل المدعومة بس) -----------------
YOYO = "https://yoyo-cinema.vip"
_YY_SESS = requests.Session()
_YY_HD = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Referer": YOYO + "/",
    "Upgrade-Insecure-Requests": "1",
}
_YY_ERR = {"last": ""}
_YY_EP = re.compile(r"/(?:download|watch)/series/([0-9a-fA-F\-]{36})/season/(\d+)/episode/(\d+)")
_YY_MV = re.compile(r"/(?:download|watch)/movies?/([0-9a-fA-F\-]{36})")
_YY_DET = re.compile(r"^/(?:movies|series)/[^/?#]+/?$")


def _yy_get(u):
    """بهيدرز متصفح وكوكيز، وبيسجّل سبب الفشل. بيدعم بروكسي من YOYO_PROXY (أو CIMA_PROXY)."""
    try:
        px = (os.environ.get("YOYO_PROXY") or os.environ.get("CIMA_PROXY") or "").strip()
        r = _YY_SESS.get(u, headers=_YY_HD, timeout=20, allow_redirects=True,
                         proxies=({"http": px, "https": px} if px else None))
        if r.status_code != 200:
            _YY_ERR["last"] = "كود %d" % r.status_code
            return None
        t = r.content.decode("utf-8", "ignore")
        if len(t) < 500 or "cf-browser-verification" in t or "Just a moment" in t[:3000]:
            _YY_ERR["last"] = "صفحة تحقق (Cloudflare)"
            return None
        return t
    except requests.Timeout:
        _YY_ERR["last"] = "انتهى الوقت (Timeout)"
    except Exception as e:
        _YY_ERR["last"] = str(e)[:80]
    return None


def _yy_anchors(page, base):
    """(رابط كامل، نص) لكل لينك داخل الموقع نفسه."""
    res = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page or "", re.S | re.I):
        hm = re.search(r'href="([^"]+)"', m.group(1))
        if not hm:
            continue
        u = urljoin(base, _html.unescape(hm.group(1))).split("#")[0]
        if not u.startswith(YOYO + "/"):
            continue
        txt = _mz_clean(m.group(2)) + " " + " ".join(re.findall(r'(?:alt|title)="([^"]*)"', m.group(0)))
        res.append((u, txt))
    return res


def _yy_title(page):
    m = re.search(r"<h1[^>]*>(.*?)</h1>\s*(?:<p[^>]*>(.*?)</p>)?", page or "", re.S)
    if not m:
        return ws_title(page)
    t = re.sub(r"^\s*سيرفرات\s+تحميل\s*:?\s*", "", _mz_clean(m.group(1)))
    sub = _mz_clean(m.group(2)) if m.group(2) else ""
    return (t + (" · " + sub if sub else "")).strip()


def yoyo_links(page, hosts):
    """روابط السيرفرات من صفحة /download/ (أزرار data-ad-placement=download)."""
    out, seen = [], set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page or "", re.S | re.I):
        at = m.group(1)
        if 'data-ad-placement="download"' not in at:
            continue
        hm = re.search(r'href="([^"]+)"', at)
        if not hm:
            continue
        u = _html.unescape(hm.group(1)).strip()
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        h = urlparse(u).netloc.lower().replace("www.", "")
        d = _host_ok(h, hosts)
        inner = _mz_clean(m.group(2))
        out.append({"url": u, "host": d or h, "quality": _q_guess(inner, u),
                    "size": _size_txt(inner), "ok": bool(d)})
    return out


def _yy_norm(u):
    """أي رابط حلقة/فيلم (watch أو download) → رابط صفحة التحميل."""
    m = _YY_EP.search(u)
    if m:
        return "%s/download/series/%s/season/%s/episode/%s" % (YOYO, m.group(1), m.group(2), m.group(3))
    m = _YY_MV.search(u)
    if m:
        return "%s/download/movie/%s" % (YOYO, m.group(1))
    return None


def _yy_expand(url, want):
    """رابط (حلقة / فيلم / صفحة مسلسل) → قايمة صفحات تحميل مطلوبة."""
    dl = _yy_norm(url)
    page = _yy_get(dl or url)
    if not page and dl and "/download/movie/" in dl:
        page = _yy_get(dl.replace("/download/movie/", "/download/movies/"))
    if not page:
        return [dl] if dl else []
    txt = page.replace("\\/", "/")
    eps = {}
    for m in _YY_EP.finditer(txt):
        eps.setdefault((int(m.group(2)), int(m.group(3))),
                       "%s/download/series/%s/season/%s/episode/%s" % (YOYO, m.group(1), m.group(2), m.group(3)))
    if eps:
        keys = sorted(eps)
        if want:
            sel = [k for k in keys if k[1] in want]
            if not sel and dl:
                return [dl]
            keys = sel or keys
        elif dl:
            return [dl]
        return [eps[k] for k in keys[:8 if want else 10]]
    if dl:
        return [dl]
    mv = _YY_MV.search(txt)
    return ["%s/download/movie/%s" % (YOYO, mv.group(1))] if mv else []


def _yy_diag(sq):
    """تشخيص لما البحث يرجع فاضي: هل النتايج ظاهرة في HTML ولا بتتحمّل من قاعدة بيانات بالجافاسكريبت؟"""
    su = YOYO + "/search?q=" + quote(sq)
    pg = _yy_get(su)
    if not pg:
        return ["تشخيص: مقدرتش أفتح صفحة البحث (%s)" % (_YY_ERR["last"] or "؟")]
    nl = sum(1 for u, _ in _yy_anchors(pg, su)
             if _YY_EP.search(u) or _YY_MV.search(u) or _YY_DET.match(urlparse(u).path))
    out = ["تشخيص: الصفحة %d حرف · الكلمة ظهرت %d مرة · روابط أعمال %d" % (len(pg), pg.count(sq), nl)]
    srcs = []
    for m in re.finditer(r'(?:src|href)="([^"]+\.js)"', pg):
        u = urljoin(YOYO + "/", m.group(1))
        if u.startswith(YOYO + "/") and u not in srcs:
            srcs.append(u)
    found = {"pid": set(), "key": set(), "col": set(), "fn": set(), "api": set(), "ext": set()}
    out.append("تشخيص: ملفات JS اتلقت %d · روابط الأعمال: %s" % (
        len(srcs), " | ".join(sorted({urlparse(u).path[:40] for u, _ in _yy_anchors(pg, su)
                                      if _YY_EP.search(u) or _YY_MV.search(u) or _YY_DET.match(urlparse(u).path)})[:6]) or "-"))
    out.append("تشخيص نص الصفحة: " + _mz_clean(re.sub(r"<(script|style)\b.*?</\1>", " ", pg, flags=re.S | re.I))[:200])

    def scan(u, txt=None):
        t = txt if txt is not None else _yy_get(u)
        if not t:
            return
        t = t.replace("\\/", "/")
        found["pid"].update(re.findall(r'projects/([a-z0-9\-]{4,40})/databases', t))
        found["pid"].update(re.findall(r'([a-z0-9\-]{4,40})\.(?:firebaseapp\.com|appspot\.com|firebasestorage\.app)', t))
        found["pid"].update(re.findall(r'(?:project_?id|PROJECT_ID)\W{1,4}([a-z0-9\-]{4,40})', t, re.I))
        found["key"].update(re.findall(r'AIza[0-9A-Za-z_\-]{35}', t))
        found["pid"].update(re.findall(r'projectId\W{1,3}([a-z0-9\-]{4,40})', t))
        found["key"].update(re.findall(r'AIza[0-9A-Za-z_\-]{35}', t))
        found["fn"].update(re.findall(r'/_serverFn/[A-Za-z0-9_\-%.]+', t))
        found["api"].update(re.findall(r'["\'`](/api/[A-Za-z0-9_\-/.?=&]{2,70})["\'`]', t))
        found["ext"].update(re.findall(r'https://[a-z0-9.\-]*(?:googleapis|firebaseio|firebase|supabase|workers\.dev|vercel\.app|convex|algolia|typesense|meilisearch)[a-z0-9./\-]*', t, re.I))
        if re.search(r"ext-db|servers-db|catalog|firebase", u):
            found["col"].update(re.findall(r'\(\s*[A-Za-z_$][\w$]*\s*,\s*"([a-z][A-Za-z0-9_]{2,25})"\s*[,)]', t))

    scan(su, pg)
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(scan, srcs[:30]))
    for k, lab in (("pid", "projectId"), ("key", "apiKey"), ("col", "collections"), ("fn", "serverFn"), ("api", "api"), ("ext", "ext")):
        if found[k]:
            out.append("تشخيص %s: %s" % (lab, " | ".join(sorted(found[k])[:25])))
    return out


SB_URL = "https://jfibkncnpwuuvbkelcfr.supabase.co"
SB_KEY = "sb_publishable_7taQr1nZvktj0smOaghYUg_sYeqBpos"   # مفتاح عام (publishable) مدمج في كود موقع Yoyo نفسه
_SB_CACHE = {"schema": None}
_TITLE_COLS = ("title", "name", "title_ar", "name_ar", "arabic_title", "original_title", "slug")


def _sb_get(path, params=None):
    px = (os.environ.get("YOYO_PROXY") or os.environ.get("CIMA_PROXY") or "").strip()
    r = requests.get(SB_URL + path, params=params, headers={"apikey": SB_KEY}, timeout=15,
                     proxies=({"http": px, "https": px} if px else None))
    if r.status_code != 200:
        raise Exception("Supabase كود %d: %s" % (r.status_code, r.text[:80]))
    return r.json()


_SB_GUESS = ("movies", "series", "episodes", "titles", "content", "contents", "catalog", "items", "media",
             "shows", "films", "tv_series", "seasons", "posts", "works")


def _sb_schema():
    """جداول وأعمدة Yoyo. الـ OpenAPI محتاج مفتاح سري، فبنجرّب أسماء الجداول الشايعة ونقرا الأعمدة من أول صف."""
    if _SB_CACHE["schema"] is None:
        sch = {}
        try:
            d = _sb_get("/rest/v1/")
            sch = {t: list((v.get("properties") or {}).keys()) for t, v in (d.get("definitions") or {}).items()}
        except Exception:
            pass
        if not sch:
            def probe(t):
                try:
                    r = _sb_get("/rest/v1/" + t, {"select": "*", "limit": "1"})
                    if isinstance(r, list):
                        sch[t] = list(r[0].keys()) if r else []
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(probe, _SB_GUESS))
        _SB_CACHE["schema"] = sch
    return _SB_CACHE["schema"]


def _sb_pat(v):
    """نفس تحويل موقع Yoyo للبحث: بنشيل التشكيل، وحروف (أإآا ة/ه ي/ى/ئ) بتبقى أي حرف، و% → *."""
    v = re.sub(r"[\u064B-\u065F\u0670]", "", v)
    v = re.sub(r'["\\*_]', "_", v)
    v = re.sub(r"[أإآاٱةهيىئ]", "_", v)
    return "*" + v.replace("%", "*") + "*"


def _yy_supabase(sq, nums=()):
    """بحث مباشر في قاعدة Yoyo: كل البيانات في جدول docs (collection = movies / series / seasons / episodes).
    بيرجّع [(rel, رابط صفحة تحميل)] جاهزة من غير ما نفتح صفحة البحث."""
    t = re.sub(r"[%,()*\"]", " ", sq).strip()
    n = re.sub(r"^(مشاهدة|تحميل|فيلم|مسلسل|انمي|مسرحية)\s+", "", t)
    n = re.sub(r"\s+(مترجم|مدبلج|كامل|اون\s*لاين|اونلاين|HD)\b.*", "", n, flags=re.I).strip()
    vs = [v for i, v in enumerate((t, n)) if len(v) >= 2 and v not in (t, n)[:i]]
    if not vs:
        return []
    orx = ",".join('data->>title.ilike."%s",data->>original_title.ilike."%s"' % (_sb_pat(v), _sb_pat(v)) for v in vs)

    def get(params):
        return _sb_get("/rest/v1/docs", params)

    def find(coll):
        return get({"collection": "eq." + coll, "or": "(%s)" % orx, "limit": "30",
                    "select": "id,title:data->title,original_title:data->original_title,status:data->status"})

    errs, res = [], {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {k: ex.submit(find, k) for k in ("movies", "series")}
        for k, f in futs.items():
            try:
                res[k] = [r for r in f.result() if r.get("status") in (None, "published")]
            except Exception as e:
                errs.append(str(e)[:100])
                res[k] = []
    if len(errs) == 2:
        raise Exception(errs[0])

    def rel_of(*txt):
        return ws_rel(sq, " ".join(str(x or "") for x in txt)) or 0.1   # القاعدة أصلاً رجّعته بالاسم

    out = {}
    for r in res["movies"]:
        if r.get("id"):
            out["%s/download/movie/%s" % (YOYO, r["id"])] = rel_of(r.get("title"), r.get("original_title"))
    ser = sorted(res["series"], key=lambda r: -rel_of(r.get("title"), r.get("original_title")))[:3]

    def eps_of(r):
        try:
            sid = "eq.%s" % r["id"]
            seas = get({"collection": "eq.seasons", "data->>series_id": sid, "limit": "200",
                        "select": "id,season_number:data->season_number"})
            eps = get({"collection": "eq.episodes", "data->>series_id": sid, "limit": "1000",
                       "select": "id,episode_number:data->episode_number,season_id:data->season_id,status:data->status"})
            return r, {x["id"]: x.get("season_number") for x in seas}, eps
        except Exception:
            return r, {}, []

    with ThreadPoolExecutor(max_workers=3) as ex:
        for r, smap, eps in ex.map(eps_of, ser):
            rl = rel_of(r.get("title"), r.get("original_title"))
            rows = []
            for e in eps:
                if e.get("status") not in (None, "published") or e.get("episode_number") is None:
                    continue
                try:
                    en = int(e["episode_number"])
                except Exception:
                    continue
                if nums and en not in nums:
                    continue
                rows.append((smap.get(e.get("season_id")) or 1, en))
            for sn, en in sorted(rows)[:30]:
                out["%s/download/series/%s/season/%s/episode/%s" % (YOYO, r["id"], sn, en)] = rl
    return sorted(((v, u) for u, v in out.items()), key=lambda x: -x[0])[:30]


_YY_DET2 = re.compile(r"^/(?:movie|series)/[^/?#]+/?$")
_YY_BROWSE = ("/", "/latest", "/movies", "/series", "/movies?sort=latest", "/series?sort=latest",
              "/movies/arabic-movies", "/movies/foreign-movies", "/series/arabic-series",
              "/series/foreign-series", "/series/turkish-series", "/movies?sort=popular")


def _yy_browse(sq):
    """بديل لما بحث الموقع يطلع فاضي (بيتحمّل بالجافاسكريبت): بنفتح صفحات القوايم اللي السيرفر بيشوفها
    (الرئيسية والأحدث والأفلام والمسلسلات) وبنفلتر العناوين بالاسم. بيغطي الأعمال الحديثة بس."""
    pages = {}

    def get(p):
        pages[p] = _yy_get(YOYO + p)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(get, _YY_BROWSE))
    found = {}
    for p, pg in pages.items():
        if not pg:
            continue
        base = YOYO + p
        for au, txt in _yy_anchors(pg, base):
            pth = urlparse(au).path
            if len(txt) > 600 or not (_YY_EP.search(pth) or _YY_MV.search(pth) or _YY_DET2.match(pth)):
                continue
            rel = ws_rel(sq, txt + " " + unquote(au))
            if rel > 0:
                k = _yy_norm(au) or au.split("?")[0].rstrip("/")
                found[k] = max(found.get(k, (0, au)), (rel, au))
        # عناوين الـ hero: <h2>العنوان</h2> ... href="/series/slug" أو "/movie/slug"
        for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", pg, re.S):
            title = _mz_clean(m.group(1))
            hm = re.search(r'href="(/(?:movie|series)/[^"/?#]+)"', pg[m.end():m.end() + 4000])
            if title and hm:
                rel = ws_rel(sq, title)
                if rel > 0:
                    au = YOYO + _html.unescape(hm.group(1))
                    found[au] = max(found.get(au, (0, au)), (rel, au))
    return sorted(found.values(), key=lambda x: -x[0])[:5], sum(1 for v in pages.values() if v)


def _yoyo_search(q):
    hosts = ws_hosts()["hosts"]
    notes = []
    nums = {int(x) for x in re.findall(r"\d+", q.translate(_AR_DIG)) if len(x) <= 4}
    sq = re.sub(r"\d+", " ", _CL_STOP.sub(" ", q.translate(_AR_DIG))).strip() or q
    cands, seen, got = [], set(), 0
    for key in ("q", "query", "search", "s", "keyword"):
        su = YOYO + "/search?%s=%s" % (key, quote(sq))
        sres = _yy_get(su)
        if not sres:
            continue
        got += 1
        for au, txt in _yy_anchors(sres, su):
            p = urlparse(au).path
            if not (_YY_EP.search(p) or _YY_MV.search(p) or _YY_DET.match(p)) or len(txt) > 600:
                continue
            k = _yy_norm(au) or au.split("?")[0].rstrip("/")
            if k in seen:
                continue
            rel = ws_rel(sq, txt + " " + unquote(au))
            if rel > 0:
                seen.add(k)
                em = _YY_EP.search(p)
                bonus = 0.5 if (nums and em and int(em.group(3)) in nums) else 0
                cands.append((rel + bonus, au))
        if cands:
            break
    if not got:
        return jsonify(error="موقع Yoyo رفض السيرفر (%s). غالباً بيحظر عناوين السيرفرات، جرّب تشغّل البوت من IP تاني أو بروكسي (YOYO_PROXY)." % (_YY_ERR["last"] or "مجهول")), 502
    cands.sort(key=lambda x: -x[0])
    cands = cands[:5]
    pre_targets = None
    if not cands:
        try:
            cands = _yy_supabase(sq, nums)
            if cands:
                pre_targets = [u for _, u in cands]
                notes.append("لقيت النتايج من قاعدة بيانات Yoyo مباشرة")
        except Exception as e:
            notes.append("بحث قاعدة بيانات Yoyo فشل: %s" % str(e)[:100])
    if not cands:
        cands, okp = _yy_browse(sq)
        if cands:
            notes.append("بحث Yoyo بيتحمّل بالجافاسكريبت، فدوّرت في قوايم الموقع (الأحدث والأفلام والمسلسلات) وده بيغطي الأعمال الحديثة بس")
    if not cands:
        return jsonify(items=[], found=0, notes=[
            "مفيش نتايج ليها علاقة بالاسم في Yoyo (أو صفحة البحث بتحمّل النتايج بالجافاسكريبت ومش ظاهرة للسيرفر)"] + _yy_diag(sq))

    targets = []
    if pre_targets:
        targets = pre_targets[:30]
    else:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for lst in ex.map(lambda c: _yy_expand(c[1], nums), cands):
                for u in lst:
                    if u not in targets:
                        targets.append(u)
        targets = targets[:20]

    def work(u):
        page = _yy_get(u)
        if not page and "/download/movie/" in u:
            page = _yy_get(u.replace("/download/movie/", "/download/movies/"))
        if not page:
            return None
        return {"title": _yy_title(page), "page": u, "links": yoyo_links(page, hosts) or _cl_loose_links(page, u, hosts)}

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
    if not items and targets:
        notes.append("لقيت %d صفحة تحميل بس مفيهاش روابط ظاهرة في HTML (الروابط بتتحمّل بالجافاسكريبت)" % len(targets))
        notes += _yy_diag(sq)
    return jsonify(items=items, notes=notes, found=sum(len(i["links"]) for i in items))


@app.post("/sitesearch")
def sitesearch():
    b = request.get_json(silent=True) or {}
    q = str(b.get("q") or "").strip()[:120]
    if not q:
        return jsonify(error="اكتب اسم العمل"), 400
    if str(b.get("site") or "") in ("yoyo", "yoyo-cinema", "ahwak", "cimalight", "cima"):
        return _yoyo_search(q)
    hosts = ws_hosts()["hosts"]
    notes = []
    sres = _mz_get(MOVIZ + "/?s=" + quote(q))
    if not sres:
        return jsonify(error="موقع MovizLand ما ردّش (%s). لو الموقع شغال عندك في المتصفح يبقى غالباً بيحظر عنوان السيرفر، جرّب بروكسي (MOVIZ_PROXY)." % (_MZ_ERR["last"] or "مجهول")), 502
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

_FI_SESS = {}
_FI_UA = ("Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0 Mobile Safari/537.36")
_FI_DIRECT = re.compile(r"https?://[a-z0-9-]+\.1fichier\.com/[A-Za-z0-9_\-]{5,}[^\s\"'<>]*", re.I)


def _fi_login(p, force=False):
    """يسجّل دخول حساب 1fichier (يوزر/باسورد) ويحتفظ بالجلسة 6 ساعات عشان ما نكررش الدخول (يسبب كابتشا/حظر)."""
    now = time.time()
    c = _FI_SESS.get(p["id"])
    if c and not force and now - c[1] < 6 * 3600:
        return c[0]
    s = requests.Session()
    s.headers.update({"User-Agent": _FI_UA, "Accept-Language": "en-US,en;q=0.9"})
    r = s.post("https://1fichier.com/login.pl",
               data={"mail": p["key"], "pass": p.get("pw", ""), "lt": "on", "purge": "off", "valider": "Send"},
               timeout=25, allow_redirects=True)
    ok = bool(s.cookies.get("SID")) or bool(re.search(r"logout|deconnexion", r.text or "", re.I))
    if not ok:
        raise Exception("تسجيل الدخول فشل (اليوزر/الباسورد غلط، أو فيه 2FA/كابتشا/حماية) كود %s" % r.status_code)
    _FI_SESS[p["id"]] = (s, now)
    return s


def r_1fichier_login(p, u):
    """سحب رابط تحميل من رصيد CDN بحساب يوزر/باسورد (زي ما المتصفح بيعمل لما تكون مسجّل دخول)."""
    last = "فشل"
    for attempt in (0, 1):
        s = _fi_login(p, force=bool(attempt))
        r = s.get(u, allow_redirects=False, timeout=25, stream=True)
        try:
            loc = r.headers.get("Location", "")
            if r.status_code in (301, 302, 303, 307, 308) and loc:
                if re.match(r"https?://[a-z0-9-]+\.1fichier\.com/", loc, re.I) and not re.match(r"https?://(www\.)?1fichier\.com", loc, re.I):
                    return loc
                last = "1fichier حوّل لصفحة تانية (%s)" % loc[:60]
                _FI_SESS.pop(p["id"], None)
                continue
            body = r.text[:200000] if r.status_code == 200 else ""
        finally:
            r.close()
        m = _FI_DIRECT.search(body)
        if m and not re.match(r"https?://(www\.)?1fichier\.com", m.group(0), re.I):
            return m.group(0).replace("&amp;", "&")
        if re.search(r'name="pass"|login\.pl', body):
            _FI_SESS.pop(p["id"], None)     # الجلسة انتهت: نسجّل دخول من جديد ونجرّب مرة تانية
            last = "الجلسة انتهت"
            continue
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if re.search(r"adz|Click here to download|must wait|You must wait", body, re.I):
            raise Exception("الحساب ده مش بياخد من الرصيد (فعّل Use my credits for my own downloads) — رد الموقع: " + txt[:120])
        raise Exception("رد غير متوقع من 1fichier (كود %s): %s" % (r.status_code, txt[:120]))
    raise Exception(last)


def r_1fichier(k, u, cdn=False):
    body = {"url": u}
    if cdn:
        body["cdn"] = 1     # حساب مجاني برصيد CDN: لازم نطلب استخدام الرصيد صراحة (البريميم بيتفعّل تلقائي)
    r = requests.post("https://api.1fichier.com/v1/download/get_token.cgi",
                      json=body,
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
    data = res.get("data") or {}
    if res.get("status") == "success" and data.get("link"):
        return data["link"]
    # مواقع الستريم (زي يوتيوب) AllDebrid بيجهزها في الخلفية ويرجّع delayed id
    # نفس الرابط بيرجّع نفس الـ id فمفيش تحميل مكرر، بنستنى لحد ما يجهز
    if res.get("status") == "success" and data.get("delayed"):
        did = data["delayed"]
        for _ in range(12):
            time.sleep(2)
            rr = jj(requests.get("https://api.alldebrid.com/v4/link/delayed",
                                 params={"agent": "Elkomy", "apikey": k, "id": did},
                                 timeout=20))
            dd = rr.get("data") or {}
            if dd.get("status") == 2 and dd.get("link"):
                return dd["link"]
            if dd.get("status") == 3:
                raise Exception("AllDebrid فشل يجهّز الرابط")
        raise Exception("AllDebrid لسه بيجهّز الرابط، جرّب تاني بعد شوية (مش هيبدأ من الأول)")
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


def _generate(data=None):
    data = data or request.get_json() or {}
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
                if p["type"] == "1fichier":
                    url = r_1fichier_login(p, q) if p.get("login") else r_1fichier(p["key"], q, bool(p.get("cdn")))
                    _FI_BAD.pop(p["id"], None)
                else:
                    url = INSTANT[p["type"]](p["key"], q)
                name = unquote(url.split("?")[0].rsplit("/", 1)[-1]) or "ملف"
                return jsonify(direct_link=url, name=name,
                               provider=p["name"], tried=tried)
            except Exception as e:
                tried.append(f"{p['name']}: {e}")
                if p["type"] == "1fichier":
                    _fi_mark(p, e)
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
    if not _public_url(base):      # حماية SSRF: ممنوع عناوين داخلية (localhost / شبكة خاصة)
        return jsonify(error="الرابط ده مش مسموح (لازم يكون عنوان عام)"), 400
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
        items=[{"id": a["id"], "name": a["name"], "stream": a["stream"],
                "base": a.get("base", "")} for a in d["addons"]])


@app.post("/stremio/addons/<aid>/move")
def st_move(aid):
    """تحريك إضافة لفوق/تحت (dir = up | down | top | bottom)"""
    dr = (request.get_json() or {}).get("dir", "")
    with ST_LOCK:
        d = st_load()
        L = d["addons"]
        i = next((k for k, a in enumerate(L) if a["id"] == aid), -1)
        if i < 0:
            return jsonify(error="الإضافة مش موجودة"), 404
        a = L.pop(i)
        j = {"up": i - 1, "down": i + 1, "top": 0, "bottom": len(L)}.get(dr, i)
        L.insert(max(0, min(len(L), j)), a)
        st_save(d)
    return jsonify(ok=True)


@app.post("/stremio/addons/<aid>/edit")
def st_edit(aid):
    """تعديل اسم الإضافة و/أو رابطها (الرابط الجديد بيتفحص الأول)"""
    b = request.get_json() or {}
    name = str(b.get("name") or "").strip()[:80]
    url = str(b.get("url") or "").strip()
    with ST_LOCK:
        d = st_load()
        a = next((x for x in d["addons"] if x["id"] == aid), None)
        if not a:
            return jsonify(error="الإضافة مش موجودة"), 404
        if url:
            base = st_base(url)
            if base != a.get("base"):
                if not base.startswith(("http://", "https://")):
                    return jsonify(error="الرابط لازم يبدأ بـ https:// أو stremio://"), 400
                if not _public_url(base):
                    return jsonify(error="الرابط ده مش مسموح (لازم يكون عنوان عام)"), 400
                if any(x["base"] == base for x in d["addons"] if x["id"] != aid):
                    return jsonify(error="الرابط ده موجود في إضافة تانية"), 400
                try:
                    man = st_manifest(base)
                except Exception as e:
                    return jsonify(error=f"الإضافة مش شغالة: {e}"), 400
                if not isinstance(man, dict) or not man.get("name"):
                    return jsonify(error="ده مش manifest إضافة صحيح"), 400
                info = st_info(man)
                a["base"] = base
                a["stream"] = info is not None
                a["types"] = (info or {}).get("types", [])
                a["prefixes"] = (info or {}).get("prefixes", [])
                a["catalogs"] = st_catalogs(man)
                if not name:
                    name = man.get("name") or a["name"]
        if name:
            a["name"] = name
        st_save(d)
    return jsonify(ok=True)


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


def _all_secret_keys():
    """كل الأسرار اللي ممنوع تظهر لعضو: مفاتيح/باسوردات المزوّدين + المفاتيح اللي جوه روابط الإضافات + authKey بتاع ستريميو."""
    ks = set()
    try:
        for p in load():
            for f in ("key", "pw"):
                if p.get(f):
                    ks.add(str(p[f]))
    except Exception:
        pass
    try:
        d = st_load()
        if d.get("authKey"):
            ks.add(str(d["authKey"]))
        for a_ in d.get("addons", []):
            ks |= {m.group(2) for m in DEBRID_RX.finditer(unquote(urlparse(a_["base"]).path))}
    except Exception:
        pass
    return {k for k in ks if len(k) >= 8}


def _url_has_secret(u):
    dec = unquote(str(u or ""))
    return any(k in dec or k in str(u or "") for k in _all_secret_keys())


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
            if not _public_url(cur):
                return jsonify(error="المصدر مش متاح"), 502
            with _guard_net():
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
                    if g.role != "admin" and _url_has_secret(cur):
                        # السيرفر مش قادر يمرّر الملف (AllDebrid رافض IP السيرفر) والجهاز ممنوع ياخد رابط فيه مفتاح
                        return jsonify(error="النسخة دي مش بتشتغل على الأعضاء، اختار «🔁 غيّر النسخة» (نسخة تانية)"), 409
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
        act_log(g.user, "stremio", req.get("name"), req.get("size"), "Stremio")
        if g.role != "admin":
            # العضو عمره ما ياخد الرابط الأصلي (فيه مفتاح أو دومين المزوّد): بياخد رابط /px/ مشفّر وموقّع
            # والسيرفر هو اللي يجيب الفيديو بالمفتاح. بنعملها هنا صراحةً (مش بالاعتماد على after_request)
            # وبعدها leak_guard بيتأكد إن مفيش أي سر في الرد.
            return jsonify(url=px_make(cur))
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
    rev_bump()
    return jsonify(ok=True, n=len(out), kv=KV_ON)


# ---- رقم نسخة الرئيسية: أي حفظ من الأدمن بيغيّره، والشاشات بتسأل عنه كل ~15 ثانية وتتحدّث لوحدها ----
REV_FILE = os.path.join(DATA_DIR, "homerev.json")
_REV_MEM = {"t": 0.0, "v": 0}


def rev_bump():
    v = int(time.time() * 1000)
    try:
        store_save(REV_FILE, {"v": v})
    except Exception:
        pass
    _REV_MEM["t"], _REV_MEM["v"] = time.time(), v


@app.get("/home/rev")
def home_rev():
    if time.time() - _REV_MEM["t"] > 3:       # كاش 3 ثواني عشان ما نضغطش على Redis
        try:
            v = int((store_load(REV_FILE) or {}).get("v", 0))
        except Exception:
            v = _REV_MEM["v"]
        _REV_MEM["t"], _REV_MEM["v"] = time.time(), v
    r = jsonify(v=_REV_MEM["v"])
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/home/ping")
def home_ping():
    return jsonify(v="layout-v6", kv=KV_ON, dir=DATA_DIR)


app.add_url_rule("/home/layout2", "layout2_get", home_layout_get, methods=["GET"])
app.add_url_rule("/home/layout2", "layout2_set", home_layout_set, methods=["POST"])


# ================= أقسام الرئيسية القابلة للترتيب (للأدمن) — بتظهر في الموقع والأندرويد والتلفزيون =================
HB_FILE = os.path.join(DATA_DIR, "homeblocks.json")
HB_FIXED = {"feat": "المميّز (بانرات / مباريات)", "cw": "▶ كمّل المشاهدة", "pins": "المثبّت من IPTV",
            "live": "📡 القنوات المباشرة", "ivsec": "أقسام IPTV (رمضان…)", "new-ar": "🇪🇬 جديد عربي",
            "new-for": "🌍 جديد أجنبي", "new-toon": "🧸 كرتون وأنمي جديد", "tz": "على ذوقك (تعلّم تلقائي)",
            "fav": "مفضلتي (المنصات والأنواع)", "ar": "صفوف الإضافات (Stremio)"}
HB_DEFAULT = ["feat", "cw", "pins", "live", "ivsec", "tz"]   # الرئيسية الخفيفة: المميّز + كمّل المشاهدة + المثبّت (أفلام/قنوات/أقسام) + على ذوقك
HB_HEAVY = {"new-ar", "new-for", "new-toon", "ar", "fav"}   # بتقفل تلقائي (الأدمن يقدر يفتحها من لوحة الأقسام)
HB_SORTS = ("new", "old", "pop", "top")
HB_CACHE = {}


def _hb_year(v):
    try:
        v = int(v)
        return v if 1900 <= v <= 2100 else 0
    except Exception:
        return 0


def hb_clean(rows_in):
    out, used = [], set()
    for r in (rows_in or [])[:80]:
        if not isinstance(r, dict):
            continue
        bid = str(r.get("id", ""))
        if bid in used:
            continue
        if bid in HB_FIXED:
            used.add(bid)
            o = {"id": bid, "on": bool(r.get("on", True))}
            t = str(r.get("t", "")).strip()[:60]
            if t:
                o["t"] = t
            out.append(o)
        elif re.fullmatch(r"c_[a-z0-9]{4,12}", bid):
            used.add(bid)
            lg = str(r.get("lg", "")).lower()
            out.append({"id": bid, "on": bool(r.get("on", True)),
                        "t": str(r.get("t", "")).strip()[:60] or "قسم جديد",
                        "ty": r.get("ty") if r.get("ty") in ("movie", "tv", "all") else "all",
                        "lg": lg if re.fullmatch(r"[a-z]{2}", lg) else "",
                        "g": r.get("g") if r.get("g") in _GENRE_KEYS else "",
                        "y1": _hb_year(r.get("y1")), "y2": _hb_year(r.get("y2")),
                        "so": r.get("so") if r.get("so") in HB_SORTS else "new"})
    return out


def hb_view():
    saved, lite = None, False
    try:
        d = store_load(HB_FILE)
        if isinstance(d, dict):
            saved = d.get("blocks")
            lite = d.get("lite") == 2
    except Exception:
        saved = None
    blocks = hb_clean(saved) if saved else []
    if blocks and not lite:          # تخزين قديم: اقفل الأقسام التقيلة مرة واحدة (أول حفظ جديد من الأدمن بيثبّت اختياراته)
        for b in blocks:
            if b["id"] in HB_HEAVY:
                b["on"] = False
    have = {x["id"] for x in blocks}
    for bid in HB_FIXED:
        if bid not in have:
            blocks.append({"id": bid, "on": bid in HB_DEFAULT})
    return blocks, bool(saved)


@app.get("/home/layoutblocks")
def hb_get():
    blocks, saved = hb_view()
    return jsonify(blocks=blocks, names=HB_FIXED, saved=saved, persist=KV_ON)


@app.post("/home/layoutblocks")
def hb_set():
    rows_in = (request.get_json(silent=True) or {}).get("blocks")
    if not isinstance(rows_in, list):
        return jsonify(error="بيانات ناقصة"), 400
    out = hb_clean(rows_in)
    try:
        store_save(HB_FILE, {"blocks": out, "t": int(time.time()), "lite": 2})
        back = store_load(HB_FILE)
        if not isinstance(back, dict) or len(back.get("blocks") or []) != len(out):
            return jsonify(error="التخزين رجّع بيانات مختلفة بعد الحفظ"), 500
    except Exception as ex:
        return jsonify(error="فشل الحفظ في التخزين: " + short_err(ex)), 500
    HB_CACHE.clear()
    rev_bump()
    return jsonify(ok=True, n=len(out), kv=KV_ON)


def hb_page(b, skip):
    today = date.today()
    types = ["movie", "tv"] if b.get("ty") == "all" else [b.get("ty") or "movie"]
    items, more = [], False
    for t in types:
        ex = {}
        if b.get("lg"):
            ex["with_original_language"] = b["lg"]
        if b.get("g"):
            ex["with_genres"] = 16 if b["g"] == "cartoon" else _GENRE_KEYS[b["g"]][1 if t == "movie" else 2]
        df = "primary_release_date" if t == "movie" else "first_air_date"
        so, y1, y2 = b.get("so", "new"), b.get("y1") or 0, b.get("y2") or 0
        if so == "old" and not y2:
            y2 = today.year - 10
        if y1:
            ex[df + ".gte"] = f"{y1}-01-01"
        ex[df + ".lte"] = today.isoformat() if (not y2 or y2 >= today.year) else f"{y2}-12-31"
        if so == "new":
            ex["sort_by"] = df + ".desc"
            ex["vote_count.gte"] = 1 if b.get("lg") else 15
        elif so == "old":
            ex["sort_by"] = "vote_count.desc"
        elif so == "top":
            ex["sort_by"] = "vote_average.desc"
            ex["vote_count.gte"] = 200
        else:
            ex["sort_by"] = "popularity.desc"
        it, mo = _discover_page(t, ex, skip)
        items += it
        more = more or mo
    if len(types) == 2:
        m_ = [x for x in items if x["type"] == "movie"]
        t_ = [x for x in items if x["type"] == "tv"]
        items = [y for pr in zip(m_, t_) for y in pr] + m_[len(t_):] + t_[len(m_):]
    return items, more


def hb_spec(bid):
    if not str(bid).startswith("c_"):
        return None
    blocks, _ = hb_view()
    return next((x for x in blocks if x["id"] == bid), None)


def hb_browse(bid, skip):
    b = hb_spec(bid)
    if not b:
        return jsonify(items=[], skip=skip, more=False)
    try:
        items, more = hb_page(b, skip)
    except Exception as ex:
        return jsonify(error=str(ex))
    return jsonify(items=items, skip=skip + 20, more=more)


@app.get("/home/blockitems")
def hb_items():
    bid = request.args.get("id", "")
    b = hb_spec(bid)
    if not b:
        return jsonify(items=[])
    ck = (bid, json.dumps(b, sort_keys=True, ensure_ascii=False))
    hit = HB_CACHE.get(ck)
    if hit and time.time() - hit[0] < 1800:
        return jsonify(items=hit[1], title=b["t"])
    try:
        items, _more = hb_page(b, 0)
    except Exception as ex:
        return jsonify(items=[], error=str(ex))
    HB_CACHE[ck] = (time.time(), items)
    return jsonify(items=items, title=b["t"])



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


@app.get("/rows/meta")
def rows_meta():
    """أسماء صفوف الإضافات بس (من غير ما نجيب الأفلام) — لوحة «مفضلتي» كانت بتستنى /rows الكامل وده بطيء جدًا."""
    d = ensure_catalogs()
    amap = {a["id"] for a in d["addons"]}
    out = [{"id": e["k"], "title": e["t"]} for e in home_layout(d)
           if e.get("on") and (e.get("a") in amap or e.get("a") == "_dub")]
    if not out:                       # مفيش صفوف إضافات متفعّلة: نفس الصفوف العامة اللي كانت بتظهر قبل كده
        try:
            r = tmdb_rows()
            r = r[0] if isinstance(r, tuple) else r
            out = [{"id": x.get("id"), "title": x.get("title")} for x in ((r.get_json(silent=True) or {}).get("rows") or [])]
        except Exception:
            out = []
    return jsonify(rows=out)


@app.get("/rows")
def rows():
    d = ensure_catalogs()
    amap = {a["id"]: a for a in d["addons"]}
    ents = [e for e in home_layout(d)
            if e.get("on") and (e.get("a") in amap or e.get("a") == "_dub")]
    if not ents:
        return tmdb_rows()
    want = {x for x in (request.args.get("ids") or "").split(",") if x}
    if want:                          # الصفحة بتطلب الصفوف اللي العضو علّمها بس
        ents = [e for e in ents if e["k"] in want]
        if not ents:
            return jsonify(rows=[])

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
        devs = devs_of(rec) if role == "user" else []
        top = devs[0] if devs else {}
        if role == "admin":
            am = ADMIN_META.get(n) or {}
            if am:
                devs = [{"ty": "web", "ip": am.get("ip", ""), "ua": ua_label(am.get("ua", "")), "geo": am.get("geo", ""),
                         "ls": int(am.get("t", 0) * 1000), "lo": 0, "fs": 0, "lg": 0, "oc": 0, "lp": "", "lpt": 0,
                         "on": time.time() - am.get("t", 0) < SESS_ACTIVE}]
                top = devs[0]
        if top:
            last = max(last, top.get("ls", 0))
        out.append({"devs": devs, "online": any(x["on"] for x in devs), "ty": top.get("ty", ""),
                    "ip": top.get("ip", ""), "ua": top.get("ua", ""), "geo": top.get("geo", ""),
                    "lo": max([x.get("lo", 0) for x in devs] or [0]), "oc": sum(x.get("oc", 0) for x in devs),
                    "lp": top.get("lp", ""), "lpt": top.get("lpt", 0),
                    "name": n, "role": role, "bytes": a.get("bytes", 0),
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
    return jsonify(devs=devs_of(rec) if name != APP_USER.lower() else [],
                   pulls=list(reversed(a.get("ev") or []))[:100], watched=watched[:100],
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
        def _row(x, e, ar_x=None):
            date_s = x.get("release_date") or x.get("first_air_date") or ""
            return {
                "id": x["id"], "type": x["media_type"], "year": date_s[:4],
                "title": x.get("title") or x.get("name") or "",
                "search_title": e.get("title") or e.get("name")
                or x.get("original_title") or x.get("original_name") or "",
                "poster": img(x.get("poster_path"), "w342"),
            }

        if _AR_RE.search(q):
            # ===== اسم عربي: نفس السلوك القديم =====
            used, ar = q, []
            for qq in _ar_variants(q):
                ar = _tmdb_multi(qq, "ar")
                if ar:
                    used = qq
                    break
            en_list = tmdb_get("/search/multi", query=used, language="en-US",
                               include_adult="false").get("results", [])
            en = {(i.get("media_type"), i["id"]): i for i in en_list}
            out = [_row(x, en.get((x["media_type"], x["id"]), x)) for x in ar]
            return jsonify(results=out[:30])

        # ===== اسم إنجليزي/أجنبي: نتايج بالاسم الإنجليزي والبوستر الأصلي الأول، =====
        # ===== وبعدها نتايج البحث العربي الحالية (من غير تكرار) =====
        en_res = _tmdb_multi(q, "en-US")
        try:
            ar_res = _tmdb_multi(q, "ar")
        except Exception:
            ar_res = []
        ar_map = {(i.get("media_type"), i["id"]): i for i in ar_res}
        out, seen = [], set()
        for x in en_res:
            k = (x["media_type"], x["id"])
            seen.add(k)
            r = _row(x, x)
            a = ar_map.get(k)
            if a:
                at = a.get("title") or a.get("name") or ""
                if at and at != r["title"]:
                    r["title_ar"] = at
                if not r["poster"]:
                    r["poster"] = img(a.get("poster_path"), "w342")
            out.append(r)
        for x in ar_res:
            k = (x["media_type"], x["id"])
            if k in seen:
                continue
            seen.add(k)
            out.append(_row(x, x))
        return jsonify(results=out[:30])
    except Exception as e:
        return jsonify(error=str(e)), 500


# ----------------- بحث ذكي: وصف مشهد / موضوع / صورة من فيلم -----------------
# بيستخدم نفس AI_KEY بتاع الترجمة (Gemini أو Claude) ويحوّل التخمينات لأعمال حقيقية من TMDB
AIF_SYS = (
    "You are an expert film and TV historian helping people identify movies and series. "
    "The user either describes a scene, plot, character, quote or topic (Arabic, Egyptian dialect or English), "
    "or sends a still frame from a film or show. Identify the work(s). "
    "For an image, rely on the scene, setting, props, costumes, composition, visible text and art style; "
    "never name real people from their faces. "
    "Return ONLY JSON: {\"guesses\":[{\"title\":\"English/international title as listed on TMDB\","
    "\"title_ar\":\"Arabic title if it is an Arabic work or you know it, else empty\","
    "\"year\":2010,\"type\":\"movie or tv\",\"why\":\"very short Arabic reason, max 14 words\"}]}. "
    "Order by confidence, best first, 1 to 6 guesses. "
    "If the user asks about a topic or genre (e.g. films about time travel) return the 6 best-matching well-known works. "
    "Never invent titles. If you cannot identify anything return {\"guesses\":[]}. No markdown, no explanations."
)


def _aif_call(text, img_b64, mime):
    text = text or "حدد العمل اللي في الصورة دي."
    if AI_GEMINI:
        parts = []
        if img_b64:
            parts.append({"inlineData": {"mimeType": mime, "data": img_b64}})
        parts.append({"text": text})
        fb = [x.strip() for x in (os.environ.get("AI_MODEL_FALLBACK") or "gemini-3.5-flash-lite,gemini-3.5-flash").split(",") if x.strip()]
        chain = [AI_MODEL, AI_MODEL] + [m for m in fb if m != AI_MODEL]
        last = "فشل الاتصال بالذكاء الاصطناعي"
        first = ""
        for n, mdl in enumerate(chain):
            gc = {"temperature": 0.2, "maxOutputTokens": 3000, "responseMimeType": "application/json"}
            if "2.5" in mdl and "flash" in mdl and "lite" not in mdl:
                gc["thinkingConfig"] = {"thinkingBudget": 1024}
            try:
                r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % mdl,
                                  timeout=(20 if n < 2 else 12), headers={"x-goog-api-key": AI_KEY, "content-type": "application/json"},
                                  json={"systemInstruction": {"parts": [{"text": AIF_SYS}]},
                                        "contents": [{"role": "user", "parts": parts}],
                                        "generationConfig": gc})
                d = r.json()
            except Exception as e:
                last = str(e)[:160]
                continue
            if r.status_code == 200:
                cs = d.get("candidates") or [{}]
                return "".join(x.get("text", "") for x in ((cs[0].get("content") or {}).get("parts") or []))
            last = str((d.get("error") or {}).get("message") or r.status_code)[:160]
            if n == 0:
                first = "%s: %s" % (mdl, last)
            if r.status_code in (400, 401, 403) and "model" not in last.lower():
                break                      # مفتاح/طلب غلط: مفيش فايدة من إعادة المحاولة
            if n == 0:
                time.sleep(1.5)
        raise Exception((first + " | " + last) if first and first != last else last)
    content = []
    if img_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}})
    content.append({"type": "text", "text": text})
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                      headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": AI_MODEL, "max_tokens": 900, "temperature": 0.2,
                            "system": AIF_SYS, "messages": [{"role": "user", "content": content}]})
    d = r.json()
    if r.status_code != 200:
        raise Exception(str((d.get("error") or {}).get("message") or r.status_code)[:160])
    return "".join(x.get("text", "") for x in d.get("content", []) if x.get("type") == "text")


def _aif_n(t):
    return re.sub(r"[\W_]+", "", (t or "").lower())


def _aif_resolve(gs):
    """تخمين من الذكاء الاصطناعي -> عمل حقيقي على TMDB (id + بوستر + اسم عربي)"""
    t = str(gs.get("title") or "").strip()
    ta = str(gs.get("title_ar") or "").strip()
    typ = gs.get("type") if gs.get("type") in ("movie", "tv") else ""
    try:
        yr = int(str(gs.get("year") or "")[:4])
    except Exception:
        yr = 0
    cands = []
    for qq, lang in ((t, "en-US"), (ta, "ar")):
        if not qq:
            continue
        try:
            cands = _tmdb_multi(qq, lang)
        except Exception:
            cands = []
        if cands:
            break
    if not cands:
        return None

    def score(x):
        sc = 0.0
        if typ and x.get("media_type") == typ:
            sc += 3
        d = (x.get("release_date") or x.get("first_air_date") or "")[:4]
        if yr and d.isdigit():
            diff = abs(int(d) - yr)
            sc += 4 if diff == 0 else (2 if diff == 1 else 0)
        nm = _aif_n(x.get("title") or x.get("name") or "")
        nm2 = _aif_n(x.get("original_title") or x.get("original_name") or "")
        if _aif_n(t) and _aif_n(t) in (nm, nm2):
            sc += 2
        sc += min(float(x.get("popularity") or 0), 60) / 100.0
        return sc
    best = max(cands[:8], key=score)
    date_s = best.get("release_date") or best.get("first_air_date") or ""
    row = {"id": best["id"], "type": best["media_type"], "year": date_s[:4],
           "title": best.get("title") or best.get("name") or t,
           "title_en": best.get("title") or best.get("name") or t,
           "poster": img(best.get("poster_path"), "w342"),
           "why": str(gs.get("why") or "")[:140]}
    try:
        a = tmdb_get("/%s/%s" % (best["media_type"], best["id"]), language="ar")
        at = (a.get("title") or a.get("name") or "").strip()
        if at:
            row["title"] = at
        if not row["poster"]:
            row["poster"] = img(a.get("poster_path"), "w342")
    except Exception:
        pass
    return row


@app.get("/ai/test")
def ai_test():
    if g.role != "admin":
        return jsonify(error="للأدمن بس"), 403
    k = AI_KEY or ""
    info = {"provider": "gemini" if AI_GEMINI else "claude", "model": AI_MODEL,
            "key_set": bool(k), "key_len": len(k),
            "key_mask": (k[:4] + "…" + k[-2:]) if len(k) > 8 else "",
            "ai_provider_env": os.environ.get("AI_PROVIDER", ""),
            "tmdb": bool(TMDB_KEY)}
    if not k:
        info["ok"] = False
        info["msg"] = "مفيش مفتاح على السيرفر (AI_KEY فاضي)"
        return jsonify(info)
    try:
        t = _aif_call("ping: reply with {\"guesses\":[]}", "", "")
        info["ok"] = True
        info["msg"] = "المفتاح شغال ✅"
        info["reply"] = (t or "")[:80]
    except Exception as e:
        info["ok"] = False
        info["msg"] = str(e)[:200]
    return jsonify(info)


@app.post("/ai/find")
def ai_find():
    if not AI_KEY:
        return jsonify(error="البحث الذكي محتاج AI_KEY على السيرفر (مفتاح Gemini أو Claude)"), 400
    if not TMDB_KEY:
        return jsonify(error="TMDB_KEY مش متضاف على السيرفر"), 500
    if g.role != "admin" and rl_hit("aif:" + g.user, 40, 3600):
        return jsonify(error="كتر البحث الذكي شوية. جرب بعد شوية."), 429
    b = request.get_json(silent=True) or {}
    q = str(b.get("q") or "").strip()[:700]
    im = str(b.get("image") or "")
    img_b64, mime = "", ""
    if im:
        m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$", im)
        if not m or len(im) > 7_000_000:
            return jsonify(error="الصورة مش مقبولة (jpg/png/webp وأقل من 5 ميجا)"), 400
        mime, img_b64 = m.group(1), m.group(2)
    if not q and not img_b64:
        return jsonify(error="اكتب وصف المشهد أو ارفع صورة"), 400
    try:
        raw = _aif_call(q, img_b64, mime)
    except Exception as e:
        return jsonify(error="الذكاء الاصطناعي رفض الطلب: %s" % e), 502
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    try:
        gl = (json.loads(raw) or {}).get("guesses") or []
    except Exception:
        mm = re.search(r"\{.*\}", raw, re.S)
        try:
            gl = (json.loads(mm.group(0)) or {}).get("guesses") if mm else []
        except Exception:
            gl = []
    gl = [x for x in gl if isinstance(x, dict)][:6]
    out, seen = [], set()
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(_aif_resolve, gl):
            if not r:
                continue
            k = (r["type"], r["id"])
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
    return jsonify(results=out, guessed=[str(x.get("title") or "") for x in gl])


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
           "without_genres": "99", "primary_release_date.gte": "d:-365",
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
            lang=a.get("original_language") or "",
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
        u.startswith("/subtitle/") and len(u) < (1500 if u.startswith("/subtitle/os3~") else 300)
        and ".." not in u and not re.search(r"[\s\\]", u)
    )


# ===== مصادر ترجمة احتياطية مجانية (غير SubDL) =====
# 1) OpenSubtitles عن طريق إضافة Stremio: من غير مفتاح ولا حد يومي.
# 2) Wyzie Subs: بيجمع ترجمات من مصادر كتير؛ محتاج مفتاح مجاني (WYZIE_KEY في Variables، من store.wyzie.io/redeem). لو مفيش مفتاح بيتخطّى.
# بتتضاف جنب نتايج SubDL، ولما SubDL يوصل لحد التحميل اليومي بتبقى هي المصدر الوحيد لمدة ساعة.
OS3_BASE = "https://opensubtitles-v3.strem.io"
WYZIE_BASE = os.environ.get("WYZIE_BASE", "https://sub.wyzie.io").rstrip("/")
WYZIE_KEY = os.environ.get("WYZIE_KEY", "")
EXT_PREFIX = "/subtitle/os3~"
SUBDL_BLOCK = {"until": 0.0}
_IMDB_CACHE = {}


def _ext_u(url):
    """مسار موقّع لرابط ترجمة خارجي: السيرفر بيقبل بس الروابط اللي هو نفسه طلّعها."""
    tok = _b64e(url.encode())
    return EXT_PREFIX + tok + "." + sign("os3|" + tok)[:24]


def _ext_url(u):
    tok, sig = u[len(EXT_PREFIX):].rsplit(".", 1)
    if not hmac.compare_digest(sig, sign("os3|" + tok)[:24]):
        raise Exception("رابط ترجمة غير صالح")
    url = _b64d(tok).decode("utf-8", "replace")
    if not url.startswith("https://") or not _as_safe_url(url):
        raise Exception("رابط ترجمة غير مسموح")
    return url


def _imdb_of(typ, tid):
    k = (typ, tid)
    if k not in _IMDB_CACHE:
        _IMDB_CACHE[k] = (tmdb_get("/%s/%s/external_ids" % (typ, tid)) or {}).get("imdb_id") or ""
    return _IMDB_CACHE[k]


def os3_items(typ, tid, s, e, lang):
    want = {"AR": "ara", "EN": "eng"}.get(str(lang).split(",")[0].upper())
    if not want or typ not in ("movie", "tv") or not str(tid).isdigit():
        return []
    imdb = _imdb_of(typ, tid)
    if not imdb:
        return []
    kind = "series" if typ == "tv" else "movie"
    vid = imdb + (":%s:%s" % (s, e) if kind == "series" and s and e else "")
    r = requests.get("%s/subtitles/%s/%s.json" % (OS3_BASE, kind, vid), timeout=15)
    if r.status_code != 200:
        return []
    out = []
    for x in (r.json() or {}).get("subtitles") or []:
        if str(x.get("lang", "")).lower() not in (want, want[:2]):
            continue
        url = str(x.get("url") or "")
        if not url.startswith("https://"):
            continue
        out.append({"u": _ext_u(url), "name": "OpenSubtitles " + str(x.get("id") or len(out) + 1),
                    "lang": "AR" if want == "ara" else "EN", "author": "OpenSubtitles", "season": s or None,
                    "episode": e or None, "full": False})
        if len(out) >= 15:
            break
    return out


def wyzie_items(typ, tid, s, e, lang):
    lg = str(lang).split(",")[0].lower()
    if not WYZIE_KEY or lg not in ("ar", "en") or typ not in ("movie", "tv") or not str(tid).isdigit():
        return []
    prm = {"id": tid, "language": lg, "format": "srt", "key": WYZIE_KEY}
    if typ == "tv" and s and e:
        prm.update(season=s, episode=e)
    r = requests.get(WYZIE_BASE + "/search", params=prm, timeout=20)
    if r.status_code != 200:
        return []
    data = r.json()
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for x in data:
        url = str((x or {}).get("url") or "")
        if not url.startswith("https://") or url in seen:
            continue
        seen.add(url)
        nm = x.get("release") or x.get("display") or x.get("source") or "Wyzie"
        if x.get("isHearingImpaired"):
            nm = str(nm) + " (SDH)"
        out.append({"u": _ext_u(url), "name": "Wyzie · " + str(nm)[:80], "lang": lg.upper(),
                    "author": str(x.get("source") or "Wyzie"), "season": s or None, "episode": e or None,
                    "full": False})
        if len(out) >= 8:
            break
    return out


_TI_CACHE = {}
POD_BASE = "https://www.podnapisi.net"
YIFY_BASE = os.environ.get("YIFY_SUBS_BASE", "https://yifysubtitles.ch").rstrip("/")
SUB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _title_year(typ, tid):
    k = (typ, tid)
    if k not in _TI_CACHE:
        d = tmdb_get("/%s/%s" % (typ, tid), language="en-US")
        _TI_CACHE[k] = (d.get("title") or d.get("name") or "",
                        (d.get("release_date") or d.get("first_air_date") or "")[:4])
    return _TI_CACHE[k]


def podnapisi_items(typ, tid, s, e, lang):
    """Podnapisi: مصدر مجاني من غير مفتاح وفيه ترجمات عربي كتير (أفلام ومسلسلات)."""
    lg = str(lang).split(",")[0].lower()
    if lg not in ("ar", "en") or typ not in ("movie", "tv") or not str(tid).isdigit():
        return []
    title, year = _title_year(typ, tid)
    if not title:
        return []
    prm = {"keywords": title, "language": lg, "movie_type": "movie" if typ == "movie" else "tv-series"}
    if typ == "movie" and year:
        prm["year"] = year
    if typ == "tv" and s and e:
        prm.update(seasons=s, episodes=e)
    r = requests.get(POD_BASE + "/subtitles/search/advanced", params=prm, timeout=20,
                     headers=dict(SUB_UA, Accept="application/json"))
    if r.status_code != 200:
        return []
    out, seen = [], set()
    for x in (r.json() or {}).get("data") or []:
        pid = str((x or {}).get("id") or "")
        if not pid or pid in seen or str(x.get("language", lg)).lower() != lg:
            continue
        seen.add(pid)
        rels = x.get("releases") or []
        nm = (rels[0] if rels else x.get("title")) or "Podnapisi"
        out.append({"u": _ext_u("%s/subtitles/%s/download" % (POD_BASE, pid)),
                    "name": "Podnapisi \u00b7 " + str(nm)[:80], "lang": lg.upper(), "author": "Podnapisi",
                    "season": s or None, "episode": e or None, "full": False})
        if len(out) >= 15:
            break
    return out


def yify_items(typ, tid, s, e, lang):
    """YIFY Subtitles: ترجمات عربي للأفلام بس، مرتبة حسب تقييم المستخدمين."""
    if typ != "movie" or str(lang).split(",")[0].lower() != "ar" or not str(tid).isdigit():
        return []
    imdb = _imdb_of(typ, tid)
    if not imdb:
        return []
    r = requests.get("%s/movie-imdb/%s" % (YIFY_BASE, imdb), timeout=15, headers=SUB_UA)
    if r.status_code != 200:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
        if "arabic" not in tr.lower():
            continue
        hm = re.search(r'href="(/subtitles/[^"]+)"', tr)
        if not hm:
            continue
        rm = re.search(r"label[^>]*>\s*(-?\d+)", tr)
        am = re.search(r'<a[^>]*href="/subtitles/[^"]+"[^>]*>(.*?)</a>', tr, re.S)
        nm = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", am.group(1) if am else "")).strip()
        nm = re.sub(r"^subtitle\s+", "", nm, flags=re.I)
        rows.append((int(rm.group(1)) if rm else 0, hm.group(1), nm))
    rows.sort(key=lambda x: -x[0])
    return [{"u": _ext_u(YIFY_BASE + h.replace("/subtitles/", "/subtitle/", 1) + ".zip"),
             "name": "YIFY \u00b7 " + (nm or "Arabic")[:80], "lang": "AR", "author": "YIFY",
             "season": None, "episode": None, "full": False} for _, h, nm in rows[:10]]


def extra_sources(typ, tid, s, e, lang):
    """كل المصادر المجانية الإضافية بالتوازي، مدموجة بالتبادل (واحدة من كل مصدر)."""
    from concurrent.futures import ThreadPoolExecutor
    fns = [os3_items, podnapisi_items, yify_items] + ([wyzie_items] if WYZIE_KEY else [])
    res = []

    def run(f):
        try:
            return f(typ, tid, s, e, lang)
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        res = list(ex.map(run, fns))
    out = []
    for i in range(max(len(r) for r in res) if res else 0):
        for r in res:
            if i < len(r):
                out.append(r[i])
    return out


def get_zip(u):
    if u in ZIP_CACHE:
        return ZIP_CACHE[u]
    if u.startswith(EXT_PREFIX):
        url = _ext_url(u)
        r = requests.get(url, timeout=30, headers=SUB_UA)
        if r.status_code != 200:
            raise Exception("تحميل الترجمة فشل (%s)" % r.status_code)
        data = r.content
        if data[:2] == b"\x1f\x8b":
            import gzip
            data = gzip.decompress(data)
        if len(data) > 6000000:
            raise Exception("ملف الترجمة كبير زيادة")
        ZIP_CACHE[u] = data
        return data
    r = None
    for attempt in range(3):
        try:
            r = requests.get(SUBDL_DL + u, timeout=30)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            break
        if r is not None and r.status_code in (403, 404):
            break
        time.sleep(0.6 * (attempt + 1))
    if r is not None and r.status_code in (402, 403, 429):
        SUBDL_BLOCK["until"] = time.time() + 3600
        raise Exception("حد تحميلات SubDL اليومي خلص (الخطة المجانية 50 تحميل/يوم) — بيتجدد 2 الفجر، أو رقّي الخطة")
    if r is None or r.status_code != 200:
        raise Exception(f"تحميل الترجمة فشل ({r.status_code if r is not None else 'مفيش اتصال'})")
    if r.content[:1] == b"{":                 # رد JSON بدل ملف: غالباً رسالة حد التحميل
        try:
            m = str((r.json() or {}).get("error") or (r.json() or {}).get("message") or "")
        except Exception:
            m = ""
        SUBDL_BLOCK["until"] = time.time() + 3600
        raise Exception("SubDL رفض التحميل: " + (m[:100] or "حد التحميل اليومي غالباً خلص"))
    if len(r.content) > 6000000:
        raise Exception("ملف الترجمة كبير زيادة")
    if len(ZIP_CACHE) >= 20:
        ZIP_CACHE.pop(next(iter(ZIP_CACHE)))
    ZIP_CACHE[u] = r.content
    return r.content


_SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".txt")


def _sub_entries(z, depth=0):
    """كل ملفات الترجمة جوه الأرشيف (بما فيها أرشيف جوه أرشيف). كل عنصر: (الاسم, الحجم, دالة بترجّع البايتات)"""
    out = []
    for i in z.infolist():
        if i.is_dir():
            continue
        low = i.filename.lower()
        if low.endswith(_SUB_EXTS) and i.file_size < 10000000 and "__macosx" not in low:
            out.append((i.filename, i.file_size, (lambda z=z, i=i: z.read(i))))
        elif low.endswith(".zip") and depth < 2 and i.file_size < 10000000:
            try:
                out += _sub_entries(zipfile.ZipFile(io.BytesIO(z.read(i))), depth + 1)
            except Exception:
                pass
    return out


def pick_sub(z, season, episode):
    names = _sub_entries(z)
    if not names:
        return None
    if episode is not None:
        hits = []
        for n in names:
            s, e = parse_ep(n[0])
            if e == episode and (season is None or s is None or s == season):
                hits.append(n)
        if not hits:
            pat = re.compile(r"(?<!\d)0*%d(?!\d)" % episode)
            hits = [n for n in names if pat.search(os.path.basename(n[0]))]
        if hits:
            names = hits
    pref = {".srt": 0, ".vtt": 1, ".ass": 2, ".ssa": 3, ".txt": 4}
    names.sort(key=lambda n: (pref.get(os.path.splitext(n[0].lower())[1], 9), -n[1]))
    return names[0]


def _raw_path(u, season, episode):
    k = hashlib.sha1(("%s|%s|%s" % (u, season, episode)).encode()).hexdigest()[:32]
    return os.path.join(DATA_DIR, "subraw_%s.json" % k)


def build_sub(u, season, episode, ar, fmt="vtt", shift=0.0, scale=1.0, ai=None):
    # كاش دائم لنص الترجمة: نفس الترجمة ما تتحمّلش من SubDL تاني (الخطة المجانية 50 تحميل/يوم بس)
    raw = ext = None
    try:
        c = store_load(_raw_path(u, season, episode))
        raw, ext = base64.b64decode(c["b64"]), c["ext"]
    except Exception:
        raw = None
    if raw is None:
        data = get_zip(u)
        if data[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(data))
            info = pick_sub(z, season, episode)
            if not info:
                raise LookupError("مفيش ملف ترجمة مدعوم جوه الأرشيف")
            raw = info[2]()
            ext = os.path.splitext(info[0].lower())[1]
        elif data[:4] == b"Rar!" or data[:2] == b"7z":
            raise LookupError("الأرشيف مضغوط بصيغة RAR/7z مش مدعومة")
        else:
            raw = data
            ext = ".ass" if b"[Script Info]" in raw[:3000] else ".srt"
        if len(raw) < 600000 and (b"-->" in raw or b"[Events]" in raw):
            try:
                store_save(_raw_path(u, season, episode), {"ext": ext, "b64": base64.b64encode(raw).decode()})
            except Exception:
                pass
    text = decode_sub(raw, ar)
    if "-->" not in text and "[Events]" not in text:
        raise LookupError("الملف مش ترجمة مفهومة")
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


SUBDL_CACHE = {}


def subdl_call(params):
    """بحث SubDL مع كاش وإعادة محاولة. قبل كده أي رفض مؤقت (حد الاستخدام مثلاً) كان بيتفسّر «مفيش ترجمة»
    فكل الأفلام تبان من غير ترجمة عربي. دلوقتي بنعيد المحاولة، وبنستخدم الكاش، ولو فشل فعلاً بنرجّع خطأ واضح."""
    key = hashlib.sha1(json.dumps({k: v for k, v in params.items() if k != "api_key"},
                                  sort_keys=True).encode()).hexdigest()[:32]
    now = time.time()
    c = SUBDL_CACHE.get(key)
    if c and now - c[0] < 6 * 3600:
        return c[1]
    path = os.path.join(DATA_DIR, "subdl_%s.json" % key)
    stale = None
    try:
        d = store_load(path)
        if isinstance(d, dict) and (d.get("res") or {}).get("subtitles"):
            stale = d["res"]
            if now - d.get("t", 0) < 24 * 3600:
                SUBDL_CACHE[key] = (now, stale)
                return stale
    except Exception:
        pass
    last = ""
    for attempt in range(3):
        try:
            r = requests.get(SUBDL_API, params=params, timeout=20)
        except requests.RequestException as ex:
            last = str(ex)[:80]
            time.sleep(0.5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = "SubDL رد بكود %s" % r.status_code
            time.sleep(0.7 * (attempt + 1))
            continue
        res = jj(r)
        if res.get("status") is False and not res.get("subtitles"):
            msg = str(res.get("error") or "")
            low = msg.lower()
            if any(w in low for w in ("limit", "quota", "too many", "rate", "wait")):
                last = msg[:80] or "حد الاستخدام"
                time.sleep(0.7 * (attempt + 1))
                continue
            if "key" in low or "api" in low:
                raise Exception(msg)
            return {}                       # مفيش ترجمات فعلاً
        if res.get("subtitles"):
            SUBDL_CACHE[key] = (now, res)
            if len(SUBDL_CACHE) > 300:
                SUBDL_CACHE.pop(next(iter(SUBDL_CACHE)))
            try:
                store_save(path, {"t": now, "res": res})
            except Exception:
                pass
        return res
    if stale:                               # SubDL واقع: نستخدم آخر نتيجة محفوظة بدل ما نقول «مفيش ترجمة»
        return stale
    raise Exception("SubDL مش بيرد دلوقتي (" + last + ")، جرّب بعد شوية")


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


# ===== تنضيف اسم الإصدار (تشغيل من لينك/ماجنت) عشان الترجمة تتلاقي زي التشغيل من صفحة الفيلم =====
_REL_EXT = re.compile(r"\.(mkv|mp4|avi|m4v|mov|wmv|ts|m2ts|webm)$", re.I)
_REL_MARK = re.compile(
    r"\b(2160p|1440p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|blu ray|bdrip|brrip|bd ?remux|remux|web ?dl|web ?rip|webrip|"
    r"hdtv|pdtv|dvdrip|dvdscr|hdrip|hdcam|cam|x264|x265|h 264|h 265|h264|h265|hevc|avc|xvid|10bit|hdr10|hdr|dolby vision|"
    r"amzn|nf|dsnp|hmax|atvp|hulu|ddp ?\d|dd ?\d|dts|truehd|atmos|aac|ac3|eac3|multi|dual audio|extended|unrated|proper|repack|imax)\b",
    re.I)


def _guess_release(raw):
    """اسم ملف/رابط (Movie.2023.1080p.WEB-DL.x264-GRP.mkv) -> (عنوان نضيف, سنة, موسم, حلقة, هل كان اسم إصدار؟)"""
    src = str(raw or "").strip()
    t = _REL_EXT.sub("", re.sub(r"https?://\S+/", " ", src))
    t = re.sub(r"[_.]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    m_se = re.search(r"\bS(\d{1,2}) ?E(\d{1,3})\b", t, re.I) or re.search(r"\b(\d{1,2})x(\d{2,3})\b", t)
    season = int(m_se.group(1)) if m_se else None
    episode = int(m_se.group(2)) if m_se else None
    mk = _REL_MARK.search(t)
    cut = len(t)
    if m_se:
        cut = min(cut, m_se.start())
    if mk:
        cut = min(cut, mk.start())
    years = [m for m in re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", t) if m.start() > 2 and m.start() < cut + 1]
    year = ""
    if years:
        y = years[-1]
        year = y.group(1)
        cut = min(cut, y.start())
    title = t[:cut]
    title = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—:,([{")
    dirty = bool(m_se or mk or year or _REL_EXT.search(src) or src.count(".") >= 3 or src.count("_") >= 2)
    return title, year, season, episode, dirty


_TMDB_RES = {}


def _tmdb_resolve(title, year, typ):
    """بيجيب (النوع, id) من TMDB لاسم نضيف عشان الترجمة تتدوّر بالـid بدل اسم الملف الخام"""
    key = (title.lower(), str(year or ""), typ or "")
    if key in _TMDB_RES:
        return _TMDB_RES[key]
    out = None
    try:
        kinds = [("tv", "/search/tv")] if typ == "tv" else [("movie", "/search/movie")] if typ == "movie" \
            else [("movie", "/search/movie"), ("tv", "/search/tv")]
        best = None
        for kind, path in kinds:
            prm = {"query": title, "language": "en-US", "include_adult": "false"}
            ykey = "year" if kind == "movie" else "first_air_date_year"
            if year:
                prm[ykey] = year
            res = tmdb_get(path, **prm).get("results", [])
            if not res and year:
                prm.pop(ykey, None)
                res = tmdb_get(path, **prm).get("results", [])
            if res:
                pop = res[0].get("popularity") or 0
                if best is None or pop > best[2]:
                    best = (kind, res[0]["id"], pop)
        out = (best[0], best[1]) if best else None
    except Exception:
        out = None
    if len(_TMDB_RES) > 500:
        _TMDB_RES.clear()
    _TMDB_RES[key] = out
    return out


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
    year = ""
    # تشغيل من لينك/ماجنت: مفيش TMDB id واسم الملف خام (Movie.2023.1080p.WEB-DL...mkv) فالبحث كان بيرجّع صفر.
    # بننضّف الاسم ونجيب الـid من TMDB، وده بيفعّل كل مصادر الترجمة زي التشغيل من صفحة الفيلم
    if q and not (tid.isdigit() and typ in ("movie", "tv")):
        try:
            gt, gy, gs, ge, dirty = _guess_release(q)
        except Exception:
            gt, gy, gs, ge, dirty = "", "", None, None, False
        if dirty and gt:
            q, year = gt, gy
            if gs is not None:
                if not s.isdigit():
                    s, e = str(gs), str(ge)
                if typ not in ("movie", "tv"):
                    typ = "tv"
            rid = _tmdb_resolve(gt, gy, typ if typ in ("movie", "tv") else "")
            if rid:
                typ, tid = rid[0], str(rid[1])
    g._sub_se = (s, e)
    base = {"api_key": SUBDL_KEY, "languages": lang}
    if tid.isdigit() and typ in ("movie", "tv"):
        base["tmdb_id"] = tid
        base["type"] = typ
    elif q:
        base["film_name"] = q
        if year and typ != "tv":
            base["year"] = year
        if typ in ("movie", "tv"):
            base["type"] = typ
    else:
        return jsonify(error="بيانات ناقصة"), 400
    is_tv = base.get("type") == "tv"
    s_i = int(s) if s.isdigit() else None
    e_i = int(e) if e.isdigit() else None
    items, mode, err = [], "none", None
    blocked = time.time() < SUBDL_BLOCK["until"]
    if not blocked:
        try:
            items, mode = subdl_search(base, s_i, e_i, is_tv)
            if not items and base.get("year"):      # السنة ممكن تكون غلط في اسم الملف: نجرّب من غيرها
                items, mode = subdl_search({k: v for k, v in base.items() if k != "year"}, s_i, e_i, is_tv)
        except requests.Timeout:
            err = "SubDL بطيء، جرّب تاني"
        except Exception as ex:
            err = f"الترجمة: {ex}"
    extra = []
    if tid.isdigit() and typ in ("movie", "tv"):
        try:
            extra = extra_sources(typ, tid, s, e, lang)
        except Exception:
            extra = []
    if blocked:
        items = extra
        mode = "extra" if extra else "none"
    elif extra:
        # أول نتيجة SubDL، وبعدها أحسن نتيجة OpenSubtitles (احتياطي لو SubDL فشل في التحميل)، وبعدين الباقي
        items = items[:1] + extra[:1] + items[1:] + extra[1:]
        mode = mode if mode != "none" else "extra"
    if err and not items:
        return jsonify(error=err), 500
    return jsonify(items=items[:60], mode=mode)


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
            if not _ip_public(ipaddress.ip_address(i[4][0].split("%")[0])):
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


# ===================== حفظ ضبط الترجمة اليدوي لكل الأعضاء =====================
# كل عضو يظبط توقيت الترجمة بالأسهم/الأزرار بيتحفظ ضبطه على حسابه هو بس، ولما يفتح نفس الفيلم تاني بتفتح له متظبطة. محدش بيأثر على غيره.
def _sf_args(b):
    u = str(b.get("u", ""))
    se = "" if b.get("s") in (None, "") else str(b.get("s"))[:6]
    ep = "" if b.get("e") in (None, "") else str(b.get("e"))[:6]
    fk = str(b.get("fkey") or b.get("key") or re.sub(r"[?#].*$", "", str(b.get("src", ""))))[:300]
    k = hashlib.sha1("|".join((str(g.user), u, se, ep, fk)).encode()).hexdigest()[:32]   # الضبط لكل عضو لوحده
    return os.path.join(DATA_DIR, "subfix_%s.json" % k)


@app.post("/sub/fix")
def sub_fix_save():
    b = request.get_json(silent=True) or {}
    if not ok_sub_path(str(b.get("u", ""))):
        return jsonify(error="رابط ترجمة غير صالح"), 400
    p = _sf_args(b)
    if b.get("clear"):
        try:
            store_del(p)
        except Exception:
            pass
        return jsonify(ok=True, cleared=True)
    try:
        sh = float(b.get("shift"))
        sc = float(b.get("scale") or 1)
    except (TypeError, ValueError):
        return jsonify(error="قيمة غير صالحة"), 400
    if not (math.isfinite(sh) and math.isfinite(sc) and abs(sh) <= 3600 and 0.9 <= sc <= 1.1):
        return jsonify(error="قيمة غير منطقية"), 400
    try:
        store_save(p, {"ok": True, "shift": round(sh, 2), "scale": round(sc, 6), "manual": True,
                       "by": g.user, "t": int(time.time())})
    except Exception:
        return jsonify(error="تعذّر الحفظ"), 500
    return jsonify(ok=True)


@app.post("/sub/auto")
def sub_auto_start():
    b = request.get_json(silent=True) or {}
    u, src = str(b.get("u", "")), str(b.get("src", ""))
    if not ok_sub_path(u):
        return jsonify(error="رابط ترجمة غير صالح"), 400
    if not b.get("nofix"):          # ضبط يدوي محفوظ من أي عضو؟ بيتقدّم على الضبط التلقائي
        try:
            fx = store_load(_sf_args(b))
            return jsonify(done=True, ok=True, shift=fx.get("shift", 0), scale=fx.get("scale", 1), manual=True)
        except Exception:
            pass
    if b.get("fixonly"):
        return jsonify(done=True, ok=False, reason="nofix")
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


# ===================== مشغّل الأندرويد الداخلي: تذكرة موقّعة بدل تسجيل الدخول =====================
# المشغّل الداخلي (LibVLC) مالوش توكن دخول، فالصفحة بتديله تذكرة موقّعة (12 ساعة) يقدر بيها يجيب قايمة الترجمات
# العربي الأقرب للملف، ويظبطها بالصوت، ويحفظ ضبطك على حسابك: نفس نظام المتصفح بالظبط.
SUBT_TTL = 12 * 3600


def _subt_make(user):
    tok = _b64e(json.dumps({"u": str(user), "exp": int(time.time()) + SUBT_TTL}, separators=(",", ":")).encode())
    return tok + "." + sign("subt|" + tok)[:40]


def _subt_user(tk):
    try:
        tok, sig = str(tk).rsplit(".", 1)
        if not hmac.compare_digest(sig, sign("subt|" + tok)[:40]):
            return None
        p = json.loads(_b64d(tok))
        if int(p["exp"]) < time.time():
            return None
        return str(p["u"])
    except Exception:
        return None


def _public_base():
    proto = request.headers.get("X-Forwarded-Proto", "https").split(",")[0].strip()
    return (os.environ.get("PUBLIC_URL") or f"{proto}://{request.host}").rstrip("/")


def _subx_url(u, s, e, lang, fmt="srt"):
    p = {"u": u, "shift": 0, "scale": 1, "s": str(s or "")[:6], "e": str(e or "")[:6],
         "lang": str(lang or "")[:20], "ai": "", "exp": int(time.time()) + SUBX_TTL, "f": fmt}
    tok = _b64e(json.dumps(p, separators=(",", ":")).encode())
    return f"{_public_base()}/subx/{tok}.{sign('subx|' + tok)[:40]}/sub.{fmt}"


def _rel_tok(s):
    s = re.sub(r"\.[a-z0-9]{2,4}$", "", str(s or "").lower())
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def _rank_subs(items, fname):
    """نفس ترتيب المتصفح: الأقرب لاسم الملف (إصدار/جروب) الأول، والموسم الكامل في الآخر"""
    have = set(_rel_tok(fname))
    gm = re.search(r"-([a-z0-9]+)(?:\.[a-z0-9]{2,4})?$", str(fname).strip(), re.I)
    grp = gm.group(1).lower() if gm else ""
    for it in items:
        toks = _rel_tok(it.get("name"))
        sc = sum((2 if len(t) > 3 else 1) for t in toks if t in have)
        if grp and grp in toks:
            sc += 6
        if it.get("full"):
            sc -= 3
        it["_sc"] = sc
    return [x for _, x in sorted(enumerate(items), key=lambda p: (-p[1]["_sc"], p[0]))]


@app.get("/sub/ticket")
def sub_ticket():
    return jsonify(tk=_subt_make(g.user), base=_public_base(), ttl=SUBT_TTL)


@app.get("/subx/pack")
def subx_pack():
    who = _subt_user(request.args.get("tk", ""))
    if not who:
        return jsonify(error="forbidden"), 403
    g.user, g.role = who, "member"
    r = subs()
    if isinstance(r, tuple):
        return r
    d = r.get_json() or {}
    se, ep = getattr(g, "_sub_se", None) or (request.args.get("s", ""), request.args.get("e", ""))
    items = _rank_subs(d.get("items") or [], request.args.get("fname", ""))[:8]
    out = [{"n": x["name"], "a": x.get("author", ""), "u": x["u"], "l": x.get("lang", ""), "f": bool(x.get("full")),
            "url": _subx_url(x["u"], se, ep, x.get("lang") or "AR")} for x in items]
    return jsonify(items=out, mode=d.get("mode", ""))


@app.route("/subx/auto", methods=["POST", "GET"])
def subx_auto():
    b = request.get_json(silent=True) or {}
    who = _subt_user(request.args.get("tk") or b.get("tk") or "")
    if not who:
        return jsonify(error="forbidden"), 403
    g.user, g.role = who, "member"
    if request.method == "GET":
        return sub_auto_poll()
    lk, msg = lock_of(who)
    if lk >= 2:
        return jsonify(error=msg, nodl=True, lock=2), 403
    return sub_auto_start()


@app.post("/subx/fix")
def subx_fix():
    b = request.get_json(silent=True) or {}
    who = _subt_user(request.args.get("tk") or b.get("tk") or "")
    if not who:
        return jsonify(error="forbidden"), 403
    g.user, g.role = who, "member"
    return sub_fix_save()


# ===================== نهاية ضبط الترجمة بالصوت =====================


# ===================== ترجمة بالذكاء الاصطناعي (لما مفيش ترجمة عربي) =====================
# بياخد أحسن ترجمة إنجليزي مطابقة للفيلم من SubDL ويترجمها عربي بـ Claude، سطر مقابل سطر وبنفس التوقيت،
# وبعدها بتعدّي على الضبط التلقائي بالصوت زي أي ترجمة. محتاج متغيّر AI_KEY (مفتاح Anthropic) على السيرفر.
# اختياري: AI_MODEL (الافتراضي claude-sonnet-5-5).
_GEM_ENV = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_KEY")
            or os.environ.get("GOOGLE_API_KEY") or "").strip().strip("\"'")
AI_KEY = (os.environ.get("AI_KEY") or os.environ.get("ANTHROPIC_API_KEY") or _GEM_ENV or "").strip().strip("\"'")
# لو المفتاح بتاع Gemini (بيبدأ بـ AIza) أو AI_PROVIDER=gemini أو المفتاح جاي من GEMINI_API_KEY بيشتغل بـ Gemini، غير كده Claude
AI_GEMINI = (os.environ.get("AI_PROVIDER", "").strip().lower() in ("gemini", "google")
             or AI_KEY.startswith(("AIza", "AQ.")) or (bool(_GEM_ENV) and AI_KEY == _GEM_ENV)
             or (bool(AI_KEY) and not AI_KEY.startswith("sk-ant")))   # مفاتيح Anthropic كلها بتبدأ بـ sk-ant
AI_MODEL = (os.environ.get("AI_MODEL") or "").strip() or ("gemini-3.8-flash" if AI_GEMINI else "claude-sonnet-5-5")
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
                                                 **({"thinkingConfig": {"thinkingBudget": 0}} if "2.5" in AI_MODEL else {})}})
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
                         {"with_original_language": lg, "without_genres": "16|99"}),
                        (f"⭐ الأعلى تقييماً — {kind} {nm}", ty,
                         {"with_original_language": lg, "without_genres": "16|99",
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
    if k.startswith("hb:"):
        return hb_browse(k[3:], skip)
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
    _h = hero_cached()
    if _h:
        return jsonify(items=_h)
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
        ("movie", "ar", 1.5, "/discover/movie", {"with_original_language": "ar", "without_genres": "99", "primary_release_date.gte": gm, "primary_release_date.lte": tdy}),
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


# ================= هيرو الرئيسية: ترند العالم + متاح أونلاين فقط، وبيتغيّر كل يوم =================
HERO_ROTATE_DAYS = 1       # التشكيلة بتتغيّر كل كام يوم (1 = كل يوم، 2 = كل يومين)
HERO_COUNT = 10            # عدد الشرايح
HERO_EG_SLOTS = 2          # كام شريحة مصرية (فيلم/مسلسل) في الهيرو
HERO_DUB_SLOTS = 2         # كام شريحة كرتون مدبلج مصري
HERO_FIXED_TOP = 3         # أسخن كام عمل ثابتين في التشكيلة (الباقي بيتبدّل)
HERO_MOVIE_MAX_AGE = 420   # الفيلم لازم يكون نزل في آخر كام يوم (يمنع إعادات العرض القديمة زي Endgame)
HERO_TV_ACTIVE_DAYS = 45   # المسلسل لازم يكون عرض حلقة في آخر كام يوم
_HERO_CACHE = {"key": None, "d": None}
_AVAIL_CACHE = {}


def _hero_avail(ty, tid, ar_overview=""):
    """بيرجّع (متاح_أونلاين, overview). الفيلم لازم له نزول ديجيتال/DVD/TV قبل النهاردة (مش سينما بس)."""
    k = (ty, tid)
    h = _AVAIL_CACHE.get(k)
    ttl = 7 * 86400 if (h and h[1]) else 86400
    if h and time.time() - h[0] < ttl:
        return h[1], h[2]
    today = date.today()
    ok, ov = False, ar_overview or ""
    try:
        if ty == "movie":
            d = tmdb_get(f"/movie/{tid}", language="en-US", append_to_response="release_dates")
            ov = ov or (d.get("overview") or "")
            prim = (d.get("release_date") or "")[:10]
            fresh = bool(prim) and 0 <= (today - date.fromisoformat(prim)).days <= HERO_MOVIE_MAX_AGE
            if fresh:
                for r in (d.get("release_dates") or {}).get("results", []):
                    for x in r.get("release_dates", []):
                        if x.get("type") in (4, 5, 6) and (x.get("release_date") or "")[:10] and (x.get("release_date") or "")[:10] <= today.isoformat():
                            ok = True
                            break
                    if ok:
                        break
        else:
            d = tmdb_get(f"/tv/{tid}", language="en-US")
            ov = ov or (d.get("overview") or "")
            la = (d.get("last_air_date") or "")[:10]
            ok = bool(la) and 0 <= (today - date.fromisoformat(la)).days <= HERO_TV_ACTIVE_DAYS
    except Exception:
        ok = False
    _AVAIL_CACHE[k] = (time.time(), ok, ov)
    return ok, ov


def build_hero():
    import random
    today = date.today()
    tdy = today.isoformat()
    jobs = [
        ("/trending/movie/day", {"page": 1}, "movie"), ("/trending/movie/day", {"page": 2}, "movie"),
        ("/trending/tv/day", {"page": 1}, "tv"), ("/trending/tv/day", {"page": 2}, "tv"),
        ("/trending/movie/week", {"page": 1}, "movie"), ("/trending/tv/week", {"page": 1}, "tv"),
        ("/discover/movie", {"with_release_type": "4|5", "region": "US", "sort_by": "popularity.desc",
                             "release_date.gte": (today - timedelta(days=30)).isoformat(), "release_date.lte": tdy,
                             "primary_release_date.gte": (today - timedelta(days=HERO_MOVIE_MAX_AGE)).isoformat()}, "movie"),
        ("/discover/tv", {"sort_by": "popularity.desc", "air_date.gte": (today - timedelta(days=10)).isoformat(),
                          "air_date.lte": tdy}, "tv"),
    ]

    def run(j):
        try:
            return tmdb_get(j[0], language="ar", include_adult="false", **j[1]).get("results", [])
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(run, jobs))
    cand = {}
    for j, lst in zip(jobs, res):
        for rank, x in enumerate(lst):
            ty = j[2]
            if x.get("media_type") in ("movie", "tv"):
                ty = x["media_type"]
            if not x.get("backdrop_path") or not x.get("poster_path"):
                continue
            k = (ty, x["id"])
            sc = (x.get("popularity") or 0) + (25 if "/trending/" in j[0] and "/day" in j[0] else 8 if "/trending/" in j[0] else 0) + max(0, 10 - rank)
            if k not in cand or sc > cand[k][1]:
                cand[k] = (x, sc)
    top = sorted(cand.items(), key=lambda kv: -kv[1][1])[:45]
    with ThreadPoolExecutor(max_workers=8) as ex:
        av = list(ex.map(lambda kv: _hero_avail(kv[0][0], kv[0][1], (kv[1][0].get("overview") or "").strip()), top))
    pool = []
    cands = [(kv, a) for kv, a in zip(top, av) if a[0] and a[1].strip()]
    with ThreadPoolExecutor(max_workers=8) as ex:
        certs = list(ex.map(lambda ca: cert_of(ca[0][0][0], ca[0][0][1]), cands))
    for ((k, (x, sc)), (_, ov)), c in zip(cands, certs):
        if not c["ok"]:
            continue
        d_s = x.get("release_date") or x.get("first_air_date") or ""
        lg = x.get("original_language") or ""
        pool.append({"id": k[1], "type": k[0], "year": d_s[:4], "title": x.get("title") or x.get("name") or "",
                     "rating": round(x.get("vote_average") or 0, 1), "poster": img(x.get("poster_path"), "w342"),
                     "badge": _FLAGS.get(lg, ""), "cl": c["label"], "lv": c["lvl"], "lg": lg, "pop": sc,
                     "overview": ov[:220], "backdrop": img(x["backdrop_path"], "w1280")})
    pool.sort(key=lambda i: -i["pop"])
    pool = [p for p in pool if p["lg"] != "ar"][:26]   # العربي بييجي من مصر بس (تحت)
    rng = random.Random(today.toordinal() // max(1, HERO_ROTATE_DAYS))
    eg = _hero_egypt(rng)
    dub = _hero_dubbed(rng)
    n_foreign = max(3, HERO_COUNT - len(eg) - len(dub))
    fixed = pool[:min(HERO_FIXED_TOP, n_foreign)]
    rest = pool[len(fixed):]
    pick = fixed + rng.sample(rest, min(len(rest), max(0, n_foreign - len(fixed))))
    pick += eg + dub
    rng.shuffle(pick)
    return pick


def _hero_item(ty, x, ov="", badge=""):
    c = cert_of(ty, x["id"])
    d_s = x.get("release_date") or x.get("first_air_date") or ""
    return {"id": x["id"], "type": ty, "year": d_s[:4], "title": x.get("title") or x.get("name") or "",
            "rating": round(x.get("vote_average") or 0, 1), "poster": img(x.get("poster_path"), "w342"),
            "badge": badge, "cl": c["label"], "lv": c["lvl"], "lg": x.get("original_language") or "", "pop": x.get("popularity") or 0,
            "overview": (ov or x.get("overview") or "")[:220], "backdrop": img(x["backdrop_path"], "w1280"), "_ok": c["ok"]}


def _hero_egypt(rng):
    """أعمال مصرية فقط (أفلام ومسلسلات) نزلت قريب: الفيلم بعد نزوله بـ 30-400 يوم (بعد السينما) أو له نزول ديجيتال، والمسلسل عرض حلقة في آخر 45 يوم."""
    if not HERO_EG_SLOTS:
        return []
    today = date.today()
    out = []
    try:
        mv = tmdb_get("/discover/movie", language="ar", include_adult="false", sort_by="popularity.desc",
                      with_origin_country="EG", with_original_language="ar", without_genres="99",
                      **{"primary_release_date.gte": (today - timedelta(days=400)).isoformat(),
                         "primary_release_date.lte": (today - timedelta(days=30)).isoformat()}).get("results", [])
        tv = tmdb_get("/discover/tv", language="ar", include_adult="false", sort_by="popularity.desc",
                      with_origin_country="EG", with_original_language="ar",
                      **{"air_date.gte": (today - timedelta(days=HERO_TV_ACTIVE_DAYS)).isoformat(),
                         "air_date.lte": today.isoformat()}).get("results", [])
    except Exception:
        return []
    cands = [("movie", x) for x in mv[:12]] + [("tv", x) for x in tv[:12]]
    cands = [(t, x) for t, x in cands if x.get("backdrop_path") and x.get("poster_path") and (x.get("overview") or "").strip()]
    items = []
    for t, x in cands:
        try:
            it = _hero_item(t, x, badge="🇪🇬")
        except Exception:
            continue
        if it.pop("_ok", False):
            items.append(it)
    return rng.sample(items, min(len(items), HERO_EG_SLOTS))


def _hero_dubbed(rng):
    """كرتون/عائلي مدبلج بالمصري فقط: من قايمة المدبلج المتأكَّد منها في إضافاتك (dub_items)."""
    if not HERO_DUB_SLOTS:
        return []
    try:
        base = list(dub_items())
    except Exception:
        return []
    rng.shuffle(base)
    out = []
    for b in base:
        if len(out) >= HERO_DUB_SLOTS:
            break
        try:
            x = tmdb_get(f"/movie/{b['id']}", language="ar")
            if not x.get("backdrop_path"):
                continue
            ov = (x.get("overview") or "").strip()
            if not ov:
                ov = (tmdb_get(f"/movie/{b['id']}", language="en-US").get("overview") or "").strip()
            if not ov:
                continue
            it = _hero_item("movie", x, ov=ov, badge="🎙 مدبلج مصري")
        except Exception:
            continue
        if it.pop("_ok", False):
            out.append(it)
    return out


def hero_cached():
    key = date.today().toordinal() // max(1, HERO_ROTATE_DAYS)
    if _HERO_CACHE["key"] == key and _HERO_CACHE["d"]:
        return _HERO_CACHE["d"]
    try:
        d = build_hero()
    except Exception:
        d = []
    if d:
        _HERO_CACHE.update(key=key, d=d)
        return d
    return _HERO_CACHE["d"] or []


@_add_get("/new")
def x_new_impl():
    if not TMDB_KEY:
        return jsonify(rows=[], hero=[])
    now = time.time()
    if _NEW_CACHE["d"] is not None and now - _NEW_CACHE["t"] < 1800:
        return jsonify(dict(_NEW_CACHE["d"], hero=hero_cached() or _NEW_CACHE["d"].get("hero", [])))
    try:
        d = build_new()
    except Exception as ex:
        if _NEW_CACHE["d"] is not None:
            return jsonify(_NEW_CACHE["d"])
        return jsonify(rows=[], hero=[], error=str(ex))
    _NEW_CACHE.update(t=now, d=d)
    return jsonify(dict(d, hero=hero_cached() or d.get("hero", [])))


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


# ============================================================
# رفع ريموت (Remote Upload) على 1fichier — للأدمن بس (/ru محمي في _ADMIN_PREFIXES)
#  - اختيار المجلد بالاسم + إنشاء مجلد جديد
#  - بعد ما 1fichier يخلّص السحب: تظبيط الاسم الحقيقي للملف + حفظ اللينك على صفحة الفيلم/المسلسل في الـIPTV
# ============================================================

FI_API = "https://api.1fichier.com/v1"
RU_FILE = os.path.join(DATA_DIR, "ru_data.json")
RU_LOCK = threading.Lock()
_RU_LAST = [0.0]


def _ru_data():
    try:
        d = store_load(RU_FILE)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _ru_accounts():
    """حسابات 1fichier اللي بمفتاح API (مستبعد حسابات اليوزر/باسورد)."""
    return [p for p in load() if p["type"] == "1fichier" and not p.get("login")]


def _ru_acc(pid):
    return next((p for p in _ru_accounts() if p["id"] == pid), None)


_FI_LAST = [0.0]


_FI_LS, _FI_LS_LAST = {}, [0.0]


def _fi_call(p, path, body):
    left = _fi_cool_left()
    if left > 0:
        # فترة هدوء بعد قفل 1fichier للحساب: مفيش أي طلب يتبعت لحد ما تخلص
        raise Exception("1fichier في فترة هدوء، جرّب بعد %d دقيقة" % (left // 60 + 1))
    is_ls = path in ("file/ls", "folder/ls")
    ck = (p["key"][-8:], path, json.dumps(body, sort_keys=True, default=str))
    if is_ls:
        c = _FI_LS.get(ck)
        if c and time.time() - c[0] < 35:
            return c[1]                 # 1fichier: نفس المجلد ميتعملوش ls أكتر من مرة كل 30 ثانية
    else:
        _FI_LS.clear()                  # أي تعديل: الكاش القديم ما يبقاش صحيح
    gap = float(os.environ.get("FI_GAP", "5") or 5) - (time.time() - _FI_LAST[0])
    if is_ls:
        gap = max(gap, 3.5 - (time.time() - _FI_LS_LAST[0]))   # ls واحد كل 3-4 ثواني على الأكتر
    if gap > 0:
        time.sleep(gap)                 # مسافة بين الطلبات عشان 1fichier ما يعتبرهاش flood
    _FI_LAST[0] = time.time()
    if is_ls:
        _FI_LS_LAST[0] = _FI_LAST[0]
    px = os.environ.get("FI_PROXY", "").strip()      # بروكسي بآيبي ثابت/نضيف لو آيبي السيرفر مشترك ومحظور
    r = requests.post(f"{FI_API}/{path}.cgi", json=body,
                      headers={"Authorization": "Bearer " + p["key"]}, timeout=30,
                      proxies=({"http": px, "https": px} if px else None))
    res = jj(r)
    if isinstance(res, dict) and res.get("status") == "KO":
        msg = res.get("message") or "1fichier رفض الطلب"
        _fi_cool(msg)
        raise Exception(msg)
    if is_ls:
        _FI_LS[ck] = (time.time(), res)
        if len(_FI_LS) > 50:
            _FI_LS.clear()
    return res


def _ru_call(p, ep, body):
    return _fi_call(p, "remote/" + ep, body)


def _ru_clean(name, fallback="file"):
    n = re.sub(r'[\\/:*?"<>|$&;\r\n\t]+', " ", str(name or "")).strip(" .")
    n = re.sub(r"\s{2,}", " ", n).replace(" .", ".")
    return n[:150] or fallback


def _ru_title(t):
    """عنوان نضيف للمسلسل: من غير سنة (2026) ولا زيادات، عشان كل الحلقات تتسمّى بنفس الشكل وتترتب صح."""
    t = re.sub(r"\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]", " ", str(t or ""))
    t = re.sub(r"\s+S\d+E\d+.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[-–|:·]+\s*$", "", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ._-")


def _ru_norm_name(n):
    """أي اسم فيه SxxExx يتحوّل لـ: «العنوان SxxExx.ext» بنفس الصيغة دايماً."""
    n = str(n or "")
    me = re.search(r"(\.[A-Za-z0-9]{2,4})$", n)
    ext = me.group(1).lower() if me else ""
    base = n[:me.start()] if me else n
    m = re.match(r"^(.*?)[\s._-]*S(\d+)E(\d+)(?:\D.*)?$", base, re.I)
    if not m:
        return n
    ti = _ru_title(m.group(1)) or "Series"
    return _ru_clean("%s S%02dE%02d%s" % (ti, int(m.group(2)), int(m.group(3)), ext))


def _ru_subs(res):
    for k in ("sub_folders", "subfolders", "folders"):
        if isinstance(res.get(k), list):
            return res[k]
    return []


# ---- "المواقع": كل موقع رفع = دالة send(p, urls, folder_id, headers) ----
# لإضافة موقع تاني: اكتب دالة بنفس الشكل وضيفها في RU_HOSTS (لازم الموقع يكون ليه ريموت API).
def _ru_send_1fichier(p, urls, folder_id="0", headers=None):
    try:
        fid = int(folder_id or 0)
    except Exception:
        fid = 0
    base = {"urls": urls, "folder_id": fid}
    # حسب وثائق 1fichier: headers لازم تكون object {"Name": "Value"}
    if isinstance(headers, dict) and headers:
        tries = [headers]
    else:
        tries = [{"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"}, {"User-Agent": "VLC"}]
    last = None
    for h in tries + [None]:
        try:
            return _ru_call(p, "request", dict(base, headers=h) if h else base)
        except Exception as e:
            last = e
            if "bad param" not in str(e).lower():
                raise
    raise last


RU_HOSTS = {"1fichier": _ru_send_1fichier}


@app.get("/ru/targets")
def ru_targets():
    return jsonify(hosts=list(RU_HOSTS), items=[{
        "id": p["id"], "name": p["name"], "type": p["type"],
        "cdn": bool(p.get("cdn")), "key": "••••" + p["key"][-4:],
    } for p in _ru_accounts()])


@app.get("/ru/folders")
def ru_folders():
    p = _ru_acc(request.args.get("account_id", ""))
    if not p:
        return jsonify(ok=False, error="حساب غلط"), 400
    try:
        parent = int(request.args.get("parent") or 0)
    except Exception:
        parent = 0
    try:
        res = _fi_call(p, "folder/ls", {"folder_id": parent})
        items = []
        for f in _ru_subs(res):
            try:
                items.append({"id": int(f.get("id")), "name": str(f.get("name") or "")})
            except Exception:
                pass
        items.sort(key=lambda x: x["name"].lower())
        return jsonify(ok=True, parent=parent, items=items)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.post("/ru/mkdir")
def ru_mkdir():
    d = request.get_json(silent=True) or {}
    p = _ru_acc(d.get("account_id", ""))
    if not p:
        return jsonify(ok=False, error="حساب غلط"), 400
    name = _ru_clean(d.get("name"), "")
    if not name:
        return jsonify(ok=False, error="اكتب اسم المجلد"), 400
    try:
        parent = int(d.get("parent") or 0)
    except Exception:
        parent = 0

    def find():
        for f in _ru_subs(_fi_call(p, "folder/ls", {"folder_id": parent})):
            if str(f.get("name")) == name:
                return int(f.get("id"))
        return None
    try:
        fid = find()                    # لو المجلد موجود بنفس الاسم نستخدمه
        if fid is None:
            res = _fi_call(p, "folder/mkdir", {"name": name, "folder_id": parent})
            fid = res.get("folder_id") or res.get("id")
            fid = int(fid) if fid else find()
        if fid is None:
            return jsonify(ok=False, error="المجلد اتعمل بس مقدرتش أجيب رقمه"), 502
        return jsonify(ok=True, id=fid, name=name)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.post("/ru/send")
def ru_send():
    d = request.get_json(silent=True) or {}
    p = _ru_acc(d.get("account_id", ""))
    if not p:
        return jsonify(error="اختار حساب صحيح"), 400
    items = []
    for it in (d.get("items") or [])[:30]:
        u = str((it or {}).get("url") or "").strip()
        if u.startswith(("http://", "https://")):
            items.append({"u": u, "n": _ru_norm_name(_ru_clean(it.get("name"), "")), "e": str(it.get("ep") or "")[:40]})
    if not items:
        return jsonify(error="مفيش روابط صالحة"), 400
    send = RU_HOSTS.get(p["type"])
    if not send:
        return jsonify(error="الموقع ده مش مدعوم للريموت"), 400
    already = False
    key = str(d.get("key") or "")[:60]
    queued = True          # كل الروابط بتعدي على الطابور: رابط واحد في المرة (IPTV بيقبل اتصال واحد بس)
    if queued:
        key = key or ("q:" + format(int(time.time() * 1000), "x"))
        fq = int(d.get("folder_id") or 0) if str(d.get("folder_id") or "0").isdigit() else 0
        _ru_queue_add(key, p["id"], fq, items)          # رابط واحد في المرة، والتاني لما الأول يخلّص
        res = {}
    else:
        try:
            res = send(p, [x["u"] for x in items], d.get("folder_id") or "0", d.get("headers") or None)
        except Exception as e:
            m_ = re.search(r"already requested\s*#?(\d+)", str(e), re.I)
            if not m_:
                _fi_mark(p, str(e))
                return jsonify(ok=False, error=str(e)), 502
            res, already = {"id": int(m_.group(1))}, True      # الطلب اتبعت قبل كده: نكمّل متابعته بدل ما نعتبره فشل
    rid = res.get("id") if isinstance(res, dict) else None
    mt = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    if rid or (key and mt):
        with RU_LOCK:
            data = _ru_data()
            if key and mt:
                m = data.setdefault("lib", {}).setdefault(key, {})
                poster = str(mt.get("poster") or "")[:500]
                m.update(title=str(mt.get("title") or "")[:120], year=str(mt.get("year") or "")[:10],
                         plot=str(mt.get("plot") or "")[:700],
                         kind="series" if mt.get("kind") == "series" else "vod")
                if poster.startswith(("http://", "https://")):
                    m["poster"] = poster
                try:
                    m["rating"] = float(mt.get("rating") or 0)
                except Exception:
                    pass
            if rid:
                jobs = data.setdefault("jobs", [])
                for old_j in [x for x in jobs if str(x.get("id")) == str(rid)]:
                    jobs.remove(old_j)                 # نفس الطلب: نعيد مزامنته من الأول
                jobs.append({"id": rid, "acc": p["id"], "key": key, "t": int(time.time()), "items": items, "done": False,
                             "folder": int(d.get("folder_id") or 0) if str(d.get("folder_id") or "0").isdigit() else None})
                data["jobs"] = jobs[-80:]
            try:
                store_save(RU_FILE, data)
            except Exception:
                pass
    wt = d.get("watch") if isinstance(d.get("watch"), dict) else None
    if wt and key.startswith("series:") and wt.get("sid"):
        with RU_LOCK:
            data = _ru_data()
            w = data.setdefault("watch", {}).setdefault(key, {"seen": []})
            sc = wt.get("scope")
            w.update(on=True, sid=str(wt.get("sid"))[:40], name=str(wt.get("name") or mt.get("title") or "")[:120],
                     acc=p["id"], folder=int(d.get("folder_id") or 0) if str(d.get("folder_id") or "0").lstrip("-").isdigit() else 0,
                     scope=(int(sc) if str(sc).isdigit() else "all"))
            w["seen"] = list(dict.fromkeys((w.get("seen") or []) + [x["e"] for x in items if x["e"]]))
            try:
                store_save(RU_FILE, data)
            except Exception:
                pass
    if queued:
        try:
            _ru_pump()
        except Exception as e:
            app.logger.warning("ru pump: %s", e)
    return jsonify(ok=True, account=p["name"], count=len(items), id=rid, already=already, queued=queued)


def _ru_info(p, rid):
    try:
        return _ru_call(p, "info", {"id": rid})
    except Exception as e:
        if "bad param" in str(e).lower():
            return _ru_call(p, "info", {"id": str(rid)})
        raise


_FI_COOL = [0.0]


RU_COOL_FILE = os.path.join(DATA_DIR, "ru_cool.json")


def _fi_cool_left():
    """الثواني المتبقية من فترة الهدوء (مشتركة بين كل الـinstances عن طريق التخزين الدائم)."""
    if time.time() >= _FI_COOL[0]:
        try:
            v = float((store_load(RU_COOL_FILE) or {}).get("t") or 0)
        except Exception:
            v = 0.0
        if v > _FI_COOL[0]:
            _FI_COOL[0] = v
    return max(0, int(_FI_COOL[0] - time.time()))


def _fi_cool(e):
    """لو 1fichier قفل الحساب/اشتكى من الزحمة: نوقف كل الطلبات 30 دقيقة (FI_COOL_MIN) في كل الـinstances."""
    if re.search(r"lock|flood|too many|#209|#211|#316|rate.?limit|banned|try again", str(e), re.I):
        _FI_COOL[0] = time.time() + max(int(os.environ.get("FI_COOL_MIN", "45") or 45), 5) * 60
        try:
            store_save(RU_COOL_FILE, {"t": _FI_COOL[0]})
        except Exception:
            pass


def _ru_scan_folder(p, j, data):
    """طريقة بديلة: لو info مش بيرد، ندوّر في المجلد نفسه على ملفات اسمها رقم الحلقة (زي 714577.mp4)."""
    fid = j.get("folder")
    if fid is None:
        fid = ((data.get("watch") or {}).get(j.get("key")) or {}).get("folder")
    if fid is None:
        return []
    res = _fi_call(p, "file/ls", {"folder_id": int(fid)})
    stems, names = {}, {}
    for f in (res.get("items") or []):
        fn = str(f.get("filename") or "")
        stems[os.path.splitext(fn)[0]] = f
        names[fn.lower()] = f
    j["lsn"] = "مجلد %s فيه %d ملف: %s | مطلوب: %s" % (
        fid, len(stems), ", ".join(list(stems)[:6]), ", ".join(str(i.get("e") or i.get("n"))[:20] for i in (j.get("items") or [])[:3]))
    out = []
    nstems = {}
    for k, v in stems.items():
        nstems.setdefault(_ru_norm_name(k + ".x").rsplit(".", 1)[0].lower(), v)
    def _tag(s):
        t = re.search(r"S(\d+)E(\d+)", str(s or ""), re.I)
        return (int(t.group(1)), int(t.group(2))) if t else None
    for it in j.get("items") or []:
        f = stems.get(str(it.get("e") or "")) if it.get("e") else None
        if not f and it.get("n"):
            f = names.get(str(it["n"]).lower())          # اتسمّى فعلاً SxxExx
        if not f and it.get("n"):
            f = nstems.get(os.path.splitext(_ru_norm_name(it["n"]))[0].lower())   # نفس الاسم بعد توحيده (من غير سنة/زيادات)
        if not f and it.get("e"):
            ee = str(it["e"])
            f = next((v for k, v in stems.items() if k == ee or re.search(r"(?<!\d)" + re.escape(ee) + r"(?!\d)", k)), None)
        if not f and it.get("n") and _tag(it["n"]):
            tg = _tag(it["n"])
            f = next((v for k, v in stems.items() if _tag(k) == tg), None)   # نفس الموسم والحلقة بالظبط (S01E03 ≠ S01E030)
        if f:
            lk = f.get("url") or f.get("link") or f.get("download_link")
            if lk:
                out.append({"url": it["u"], "status": "OK", "download_link": lk})
            else:
                j["lsn"] = (str(j.get("lsn") or "") + " | الملف لقيته بس مفيش رابط في الرد، الحقول: " + ",".join(map(str, f.keys())))[:300]
    return out


def _ru_sync_core():
    """بيشوف الطلبات اللي لسه ما خلصتش؛ اللي خلص: يظبط الاسم الحقيقي ويحفظ اللينك."""
    if _fi_cool_left() > 0:
        return -1
    with RU_LOCK:
        data = _ru_data()
        jobs = data.get("jobs") or []
        links = data.setdefault("links", {})
        changed = False
        budget = 4
        for j in jobs:
            if j.get("done"):
                continue
            if budget <= 0:
                break
            p = _ru_acc(j.get("acc"))
            if not p:
                continue
            if time.time() - float(j.get("chk") or 0) < min(120 * (2 ** int(j.get("nchk") or 0)), 900):
                continue                    # كل فشل بيزوّد المسافة (2، 4، 8، لحد 15 دقيقة) عشان منزعّجش 1fichier
            j["chk"] = time.time()
            j["nchk"] = int(j.get("nchk") or 0) + 1
            changed = True
            budget -= 1
            if _fi_cool_left() > 0:
                break
            info, einfo = {}, None
            cands = [p]                     # حساب الطلب بس: كل حساب زيادة = طلبات زيادة على 1fichier
            for a_ in cands:
                try:
                    info = _ru_info(a_, j["id"])
                    p, j["acc"], einfo = a_, a_["id"], None
                    time.sleep(0.4)
                    break
                except Exception as e:
                    einfo = str(e)[:200]
                    _fi_cool(e)
                    if _fi_cool_left() > 0:
                        break
                    time.sleep(0.4)
            res_l = (info.get("result") or []) if isinstance(info, dict) else []
            complete = isinstance(info, dict) and (info.get("execution_date") or any(isinstance(r, dict) and r.get("download_link") for r in res_l))
            if not complete:
                found, scan_ok = [], False
                if time.time() - int(j.get("t") or 0) > 120:
                    for a_ in cands:
                        try:
                            found = _ru_scan_folder(a_, j, data)
                            scan_ok = True
                            if found:
                                p, j["acc"] = a_, a_["id"]
                                break
                        except Exception as e:
                            einfo = einfo or str(e)[:200]
                            _fi_cool(e)
                            if _fi_cool_left() > 0:
                                break
                        time.sleep(0.4)
                gone = bool(einfo and re.search(r"no such remote", einfo, re.I))
                if not found:
                    if gone:
                        if scan_ok and time.time() - int(j.get("t") or 0) > 1200:
                            j["done"] = True        # الطلب اتشال من 1fichier ومفيش ملف: المتابعة هتعيد الحلقة
                            j["err"] = "الطلب ضاع عند 1fichier، هيتعاد تلقائي"
                            changed = True
                            continue
                        if not scan_ok:
                            # لسه ما دوّرناش في المجلد (الطلب أحدث من دقيقتين): مفيش خطأ، نعيد الفحص بعد ~45 ثانية
                            j["nchk"] = 0
                            j["chk"] = time.time() - 75
                            j.pop("err", None)
                        else:
                            j["err"] = ("الطلب خلص عند 1fichier ومش لاقي الملف. " + str(j.get("lsn") or ""))[:300]
                        changed = True
                    elif einfo:
                        j["err"] = einfo
                        changed = True
                    continue                        # لسه بيتسحب
                info = {"result": found}
                j["scan"] = True
                if len(found) < len(j.get("items") or []):
                    j["partial"] = True
            j.pop("err", None)
            by_src = {x["u"]: x for x in j.get("items") or []}
            ren, entries = [], []
            for r in info.get("result") or []:
                it = by_src.get(r.get("url")) or {}
                name = it.get("n") or ""
                ent = {"ep": it.get("e") or "", "name": name or str(r.get("url") or "")[-40:], "t": int(time.time())}
                dl = r.get("download_link")
                if r.get("status") == "OK" and dl:
                    ent.update(ok=True, link=dl)
                    if name:
                        ren.append({"url": dl, "filename": name})
                else:
                    ent.update(ok=False, err=str(r.get("message") or "فشل")[:160])
                entries.append(ent)
            if ren:
                try:
                    _fi_call(p, "file/rename", {"urls": ren})
                except Exception as e:
                    j["rename_err"] = str(e)[:160]
                time.sleep(0.4)
            if j.get("key"):
                cur = links.setdefault(j["key"], {"files": []})["files"]
                for ent in entries:
                    same = next((i for i, f in enumerate(cur)
                                 if (ent["ep"] and f.get("ep") == ent["ep"]) or (not ent["ep"] and f.get("name") == ent["name"])), None)
                    if same is None:
                        cur.append(ent)
                    else:
                        cur[same] = ent
            j["done"] = not j.pop("partial", False)
            changed = True
        if changed:
            try:
                store_save(RU_FILE, data)
            except Exception:
                pass
        return sum(1 for j in jobs if not j.get("done"))


def _ru_has_active(key):
    d = _ru_data()
    if any(j.get("key") == key and not j.get("done") for j in (d.get("jobs") or [])):
        return True
    return bool(((d.get("queue") or {}).get(key) or {}).get("items"))


def _ru_queue_add(key, acc, folder, items):
    """بيضيف روابط للطابور (من غير تكرار): الرفع بيتم رابط واحد في المرة."""
    with RU_LOCK:
        data = _ru_data()
        q = data.setdefault("queue", {}).setdefault(key, {"items": []})
        q["acc"], q["folder"] = acc, folder
        have = {x.get("e") for x in q["items"] if x.get("e")} | {x.get("u") for x in q["items"]}
        for x in items:
            if (x.get("e") and x["e"] in have) or x.get("u") in have:
                continue
            q["items"].append({"u": x["u"], "n": _ru_norm_name(x.get("n") or ""), "e": x.get("e") or ""})
        store_save(RU_FILE, data)


def _ru_pump():
    """لكل عمل في الطابور: لو مفيش طلب شغّال له، يبعت الرابط اللي بعده (QUEUE_BATCH، الافتراضي 1)."""
    if _fi_cool_left() > 0:
        return 0
    nb = 1      # IPTV بيسمح بلينك واحد بس في نفس الوقت: رابط واحد في المرة دايماً (مفيش دفعات)
    sent, t0 = 0, time.time()
    for key in list((_ru_data().get("queue") or {}).keys()):
        if time.time() - t0 > 40 or _fi_cool_left() > 0:
            break
        with RU_LOCK:
            data = _ru_data()
            q = (data.get("queue") or {}).get(key)
            if not q or not q.get("items"):
                (data.get("queue") or {}).pop(key, None)
                store_save(RU_FILE, data)
                continue
            _now = time.time()
            if any((not j.get("done")) and _now - int(j.get("t") or 0) < 6 * 3600 for j in (data.get("jobs") or [])):
                continue                                    # لسه فيه رابط بيتسحب (أي مسلسل): حساب IPTV بلينك واحد بس
            if _now - float(data.get("last_send") or 0) < max(int(os.environ.get("RU_SEND_GAP", "300") or 300), 30):
                continue                                    # مهلة بين كل رفع والتاني (الافتراضي 300 ثانية) عشان 1fichier ما يقفلش الحساب
            if _now - float(q.get("claim") or 0) < 60:
                continue                                    # instance تانية بتبعته دلوقتي
            if any(k2 != key and _now - float((q2 or {}).get("claim") or 0) < 60
                   for k2, q2 in (data.get("queue") or {}).items()):
                continue                                    # رابط من مسلسل تاني بيتبعت دلوقتي
            q["claim"] = time.time()
            _okeps = {str(f.get("ep")) for f in ((data.get("links") or {}).get(key) or {}).get("files") or [] if f.get("ep") and f.get("ok")}
            _pend = set()
            for j in data.get("jobs") or []:
                if j.get("key") == key and not j.get("done"):
                    for x in j.get("items") or []:
                        _pend.add(x.get("u")); _pend.add(x.get("e"))
            q["items"] = [x for x in q["items"] if not (x.get("u") in _pend or (x.get("e") and (x["e"] in _pend or str(x["e"]) in _okeps)))]
            if not q["items"]:
                data["queue"].pop(key, None)
                store_save(RU_FILE, data)
                continue
            items, acc, folder = list(q["items"][:nb]), q.get("acc"), q.get("folder")
            store_save(RU_FILE, data)
        p = _ru_acc(acc) or (_ru_accounts() or [None])[0]
        send = RU_HOSTS.get(p["type"]) if p else None
        if not send:
            continue
        try:
            try:
                res = send(p, [x["u"] for x in items], str(folder or 0), None)
            except Exception as e:
                m_ = re.search(r"already requested\s*#?(\d+)", str(e), re.I)
                if not m_:
                    _fi_mark(p, str(e))
                    raise
                res = {"id": int(m_.group(1))}
        except Exception as e:
            _fi_cool(e)
            app.logger.warning("ru pump %s: %s", key, e)
            with RU_LOCK:
                data = _ru_data()
                q = (data.get("queue") or {}).get(key)
                if q is not None:
                    q.pop("claim", None)
                    q["err"] = str(e)[:160]
                    q["fails"] = int(q.get("fails") or 0) + 1
                    if q["fails"] >= 5 and q["items"]:         # رابط بيفشل كل مرة: نشيله ونسجّله فاشل ونكمّل اللي بعده
                        bad = q["items"].pop(0)
                        data.setdefault("links", {}).setdefault(key, {"files": []})["files"].append(
                            {"ep": bad.get("e") or "", "name": bad.get("n") or "", "t": int(time.time()), "ok": False,
                             "err": str(e)[:160]})
                        q["fails"] = 0
                    if not q["items"]:
                        data["queue"].pop(key, None)
                    store_save(RU_FILE, data)
            continue
        rid = res.get("id") if isinstance(res, dict) else None
        with RU_LOCK:
            data = _ru_data()
            q = (data.get("queue") or {}).get(key)
            if q is not None:
                done_e = {(x.get("e"), x.get("u")) for x in items}
                q["items"] = [x for x in q["items"] if (x.get("e"), x.get("u")) not in done_e]
                q.pop("claim", None)
                q.pop("err", None)
                q["fails"] = 0
                if not q["items"]:
                    data["queue"].pop(key, None)
            data["last_send"] = time.time()
            w = (data.get("watch") or {}).get(key)
            if w is not None:
                w["seen"] = list(dict.fromkeys((w.get("seen") or []) + [x["e"] for x in items if x.get("e")]))
                tr = w.setdefault("tries", {})
                for x in items:
                    if x.get("e"):
                        tr[x["e"]] = int(tr.get(x["e"], 0)) + 1
                w["last"] = int(time.time())
            if rid:
                jobs = data.setdefault("jobs", [])
                for oj in [x for x in jobs if str(x.get("id")) == str(rid)]:
                    jobs.remove(oj)
                jobs.append({"id": rid, "acc": p["id"], "key": key, "t": int(time.time()), "items": items,
                             "done": False, "folder": folder})
                data["jobs"] = jobs[-80:]
            store_save(RU_FILE, data)
        sent += len(items)
    return sent


def _ru_sync():
    r = _ru_sync_core()
    try:
        _ru_pump()
    except Exception as e:
        app.logger.warning("ru pump: %s", e)
    return r


@app.post("/ru/sync")
def ru_sync():
    if time.time() - _RU_LAST[0] < 8:
        return jsonify(ok=True, skipped=True)
    _RU_LAST[0] = time.time()
    try:
        return jsonify(ok=True, pending=_ru_sync())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/ru/links")
def ru_links():
    key = str(request.args.get("key", ""))[:60]
    _lib_bg_sync()
    data = _ru_data()
    files = ((data.get("links") or {}).get(key) or {}).get("files") or []
    pend = sum(1 for j in (data.get("jobs") or []) if j.get("key") == key and not j.get("done"))
    w = (data.get("watch") or {}).get(key) or {}
    errs = [str(j.get("err"))[:320] for j in (data.get("jobs") or []) if j.get("key") == key and not j.get("done") and j.get("err")]
    qn = len(((data.get("queue") or {}).get(key) or {}).get("items") or [])
    qe = str(((data.get("queue") or {}).get(key) or {}).get("err") or "")
    if qe:
        errs.append(qe)
    return jsonify(ok=True, files=files, pending=pend, queued=qn, errs=errs[:3], cool=_fi_cool_left(), watch=({"on": bool(w.get("on")), "n": len(w.get("seen") or [])} if w else None))


@app.get("/ru/status")
def ru_status():
    p = _ru_acc(request.args.get("account_id", ""))
    if not p:
        return jsonify(error="حساب غلط"), 400
    try:
        rid = request.args.get("id")
        res = _ru_call(p, "info", {"id": rid}) if rid else _ru_call(p, "ls", {})
        return jsonify(ok=True, res=res)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


# ---- متابعة المسلسلات: أي حلقة جديدة بتظهر في الـIPTV بتترفع لوحدها بنفس المجلد وباسم SxxExx ----
def _ru_iptv_acc():
    al = iptv_load()
    rec = al.get(IPTV_ALL) or {}
    if not rec.get("host"):
        rec = next((v for k, v in al.items() if isinstance(v, dict) and v.get("host") and not str(k).startswith("__")), {})
    accs = iptv_accs(rec)
    return accs[0] if accs else None


def _ru_series_pull(a, sid):
    """من الـIPTV: (meta, eps). meta = بيانات العمل، eps = كل الحلقات مرتّبة (موسم ثم حلقة).
    لو السيرفر مش بيوصل للـIPTV (آيبي مصري مثلًا) ضيف متغير IPTV_PROXY."""
    px = os.environ.get("IPTV_PROXY", "").strip()
    r = requests.get(f"{a['host']}/player_api.php",
                     params={"username": a["user"], "password": a["pass"], "action": "get_series_info", "series_id": sid},
                     timeout=40, proxies=({"http": px, "https": px} if px else None))
    v = jj(r)
    if not isinstance(v, dict) or not (v.get("episodes") or v.get("info")):
        raise Exception("الـIPTV مرجّعش بيانات للمسلسل (السيرفر ممكن يكون محجوب أو الاشتراك منتهي)")
    eps = v.get("episodes") or {}
    out = []

    def add(x, sn=None):
        try:
            out.append({"id": str(x.get("id")), "ext": x.get("container_extension") or "mp4",
                        "s": int(x.get("season") or sn or 0), "e": int(x.get("episode_num") or 0)})
        except Exception:
            pass
    if isinstance(eps, list):
        for a_ in eps:
            for x in (a_ if isinstance(a_, list) else [a_]):
                add(x)
    elif isinstance(eps, dict):
        for sn, lst in eps.items():
            for x in (lst or []):
                add(x, sn)
    # حلقة من غير رقم: نديها رقم متسلسل جوه موسمها عشان الاسم SxxExx ميتكررش
    by_s = {}
    for x in out:
        by_s.setdefault(x["s"], []).append(x)
    for lst in by_s.values():
        used = {x["e"] for x in lst if x["e"]}
        n = 0
        for x in sorted(lst, key=lambda z: (z["e"] or 10 ** 9, int(z["id"]) if z["id"].isdigit() else 0)):
            if not x["e"]:
                n += 1
                while n in used:
                    n += 1
                x["e"] = n
                used.add(n)
    out.sort(key=lambda x: (x["s"], x["e"]))
    inf = v.get("info") or {}

    def num(z):
        try:
            return float(z)
        except Exception:
            return 0.0
    bd = inf.get("backdrop_path")
    meta = {"title": str(inf.get("name") or "")[:120],
            "poster": str(inf.get("cover") or inf.get("cover_big") or (bd[0] if isinstance(bd, list) and bd else "") or ""),
            "year": str(inf.get("releaseDate") or inf.get("release_date") or inf.get("year") or "")[:4],
            "rating": num(inf.get("rating")) or num(inf.get("rating_5based")) * 2,
            "plot": str(inf.get("plot") or inf.get("description") or "")[:700]}
    return meta, out


def _ru_series_eps(a, sid):
    return a, _ru_series_pull(a, sid)[1]


def _ru_mk(p, name, parent=0):
    """مجلد بالاسم جوه parent (لو موجود بنستخدمه) → رقمه."""
    def find():
        for f in _ru_subs(_fi_call(p, "folder/ls", {"folder_id": parent})):
            if str(f.get("name")) == name:
                return int(f.get("id"))
        return None
    fid = find()
    if fid is None:
        res = _fi_call(p, "folder/mkdir", {"name": name, "folder_id": parent})
        fid = res.get("folder_id") or res.get("id")
        fid = int(fid) if fid else find()
    if fid is None:
        raise Exception("المجلد اتعمل بس مقدرتش أجيب رقمه")
    return fid


def _ru_watch_run(only=None, force=False, eps_override=None):
    """بيفحص المسلسلات المتابَعة: أي حلقة جديدة (أو فاشلة/ضايعة) بتتبعت لـ1fichier بنفس المجلد وباسم SxxExx.
    الدور بيتحجز في التخزين الدائم عشان أكتر من instance ما يرفعوش نفس الحلقة مرتين."""
    if _fi_cool_left() > 0:
        return 0
    data = _ru_data()
    ws = {k: w for k, w in (data.get("watch") or {}).items() if w.get("on") and (not only or k == only)}
    if not ws:
        return 0
    a = _ru_iptv_acc()
    if not a:
        return 0
    gap = max(int(os.environ.get("RU_WATCH_MIN", "15") or 15), 3) * 60
    t0, sent = time.time(), 0
    for key, _w in sorted(ws.items(), key=lambda kv: float(kv[1].get("chk") or 0)):
        if time.time() - t0 > 45 or _fi_cool_left() > 0:
            break
        with RU_LOCK:
            d2 = _ru_data()
            w = (d2.get("watch") or {}).get(key)
            if not w or not w.get("on"):
                continue
            if not force and time.time() - float(w.get("chk") or 0) < (240 if w.get("backlog") else gap):
                continue
            w["chk"] = time.time()
            try:
                store_save(RU_FILE, d2)
            except Exception:
                pass
        try:
            p = _ru_acc(w.get("acc")) or (_ru_accounts() or [None])[0]
            send = RU_HOSTS.get(p["type"]) if p else None
            if not send:
                continue
            eps = (eps_override or {}).get(key)
            if eps is None:
                eps = _ru_series_pull(a, w["sid"])[1]
            cur = _ru_data()
            cw = (cur.get("watch") or {}).get(key) or {}
            files = ((cur.get("links") or {}).get(key) or {}).get("files") or []
            okeps = {str(f["ep"]) for f in files if f.get("ep") and f.get("ok")}
            failed = {str(f["ep"]) for f in files if f.get("ep") and not f.get("ok")}
            seen, tries = set(cw.get("seen") or []), cw.get("tries") or {}
            pend = set()
            for j in cur.get("jobs") or []:
                if j.get("key") == key and not j.get("done") and time.time() - int(j.get("t") or 0) < 6 * 3600:
                    pend |= {str(x.get("e")) for x in j.get("items") or []}
            pend |= {str(x.get("e")) for x in ((cur.get("queue") or {}).get(key) or {}).get("items") or []}
            # اتبعتت قبل كده بس مفيش ملف ولا طلب شغّال (فشلت/ضاعت): نعيدها لحد 3 مرات
            lost = {e for e in ((seen | failed) - okeps - pend) if int(tries.get(e, 0)) < 3}
            have = (seen | okeps | failed) - lost
            sc = w.get("scope", "all")
            new = [x for x in eps if x["id"] not in have and (sc == "all" or x["s"] == sc)]
            if not new:
                with RU_LOCK:
                    data = _ru_data()
                    ww = (data.get("watch") or {}).get(key)
                    if ww is not None and (ww.get("backlog") or ww.get("err")):
                        ww["backlog"] = False
                        ww.pop("err", None)
                        store_save(RU_FILE, data)
                continue
            items = [{"u": f"{a['host']}/series/{quote(a['user'])}/{quote(a['pass'])}/{x['id']}.{x['ext']}",
                      "n": _ru_norm_name(_ru_clean("%s S%02dE%02d.%s" % (_ru_title(w.get("name") or key), x["s"], x["e"], x["ext"]))),
                      "e": x["id"]} for x in new]
            _ru_queue_add(key, p["id"], w.get("folder"), items)      # بتترفع واحدة واحدة من الطابور
            with RU_LOCK:
                data = _ru_data()
                ww = (data.get("watch") or {}).get(key)
                if ww is None:
                    continue
                ww["seen"] = list(dict.fromkeys((ww.get("seen") or []) + [x["e"] for x in items]))
                ww["backlog"] = False
                ww.pop("err", None)
                store_save(RU_FILE, data)
            sent += len(items)
        except Exception as e:
            _fi_cool(e)
            app.logger.warning("ru watch %s: %s", key, e)
            try:
                with RU_LOCK:
                    data = _ru_data()
                    ww = (data.get("watch") or {}).get(key)
                    if ww is not None:
                        ww["err"] = str(e)[:160]
                        store_save(RU_FILE, data)
            except Exception:
                pass
    if sent:
        try:
            _ru_pump()
        except Exception as e:
            app.logger.warning("ru pump: %s", e)
    return sent


def _ru_bg_loop():
    time.sleep(60)
    n = 0
    every = max(int(os.environ.get("RU_WATCH_MIN", "15")), 5)
    while True:
        try:
            _d_ = _ru_data()
            if any(not j.get("done") for j in (_d_.get("jobs") or [])):
                _ru_sync()
            elif any((q_ or {}).get("items") for q_ in (_d_.get("queue") or {}).values()):
                _ru_pump()          # مفيش رابط شغّال والطابور فيه حاجة: ابعت اللي بعده
            if n % every == 0:
                _ru_watch_run()
        except Exception:
            pass
        n += 1
        time.sleep(60)


if os.environ.get("RU_AUTOWATCH", "1") != "0":
    threading.Thread(target=_ru_bg_loop, daemon=True).start()


@app.post("/ru/watch")
def ru_watch_set():
    d = request.get_json(silent=True) or {}
    key = str(d.get("key") or "")[:60]
    with RU_LOCK:
        data = _ru_data()
        w = (data.get("watch") or {}).get(key)
        if not w:
            return jsonify(ok=False, error="مفيش متابعة للمسلسل ده"), 404
        w["on"] = bool(d.get("on"))
        store_save(RU_FILE, data)
    if d.get("now"):
        threading.Thread(target=_ru_watch_run, daemon=True).start()
    return jsonify(ok=True, on=w["on"])


@app.post("/ru/follow")
def ru_follow():
    """ضغطة واحدة: يعمل مجلد بالاسم، يرفع كل الحلقات، يضيف المسلسل للمكتبة، ويفضل يتابع الحلقات الجديدة لوحده."""
    d = request.get_json(silent=True) or {}
    sid = str(d.get("sid") or "").strip()
    if not sid.isdigit() or len(sid) > 20:
        return jsonify(ok=False, error="رقم المسلسل غلط"), 400
    a = _ru_iptv_acc()
    if not a:
        return jsonify(ok=False, error="مفيش حساب IPTV متسجّل"), 400
    key = "series:" + sid
    d0 = _ru_data()
    cfg, old = d0.get("cfg") or {}, (d0.get("watch") or {}).get(key) or {}
    p = _ru_acc(d.get("account_id") or old.get("acc") or cfg.get("acc") or "") or (_ru_accounts() or [None])[0]
    if not p:
        return jsonify(ok=False, error="مفيش حساب 1fichier بمفتاح API. ضيفه من الإعدادات ← المزوّدين"), 400
    cm = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    meta, eps, from_client = {}, [], False
    try:
        meta, eps = _ru_series_pull(a, sid)
    except Exception as e:
        # السيرفر مش واصل للـIPTV: نستخدم قايمة الحلقات اللي المتصفح باعتها (أول دفعة بس)
        for x in (d.get("eps") or [])[:600]:
            try:
                eps.append({"id": str(int(x.get("id"))), "ext": re.sub(r"\W", "", str(x.get("ext") or "mp4"))[:5] or "mp4",
                            "s": int(x.get("s") or 0), "e": int(x.get("e") or 0)})
            except Exception:
                pass
        if not eps:
            return jsonify(ok=False, error="السيرفر مش واصل للـIPTV: " + str(e)[:140]), 502
        from_client = True
    title = _ru_clean(d.get("name") or meta.get("title") or cm.get("title"), "") or ("series " + sid)
    parent = int(d["parent"]) if str(d.get("parent", "")).isdigit() else int(cfg.get("parent") or 0)
    try:
        fid = old["folder"] if old.get("folder") is not None else _ru_mk(p, title, parent)
    except Exception as e:
        _fi_mark(p, str(e))
        return jsonify(ok=False, error=str(e)), 502
    with RU_LOCK:
        data = _ru_data()
        m = data.setdefault("lib", {}).setdefault(key, {})
        poster = str(meta.get("poster") or cm.get("poster") or "")[:500]
        m.update(title=title, kind="series", year=str(meta.get("year") or cm.get("year") or "")[:10],
                 plot=str(meta.get("plot") or cm.get("plot") or "")[:700])
        if poster.startswith(("http://", "https://")):
            m["poster"] = poster
        try:
            m["rating"] = float(meta.get("rating") or cm.get("rating") or 0)
        except Exception:
            pass
        w = data.setdefault("watch", {}).setdefault(key, {"seen": []})
        sc = d.get("scope")
        w.update(on=True, sid=sid, name=title, acc=p["id"], folder=fid, scope=(int(sc) if str(sc).isdigit() else "all"),
                 auto=True, backlog=True, chk=0)
        w.setdefault("added", int(time.time()))
        data["cfg"] = {"acc": p["id"], "parent": parent}
        store_save(RU_FILE, data)
    sent = _ru_watch_run(only=key, force=True, eps_override=({key: eps} if from_client else None))
    return jsonify(ok=True, title=title, folder=fid, account=p["name"], episodes=len(eps), sent=sent,
                   already=bool(old.get("on")))


@app.get("/ru/watchlist")
def ru_watchlist():
    data = _ru_data()
    jobs, out = data.get("jobs") or [], []
    for k, w in (data.get("watch") or {}).items():
        files = ((data.get("links") or {}).get(k) or {}).get("files") or []
        out.append({"key": k, "name": w.get("name") or k, "on": bool(w.get("on")),
                    "ok": sum(1 for f in files if f.get("ok") and f.get("link")),
                    "bad": sum(1 for f in files if not f.get("ok")),
                    "pending": sum(1 for j in jobs if j.get("key") == k and not j.get("done")),
                    "queued": len(((data.get("queue") or {}).get(k) or {}).get("items") or []),
                    "backlog": bool(w.get("backlog")), "err": str(w.get("err") or ""),
                    "chk": int(w.get("chk") or 0), "last": int(w.get("last") or 0),
                    "poster": ((data.get("lib") or {}).get(k) or {}).get("poster") or ""})
    for x in out:
        qe = str(((data.get("queue") or {}).get(x["key"]) or {}).get("err") or "")
        if qe and not x["err"]:
            x["err"] = qe
    out.sort(key=lambda x: x["name"].lower())
    return jsonify(ok=True, items=out, cron=bool(os.environ.get("CRON_KEY") or os.environ.get("CRON_SECRET")),
                   cool=_fi_cool_left())


@app.post("/ru/wipe")
def ru_wipe():
    """إلغاء ومسح كل حاجة قديمة للمسلسل (أو لكل المتابَعات): الطلبات، الطابور، المتابعة، الروابط، والكارت في المكتبة.
    purge=true بيمسح كمان الملفات من مجلد 1fichier (مجلد المسلسل بس، عمره ما بيلمس الجذر)."""
    d = request.get_json(silent=True) or {}
    key = str(d.get("key") or "")[:60]
    allk = bool(d.get("all"))
    if not key and not allk:
        return jsonify(ok=False, error="حدد المسلسل"), 400
    purge = bool(d.get("purge"))
    purged, perr = 0, ""
    with RU_LOCK:
        data = _ru_data()
        lib = data.get("lib") or {}
        if allk:
            keys = set(data.get("watch") or {}) | set(data.get("queue") or {}) | {j.get("key") for j in data.get("jobs") or [] if j.get("key")}
            keys |= {k for k in (data.get("links") or {}) if not (lib.get(k) or {}).get("manual")}
            keys.discard(None)
        else:
            keys = {key}
        folders = {}
        for k in keys:
            w = (data.get("watch") or {}).get(k) or {}
            fids = {w.get("folder")} | {j.get("folder") for j in data.get("jobs") or [] if j.get("key") == k}
            q = (data.get("queue") or {}).get(k) or {}
            fids.add(q.get("folder"))
            acc = w.get("acc") or q.get("acc") or next((j.get("acc") for j in data.get("jobs") or [] if j.get("key") == k), None)
            for f in fids:
                try:
                    if f is not None and int(f) > 0:
                        folders[int(f)] = acc
                except Exception:
                    pass
        data["jobs"] = [j for j in (data.get("jobs") or []) if j.get("key") not in keys]
        for sec in ("queue", "watch", "links", "lib"):
            for k in keys:
                (data.get(sec) or {}).pop(k, None)
        store_save(RU_FILE, data)
    if purge and folders:
        if _fi_cool_left() > 0:
            perr = "1fichier في فترة هدوء، الملفات ما اتمسحتش من عندهم (امسحها من الموقع)"
        else:
            for fid, acc in folders.items():
                try:
                    p = _ru_acc(acc) or (_ru_accounts() or [None])[0]
                    if not p:
                        continue
                    res = _fi_call(p, "file/ls", {"folder_id": fid})
                    urls = [{"url": f["url"]} for f in (res.get("items") or []) if f.get("url")]
                    if urls:
                        _fi_call(p, "file/rm", {"files": urls})
                        purged += len(urls)
                except Exception as e:
                    perr = str(e)[:160]
                    _fi_cool(e)
                    break
    return jsonify(ok=True, wiped=len(keys), purged=purged, purge_error=perr, cool=_fi_cool_left())


@app.post("/ru/reset")
def ru_reset():
    """إعادة المحاولة: يمسح الخطأ العالق وفترة الهدوء، ويرجّع الطلبات العالقة للطابور ويبعت أول رابط.
    لو 1fichier لسه قافل الحساب، الخطأ الحقيقي الجديد هو اللي هيظهر."""
    d = request.get_json(silent=True) or {}
    key = str(d.get("key") or "")[:60]
    if _fi_cool_left() > 0 and not d.get("force"):
        # 1fichier لسه قافل: أي محاولة دلوقتي بتطوّل الحظر. نستنى وهو يكمّل لوحده بعد الهدوء.
        return jsonify(ok=True, requeued=0, sent=0, cool=_fi_cool_left(), waiting=True)
    _FI_COOL[0] = 0.0
    try:
        store_save(RU_COOL_FILE, {"t": 0})
    except Exception:
        pass
    moved = 0
    with RU_LOCK:
        data = _ru_data()
        files = ((data.get("links") or {}).get(key) or {}).get("files") or []
        okeps = {str(f.get("ep")) for f in files if f.get("ep") and f.get("ok")}
        keep = []
        for j in data.get("jobs") or []:
            k = j.get("key")
            if j.get("done") or not k or (key and k != key):
                keep.append(j)
                continue
            if time.time() - float(j.get("t") or 0) < 3 * 3600:
                # 1fichier قبل الطلب فعلاً: منعيدش إرساله (بيعمل ملف مكرر) — بس نصفّر الفحص عشان يتسجّل بعد الهدوء
                j["chk"] = 0
                j.pop("err", None)
                keep.append(j)
                continue
            q = data.setdefault("queue", {}).setdefault(k, {"items": []})
            q["acc"], q["folder"] = j.get("acc"), j.get("folder")
            have = {x.get("e") for x in q["items"] if x.get("e")} | {x.get("u") for x in q["items"]}
            for x in j.get("items") or []:
                if not x.get("u") or x.get("u") in have or (x.get("e") and (x["e"] in have or str(x["e"]) in okeps)):
                    continue
                q["items"].append({"u": x["u"], "n": x.get("n") or "", "e": x.get("e") or ""})
                moved += 1
        data["jobs"] = keep
        for k, q in (data.get("queue") or {}).items():
            if not key or k == key:
                for f in ("err", "fails", "claim"):
                    q.pop(f, None)
        for k, w in (data.get("watch") or {}).items():
            if not key or k == key:
                w.pop("err", None)
        store_save(RU_FILE, data)
    sent = 0
    try:
        sent = _ru_pump()
    except Exception as e:
        app.logger.warning("ru reset pump: %s", e)
    return jsonify(ok=True, requeued=moved, sent=sent, cool=_fi_cool_left())


@app.post("/ru/runnow")
def ru_runnow():
    try:
        pend = _ru_sync()
        sent = _ru_watch_run(force=True)
        return jsonify(ok=True, pending=pend, sent=sent)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502


@app.route("/cron/ru", methods=["GET", "POST"])
def cron_ru():
    """بيتنادى كل 5 دقايق من Vercel Cron أو cron-job.org: يكمّل الرفع المعلّق ويفحص الحلقات الجديدة.
    محمي بمتغير CRON_KEY (أو CRON_SECRET بتاع Vercel) — ?key=... أو Authorization: Bearer ..."""
    want = os.environ.get("CRON_KEY", "") or os.environ.get("CRON_SECRET", "")
    got = request.args.get("key", "") or request.headers.get("X-Cron-Key", "")
    au = request.headers.get("Authorization", "")
    if not got and au.startswith("Bearer "):
        got = au[7:]
    if not want or not hmac.compare_digest(got.encode(), want.encode()):
        return jsonify(error="forbidden"), 403
    out = {}
    try:
        out["pending"] = _ru_sync()
    except Exception as e:
        out["sync_err"] = str(e)[:120]
    try:
        out["sent"] = _ru_watch_run()
    except Exception as e:
        out["watch_err"] = str(e)[:120]
    return jsonify(ok=True, **out)


# ============================================================
# المكتبة: اللي بيترفع بيظهر للأعضاء كلهم بلينكات المرفوعات (اللينك نفسه مخفي عنهم)
# الأدمن: تثبيت / إلغاء تثبيت / شيل من المكتبة. الترتيب العادي = الأحدث رفعًا الأول، والمثبّت فوق.
# ============================================================

def _nat(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(s or ""))]


def _lib_items(data):
    lib = data.get("lib") or {}
    out = []
    for key, v in (data.get("links") or {}).items():
        files = [f for f in (v.get("files") or []) if f.get("ok") and f.get("link")]
        if not files:
            continue
        m = lib.get(key) or {}
        try:
            rating = float(m.get("rating") or 0)
        except Exception:
            rating = 0
        ts = [int(f.get("t") or 0) for f in files]
        newest = max(files, key=lambda f: int(f.get("t") or 0))
        tg = re.search(r"S\d+E\d+", str(newest.get("name") or ""), re.I)
        newn = sum(1 for t_ in ts if time.time() - t_ < 3 * 86400)
        out.append({"key": key, "title": m.get("title") or key, "poster": m.get("poster") or "",
                    "year": m.get("year") or "", "rating": rating,
                    "kind": m.get("kind") or key.split(":")[0], "n": len(files),
                    "pin": int(m.get("pin") or 0), "new": newn, "last": tg.group(0).upper() if tg else "",
                    "t": max(ts or [0])})
    out.sort(key=lambda x: (0, -x["pin"], -x["t"]) if x["pin"] else (1, 0, -x["t"]))
    return out


_LIB_SYNC = [0.0]


_WATCH_LAST = [0.0]


def _lib_bg_sync():
    """بيتشغّل مع أي طلب للمكتبة (من غير الاعتماد على خيط خلفي): بيكمّل الرفع المعلّق وبيفحص الحلقات الجديدة."""
    try:
        now = time.time()
        if now - _LIB_SYNC[0] >= 45:
            _LIB_SYNC[0] = now
            if any(not j.get("done") for j in (_ru_data().get("jobs") or [])):
                _ru_sync()
        if now - _WATCH_LAST[0] >= max(int(os.environ.get("RU_WATCH_MIN", "15")), 5) * 60:
            _WATCH_LAST[0] = now
            if any(w.get("on") for w in (_ru_data().get("watch") or {}).values()):
                threading.Thread(target=_ru_watch_run, daemon=True).start()
    except Exception as e:
        app.logger.warning("lib sync: %s", e)


def _lib_norm(t):
    t = re.sub(r"\(\s*\d{4}\s*\)", "", str(t or ""))
    return re.sub(r"[\W_]+", "", t, flags=re.U).lower()


def _lib_merge():
    """نفس العمل اتسجّل تحت مفتاحين (إضافة يدوي + متابعة تلقائية): نجمعهم في كارت واحد."""
    with RU_LOCK:
        data = _ru_data()
        lib, lnk = data.get("lib") or {}, data.get("links") or {}
        groups = {}
        for k in lnk:
            m = lib.get(k) or {}
            nt = _lib_norm(m.get("title") or "")
            if nt:
                groups.setdefault((nt, m.get("kind") or k.split(":")[0]), []).append(k)
        changed = False
        for ks in groups.values():
            if len(ks) < 2:
                continue
            ks.sort(key=lambda k: (not k.startswith("series:"), -len((lnk.get(k) or {}).get("files") or [])))
            main = ks[0]
            mf = lnk.setdefault(main, {"files": []}).setdefault("files", [])
            for k in ks[1:]:
                for f in (lnk.get(k) or {}).get("files") or []:
                    tg = re.search(r"S\d+E\d+", str(f.get("name") or ""), re.I)
                    dup = next((x for x in mf if (f.get("link") and x.get("link") == f.get("link")) or
                                (tg and re.search(re.escape(tg.group(0)), str(x.get("name") or ""), re.I))), None)
                    if dup is None:
                        mf.append(f)
                    elif f.get("ok") and not dup.get("ok"):
                        mf[mf.index(dup)] = f
                for j in data.get("jobs") or []:
                    if j.get("key") == k:
                        j["key"] = main
                lm = lib.setdefault(main, {})
                for fld in ("poster", "plot", "year", "rating"):
                    if not lm.get(fld) and (lib.get(k) or {}).get(fld):
                        lm[fld] = lib[k][fld]
                lm["pin"] = max(int(lm.get("pin") or 0), int((lib.get(k) or {}).get("pin") or 0))
                lnk.pop(k, None)
                lib.pop(k, None)
                changed = True
        if changed:
            store_save(RU_FILE, data)


@app.get("/lib/list")
def lib_list():
    try:
        _lib_merge()
    except Exception as e:
        app.logger.warning("lib merge: %s", e)
    _lib_bg_sync()
    return jsonify(ok=True, items=_lib_items(_ru_data()), admin=(g.role == "admin"))


@app.get("/lib/item")
def lib_item():
    key = str(request.args.get("key", ""))[:60]
    data = _ru_data()
    v = (data.get("links") or {}).get(key) or {}
    adm = g.role == "admin"
    files = [dict({"i": i, "ep": f.get("ep") or "", "name": f.get("name") or ""}, **({"link": f.get("link")} if adm else {}))
             for i, f in enumerate(v.get("files") or []) if f.get("ok") and f.get("link")]
    if not files:
        return jsonify(ok=False, error="مش موجود في المكتبة"), 404
    def _ek(f):
        mm = re.search(r"S(\d+)E(\d+)", f["name"], re.I)
        return (0, int(mm.group(1)), int(mm.group(2)), _nat(f["name"])) if mm else (1, 0, 0, _nat(f["name"]))
    files.sort(key=_ek)                     # الموسم ثم الحلقة، مهما كان ترتيب الرفع
    m = (data.get("lib") or {}).get(key) or {}
    return jsonify(ok=True, key=key, title=m.get("title") or key, poster=m.get("poster") or "",
                   year=m.get("year") or "", rating=m.get("rating") or 0, plot=m.get("plot") or "",
                   kind=m.get("kind") or key.split(":")[0], pin=int(m.get("pin") or 0),
                   files=files, admin=(g.role == "admin"))


@app.post("/lib/go")
def lib_go():
    """بيحوّل لينك 1fichier المحفوظ (من غير ما العضو يشوفه) لرابط تحميل/تشغيل بنفس منطق /generate."""
    d = request.get_json(silent=True) or {}
    key = str(d.get("key") or "")[:60]
    try:
        i = int(d.get("i"))
    except Exception:
        return jsonify(error="ملف غلط"), 400
    files = ((_ru_data().get("links") or {}).get(key) or {}).get("files") or []
    if not (0 <= i < len(files)) or not files[i].get("ok") or not files[i].get("link"):
        return jsonify(error="الملف مش موجود"), 404
    resp = make_response(_generate({"link": files[i]["link"]}))
    if resp.status_code == 200:
        try:
            r = resp.get_json(silent=True) or {}
            if r.get("direct_link"):
                r["name"] = files[i].get("name") or r.get("name")
                act_log(g.user, "link", r.get("name"), r.get("size"), r.get("provider"))
                return jsonify(r)
        except Exception:
            pass
    return resp


def _lib_admin():
    return g.role == "admin"


@app.post("/lib/pin")
def lib_pin():
    if not _lib_admin():
        return jsonify(error="للأدمن بس"), 403
    d = request.get_json(silent=True) or {}
    key = str(d.get("key") or "")[:60]
    with RU_LOCK:
        data = _ru_data()
        if key not in (data.get("links") or {}):
            return jsonify(error="مش موجود"), 404
        lib = data.setdefault("lib", {})
        m = lib.setdefault(key, {})
        m["pin"] = (max([int(v.get("pin") or 0) for v in lib.values()] + [0]) + 1) if d.get("pin") else 0
        store_save(RU_FILE, data)
    return jsonify(ok=True, pin=m["pin"])


@app.post("/lib/del")
def lib_del():
    if not _lib_admin():
        return jsonify(error="للأدمن بس"), 403
    key = str((request.get_json(silent=True) or {}).get("key") or "")[:60]
    with RU_LOCK:
        data = _ru_data()
        (data.get("links") or {}).pop(key, None)
        (data.get("lib") or {}).pop(key, None)
        data["jobs"] = [j for j in (data.get("jobs") or []) if j.get("key") != key or j.get("done")]
        store_save(RU_FILE, data)
    return jsonify(ok=True)


@app.post("/lib/add")
def lib_add():
    """إضافة لينك/لينكات للمكتبة بإيدك (من غير IPTV): فيلم / مسلسل / كرتون، أو إضافة ملفات لعمل موجود."""
    if not _lib_admin():
        return jsonify(error="للأدمن بس"), 403
    d = request.get_json(silent=True) or {}
    links = []
    for u in (d.get("links") or [])[:60]:
        u = str(u).strip()
        if u.startswith(("http://", "https://")) and u not in links:
            links.append(u)
    if not links:
        return jsonify(error="حط لينك واحد على الأقل (http/https)"), 400
    key = str(d.get("key") or "")[:60]
    title = str(d.get("title") or "").strip()[:120]
    kind = d.get("kind") if d.get("kind") in ("vod", "series", "cartoon") else "vod"
    with RU_LOCK:
        data = _ru_data()
        lnk = data.setdefault("links", {})
        lib = data.setdefault("lib", {})
        if key and key not in lnk and not title:
            return jsonify(error="العمل مش موجود"), 404
        if not key or key not in lnk:
            if not title:
                return jsonify(error="اكتب اسم العمل"), 400
            key = key or ("m:" + format(int(time.time() * 1000), "x"))
            poster = str(d.get("poster") or "")[:500]
            m = {"title": title, "kind": kind, "year": str(d.get("year") or "")[:10],
                 "plot": str(d.get("plot") or "")[:700], "manual": True}
            if poster.startswith(("http://", "https://")):
                m["poster"] = poster
            try:
                m["rating"] = float(d.get("rating") or 0)
            except Exception:
                m["rating"] = 0
            lib[key] = m
        files = lnk.setdefault(key, {"files": []}).setdefault("files", [])
        have = {f.get("link") for f in files}
        acc = (_ru_accounts() or [None])[0]
        added = 0
        for n, u in enumerate(links, 1):
            if u in have:
                continue
            name = ""
            if acc and "1fichier.com" in u:          # الاسم الحقيقي للملف من 1fichier نفسه
                try:
                    name = _ru_clean(_fi_call(acc, "file/info", {"url": u}).get("filename"), "")
                except Exception:
                    name = ""
                time.sleep(0.35)
            if not name:
                name = _ru_clean(title or lib.get(key, {}).get("title") or "ملف", "ملف") + (" %d" % (len(files) + 1) if (len(links) > 1 or files) else "")
            files.append({"ep": "", "name": name, "ok": True, "link": u, "t": int(time.time())})
            added += 1
        store_save(RU_FILE, data)
    return jsonify(ok=True, key=key, added=added)


@app.post("/lib/rmfile")
def lib_rmfile():
    if not _lib_admin():
        return jsonify(error="للأدمن بس"), 403
    d = request.get_json(silent=True) or {}
    key = str(d.get("key") or "")[:60]
    try:
        i = int(d.get("i"))
    except Exception:
        return jsonify(error="ملف غلط"), 400
    with RU_LOCK:
        data = _ru_data()
        files = ((data.get("links") or {}).get(key) or {}).get("files") or []
        if not (0 <= i < len(files)):
            return jsonify(error="مش موجود"), 404
        files.pop(i)
        if not any(f.get("ok") and f.get("link") for f in files):
            (data.get("links") or {}).pop(key, None)
            (data.get("lib") or {}).pop(key, None)
        store_save(RU_FILE, data)
    return jsonify(ok=True)
