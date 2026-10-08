
import os
import time
import threading
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, jsonify
from flask_cors import CORS

# ==========================================
# 1. إعدادات السيرفر (البروكسي السحري المعدل)
# ==========================================
app = Flask(__name__)
CORS(app) # دي اللي بتسمح لموقعك إنه يكلم السيرفر بدون حظر

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8866554993:AAHr5turtyIRKBm4kgBvSU_elnCkpCqLITw")
TORBOX_KEY = os.environ.get("TORBOX_KEY", "610978e6-60be-4d90-97af-c77f0efd8caf")
CHANNEL_ID = -1004323086203

@app.route('/')
def home():
    return "سيرفر البوت والبروكسي يعمل بكفاءة 24/7! 🚀"

# الدالة اللي هتاخد الطلب من موقعك تبعته لـ TorBox وترجعهولك
@app.route('/proxy/<path:endpoint>', methods=['GET', 'POST'])
def proxy(endpoint):
    url = f"https://api.torbox.app/v1/api/{endpoint}"
    headers = {"Authorization": f"Bearer {TORBOX_KEY}"}
    
    try:
        if request.method == 'POST':
            res = requests.post(url, headers=headers, data=request.form, timeout=15)
        else:
            params = request.args.to_dict()
            # 🔴 التعديل السحري: إضافة التوكن إجبارياً في الرابط لحل مشكلة سحب الملفات
            params['token'] = TORBOX_KEY
            
            res = requests.get(url, headers=headers, params=params, timeout=15)
        
        return jsonify(res.json())
    except Exception as e:
        return jsonify({"success": False, "detail": f"Server Error: {str(e)}"})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# تشغيل السيرفر في الخلفية
threading.Thread(target=run_flask).start()


# ==========================================
# 2. إعدادات بوت التليجرام (للتحميل المباشر)
# ==========================================
bot = telebot.TeleBot(BOT_TOKEN)

def format_size(size_bytes):
    if not size_bytes: return "غير معروف"
    try:
        size_bytes = float(size_bytes)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024.0: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
    except: return "غير معروف"

def torbox_request(method, endpoint, data=None, params=None):
    url = f"https://api.torbox.app/v1/api/{endpoint}"
    headers = {"Authorization": f"Bearer {TORBOX_KEY}"}
    try:
        if method == "POST":
            res = requests.post(url, headers=headers, data=data, timeout=15)
        else:
            res = requests.get(url, headers=headers, params=params, timeout=15)
        return res.json()
    except Exception as e:
        return {"success": False, "detail": str(e)}

def monitor_download(item_id, is_torrent, chat_id, message_id):
    api_path = "torrents" if is_torrent else "webdl"
    try: bot.edit_message_text(f"🚀 3/3: تم الربط! جاري سحب الملف...", chat_id=chat_id, message_id=message_id)
    except: pass

    for attempt in range(60):
        time.sleep(5)
        list_res = torbox_request("GET", f"{api_path}/mylist")
        if not list_res.get("success"): continue
            
        items = list_res.get("data", [])
        target_item = next((item for item in items if str(item.get("id")) == str(item_id)), None)
        if not target_item and items: target_item = items[0]
            
        if target_item:
            state = target_item.get("download_state") or "unknown"
            if state in ["error", "failed", "cancelled", "deleted"]:
                try: bot.edit_message_text(f"❌ فشل التحميل.", chat_id=chat_id, message_id=message_id)
                except: pass
                return

            if state in ["completed", "seeding", "cached", "finished", "downloaded"]:
                files = target_item.get("files", [])
                file_id = None
                file_name = target_item.get("name", "ملف جاهز")
                file_size = target_item.get("size", 0) 
                
                if files:
                    target_file = max(files, key=lambda f: f.get("size", 0))
                    file_id = target_file.get("id")
                    file_name = target_file.get("name")
                    file_size = target_file.get("size", 0)
                
                size_str = format_size(file_size)
                params = {"token": TORBOX_KEY, "zip_link": "false"}
                if is_torrent: params["torrent_id"] = target_item.get("id")
                else: params["web_id"] = target_item.get("id")
                if file_id: params["file_id"] = file_id

                dl_res = torbox_request("GET", f"{api_path}/requestdl", params=params)
                if not dl_res.get("success") and file_id:
                    del params["file_id"]
                    dl_res = torbox_request("GET", f"{api_path}/requestdl", params=params)

                if dl_res.get("success"):
                    direct_link = dl_res["data"]
                    caption = f"🎬 **الملف جاهز:**\n{file_name}\n\n⚖️ **الحجم:** {size_str}\n\n📥 **الرابط:**\n`{direct_link}`"
                    kb = InlineKeyboardMarkup()
                    kb.row(InlineKeyboardButton("▶️ مشاهدة أو تحميل 📥", url=direct_link))
                    try:
                        bot.send_message(CHANNEL_ID, caption, reply_markup=kb, parse_mode="Markdown")
                        bot.edit_message_text(f"✅ اكتمل التحميل! تم الإرسال للجروب.", chat_id=chat_id, message_id=message_id)
                    except: pass
                    return 
            elif attempt % 3 == 0:
                try: bot.edit_message_text(f"⏳ جاري التحميل... (الحالة: {state})", chat_id=chat_id, message_id=message_id)
                except: pass
        else:
            if attempt % 3 == 0:
                try: bot.edit_message_text(f"⏳ في انتظار استجابة السيرفر...", chat_id=chat_id, message_id=message_id)
                except: pass
                    
    try: bot.edit_message_text("⚠️ انتهى وقت الانتظار.", chat_id=chat_id, message_id=message_id)
    except: pass

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = "✅ **البوت والسيرفر يعملان الآن 24/7!**\nأرسل أي رابط لتحميله."
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True)
def handle_links(message):
    text = message.text.strip()
    is_torrent = text.startswith("magnet:")
    is_web = text.startswith("http://") or text.startswith("https://")

    if is_torrent or is_web:
        status_msg = bot.reply_to(message, "🔄 1/3: جاري الاتصال بـ TorBox...")
        res = torbox_request("POST", "torrents/createtorrent" if is_torrent else "webdl/createwebdownload", data={"magnet" if is_torrent else "link": text})
            
        if res.get("success"):
            try: bot.edit_message_text("✅ 2/3: تم القبول! جاري التجهيز...", chat_id=message.chat.id, message_id=status_msg.message_id)
            except: pass
            
            data = res.get("data")
            item_id = data.get("torrent_id") or data.get("web_id") or data.get("id") if isinstance(data, dict) else data
                
            if not item_id:
                time.sleep(3) 
                list_res = torbox_request("GET", f"{'torrents' if is_torrent else 'webdl'}/mylist")
                if list_res.get("success") and list_res.get("data"): item_id = list_res["data"][0]["id"]

            if item_id: threading.Thread(target=monitor_download, args=(item_id, is_torrent, message.chat.id, status_msg.message_id)).start()
        else:
            try: bot.edit_message_text(f"❌ فشل قبول الرابط", chat_id=message.chat.id, message_id=status_msg.message_id)
            except: pass
    else:
        bot.reply_to(message, "⚠ يرجى إرسال رابط صالح.")

print("=====================================================")
print("🚀 السيرفر يعمل الآن وجاهز لاستقبال طلبات الموقع وتخطي الحظر!")
print("=====================================================")

while True:
    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5, skip_pending=True)
    except Exception as e:
        time.sleep(3)
