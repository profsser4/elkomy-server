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
        if os.path.exists(PFILE):
            with open(PFILE, encoding="utf-8") as f:
                d = json.load(f)
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
        with open(PFILE, "w", encoding="utf-8") as f:
            json.dump(ps, f, ensure_ascii=False)
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
        with open(USERS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def users_save(d):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, USERS_FILE)


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
            if user != APP_USER.lower() or fp != fp_of(APP_PASS):
                return None
        else:
            rec = users_load().get(user)
            if not rec or fp != fp_of(rec["h"]):
                return None
        return user, role
    except Exception:
        return None


def needs_admin(path, method):
    if path.startswith(("/providers", "/users", "/settings", "/home/", "/activity", "/push/check", "/websearch")):
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
    if g.role != "admin" and request.method == "POST" and request.path in ("/play/start", "/generate", "/stremio/resolve"):
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


def too_many(ip):
    now = time.time()
    lst = [t for t in FAILS.get(ip, []) if now - t < 600]
    FAILS[ip] = lst
    return len(lst) >= 10


@app.post("/auth")
def auth():
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if too_many(ip):
        return jsonify(ok=False, error="محاولات كتير"), 429
    d = request.get_json(silent=True) or {}
    u = str(d.get("u", "")).strip().lower()
    p = str(d.get("p", "")).strip()
    if u == APP_USER.lower() and hmac.compare_digest(p.encode(), APP_PASS.encode()):
        return jsonify(ok=True, role="admin", user=u,
                       token=make_token(u, "admin", fp_of(APP_PASS)))
    rec = users_load().get(u)
    if rec and check_password_hash(rec["h"], p):
        return jsonify(ok=True, role="user", user=u,
                       token=make_token(u, "user", fp_of(rec["h"])))
    FAILS.setdefault(ip, []).append(time.time())
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
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
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
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)


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


def lock_of(user):
    """مستوى الصلاحية: 0 عادي، 1 ممنوع التحميل، 2 تصفح فقط (ممنوع المشاهدة والتحميل) + رسالة الأدمن"""
    rec = users_load().get(user) or {}
    lk = rec.get("lock")
    if lk is None:
        lk = 1 if rec.get("nodl") else 0
    try:
        lk = int(lk)
    except (TypeError, ValueError):
        lk = 0
    msg = rec.get("nodl_msg") or ("المشاهدة والتحميل مش متاحين لحسابك." if lk >= 2 else "التحميل مش متاح لحسابك.")
    return lk, msg


@app.get("/me")
def me():
    out = {"user": g.user, "role": g.role}
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
    return jsonify(items=[{"name": n, "c": v.get("c", 0), "lock": lock_of(n)[0],
                           "msg": v.get("nodl_msg", ""),
                           "wp": v.get("wp", ""), "dp": v.get("dp", "")}
                          for n, v in sorted(us.items())])


@app.post("/users")
def users_add():
    d = request.get_json(silent=True) or {}
    n = str(d.get("username", "")).strip().lower()
    p = str(d.get("password", "")).strip()
    if not USER_RX.fullmatch(n):
        return jsonify(error="الاسم 3-20 حرف إنجليزي أو أرقام أو _ . -"), 400
    if n == APP_USER.lower():
        return jsonify(error="الاسم ده محجوز للأدمن"), 400
    if len(p) < 4:
        return jsonify(error="الباسورد لازم 4 حروف على الأقل"), 400
    with USERS_LOCK:
        us = users_load()
        if n in us:
            return jsonify(error="المستخدم موجود بالفعل"), 400
        us[n] = {"h": generate_password_hash(p), "c": int(time.time())}
        users_save(us)
    return jsonify(ok=True)


@app.post("/users/<name>/password")
def users_pass(name):
    p = str((request.get_json(silent=True) or {}).get("password", "")).strip()
    if len(p) < 4:
        return jsonify(error="الباسورد لازم 4 حروف على الأقل"), 400
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


@app.delete("/users/<name>")
def users_del(name):
    with USERS_LOCK:
        us = users_load()
        if name not in us:
            return jsonify(error="المستخدم مش موجود"), 404
        us.pop(name)
        users_save(us)
    try:
        os.remove(ud_path(name))
    except OSError:
        pass
    return jsonify(ok=True)


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
        with open(ud_path(user), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("profiles", {})
    d.setdefault("items", {})
    return d


def ud_save(d, user):
    path = ud_path(user)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


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


def ws_hosts(force=False):
    """كل الدومينات المدعومة (بتتحدّث كل 24 ساعة): مزوّدينك + قايمة TorBox + قايمة AllDebrid العامة + قايمة جاهزة."""
    c = _HOSTS_CACHE
    if not force and c["hosts"] and time.time() - c["t"] < 86400:
        return c
    hosts, info = set(WS_BUILTIN), {"جاهزة": len(WS_BUILTIN)}
    for pv in load():
        n = set()
        for d in pv.get("domains", []) or []:
            _walk_domains(str(d), n)
        if n:
            info[pv["name"] if pv.get("name") else pv["type"]] = len(n)
            hosts |= n
    tbp = tb_provider()
    if tbp:
        try:
            n = set()
            _walk_domains((tb(tbp["key"], "GET", "/webdl/hosters") or {}).get("data"), n)
            info["TorBox"] = len(n)
            hosts |= n
        except Exception as ex:
            info["TorBox"] = "فشل: " + str(ex)[:60]
    if any(pv["type"] == "alldebrid" for pv in load()):
        try:    # قايمة عامة من غير مفتاح الحساب، فمفيش خطر على الحساب
            r = requests.get(AD_API + "/hosts", params={"agent": "Elkomy"}, timeout=15)
            d = (jj(r).get("data") or {}).get("hosts") or {}
            n = set()
            _walk_domains(d, n)
            info["AllDebrid"] = len(n)
            hosts |= n
        except Exception as ex:
            info["AllDebrid"] = "فشل: " + str(ex)[:60]
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
            raw.append((u, o.get("title") or ""))
    with ThreadPoolExecutor(max_workers=8) as ex:
        real = list(ex.map(lambda x: _resolve_goto(x[0]), raw))
    items, direct = [], []
    for (u0, ttl), u in zip(raw, real):
        if not u:
            continue
        h = urlparse(u).netloc.lower().replace("www.", "")
        if _host_ok(h, hosts):
            direct.append({"url": u, "title": ttl, "page": u})
        elif not _host_ok(h, WS_SKIP_PAGES) and _public_url(u):
            items.append((u, ttl))
    items = items[:10]

    pages, found = [], list(direct)

    def work(it):
        u, ttl = it
        try:
            txt, fu = _fetch_html(u)
            if txt is None:
                return {"url": u, "title": ttl, "links": [], "status": "مش بيفتح"}
            return {"url": u, "title": ws_title(txt) or ttl, "links": ws_extract(txt, fu, hosts), "status": "تمام"}
        except Exception as e:
            return {"url": u, "title": ttl, "links": [], "status": "فشل: " + str(e)[:50]}

    ex = ThreadPoolExecutor(max_workers=6)
    futs = [ex.submit(work, it) for it in items]
    try:
        for f in as_completed(futs, timeout=28):
            r = f.result()
            pages.append({"url": r["url"], "title": r["title"], "found": len(r["links"]), "status": r["status"]})
            for lk in r["links"]:
                found.append({"url": lk, "title": r["title"], "page": r["url"]})
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
    out = [{"host": h, "links": v[:30]} for h, v in
           sorted(groups.items(), key=lambda kv: (order.get(kv[0], 99), -len(kv[1])))]
    return jsonify(groups=out, pages=pages, total=len(seen_u), notes=notes,
                   searched=len(raw), hosts=len(hosts))


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
        with open(ACT_FILE, encoding="utf-8") as f:
            d = json.load(f)
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
        tmp = ACT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, ACT_FILE)


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
        with open(SFILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("addons", [])
    d.setdefault("ignored", [])
    return d


def st_save(d):
    tmp = SFILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, SFILE)


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
        with open(HOME_FILE, encoding="utf-8") as f:
            saved = json.load(f)
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
        with open(DUB_FILE, encoding="utf-8") as f:
            d = json.load(f)
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
    return jsonify(rows=out, dub=dub_status())


@app.post("/home/layout")
def home_layout_set():
    d = st_load()
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
    seen = {"dubeg"}
    for a in d["addons"]:
        for c in a.get("catalogs") or []:
            seen.add(cat_key(a["id"], c["type"], c["id"]))
    tmp = HOME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"rows": out, "seen": sorted(seen)}, f, ensure_ascii=False)
    os.replace(tmp, HOME_FILE)
    return jsonify(ok=True)


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
    tmp = DUB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "items": items}, f, ensure_ascii=False)
    os.replace(tmp, DUB_FILE)


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
        try:
            items = cat_items(amap[e["a"]], e)
        except Exception as ex:
            CAT_STATS[(e["a"], e["ty"], e["c"], e.get("g", ""))] = {"err": short_err(ex)}
            return None
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


@app.get("/activity")
def activity_list():
    admin = APP_USER.lower()
    names = [(admin, "admin")] + [(n, "user") for n in sorted(users_load())]
    act = act_load()
    out = []
    for n, role in names:
        a = act.get(n, {})
        secs, titles, last_w, _ = watch_stats(n)
        ev = a.get("ev") or []
        last = max(last_w, ev[-1]["t"] if ev else 0, int(LAST.get(n, 0) * 1000))
        out.append({"name": n, "role": role, "bytes": a.get("bytes", 0),
                    "count": a.get("count", 0), "watch": secs, "titles": titles,
                    "last": last})
    return jsonify(items=out)


@app.get("/activity/<name>")
def activity_user(name):
    name = name.lower()
    if name != APP_USER.lower() and name not in users_load():
        return jsonify(error="المستخدم مش موجود"), 404
    a = act_load().get(name, {})
    _, _, _, watched = watch_stats(name)
    return jsonify(pulls=list(reversed(a.get("ev") or []))[:100], watched=watched[:100],
                   bytes=a.get("bytes", 0), count=a.get("count", 0))


# ----------------- TMDB -----------------

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
        ar = tmdb_get("/search/multi", query=q, language="ar",
                      include_adult="false").get("results", [])
        en_list = tmdb_get("/search/multi", query=q, language="en-US",
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
        })
    out.sort(key=lambda i: i["seeders"], reverse=True)
    return out


@app.get("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    try:
        out = prowlarr_search(q, CATS.get(request.args.get("cat", "")))
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


def build_sub(u, season, episode, ar, fmt="vtt", shift=0.0, scale=1.0):
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
                            "ar" in str(p.get("lang", "")).lower(), p.get("f", "srt"), shift, scale)
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
    try:
        text, mt = build_sub(u, season, episode, ar, fmt, shift, scale)
        return Response(text, mimetype=mt)
    except LookupError as ex:
        return jsonify(error=str(ex)), 404
    except zipfile.BadZipFile:
        return jsonify(error="ملف الترجمة تالف"), 502
    except requests.Timeout:
        return jsonify(error="تحميل الترجمة أخد وقت طويل"), 504
    except Exception as ex:
        return jsonify(error=f"الترجمة: {ex}"), 500


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
        with open(PUSH_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("subs", {})
    d.setdefault("state", {})
    return d


def push_save(d):
    tmp = PUSH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, PUSH_FILE)


def vapid_keys():
    """مفاتيح VAPID بتتعمل أول مرة وتتحفظ في DATA_DIR"""
    pem = os.path.join(DATA_DIR, "vapid_private.pem")
    pubf = os.path.join(DATA_DIR, "vapid_public.txt")
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
