
import os
import time
import urllib.parse
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

TORBOX_KEY = os.getenv("TORBOX_API_KEY", "39056ee9-f78d-4670-b61b-e5677e897919").strip()
TORBOX_URL = "https://api.torbox.app/v1/api"

@app.route('/', methods=['GET'])
def home():
    return "Elkomy Server is Stable & Active! 🚀"

@app.route("/generate", methods=["POST", "OPTIONS"])
def generate():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json() or {}
    raw = data.get("query") or data.get("magnet") or data.get("link") or ""

    if not raw:
        return jsonify({"error": "مفيش رابط مبعوت"}), 400

    # فك التشفير اللي بيحل مشكلة الروابط البايظة
    query = urllib.parse.unquote(raw).strip()
    is_torrent = query.startswith("magnet:")
    headers = {"Authorization": f"Bearer {TORBOX_KEY}"}

    try:
        # إضافة الملف
        if is_torrent:
            if "&tr=" not in query:
                query += "&tr=udp://tracker.opentrackr.org:1337/announce&tr=udp://open.stealth.si:80/announce"
            add = requests.post(f"{TORBOX_URL}/torrents/createtorrent", headers=headers, data={"magnet": query, "seed": 1})
        else:
            add = requests.post(f"{TORBOX_URL}/webdownloads/createwebdownload", headers=headers, data={"link": query})
            
        j = add.json()

        if not j.get("success"):
            return jsonify({"error": f"الملف محذوف من المصدر، أو الرابط غير مدعوم. {j.get('detail', '')}"}), 400

        item_id = j.get("data", {}).get("torrent_id") if is_torrent else j.get("data", {}).get("id")

        # انتظار 6 ثواني لجلب الرابط
        time.sleep(6)
        
        check_url = f"{TORBOX_URL}/torrents/mylist" if is_torrent else f"{TORBOX_URL}/webdownloads/mylist"
        mylist = requests.get(check_url, headers=headers, params={"bypass_cache": True}).json()

        for item in mylist.get("data", []):
            if str(item.get("id")) == str(item_id):
                if is_torrent:
                    files = item.get("files", [])
                    if files:
                        # جلب أكبر ملف لتجنب ملفات الترجمة والصور
                        target_file = max(files, key=lambda f: f.get("size", 0))
                        dl = requests.get(f"{TORBOX_URL}/torrents/requestdl", headers=headers, params={"torrent_id": item_id, "file_id": target_file["id"]}).json()
                        if dl.get("success"):
                            return jsonify({"direct_link": dl["data"]})
                else:
                    dl_link = item.get("download_link") or item.get("link")
                    if dl_link:
                        return jsonify({"direct_link": dl_link})

        # لو مفيش رابط مباشر رجع رسالة واضحة للمستخدم
        return jsonify({"error": "الفيلم غير متوفر (كاش)، TorBox يقوم بتحميله الآن لحسابك. يرجى الانتظار دقيقة ثم الضغط على سحب مرة أخرى."})

    except Exception as e:
        return jsonify({"error": f"خطأ في السيرفر: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
