import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
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

    # نرسل التوكن في الهيدر وفي الرابط لضمان قبول TorBox للطلب
    headers = {
        "Authorization": f"Bearer {TORBOX_KEY}"
    }
    
    try:
        # تحديد إذا كان الرابط تورنت أم رابط مباشر
        if query.startswith("magnet:"):
            url = f"https://api.torbox.app/v1/api/torrents/createtorrent?token={TORBOX_KEY}"
            payload = {"magnet": query}
        else:
            url = f"https://api.torbox.app/v1/api/webdownloads/createwebdownload?token={TORBOX_KEY}"
            payload = {"link": query}
            
        res = requests.post(url, headers=headers, data=payload)
        
        # لو المسار خطأ أو الملف محذوف
        if res.status_code == 404:
            return jsonify({"error": "الملف محذوف من المصدر، أو الرابط غير مدعوم."})
            
        res_data = res.json()
        
        if res.ok and res_data.get("success"):
            # استخراج الرابط المباشر
            direct_link = res_data.get("data", {}).get("download_link", "")
            if not direct_link:
                 direct_link = res_data.get("data", {}).get("link", "")
                 
            # إذا كان الفيلم (كاش) سيظهر الرابط فوراً
            if direct_link:
                return jsonify({"direct_link": direct_link})
            else:
                # إذا كان الفيلم جديد وغير متوفر كاش
                return jsonify({"error": "⏳ تم إضافة الفيلم لحسابك في TorBox بنجاح! ولكنه يحتاج بعض الوقت للتحميل لأنه (غير متوفر كاش). يرجى المحاولة لاحقاً."})
        else:
            return jsonify({"error": str(res_data.get("detail", res_data))})
            
    except Exception as e:
        return jsonify({"error": f"خطأ داخلي في السيرفر: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
