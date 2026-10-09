import os
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
    r = requests.request(method, TORBOX_BASE + path, headers=TORBOX_H, timeout=20, **kw)
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
            headers={"Authorization": f"Bearer {KEYS['ONEFICHIER']}", "Content-Type": "application/json"},
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
        return {"success": False, "error": "AllDebrid: " + str(res.get("error", {}).get("message", "فشل"))}
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
            content = res.get("content") or []
            links = [c for c in content if c.get("link")]
            if links:
                best = max(links, key=lambda c: c.get("size", 0))
                return {"success": True, "url": best["link"]}
            if res.get("location"):
                return {"success": True, "url": res["location"]}
        return {"success": False, "error": "Premiumize: " + str(res.get("message", "الرابط غير مدعوم أو غير جاهز"))}
    except Exception as e:
        return {"success": False, "error": f"Premiumize: {e}"}


# ----------------- TorBox روابط الويب -----------------

def torbox_web(q, web_id=None):
    """يرجع (response, status_code)"""
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

    dl = tb("GET", "/webdl/requestdl", params={"token": KEYS["TORBOX"], "web_id": web_id, "file_id": 0})
    if dl.get("success"):
        return {"direct_link": dl["data"], "name": "TorBox Web VIP 🚀"}, 200
    return {"error": "جاري التجهيز...", "torrent_id": f"web:{web_id}"}, 202


# ----------------- السيرفر -----------------

@app.get("/")
def home():
    return "Elkomy Server (Multi-API) is Active! 🚀"


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
                    return jsonify(error=add.get("detail") or add.get("error") or "فشل إضافة التورنت"), 400
                tid = add["data"]["torrent_id"]

            info = tb("GET", "/torrents/mylist", params={"id": tid, "bypass_cache": "true"}).get("data")
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
            dl = tb("GET", "/torrents/requestdl",
                    params={"token": KEYS["TORBOX"], "torrent_id": tid, "file_id": f["id"]})
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
