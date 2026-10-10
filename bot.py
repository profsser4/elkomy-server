import os
import re
import json
import uuid
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==============================================================
# Variables على Railway: TMDB_KEY, PROWLARR_URL, PROWLARR_KEY,
# APP_USER, APP_PASS (الافتراضي admin/admin), DATA_DIR (اختياري)
# مفاتيح المزوّدين بتتضاف من جوه الموقع مش من الكود
# ==============================================================
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASS = os.environ.get("APP_PASS", "admin")
TMDB_KEY = os.environ.get("TMDB_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_KEY = os.environ.get("PROWLARR_KEY", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
PFILE = os.path.join(DATA_DIR, "providers.json")

TYPES = {
    "torbox": "TorBox",
    "realdebrid": "Real-Debrid",
    "alldebrid": "AllDebrid",
    "premiumize": "Premiumize",
    "1fichier": "1Fichier",
    "debridlink": "Debrid-Link",
}
DEFAULT_DOMAINS = {"1fichier": ["1fichier.com"]}  # أنواع بتشتغل على موقعها بس
ENV_SEED = {
    "debridlink": "DEBRIDLINK_KEY",
    "torbox": "TORBOX_KEY", "1fichier": "ONEFICHIER_KEY",
    "realdebrid": "REALDEBRID_KEY", "alldebrid": "ALLDEBRID_KEY",
    "premiumize": "PREMIUMIZE_KEY",
}

TORBOX_BASE = "https://api.torbox.app/v1/api"
VIDEO = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v")
TRACKERS = (
    "&tr=udp://tracker.opentrackr.org:1337/announce"
    "&tr=udp://open.stealth.si:80/announce"
    "&tr=udp://tracker.bittor.pw:1337/announce"
)


# ----------------- المزوّدين (محفوظين في ملف) -----------------

def load():
    if not os.path.exists(PFILE):
        seed = []
        for t, env in ENV_SEED.items():
            k = os.environ.get(env, "").strip()
            if k:
                seed.append({"id": uuid.uuid4().hex[:8], "type": t, "name": TYPES[t],
                             "key": k, "domains": DEFAULT_DOMAINS.get(t, [])})
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
    hit = lambda p: any(d and (host == d or host.endswith("." + d)) for d in p["domains"])
    first = [p for p in ps if hit(p)]
    rest = [p for p in ps if p not in first and p["type"] not in DEFAULT_DOMAINS]
    return first + rest


@app.before_request
def guard():
    if request.method == "OPTIONS" or request.path in ("/", "/auth"):
        return None
    if request.headers.get("X-Pass") != APP_PASS:
        return jsonify(error="غير مصرّح"), 401
    return None


@app.get("/")
def home():
    return "Elkomy Server is Active! 🚀"


@app.post("/auth")
def auth():
    d = request.get_json() or {}
    ok = str(d.get("u", "")).strip().lower() == APP_USER.lower() and str(d.get("p", "")).strip() == APP_PASS
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False), 401)


@app.get("/providers")
def providers_get():
    items = [{"id": p["id"], "name": p["name"], "type": p["type"], "domains": p["domains"],
              "key": "••••" + p["key"][-4:]} for p in load()]
    return jsonify(types=TYPES, items=items)


@app.post("/providers")
def providers_add():
    d = request.get_json() or {}
    t = d.get("type")
    k = (d.get("key") or "").strip()
    if t not in TYPES or not k:
        return jsonify(error="النوع أو المفتاح ناقص"), 400
    doms = [dom(x) for x in re.split(r"[,\s]+", d.get("domains") or "") if x.strip()]
    doms = doms or DEFAULT_DOMAINS.get(t, [])
    ps = load()
    ps.append({"id": uuid.uuid4().hex[:8], "type": t, "name": (d.get("name") or TYPES[t]).strip(),
               "key": k, "domains": doms})
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
    items = [x for x in ((request.get_json() or {}).get("items") or [])
             if isinstance(x, dict) and x.get("type") in TYPES and x.get("key")]
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
            ok = requests.get("https://api.real-debrid.com/rest/1.0/user", headers=h, timeout=15).status_code == 200
        elif t == "alldebrid":
            ok = requests.get("https://api.alldebrid.com/v4/user", params={"agent": "Elkomy", "apikey": k}, timeout=15).json().get("status") == "success"
        elif t == "premiumize":
            ok = requests.get("https://www.premiumize.me/api/account/info", params={"apikey": k}, timeout=15).json().get("status") == "success"
        elif t == "debridlink":
            ok = requests.get("https://debrid-link.com/api/v2/account/infos", headers=h, timeout=15).json().get("success") is True
        else:
            r = requests.post("https://api.1fichier.com/v1/user/info.cgi", headers=h, json={}, timeout=15)
            ok = r.status_code == 200 and r.json().get("status") != "KO"
        return jsonify(ok=bool(ok))
    except Exception as e:
        return jsonify(ok=False, error=str(e))


# ----------------- TorBox -----------------

def tb(key, method, path, **kw):
    r = requests.request(method, TORBOX_BASE + path,
                         headers={"Authorization": f"Bearer {key}"}, timeout=20, **kw)
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


def prog(info, prov):
    return {
        "provider": prov, "status": info.get("download_state"),
        "progress": info.get("progress"), "speed": info.get("download_speed"),
        "eta": info.get("eta"), "seeds": info.get("seeds"), "peers": info.get("peers"),
        "size": info.get("size"), "file": info.get("name"),
    }


def pick_file(files):
    vids = [f for f in files if f["name"].lower().endswith(VIDEO)] or files
    return max(vids, key=lambda f: f.get("size", 0))


def clean_torbox(k):
    items = tb(k, "GET", "/webdl/mylist").get("data") or []
    for x in (items if isinstance(items, list) else []):
        if not (x.get("download_finished") or x.get("download_present")):
            tb(k, "POST", "/webdl/controlwebdownload", json={"webdl_id": x["id"], "operation": "delete"})


def torbox_web(p, q, web_id=None):
    k = p["key"]
    if not web_id:
        add = tb(k, "POST", "/webdl/createwebdownload", data={"link": q})
        if not add.get("success") and "limit" in str(add.get("detail") or add.get("error") or "").lower():
            clean_torbox(k)
            add = tb(k, "POST", "/webdl/createwebdownload", data={"link": q})
        if not add.get("success"):
            raise Exception(add.get("detail") or add.get("error") or "الرابط غير مدعوم")
        d = add.get("data") or {}
        web_id = d.get("webdownload_id") or d.get("id")
    info = as_dict(tb(k, "GET", "/webdl/mylist", params={"id": web_id, "bypass_cache": "true"}).get("data"))
    tid = f"web:{p['id']}:{web_id}"
    st = str(info.get("download_state", "")).lower()
    if st in ("error", "failed", "expired"):
        raise Exception(f"فشل التحميل ({st})")
    if info and not (info.get("download_finished") or info.get("download_present")):
        return {"error": "جاري التجهيز...", "torrent_id": tid, **prog(info, p["name"])}, 202
    dl = tb(k, "GET", "/webdl/requestdl", params={"token": k, "web_id": web_id, "file_id": 0})
    if dl.get("success"):
        return {"direct_link": dl["data"], "name": info.get("name") or "ملف",
                "size": info.get("size"), "provider": p["name"]}, 200
    return {"error": "جاري التجهيز...", "torrent_id": tid, "provider": p["name"]}, 202


def torrent_flow(q, tid):
    p = tb_provider()
    if not p:
        return jsonify(error="ضيف TorBox من إعدادات المزوّدين الأول"), 400
    k = p["key"]
    if not tid:
        if "&tr=" not in q:
            q += TRACKERS
        add = tb(k, "POST", "/torrents/createtorrent", data={"magnet": q, "seed": 1})
        if not add.get("success"):
            return jsonify(error=add.get("detail") or add.get("error") or "فشل إضافة التورنت"), 400
        tid = add["data"]["torrent_id"]
    info = as_dict(tb(k, "GET", "/torrents/mylist", params={"id": tid, "bypass_cache": "true"}).get("data"))
    if not info or not (info.get("download_finished") or info.get("download_present")):
        return jsonify(error="جاري التجهيز...", torrent_id=tid, **prog(info, p["name"])), 202
    files = info.get("files", [])
    if not files:
        return jsonify(error="التورنت فاضي مفيهوش ملفات"), 400
    f = pick_file(files)
    dl = tb(k, "GET", "/torrents/requestdl", params={"token": k, "torrent_id": tid, "file_id": f["id"]})
    if dl.get("success"):
        return jsonify(direct_link=dl["data"], name=f["name"], size=f.get("size"), provider=p["name"])
    return jsonify(error="TorBox رفض إنشاء الرابط"), 400


@app.get("/cached")
def cached():
    hs = [h for h in request.args.get("h", "").lower().split(",") if h][:60]
    p = tb_provider()
    if not hs or not p:
        return jsonify(cached=[])
    try:
        r = tb(p["key"], "GET", "/torrents/checkcached", params={"hash": ",".join(hs), "format": "list"})
        d = r.get("data") or []
        found = list(d.keys()) if isinstance(d, dict) else [x.get("hash") if isinstance(x, dict) else x for x in d]
        return jsonify(cached=[str(x).lower() for x in found if x])
    except Exception:
        return jsonify(cached=[])


# ----------------- باقي المزوّدين (روابط فورية) -----------------

def r_1fichier(k, u):
    res = requests.post("https://1fichier.com/v1/download/get_token.cgi", json={"url": u},
                        headers={"Authorization": f"Bearer {k}"}, timeout=15).json()
    if res.get("status") == "OK" and res.get("url"):
        return res["url"]
    raise Exception(res.get("message", "فشل"))


def r_realdebrid(k, u):
    res = requests.post("https://api.real-debrid.com/rest/1.0/unrestrict/link",
                        headers={"Authorization": f"Bearer {k}"}, data={"link": u}, timeout=15).json()
    if res.get("download"):
        return res["download"]
    raise Exception(str(res.get("error", "فشل")))


def r_alldebrid(k, u):
    res = requests.get("https://api.alldebrid.com/v4/link/unlock",
                       params={"agent": "Elkomy", "apikey": k, "link": u}, timeout=15).json()
    if res.get("status") == "success" and res.get("data", {}).get("link"):
        return res["data"]["link"]
    raise Exception(str(res.get("error", {}).get("message", "فشل")))


def r_premiumize(k, u):
    res = requests.post("https://www.premiumize.me/api/transfer/directdl",
                        data={"src": u, "apikey": k}, timeout=20).json()
    if res.get("status") == "success":
        links = [c for c in (res.get("content") or []) if c.get("link")]
        if links:
            return max(links, key=lambda c: c.get("size", 0))["link"]
        if res.get("location"):
            return res["location"]
    raise Exception(str(res.get("message", "الرابط غير مدعوم")))


def r_debridlink(k, u):
    res = requests.post("https://debrid-link.com/api/v2/downloader/add",
                        headers={"Authorization": f"Bearer {k}"}, data={"url": u}, timeout=20).json()
    if res.get("success") and (res.get("value") or {}).get("downloadUrl"):
        return res["value"]["downloadUrl"]
    raise Exception(str(res.get("error", "فشل")))


INSTANT = {"debridlink": r_debridlink, "1fichier": r_1fichier, "realdebrid": r_realdebrid,
           "alldebrid": r_alldebrid, "premiumize": r_premiumize}


@app.post("/generate")
def generate():
    data = request.get_json() or {}
    q = (data.get("query") or data.get("magnet") or data.get("link") or "").strip()
    tid = data.get("torrent_id")
    if not q and not tid:
        return jsonify(error="مفيش رابط مبعوت"), 400
    try:
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

        if tid or q.startswith("magnet:"):
            return torrent_flow(q, tid)

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
                return jsonify(direct_link=url, name=unquote(url.rsplit("/", 1)[-1]) or "ملف",
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
    r = requests.get("https://api.themoviedb.org/3" + path, params=params, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"TMDB رجّع {r.status_code}: تأكد من المفتاح (v3)")
    return r.json()


def img(p, size):
    return f"https://image.tmdb.org/t/p/{size}{p}" if p else None


@app.get("/tmdb")
def tmdb():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    try:
        ar = tmdb_get("/search/multi", query=q, language="ar", include_adult="false").get("results", [])
        en = {i["id"]: i for i in tmdb_get("/search/multi", query=q, language="en-US",
                                            include_adult="false").get("results", [])}
        out = []
        for x in ar:
            if x.get("media_type") not in ("movie", "tv"):
                continue
            e = en.get(x["id"], x)
            date = x.get("release_date") or x.get("first_air_date") or ""
            out.append({
                "id": x["id"], "type": x["media_type"], "year": date[:4],
                "title": x.get("title") or x.get("name") or "",
                "search_title": e.get("title") or e.get("name") or x.get("original_title")
                or x.get("original_name") or "",
                "poster": img(x.get("poster_path"), "w342"),
            })
        return jsonify(results=out[:30])
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/details")
def details():
    t, i = request.args.get("type"), request.args.get("id")
    if t not in ("movie", "tv") or not i:
        return jsonify(error="بيانات ناقصة"), 400
    try:
        a = tmdb_get(f"/{t}/{i}", language="ar", append_to_response="credits")
        e = tmdb_get(f"/{t}/{i}", language="en-US")
        date = a.get("release_date") or a.get("first_air_date") or ""
        rt = a.get("runtime") or (a.get("episode_run_time") or [None])[0]
        return jsonify(
            id=int(i), type=t, year=date[:4], runtime=rt,
            title=a.get("title") or a.get("name") or "",
            search_title=e.get("title") or e.get("name") or "",
            rating=round(a.get("vote_average") or 0, 1),
            overview=a.get("overview") or e.get("overview") or "",
            genres=[g["name"] for g in a.get("genres", [])],
            cast=[c["name"] for c in (a.get("credits") or {}).get("cast", [])[:10]],
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
    try:
        r = requests.get(f"{PROWLARR_URL}/api/v1/search", headers={"X-Api-Key": PROWLARR_KEY},
                         params={"query": q, "type": "search", "limit": 100}, timeout=45)
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
                    mag = f"magnet:?xt=urn:btih:{x['infoHash']}&dn={quote(x.get('title', ''))}"
                else:
                    continue
            out.append({"title": x.get("title", ""), "size": x.get("size") or 0,
                        "seeders": x.get("seeders") or 0, "indexer": x.get("indexer", ""),
                        "magnet": mag})
        out.sort(key=lambda i: i["seeders"], reverse=True)
        return jsonify(results=out[:60])
    except requests.Timeout:
        return jsonify(error="البحث أخد وقت طويل، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في البحث: {e}"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
