import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# مؤقت للتجربة: بعد ما يشتغل ولّد مفتاح جديد وحطه في Variables على Railway (TORBOX_KEY)
KEY = os.environ.get("TORBOX_KEY", "39056ee9-f78d-4670-b61b-e5677e897919")
BASE = "https://api.torbox.app/v1/api"
H = {"Authorization": f"Bearer {KEY}"}
VIDEO = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v")
TRACKERS = (
    "&tr=udp://tracker.opentrackr.org:1337/announce"
    "&tr=udp://open.stealth.si:80/announce"
    "&tr=udp://tracker.bittor.pw:1337/announce"
)


def tb(method, path, **kw):
    r = requests.request(method, BASE + path, headers=H, timeout=20, **kw)
    return r.json()


def pick_file(files):
    vids = [f for f in files if f["name"].lower().endswith(VIDEO)] or files
    return max(vids, key=lambda f: f.get("size", 0))


@app.get("/")
def home():
    return "Elkomy Server is Active! 🚀"


@app.post("/generate")
def generate():
    data = request.get_json() or {}
    q = (data.get("query") or data.get("magnet") or data.get("link") or "").strip()
    tid = data.get("torrent_id")
    if not q and not tid:
        return jsonify(error="مفيش رابط مبعوت"), 400

    try:
        # ---------- تورنت ----------
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
                    params={"token": KEY, "torrent_id": tid, "file_id": f["id"]})
            if dl.get("success"):
                return jsonify(direct_link=dl["data"], name=f["name"])
            return jsonify(error="TorBox رفض إنشاء الرابط", detail=dl), 400

        # ---------- رابط عادي ----------
        add = tb("POST", "/webdownloads/createwebdownload", data={"link": q})
        if not add.get("success"):
            return jsonify(error=add.get("detail") or add.get("error") or "الرابط غير مدعوم"), 400

        d = add["data"]
        wid = d.get("webdownload_id") or d.get("id")
        dl = tb("GET", "/webdownloads/requestdl",
                params={"token": KEY, "web_id": wid, "file_id": 0})
        if dl.get("success"):
            return jsonify(direct_link=dl["data"])
        return jsonify(error="رابط الويب لسه بيجهز", web_id=wid), 202

    except requests.Timeout:
        return jsonify(error="TorBox بطيء حالياً، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في السيرفر: {e}"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
