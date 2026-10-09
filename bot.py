
import os
import time
import urllib.parse
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# حطينا التوكن كقيمة افتراضية عشان لو Railway معلق
TORBOX_KEY = os.getenv("TORBOX_API_KEY", "39056ee9-f78d-4670-b61b-e5677e897919").strip()
TORBOX_URL = "https://api.torbox.app/v1/api"

@app.route('/', methods=['GET'])
def home():
    return "Elkomy Server is Active & Upgraded! 🚀"

@app.route("/generate", methods=["POST", "OPTIONS"])
def generate():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json() or {}
    raw = data.get("query") or data.get("magnet") or data.get("link") or ""

    if not raw:
        return jsonify({"error": "مفيش رابط مبعوت"}), 400

    # 🔥 العبقرية هنا: فك التشفير بتاع الرابط عشان التراكرز متتبعتش بايظة
    query = urllib.parse.unquote(raw).strip()

    headers = {"Authorization": f"Bearer {TORBOX_KEY}"}
    is_torrent = query.startswith("magnet:")

    # 1- التأكد من أن التوكن يعمل
    check_url = f"{TORBOX_URL}/torrents/mylist" if is_torrent else f"{TORBOX_URL}/webdownloads/mylist"
    check = requests.get(check_url, headers=headers)
    
    if check.status_code == 401 or check.status_code == 403:
        return jsonify({"error": f"التوكن ده مرفوض من TorBox. التوكن منتهي، جدده وحطه في الكود."}), 401

    try:
        # 2- إضافة الملف
        if is_torrent:
            # إضافة تراكرز لو مش موجودة
            if "&tr=" not in query:
                query += "&tr=udp://tracker.opentrackr.org:1337/announce&tr=udp://open.stealth.si:80/announce"
            
            add = requests.post(f"{TORBOX_URL}/torrents/createtorrent", headers=headers, data={"magnet": query, "seed": 1})
        else:
            add = requests.post(f"{TORBOX_URL}/webdownloads/createwebdownload", headers=headers, data={"link": query})
            
        j = add.json()

        if not j.get("success"):
            return jsonify({"error": f"الملف محذوف من المصدر، أو الرابط غير مدعوم. Not Found - {j.get('detail', j.get('error', ''))}"}), 400

        item_id = j.get("data", {}).get("torrent_id") if is_torrent else j.get("data", {}).get("id")

        # 3- الانتظار وجلب الرابط المباشر
        time.sleep(6)
        
        mylist = requests.get(check_url, headers=headers, params={"bypass_cache": True}).json()

        for item in mylist.get("data", []):
            if str(item.get("id")) == str(item_id):
                if is_torrent:
                    files = item.get("files", [])
                    if files:
                        # اختيار أكبر ملف عشان نتجنب ملفات الـ txt والـ sample
                        target_file = max(files, key=lambda f: f.get("size", 0))
                        fid = target_file["id"]
                        dl = requests.get(f"{TORBOX_URL}/torrents/requestdl", headers=headers, params={"torrent_id": item_id, "file_id": fid}).json()
                        if dl.get("success"):
                            return jsonify({"direct_link": dl["data"]})
                else:
                    dl_link = item.get("download_link") or item.get("link")
                    if dl_link:
                        return jsonify({"direct_link": dl_link})

        return jsonify({"error": "⏳ الملف لسه بيجهز في TorBox، استنى دقيقة وجرب سحب تاني"}), 202

    except Exception as e:
        return jsonify({"error": f"مشكلة في السيرفر: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
