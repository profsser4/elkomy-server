
import os
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app) # عشان يقبل الطلبات من github.io

# يقبل المتغيرين عشان لو سميته كده أو كده في Railway
TORBOX_KEY = os.environ.get("TORBOX_API_KEY") or os.environ.get("TORBOX_KEY")
TORBOX_URL = "https://api.torbox.app/v1/api"

@app.route('/', methods=['GET'])
def home():
    return "Elkomy Server is Active & Upgraded! 🚀"

@app.route("/generate", methods=["POST", "OPTIONS"])
def generate():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json()
    query = data.get("query") or data.get("magnet") or data.get("link")

    if not query:
        return jsonify({"error": "مفيش رابط مبعوت"}), 400

    if not TORBOX_KEY:
        return jsonify({"error": "مفتاح TorBox مش موجود في إعدادات Railway"}), 500

    # تنظيف المفتاح من المسافات
    headers = {"Authorization": f"Bearer {TORBOX_KEY.strip()}"}
    is_torrent = query.startswith("magnet:")

    # 1- التأكد من أن التوكن يعمل
    check_url = f"{TORBOX_URL}/torrents/mylist" if is_torrent else f"{TORBOX_URL}/webdownloads/mylist"
    check = requests.get(check_url, headers=headers)
    
    if check.status_code == 401 or check.status_code == 403:
        return jsonify({"error": "An error occurred while verifying your token. Please try again - التوكن منتهي، جدده من TorBox وحطه في Railway"}), 401

    try:
        # 2- إضافة الملف لحسابك
        if is_torrent:
            add = requests.post(f"{TORBOX_URL}/torrents/createtorrent", headers=headers, data={"magnet": query, "seed": 1})
            j = add.json()
            
            # لو فشل، نجرب نضيف Trackers كما أشار التحليل
            if not j.get("success") and "&tr=" not in query:
                query += "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce"
                add = requests.post(f"{TORBOX_URL}/torrents/createtorrent", headers=headers, data={"magnet": query})
                j = add.json()
        else:
            add = requests.post(f"{TORBOX_URL}/webdownloads/createwebdownload", headers=headers, data={"link": query})
            j = add.json()

        if not j.get("success"):
            return jsonify({"error": f"الملف محذوف من المصدر، أو الرابط غير مدعوم. {j.get('detail', '')}"}), 400

        # استخراج الـ ID الخاص بالملف
        item_id = j.get("data", {}).get("torrent_id") if is_torrent else j.get("data", {}).get("id")

        # 3- الانتظار وجلب الرابط المباشر
        time.sleep(5) # ننتظر 5 ثواني حتى يقوم TorBox بمعالجة الملف
        
        mylist = requests.get(check_url, headers=headers, params={"bypass_cache": True}).json()

        for item in mylist.get("data", []):
            if str(item.get("id")) == str(item_id):
                # إذا كان تورنت، نجلب رابط أكبر ملف (ملف الفيديو)
                if is_torrent:
                    files = item.get("files", [])
                    if files:
                        target_file = max(files, key=lambda f: f.get("size", 0))
                        fid = target_file["id"]
                        dl = requests.get(f"{TORBOX_URL}/torrents/requestdl", headers=headers, params={"torrent_id": item_id, "file_id": fid}).json()
                        if dl.get("success"):
                            return jsonify({"direct_link": dl["data"]})
                # إذا كان رابط مباشر
                else:
                    dl_link = item.get("download_link")
                    if dl_link:
                        return jsonify({"direct_link": dl_link})

        return jsonify({"error": "⏳ الملف لسه بيحمل في حسابك على TorBox، جرب تدوس على الجودة كمان دقيقة."}), 202

    except Exception as e:
        return jsonify({"error": f"مشكلة في السيرفر: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
