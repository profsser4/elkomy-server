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
from flask import Flask, request, jsonify, Response, g
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
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
PFILE = os.path.join(DATA_DIR, "providers.json")
UDFILE = os.path.join(DATA_DIR, "userdata.json")
SFILE = os.path.join(DATA_DIR, "stremio.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
UD_LOCK = threading.Lock()
ST_LOCK = threading.Lock()
USERS_LOCK = threading.Lock()
REF_LOCK = threading.Lock()
MAX_PROFILES = 10
MAX_ITEMS = 150
SECRET = os.environ.get("SECRET_KEY") or APP_PASS
REFS = {}
FAILS = {}
USER_RX = re.compile(r"[a-z0-9_.-]{3,20}")

TYPES = {
    "torbox": "TorBox",
    "realdebrid": "Real-Debrid",
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
    "realdebrid": "REALDEBRID_KEY", "alldebrid": "ALLDEBRID_KEY",
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

def load():
    if not os.path.exists(PFILE):
        seed = []
        for t, env in ENV_SEED.items():
            k = os.environ.get(env, "").strip()
            if k:
                seed.append({
                    "id": uuid.uuid4().hex[:8], "type": t, "name": TYPES[t],
                    "key": k, "domains": DEFAULT_DOMAINS.get(t, []),
                })
        if seed:
            save(seed)
        return seed
    try:
        with open(PFILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save(ps):
    with open(PFILE, "w", encoding="utf-8") as f:
        json.dump(ps, f, ensure_ascii=False)


def dom(x):
    x = x.strip().lower()
    h = urlparse(x if "//" in x else "//" + x).netloc
    return h.replace("www.", "")


def candidates(u):
    host = urlparse(u).netloc.lower().replace("www.", "")
    ps = load()

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
    if path.startswith(("/providers", "/users")):
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
    if request.method == "OPTIONS" or request.path in open_paths:
        return None
    who = read_token(request.headers.get("X-Token", ""))
    if not who:
        return jsonify(error="غير مصرّح"), 401
    g.user, g.role = who
    if g.role != "admin" and needs_admin(request.path, request.method):
        return jsonify(error="للأدمن بس"), 403
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


@app.get("/me")
def me():
    return jsonify(user=g.user, role=g.role)


@app.get("/users")
def users_list():
    us = users_load()
    return jsonify(items=[{"name": n, "c": v.get("c", 0)}
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
        if len(vids) > 1:
            resp["pack"] = True
            resp["files"] = vids
            resp["total_size"] = info.get("size")
        return jsonify(resp)
    except Exception as e:
        return jsonify(error=f"TorBox: {e}"), 400


# ----------------- تورنت على TorBox / Real-Debrid / Debrid-Link -----------------

TORRENT_TYPES = ("torbox", "debridlink", "realdebrid")   # الترتيب = الأولوية
CHECKABLE = ("torbox", "debridlink")                      # بيدعموا فحص "فوري"
RD_API = "https://api.real-debrid.com/rest/1.0"
DL_API = "https://debrid-link.com/api/v2"


def torrent_providers():
    ps = [p for p in load() if p["type"] in TORRENT_TYPES]
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


def rd_link(k, f):
    code, d = rd(k, "POST", "/unrestrict/link", data={"link": f["link"]})
    if d.get("download"):
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


ADAPT = {
    "realdebrid": {"find": rd_find, "add": rd_add, "info": rd_info, "link": rd_link},
    "debridlink": {"find": dl_find, "add": dl_add, "info": dl_info,
                   "link": lambda k, f: f["link"]},
}


def cached_for(p, hs):
    if p["type"] == "torbox":
        return tb_cached(p["key"], hs)
    if p["type"] == "debridlink":
        return dl_cached(p["key"], hs)
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


def ordered_tprovs(h):
    ps = torrent_providers()
    first = []
    for p in ps:
        if h and p["type"] in CHECKABLE:
            try:
                if h in cached_for(p, [h]):
                    first.append(p)
            except Exception:
                pass
    return first + [p for p in ps if p not in first]


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


def torrent_dispatch(q, tid, file_id=None, want_zip=False, want_all=False):
    t = str(tid) if tid else ""
    if t.startswith("x:"):
        _, _, pid, rid = t.split(":", 3)
        p = next((x for x in load() if x["id"] == pid), None)
        if not p:
            return jsonify(error="المزوّد اتحذف"), 400
        return generic_flow(p, q, rid, file_id, want_zip, want_all)
    if t:
        return torrent_flow(q, tid, file_id, want_zip, want_all)
    ps = ordered_tprovs(magnet_hash(q))
    if not ps:
        return jsonify(
            error="ضيف مزوّد (TorBox أو Real-Debrid أو Debrid-Link) من الإعدادات الأول"), 400
    last = None
    for p in ps:
        if p["type"] == "torbox":
            r = torrent_flow(q, None, file_id, want_zip, want_all)
        else:
            r = generic_flow(p, q, None, file_id, want_zip, want_all)
        code = r[1] if isinstance(r, tuple) else r.status_code
        if code in (200, 202):
            return r
        last = r  # المزوّد ده فشل (اشتراك منتهي مثلاً)، نجرّب اللي بعده
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


@app.post("/generate")
def generate():
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
            return torrent_dispatch(
                q, tid,
                file_id=data.get("file_id"),
                want_zip=bool(data.get("zip")),
                want_all=bool(data.get("all")),
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


# ----------------- Stremio (حساب + إضافات + مكتبة) -----------------

DEBRID_RX = re.compile(
    r"(?:^|[/|,;])(realdebrid|alldebrid|premiumize|debridlink|torbox)="
    r"([^/|,;&\s]+)", re.I)
SEEDS_RX = re.compile(r"👤\s*(\d+)")
SIZE_RX = re.compile(r"💾\s*([\d.]+)\s*(TB|GB|MB|KB)", re.I)
UNITS = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


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


def st_add(d, base, man):
    if any(a["base"] == base for a in d["addons"]):
        return False
    info = st_info(man)
    d["addons"].append({
        "id": uuid.uuid4().hex[:8], "base": base,
        "name": man.get("name") or "Addon",
        "stream": info is not None,
        "types": (info or {}).get("types", []),
        "prefixes": (info or {}).get("prefixes", []),
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


def ref_for(url):
    rid = uuid.uuid4().hex[:16]
    with REF_LOCK:
        REFS[rid] = url
        while len(REFS) > 800:
            REFS.pop(next(iter(REFS)))
    return rid


@app.get("/stremio/streams")
def st_streams():
    is_admin = g.role == "admin"
    typ = "series" if request.args.get("type") == "tv" else "movie"
    imdb = request.args.get("imdb", "")
    s, e = request.args.get("s", ""), request.args.get("e", "")
    if not re.fullmatch(r"tt\d+", imdb):
        return jsonify(error="العمل ده ملوش IMDb id"), 400
    sid = f"{imdb}:{s}:{e}" if typ == "series" and s.isdigit() and e.isdigit() else imdb

    def usable(a):
        if not a.get("stream"):
            return False
        if a.get("types") and typ not in a["types"]:
            return False
        pre = a.get("prefixes")
        return not pre or any(imdb.startswith(p) for p in pre)

    own = bool(torrent_providers())

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
                zm = SIZE_RX.search(txt)
                size = bh.get("videoSize") or (
                    int(float(zm.group(1)) * UNITS[zm.group(2).upper()]) if zm else 0)
                u = x.get("url") or ""
                direct = u if u.startswith(("http://", "https://")) else ""
                if not direct and not h:
                    continue
                out.append({
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
    ref = str((request.get_json(silent=True) or {}).get("ref", ""))
    url = REFS.get(ref)
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
                    body = r.raw.read(300, decode_content=True).decode("utf-8", "ignore")
                except Exception:
                    body = ""
            r.close()
            if code in (301, 302, 303, 307, 308) and loc:
                cur = urljoin(cur, loc)
                continue
            if code >= 400:
                msg = f"المصدر رد {code}"
                if g.role == "admin" and body.strip():
                    msg += ": " + re.sub(r"\s+", " ", body)[:200]
                return jsonify(error=msg), 502
            break
        if g.role != "admin" and any(p.get("key") and p["key"] in cur for p in load()):
            return jsonify(error="المصدر ده مش متاح للمستخدمين العاديين"), 403
        return jsonify(url=cur)
    except Exception as ex:
        return jsonify(error=f"فشل تجهيز الرابط: {short_err(ex)}"), 502


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


@app.get("/rows")
def rows():
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


def to_vtt(text, shift=0.0):
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\{\\[^}]*\}", "", text).strip()
    if text.startswith("WEBVTT"):
        text = text[6:]
    lines = []
    for line in text.split("\n"):
        if "-->" in line:
            line = TS_RE.sub(lambda m: fmt_secs(ts_secs(m) + shift), line)
        lines.append(line)
    return "WEBVTT\n\n" + "\n".join(lines).strip() + "\n"


def ass_secs(t):
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def ass_to_vtt(text, shift=0.0):
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
        out.append(fmt_secs(st + shift) + " --> " + fmt_secs(en + shift))
        out.append(txt)
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


def build_sub(u, season, episode, ar, fmt="vtt", shift=0.0):
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
    if ext in (".ass", ".ssa"):
        vtt = ass_to_vtt(text, shift)
    else:
        vtt = to_vtt(text, shift)
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
    se, ep = request.args.get("s", ""), request.args.get("e", "")
    season = int(se) if se.isdigit() else None
    episode = int(ep) if ep.isdigit() else None
    ar = "ar" in request.args.get("lang", "").lower()
    try:
        text, mt = build_sub(u, season, episode, ar, fmt, shift)
        return Response(text, mimetype=mt)
    except LookupError as ex:
        return jsonify(error=str(ex)), 404
    except zipfile.BadZipFile:
        return jsonify(error="ملف الترجمة تالف"), 502
    except requests.Timeout:
        return jsonify(error="تحميل الترجمة أخد وقت طويل"), 504
    except Exception as ex:
        return jsonify(error=f"الترجمة: {ex}"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
