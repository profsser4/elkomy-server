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
​import requests
from flask import Flask, request, jsonify, Response, g, make_response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
​app = Flask(name)
CORS(app)
​APP_USER = os.environ.get("APP_USER", "admin")
APP_PASS = os.environ.get("APP_PASS", "admin")
TMDB_KEY = os.environ.get("TMDB_KEY", "")
SUBDL_KEY = os.environ.get("SUBDL_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_KEY = os.environ.get("PROWLARR_KEY", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)
​PFILE = os.path.join(DATA_DIR, "providers.json")
UDFILE = os.path.join(DATA_DIR, "userdata.json")
SFILE = os.path.join(DATA_DIR, "stremio.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
HOME_FILE = os.path.join(DATA_DIR, "home.json")
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
SECRET = os.environ.get("SECRET_KEY") or APP_PASS
REFS = {}
FAILS = {}
USER_RX = re.compile(r"[a-z0-9_.-]{3,20}")
​TYPES = {
"torbox": "TorBox",
"realdebrid": "Real-Debrid",
"alldebrid": "AllDebrid",
"premiumize": "Premiumize",
"1fichier": "1Fichier",
"debridlink": "Debrid-Link",
}
​DEFAULT_DOMAINS = {
"1fichier": [
"1fichier.com", "alterupload.com", "cjoint.net", "desfichiers.com",
"dfichiers.com", "megadl.fr", "mesfichiers.org", "piecejointe.net",
"pjointe.com", "tenvoi.com", "dl4free.com",
],
}
​PRIORITY = {
"realdebrid": 1, "alldebrid": 2, "debridlink": 3,
"premiumize": 4, "torbox": 9,
}
​ENV_SEED = {
"debridlink": "DEBRIDLINK_KEY",
"torbox": "TORBOX_KEY", "1fichier": "ONEFICHIER_KEY",
"realdebrid": "REALDEBRID_KEY", "alldebrid": "ALLDEBRID_KEY",
"premiumize": "PREMIUMIZE_KEY",
}
​TORBOX_BASE = "https://api.torbox.app/v1/api"
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
re.compile(r"sS[ .-]*eE"),
re.compile(r"(?<!\d)(\d{1,2})xX(?!\d)"),
)
EP_LOOSE = (
re.compile(r"(?<![A-Za-z0-9])(?:Episode|EP|E)[ .-]?(\d{1,3})(?!\d)", re.I),
re.compile(r"الحلقة[ ._-]*(\d{1,3})"),
)
CATS = {"movie": "2000", "tv": "5000", "anime": "5070"}
LATIN_RE = re.compile(r"[A-Za-z\u00C0-\u024F]")
NONLAT_RE = re.compile(
r"[\u0400-\u04FF\u0590-\u06FF\u0750-\u077F\u3040-\u30FF"
r"\u4E00-\u9FFF\uAC00-\uD7AF]"
)
​def load():
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
​def save(ps):
with open(PFILE, "w", encoding="utf-8") as f:
json.dump(ps, f, ensure_ascii=False)
​def dom(x):
x = x.strip().lower()
h = urlparse(x if "//" in x else "//" + x).netloc
return h.replace("www.", "")
​def candidates(u):
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
​def jj(r):
try:
return r.json()
except ValueError:
raise Exception(f"رد غير مفهوم من الموقع (كود {r.status_code})")
​def users_load():
try:
with open(USERS_FILE, encoding="utf-8") as f:
d = json.load(f)
return d if isinstance(d, dict) else {}
except Exception:
return {}
​def users_save(d):
tmp = USERS_FILE + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
json.dump(d, f, ensure_ascii=False)
os.replace(tmp, USERS_FILE)
​def fp_of(s):
return hashlib.sha256(("fp:" + s).encode()).hexdigest()[:10]
​def sign(body):
return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
​def make_token(user, role, fp):
body = f"{user}|{role}|{int(time.time()) + 30 * 86400}|{fp}"
return base64.urlsafe_b64encode(body.encode()).decode() + "." + sign(body)
​def read_token(tok):
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
​def needs_admin(path, method):
if path.startswith(("/providers", "/users", "/home/", "/activity", "/push/check")):
return True
if path in ("/stremio/login", "/stremio/sync", "/stremio/logout", "/stremio/library"):
return True
if path.startswith("/stremio/addon"):
return not (path == "/stremio/addons" and method == "GET")
return False
​@app.before_request
def guard():
open_paths = ("/", "/auth", "/ud/beacon")
if request.method == "OPTIONS" or request.path in open_paths:
return None
who = read_token(request.headers.get("X-Token", ""))
if not who:
return jsonify(error="غير مصرّح"), 401
g.user, g.role = who
LAST[g.user] = time.time()
if g.role != "admin" and needs_admin(request.path, request.method):
return jsonify(error="للأدمن بس"), 403
return None
​@app.get("/")
def home():
return "Elkomy Server is Active! 🚀"
​@app.post("/auth")
def auth():
ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "")
d = request.get_json(silent=True) or {}
u = str(d.get("u", "")).strip().lower()
p = str(d.get("p", "")).strip()
if u == APP_USER.lower() and hmac.compare_digest(p.encode(), APP_PASS.encode()):
return jsonify(ok=True, role="admin", user=u, token=make_token(u, "admin", fp_of(APP_PASS)))
rec = users_load().get(u)
if rec and check_password_hash(rec["h"], p):
return jsonify(ok=True, role="user", user=u, token=make_token(u, "user", fp_of(rec["h"])))
return jsonify(ok=False), 401
​@app.get("/me")
def me():
return jsonify(user=g.user, role=g.role)
​@app.get("/users")
def users_list():
us = users_load()
return jsonify(items=[{"name": n, "c": v.get("c", 0)} for n, v in sorted(us.items())])
​@app.post("/users")
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
​@app.delete("/users/<name>")
def users_del(name):
with USERS_LOCK:
us = users_load()
if name not in us:
return jsonify(error="المستخدم مش موجود"), 404
us.pop(name)
users_save(us)
return jsonify(ok=True)
​@app.get("/providers")
def providers_get():
items = [{
"id": p["id"], "name": p["name"], "type": p["type"],
"domains": p.get("domains", []), "key": "••••" + p["key"][-4:],
} for p in load()]
return jsonify(types=TYPES, items=items)
​@app.post("/providers")
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
​@app.delete("/providers/<pid>")
def providers_del(pid):
save([p for p in load() if p["id"] != pid])
return jsonify(ok=True)
​@app.get("/providers/export")
def providers_export():
return jsonify(items=load())
​@app.post("/providers/import")
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
​def tnum(x):
t = x.get("t") if isinstance(x, dict) else 0
return t if isinstance(t, (int, float)) else 0
​def ud_path(user):
if user == APP_USER.lower():
return UDFILE
return os.path.join(DATA_DIR, f"ud_{user}.json")
​def ud_load(user):
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
​def ud_save(d, user):
path = ud_path(user)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
json.dump(d, f, ensure_ascii=False)
os.replace(tmp, path)
​@app.get("/ud")
def ud_get():
with UD_LOCK:
return jsonify(ud_load(g.user))
​@app.post("/ud/merge")
def ud_merge_ep():
inc = request.get_json(silent=True) or {}
with UD_LOCK:
base = ud_load(g.user)
for pid, p in inc.get("profiles", {}).items():
base["profiles"][pid] = p
for pid, its in inc.get("items", {}).items():
dst = base["items"].setdefault(pid, {})
for k, it in its.items():
dst[k] = it
ud_save(base, g.user)
if request.args.get("lite"):
return jsonify(ok=True)
return jsonify(base)
​@app.post("/ud/beacon")
def ud_beacon():
try:
d = json.loads(request.get_data(as_text=True) or "{}")
except ValueError:
return "", 400
who = read_token(str(d.get("t", "")))
if not who:
return "", 401
with UD_LOCK:
base = ud_load(who[0])
ud_save(base, who[0])
return "", 204
​@app.get("/rows")
def rows():
return jsonify(rows=[])
​@app.get("/tmdb")
def tmdb():
q = request.args.get("q", "").strip()
return jsonify(results=[])
​@app.get("/details")
def details():
return jsonify(error="Not found"), 404
​@app.get("/season")
def season():
return jsonify(episodes=[])
​@app.get("/search")
def search():
return jsonify(results=[])
​@app.get("/subs")
def subs():
return jsonify(items=[])
​@app.get("/sub")
def sub_get():
return jsonify(error="Not found"), 404
​@app.get("/activity")
def activity_list():
return jsonify(items=[])
​@app.get("/home/layout")
def home_layout_get():
return jsonify(rows=[])
​@app.post("/home/layout")
def home_layout_set():
return jsonify(ok=True)
​@app.get("/home/catalogs")
def home_catalogs():
return jsonify(items=[])
​@app.post("/generate")
def generate():
return jsonify(error="Not implemented"), 400
​@app.get("/stremio/addons")
def st_list():
return jsonify(logged=False, email="", items=[], n=0)
​@app.get("/stremio/library")
def st_library():
return jsonify(items=[])
​@app.get("/stremio/streams")
def st_streams():
return jsonify(results=[])
​if name == "main":
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
