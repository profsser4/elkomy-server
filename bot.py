
import os
import time
import urllib.parse
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# التوكن بتاعك محطوط مباشر عشان نلغي أي مشاكل في Railway
TORBOX_KEY = os.environ.get("TORBOX_API_KEY", "39056ee9-f78d-4670-b61b-e5677e897919").strip()
TORBOX_URL = "https://api.torbox.app/v1/api"

@app.route('/', methods=['GET'])
def home():
    return "Elkomy Server is Active & Waiting! 🚀"

@app.route("/generate", methods=["POST", "OPTIONS"])
def generate():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json() or {}
    raw = data.get("query") or data.get("magnet") or data.get("link") or ""

    if not raw:
        return jsonify({"error": "مفيش رابط مبعوت"}), 400

    # فك التشفير اللي بيحل مشكلة الروابط البايظة (Not Found)
    query = urllib.parse.unquote(raw).strip()
    is_torrent = query.startswith("magnet:")
    headers = {"Authorization": f"Bearer {TORBOX_KEY}"}

    try:
        # الخطوة الأولى: إضافة الملف
        if is_torrent:
            if "&tr=" not in query:
                query += "&tr=udp://tracker.opentrackr.org:1337/announce&tr=udp://open.stealth.si:80/announce"
            add_req = requests.post(f"{TORBOX_URL}/torrents/createtorrent", headers=headers, data={"magnet": query, "seed": 1})
        else:
            add_req = requests.post(f"{TORBOX_URL}/webdownloads/createwebdownload", headers=headers, data={"link": query})
            
        add_data = add_req.json()

        if not add_data.get("success"):
            return jsonify({"error": f"الملف محذوف من المصدر، أو الرابط غير مدعوم. {add_data.get('detail', '')}"}), 400

        item_id = add_data.get("data", {}).get("torrent_id") if is_torrent else add_data.get("data", {}).get("id")

        # الخطوة الثانية: اللوب الذكي (الانتظار زي بوت التلجرام)
        check_url = f"{TORBOX_URL}/torrents/mylist" if is_torrent else f"{TORBOX_URL}/webdownloads/mylist"
        
        # هنستنى لحد 30 ثانية (6 محاولات * 5 ثواني)
        for attempt in range(6): 
            time.sleep(5)
            mylist = requests.get(check_url, headers=headers, params={"bypass_cache": True}).json()
            
            for item in mylist.get("data", []):
                if str(item.get("id")) == str(item_id):
                    # لو تورنت، لازم نتأكد إن الملفات ظهرت وننقي أكبر ملف
                    if is_torrent:
                        files = item.get("files", [])
                        if files:
                            target_file = max(files, key=lambda f: f.get("size", 0))
                            dl_req = requests.get(f"{TORBOX_URL}/torrents/requestdl", headers=headers, params={"torrent_id": item_id, "file_id": target_file["id"]}).json()
                            if dl_req.get("success"):
                                return jsonify({"direct_link": dl_req["data"]})
                    # لو رابط عادي 1fichier
                    else:
                        dl_link = item.get("download_link") or item.get("link")
                        if dl_link:
                            return jsonify({"direct_link": dl_link})
        
        # لو خلصنا الـ 30 ثانية والملف لسه مخلصش (معناها إنه ملف ضخم)
        return jsonify({"error": "الفيلم حجمه كبير ولسه بيحمل في حسابك، جرب تضغط سحب كمان دقيقة."})

    except Exception as e:
        return jsonify({"error": f"مشكلة في السيرفر: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
