import os
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==============================================================
# مفاتيح الـ APIs (الأفضل تتحط في Variables على Railway)
# ==============================================================
KEYS = {
    "TORBOX": os.environ.get("TORBOX_KEY", "6244452f-12cf-453d-a1d2-fb855e9b9c51"),
    "ONEFICHIER": os.environ.get("ONEFICHIER_KEY", "vvtl3GNHckzvhtCwHER3AhulMQOAP0oZ"),
    "REAL_DEBRID": os.environ.get("REALDEBRID_KEY", ""),
    "ALLDEBRID": os.environ.get("ALLDEBRID_KEY", ""),
    "PREMIUMIZE": os.environ.get("PREMIUMIZE_KEY", ""),
}
TMDB_KEY = os.environ.get("TMDB_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_KEY = os.environ.get("PROWLARR_KEY", "")
# ==============================================================


def has(name):
    v = KEYS.get(name, "")
    return bool(v) and "حط_توكن" not in v


TORBOX_BASE = "https://api.torbox.app/v1/api"
TORBOX_H = {"Authorization": f"Bearer {KEYS['TORBOX']}"}
VIDEO = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v")
TRACKERS = (
    "&tr=udp://tracker.opentrackr.org:1337/announce"
    "&tr=udp://open.stealth.si:80/announce"
    "&tr=udp://tracker.bittor.pw:1337/announce"
)


def tb(method, path, **kw):
    r = requests.request(
        method, TORBOX_BASE + path, headers=TORBOX_H, timeout=20, **kw
    )
    try:
        return r.json()
    except ValueError:
        return {"success": False, "detail": f"TorBox رد غير مفهوم ({r.status_code})"}


def pick_file(files):
    vids = [f for f in files if f["name"].lower().endswith(VIDEO)] or files
    return max(vids, key=lambda f: f.get("size", 0))


# ----------------- المواقع -----------------

def resolve_1fichier(url):
    try:
        res = requests.post(
            "https://1fichier.com/v1/download/get_token.cgi",
            json={"url": url},
            headers={
                "Authorization": f"Bearer {KEYS['ONEFICHIER']}",
                "Content-Type": "application/json",
            },
            timeout=15,
        ).json()
        if res.get("status") == "OK" and res.get("url"):
            return {"success": True, "url": res["url"]}
        return {"success": False, "error": "1Fichier: " + str(res.get("message", "فشل"))}
    except Exception as e:
        return {"success": False, "error": f"1Fichier: {e}"}


def resolve_real_debrid(url):
    try:
        res = requests.post(
            "https://api.real-debrid.com/rest/1.0/unrestrict/link",
            headers={"Authorization": f"Bearer {KEYS['REAL_DEBRID']}"},
            data={"link": url},
            timeout=15,
        ).json()
        if res.get("download"):
            return {"success": True, "url": res["download"]}
        return {"success": False, "error": "Real-Debrid: " + str(res.get("error", "فشل"))}
    except Exception as e:
        return {"success": False, "error": f"Real-Debrid: {e}"}


def resolve_alldebrid(url):
    try:
        res = requests.get(
            "https://api.alldebrid.com/v4/link/unlock",
            params={"agent": "Elkomy", "apikey": KEYS["ALLDEBRID"], "link": url},
            timeout=15,
        ).json()
        if res.get("status") == "success" and res.get("data", {}).get("link"):
            return {"success": True, "url": res["data"]["link"]}
        msg = res.get("error", {}).get("message", "فشل")
        return {"success": False, "error": "AllDebrid: " + str(msg)}
    except Exception as e:
        return {"success": False, "error": f"AllDebrid: {e}"}


def resolve_premiumize(url):
    try:
        res = requests.post(
            "https://www.premiumize.me/api/transfer/directdl",
            data={"src": url, "apikey": KEYS["PREMIUMIZE"]},
            timeout=20,
        ).json()
        if res.get("status") == "success":
            links = [c for c in (res.get("content") or []) if c.get("link")]
            if links:
                best = max(links, key=lambda c: c.get("size", 0))
                return {"success": True, "url": best["link"]}
            if res.get("location"):
                return {"success": True, "url": res["location"]}
        msg = res.get("message", "الرابط غير مدعوم أو غير جاهز")
        return {"success": False, "error": "Premiumize: " + str(msg)}
    except Exception as e:
        return {"success": False, "error": f"Premiumize: {e}"}


# ----------------- TorBox روابط الويب -----------------

def torbox_web(q, web_id=None):
    if not web_id:
        add = tb("POST", "/webdl/createwebdownload", data={"link": q})
        if not add.get("success"):
            msg = add.get("detail") or add.get("error") or "TorBox لا يدعم هذا الرابط"
            return {"error": f"TorBox: {msg}"}, 400
        d = add.get("data") or {}
        web_id = d.get("webdownload_id") or d.get("id")

    info = tb("GET", "/webdl/mylist", params={"id": web_id, "bypass_cache": "true"}).get("data")
    if isinstance(info, dict):
        state = str(info.get("download_state", "")).lower()
        if state in ("error", "failed", "expired"):
            return {"error": f"TorBox: فشل التحميل ({state})"}, 400
        if not (info.get("download_finished") or info.get("download_present")):
            return {
                "error": "جاري التجهيز...",
                "torrent_id": f"web:{web_id}",
                "status": info.get("download_state"),
                "progress": info.get("progress"),
            }, 202

    dl = tb(
        "GET",
        "/webdl/requestdl",
        params={"token": KEYS["TORBOX"], "web_id": web_id, "file_id": 0},
    )
    if dl.get("success"):
        return {"direct_link": dl["data"], "name": "TorBox Web VIP 🚀"}, 200
    return {"error": "جاري التجهيز...", "torrent_id": f"web:{web_id}"}, 202


# ----------------- السيرفر -----------------

@app.get("/")
def home():
    return "Elkomy Server (Multi-API) is Active! 🚀"


# ---------- بحث الأفلام والمسلسلات (بوسترات من TMDB) ----------
@app.get("/tmdb")
def tmdb():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    if not TMDB_KEY:
        return jsonify(error="TMDB_KEY مش متضاف في Variables على Railway"), 500

    def call(lang):
        r = requests.get(
            "https://api.themoviedb.org/3/search/multi",
            params={
                "api_key": TMDB_KEY,
                "query": q,
                "language": lang,
                "include_adult": "false",
            },
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"TMDB رجّع {r.status_code}: تأكد من المفتاح (v3)")
        return r.json().get("results", [])

    try:
        ar = call("ar")
        en = {}
        for item in call("en-US"):
            en[item["id"]] = item

        out = []
        for x in ar:
            if x.get("media_type") not in ("movie", "tv"):
                continue
            e = en.get(x["id"], x)
            date = x.get("release_date") or x.get("first_air_date") or ""
            poster = None
            if x.get("poster_path"):
                poster = "https://image.tmdb.org/t/p/w342" + x["poster_path"]
            search_title = (
                e.get("title")
                or e.get("name")
                or x.get("original_title")
                or x.get("original_name")
                or ""
            )
            out.append({
                "title": x.get("title") or x.get("name") or "",
                "search_title": search_title,
                "year": date[:4],
                "type": x["media_type"],
                "poster": poster,
            })
        return jsonify(results=out[:18])
    except Exception as e:
        return jsonify(error=str(e)), 500


# ---------- بحث التورنت (Prowlarr) ----------
@app.get("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(results=[])
    if not (PROWLARR_URL and PROWLARR_KEY):
        return jsonify(error="PROWLARR_URL و PROWLARR_KEY مش متضافين في Variables"), 500

    try:
        r = requests.get(
            f"{PROWLARR_URL}/api/v1/search",
            headers={"X-Api-Key": PROWLARR_KEY},
            params={"query": q, "type": "search", "limit": 100},
            timeout=45,
        )
        if r.status_code != 200:
            return jsonify(error=f"Prowlarr رجّع {r.status_code}: تأكد من الرابط والمفتاح"), 502
        items = r.json()
        out = []
        for x in items:
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
            out.append({
                "title": x.get("title", ""),
                "size": x.get("size") or 0,
                "seeders": x.get("seeders") or 0,
                "indexer": x.get("indexer", ""),
                "magnet": mag,
            })
        out.sort(key=lambda i: i["seeders"], reverse=True)
        return jsonify(results=out[:60])
    except requests.Timeout:
        return jsonify(error="البحث أخد وقت طويل، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في البحث: {e}"), 500


@app.post("/generate")
def generate():
    data = request.get_json() or {}
    q = (data.get("query") or data.get("magnet") or data.get("link") or "").strip()
    tid = data.get("torrent_id")
    if not q and not tid:
        return jsonify(error="مفيش رابط مبعوت"), 400

    try:
        # ---------- متابعة رابط ويب كان بيجهز ----------
        if tid and str(tid).startswith("web:"):
            body, code = torbox_web(q, web_id=str(tid)[4:])
            return jsonify(body), code

        # ---------- تورنت (TorBox) ----------
        if tid or q.startswith("magnet:"):
            if not tid:
                if "&tr=" not in q:
                    q += TRACKERS
                add = tb("POST", "/torrents/createtorrent", data={"magnet": q, "seed": 1})
                if not add.get("success"):
                    msg = add.get("detail") or add.get("error") or "فشل إضافة التورنت"
                    return jsonify(error=msg), 400
                tid = add["data"]["torrent_id"]

            info = tb(
                "GET",
                "/torrents/mylist",
                params={"id": tid, "bypass_cache": "true"},
            ).get("data")
            if not info:
                return jsonify(error="جاري التجهيز...", torrent_id=tid), 202

            if not (info.get("download_finished") or info.get("download_present")):
                return jsonify(
                    error="جاري التجهيز...",
                    torrent_id=tid,
                    status=info.get("download_state"),
                    progress=info.get("progress"),
                ), 202

            files = info.get("files", [])
            if not files:
                return jsonify(error="التورنت فاضي مفيهوش ملفات"), 400

            f = pick_file(files)
            dl = tb(
                "GET",
                "/torrents/requestdl",
                params={"token": KEYS["TORBOX"], "torrent_id": tid, "file_id": f["id"]},
            )
            if dl.get("success"):
                return jsonify(direct_link=dl["data"], name=f["name"])
            return jsonify(error="TorBox رفض إنشاء الرابط", detail=dl), 400

        # ---------- روابط الويب (توجيه ذكي) ----------
        errors = []

        if "1fichier.com" in q and has("ONEFICHIER"):
            res = resolve_1fichier(q)
            if res["success"]:
                return jsonify(direct_link=res["url"], name="1Fichier VIP 🚀")
            errors.append(res["error"])

        if has("REAL_DEBRID"):
            res = resolve_real_debrid(q)
            if res["success"]:
                return jsonify(direct_link=res["url"], name="Real-Debrid VIP 🚀")
            errors.append(res["error"])

        if has("ALLDEBRID"):
            res = resolve_alldebrid(q)
            if res["success"]:
                return jsonify(direct_link=res["url"], name="AllDebrid VIP 🚀")
            errors.append(res["error"])

        if has("PREMIUMIZE"):
            res = resolve_premiumize(q)
            if res["success"]:
                return jsonify(direct_link=res["url"], name="Premiumize VIP 🚀")
            errors.append(res["error"])

        if has("TORBOX"):
            body, code = torbox_web(q)
            if code in (200, 202):
                return jsonify(body), code
            errors.append(body.get("error", "TorBox فشل"))

        return jsonify(error="فشل السحب من جميع السيرفرات ❌ " + " | ".join(errors)), 400

    except requests.Timeout:
        return jsonify(error="السيرفرات بطيئة حالياً، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في السيرفر: {e}"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
