import os
import io
import re
import json
import uuid
import hmac
import time
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==============================================================
# Variables على Railway: TMDB_KEY, PROWLARR_URL, PROWLARR_KEY,
# SUBDL_KEY, APP_USER, APP_PASS (الافتراضي admin/admin),
# DATA_DIR (اختياري)
# مفاتيح المزوّدين بتتضاف من جوه الموقع مش من الكود
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
UD_LOCK = threading.Lock()
MAX_PROFILES = 10
MAX_ITEMS = 150

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


@app.before_request
def guard():
    open_paths = ("/", "/auth", "/ud/beacon")
    if request.method == "OPTIONS" or request.path in open_paths:
        return None
    sent = request.headers.get("X-Pass", "")
    if not hmac.compare_digest(sent.encode(), APP_PASS.encode()):
        return jsonify(error="غير مصرّح"), 401
    return None


@app.get("/")
def home():
    return "Elkomy Server is Active! 🚀"


@app.post("/auth")
def auth():
    d = request.get_json() or {}
    u_ok = str(d.get("u", "")).strip().lower() == APP_USER.lower()
    p_ok = str(d.get("p", "")).strip() == APP_PASS
    if u_ok and p_ok:
        return jsonify(ok=True), 200
    return jsonify(ok=False), 401


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


def ud_load():
    try:
        with open(UDFILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("profiles", {})
    d.setdefault("items", {})
    return d


def ud_save(d):
    tmp = UDFILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, UDFILE)


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
        return jsonify(ud_load())


@app.post("/ud/merge")
def ud_merge_ep():
    inc = request.get_json(silent=True) or {}
    with UD_LOCK:
        base = ud_merge(ud_load(), inc)
        ud_save(base)
    if request.args.get("lite"):
        return jsonify(ok=True)
    return jsonify(base)


@app.post("/ud/beacon")
def ud_beacon():
    try:
        d = json.loads(request.get_data(as_text=True) or "{}")
    except ValueError:
        return "", 400
    sent = str(d.get("p", ""))
    if not hmac.compare_digest(sent.encode(), APP_PASS.encode()):
        return "", 401
    with UD_LOCK:
        base = ud_merge(ud_load(), d.get("data") or {})
        ud_save(base)
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


@app.get("/cached")
def cached():
    hs = [h for h in request.args.get("h", "").lower().split(",") if h][:60]
    p = tb_provider()
    if not hs or not p:
        return jsonify(cached=[])
    try:
        r = tb(p["key"], "GET", "/torrents/checkcached",
               params={"hash": ",".join(hs), "format": "list"})
        d = r.get("data") or []
        if isinstance(d, dict):
            found = list(d.keys())
        else:
            found = [x.get("hash") if isinstance(x, dict) else x for x in d]
        return jsonify(cached=[str(x).lower() for x in found if x])
    except Exception:
        return jsonify(cached=[])


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
            return torrent_flow(
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
            date = x.get("release_date") or x.get("first_air_date") or ""
            out.append({
                "id": x["id"], "type": x["media_type"], "year": date[:4],
                "title": x.get("title") or x.get("name") or "",
                "search_title": e.get("title") or e.get("name")
                or x.get("original_title") or x.get("original_name") or "",
                "poster": img(x.get("poster_path"), "w342"),
            })
        return jsonify(results=out[:30])
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/trending")
def trending():
    try:
        res = tmdb_get("/trending/all/week", language="ar").get("results", [])
        out = []
        for x in res:
            if x.get("media_type") not in ("movie", "tv"):
                continue
            date = x.get("release_date") or x.get("first_air_date") or ""
            out.append({
                "id": x["id"], "type": x["media_type"], "year": date[:4],
                "title": x.get("title") or x.get("name") or "",
                "poster": img(x.get("poster_path"), "w342"),
            })
        return jsonify(results=out[:20])
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/details")
def details():
    t, i = request.args.get("type"), request.args.get("id")
    if t not in ("movie", "tv") or not i:
        return jsonify(error="بيانات ناقصة"), 400
    try:
        a = tmdb_get(f"/{t}/{i}", language="ar",
                     append_to_response="credits,alternative_titles")
        e = tmdb_get(f"/{t}/{i}", language="en-US")
        date = a.get("release_date") or a.get("first_air_date") or ""
        rt = a.get("runtime") or (a.get("episode_run_time") or [None])[0]
        cast = (a.get("credits") or {}).get("cast", [])[:10]
        lat = latin_titles(a, e)
        return jsonify(
            id=int(i), type=t, year=date[:4], runtime=rt,
            title=a.get("title") or a.get("name") or "",
            search_title=lat[0] if lat else "",
            alt=lat,
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

@app.get("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    if not (PROWLARR_URL and PROWLARR_KEY):
        return jsonify(error="PROWLARR_URL و PROWLARR_KEY مش متضافين في Variables"), 500
    params = {"query": q, "type": "search", "limit": 100}
    cat = CATS.get(request.args.get("cat", ""))
    if cat:
        params["categories"] = cat
    try:
        r = requests.get(f"{PROWLARR_URL}/api/v1/search",
                         headers={"X-Api-Key": PROWLARR_KEY},
                         params=params, timeout=45)
        if r.status_code != 200:
            return jsonify(error=f"Prowlarr رجّع {r.status_code}: تأكد من الرابط والمفتاح"), 502
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

    attempts = []
    if base.get("type") == "tv" and s.isdigit():
        se = dict(base, season_number=s)
        if e.isdigit():
            attempts.append(("exact", dict(se, episode_number=e)))
        attempts.append(("season", dict(se, full_season=1)))
    last = "loose" if base.get("type") == "tv" else "exact"
    attempts.append((last, base))
    try:
        for mode, params in attempts:
            items = sub_items(subdl_call(params))
            if items:
                return jsonify(items=items[:40], mode=mode)
        return jsonify(items=[], mode="none")
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
        data = get_zip(u)
        if data[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(data))
            info = pick_sub(z, season, episode)
            if not info:
                return jsonify(error="مفيش ملف ترجمة مدعوم جوه الأرشيف"), 404
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
            return Response(vtt_to_srt(vtt), mimetype="application/x-subrip")
        return Response(vtt, mimetype="text/vtt")
    except zipfile.BadZipFile:
        return jsonify(error="ملف الترجمة تالف"), 502
    except requests.Timeout:
        return jsonify(error="تحميل الترجمة أخد وقت طويل"), 504
    except Exception as ex:
        return jsonify(error=f"الترجمة: {ex}"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
