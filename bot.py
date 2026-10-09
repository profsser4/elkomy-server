
import os
import re
import urllib.parse
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

TORBOX_KEY = "39056ee9-f78d-4670-b61b-e5677e897919"
TORBOX_URL = "https://api.torbox.app/v1/api"
TRACKERS = "&tr=udp://tracker.opentrackr.org:1337/announce&tr=udp://open.stealth.si:80/announce&tr=udp://tracker.bittor.pw:1337/announce"

@app.route('/')
def home():
    return "Elkomy Server is Active! 🚀"

@app.route("/generate", methods=["POST", "OPTIONS"])
def generate():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json() or {}
    raw = data.get("magnet") or data.get("link") or data.get("query") or ""
    query = urllib.parse.unquote(raw).strip()

    if not query:
        return jsonify({"error": "مفيش رابط مبعوت"}), 400

    is_torrent = query.startswith("magnet:")
    headers = {"Authorization": f"Bearer {TORBOX_KEY}"}

    try:
        # اتأكد التوكن سليم بسرعة
        chk = requests.get(f"{TORBOX_URL}/torrents/mylist", headers=headers, params={"limit": 1}, timeout=10)
        if chk.status_code == 401:
            return jsonify({"error": "An error occurred while verifying your token. جدد التوكن من TorBox"}), 401

        target_id = None
        magnet_hash = None

        if is_torrent:
            m = re.search(r'btih:([a-zA-Z0-9]+)', query, re.I)
            if m: magnet_hash = m.group(1).lower()

            if "&tr=" not in query:
                query += TRACKERS

            add = requests.post(f"{TORBOX_URL}/torrents/createtorrent", headers=headers, data={"magnet": query, "seed": 1}, timeout=20).json()
            
            if add.get("success"):
                target_id = add.get("data", {}).get("torrent_id")
            
            # دور في الليستة
            mylist = requests.get(f"{TORBOX_URL}/torrents/mylist", headers=headers, params={"bypass_cache": True, "limit": 100}, timeout=15).json()
            
            for item in mylist.get("data", []):
                if (target_id and str(item.get("id")) == str(target_id)) or (magnet_hash and magnet_hash in str(item.get("hash","")).lower()):
                    if item.get("download_state") not in ["completed", "cached", "seeding", "finished"]:
                        return jsonify({"error": f"⏳ الملف لسه بيجهز في TorBox... استنى دقيقة ودوس تاني", "status": item.get("download_state")}), 202

                    files = item.get("files", [])
                    if not files:
                        return jsonify({"error": "التورنت فاضي مفيهوش ملفات"}), 400
                    
                    biggest = max(files, key=lambda f: f.get("size", 0))
                    dl = requests.get(f"{TORBOX_URL}/torrents/requestdl", headers=headers, params={"torrent_id": item["id"], "file_id": biggest["id"]}, timeout=15).json()
                    if dl.get("success"):
                        return jsonify({"direct_link": dl["data"], "name": biggest.get("name")})
                    else:
                        return jsonify({"error": dl.get("error", "فشل جلب الرابط المباشر")}), 400

            return jsonify({"error": "⏳ اتضاف بس لسه بيجهز، استنى 30 ثانية واضغط سحب تاني", "retry": True}), 202

        else:
            add = requests.post(f"{TORBOX_URL}/webdownloads/createwebdownload", headers=headers, data={"link": query}, timeout=20).json()
            if not add.get("success"):
                return jsonify({"error": add.get("error", "الرابط المباشر غير مدعوم")}), 400
            
            wid = add.get("data", {}).get("id")
            time.sleep(3)
            mylist = requests.get(f"{TORBOX_URL}/webdownloads/mylist", headers=headers, params={"bypass_cache": True}, timeout=15).json()
            for item in mylist.get("data", []):
                if str(item.get("id")) == str(wid):
                    dl = requests.get(f"{TORBOX_URL}/webdownloads/requestdl", headers=headers, params={"webdownload_id": wid}, timeout=15).json()
                    if dl.get("success"):
                        return jsonify({"direct_link": dl["data"]})
            return jsonify({"error": "رابط الويب لسه بيجهز، جرب تاني بعد شوية"}), 202

    except requests.exceptions.Timeout:
        return jsonify({"error": "TorBox بطيء حاليا، جرب تاني"}), 504
    except Exception as e:
        return jsonify({"error": f"مشكلة في السيرفر: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
