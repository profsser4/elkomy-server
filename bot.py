import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
# السطر ده هو اللي هيحل مشكلة Failed to fetch للأبد
CORS(app) 

TORBOX_KEY = os.environ.get("TORBOX_KEY")

@app.route('/', methods=['GET'])
def home():
    return "Elkomy Server is Active! 🚀"

@app.route('/generate', methods=['POST'])
def generate():
    data = request.get_json()
    query = data.get('query')
    
    if not query:
        return jsonify({"error": "لم يتم إرسال رابط"}), 400

    # تجهيز الطلب لـ TorBox مع الـ Token
    headers = {
        "Authorization": f"Bearer {TORBOX_KEY}"
    }
    
    try:
        # تحديد إذا كان الرابط تورنت (Magnet) أو رابط مباشر
        if query.startswith("magnet:"):
            url = "https://api.torbox.app/v1/api/torrents/createtorrent"
            payload = {"magnet": query}
        else:
            url = "https://api.torbox.app/v1/api/webdownloads/create"
            payload = {"link": query}
            
        # إرسال الطلب
        res = requests.post(url, headers=headers, data=payload)
        res_data = res.json()
        
        # لو الرد ناجح، نرجع الرابط للموقع
        if res.ok and res_data.get("success"):
            # استخراج الرابط المباشر من رد TorBox
            direct_link = res_data.get("data", {}).get("link", "")
            if not direct_link:
                 direct_link = res_data.get("data", {}).get("download_link", "")
            return jsonify({"direct_link": direct_link})
        else:
            return jsonify({"error": str(res_data.get("detail", res_data))})
            
    except Exception as e:
        return jsonify({"error": f"خطأ داخلي في السيرفر: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
