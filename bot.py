
import os
import time
import urllib.parse
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

TORBOX_KEY = "39056ee9-f78d-4670-b61b-e5677e897919"
TORBOX_URL = "https://api.torbox.app/v1/api"

@app.route('/', methods=['GET'])
def home():
    return "Elkomy Server is Active! 🚀"

@app.route("/generate", methods=["POST", "OPTIONS"])
def generate():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json() or {}
    raw = data.get("query") or data.get("magnet") or data.get("link") or ""

    if not raw:
        return jsonify({"error": "مفيش رابط مبعوت"}), 400

    query = urllib.parse.unquote(raw).strip()
    is_torrent = query.startswith("magnet:")
    headers = {"Authorization": f"Bearer {TORBOX_KEY}"}

    magnet_hash = None
    if is_torrent:
        match = re.search(r'urn:btih:([a-zA-Z0-9]+)', query, re.IGNORECASE)
        if match:
            magnet_hash = match.group(1).lower()
        
        if "&tr=" not in query:
            query += "&tr=udp://tracker.opentrackr.org:1337/announce"

    try:
        target_id = None
        if is_torrent:
            add_req = requests.post(f"{TORBOX_URL}/torrents/createtorrent", headers=headers, data={"magnet": query, "seed": 1}).json()
            target_id = add_req.get("data", {}).get("torrent_id")
        else:
            add_req = requests.post(f"{TORBOX_URL}/webdownloads/createwebdownload", headers=headers, data={"link": query}).json()
            target_id = add_req.get("data", {}).get("id")

        check_url = f"{TORBOX_URL}/torrents/mylist" if is_torrent else f"{TORBOX_URL}/webdownloads/mylist"
        
        for _ in range(10):
            time.sleep(3)
            
            mylist = requests.get(check_url, headers=headers, params={"bypass_cache": True, "limit": 1000}).json()
            
            for item in mylist.get("data", []):
                item_id_str = str(item.get("id"))
                item_hash = str(item.get("hash", "")).lower()
                
                if (target_id and item_id_str == str(target_id)) or (magnet_hash and magnet_hash == item_hash):
                    if is_torrent:
                        files = item.get("files", [])
                        if files:
                            target_file = max(files, key=lambda f: f.get("size", 0))
                            dl_req = requests.get(f"{TORBOX_URL}/torrents/requestdl", headers=headers, params={"torrent_id": item.get("id"), "file_id": target_file["id"]}).json()
                            if dl_req.get("success"):
                                return jsonify({"direct_link": dl_req["data"]})
                    else:
                        dl_link = item.get("download_link") or item.get("link")
                        if dl_link:
                            return jsonify({"direct_link": dl_link})
        
        return jsonify({"error": "⏳ الملف ضخم أو لسه بيحمل في سيرفرات TorBox... ارجع اضغط سحب كمان دقيقتين."})

    except Exception as e:
        return jsonify({"error": f"مشكلة في السيرفر: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
