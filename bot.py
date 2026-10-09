
import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==============================================================
# 🔑 مفاتيح الـ APIs (ضَع مفاتيحك هنا)
# ==============================================================
KEYS = {
    # TorBox: الأساسي (للتورنت وأي رابط غير مدعوم في الباقي)
    "TORBOX": os.environ.get("TORBOX_KEY", "6244452f-12cf-453d-a1d2-fb855e9b9c51"),
    
    # 1Fichier Premium API Key
    "ONEFICHIER": "حط_توكن_1fichier_هنا",
    
    # Real-Debrid API Key (بيدعم ميجا وأكتر من 50 موقع)
    "REAL_DEBRID": "حط_توكن_Real-Debrid_هنا",
    
    # AllDebrid API Key
    "ALLDEBRID": "حط_توكن_AllDebrid_هنا",
    
    # Premiumize API Key
    "PREMIUMIZE": "حط_توكن_Premiumize_هنا"
}
# ==============================================================

# إعدادات TorBox
TORBOX_BASE = "https://api.torbox.app/v1/api"
TORBOX_H = {"Authorization": f"Bearer {KEYS['TORBOX']}"}
VIDEO = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v")
TRACKERS = (
    "&tr=udp://tracker.opentrackr.org:1337/announce"
    "&tr=udp://open.stealth.si:80/announce"
    "&tr=udp://tracker.bittor.pw:1337/announce"
)

# ----------------- دوال السحب من المواقع -----------------

def tb(method, path, **kw):
    r = requests.request(method, TORBOX_BASE + path, headers=TORBOX_H, timeout=20, **kw)
    return r.json()

def pick_file(files):
    vids = [f for f in files if f["name"].lower().endswith(VIDEO)] or files
    return max(vids, key=lambda f: f.get("size", 0))

def resolve_1fichier(url):
    try:
        res = requests.post(
            "https://1fichier.com/v1/download/get_token.cgi",
            json={"url": url},
            headers={"Authorization": f"Bearer {KEYS['ONEFICHIER']}", "Content-Type": "application/json"},
            timeout=15
        ).json()
        if res.get("status") == "OK" and res.get("url"):
            return {"success": True, "url": res["url"]}
        return {"success": False, "error": res.get("message", "فشل 1Fichier")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def resolve_real_debrid(url):
    try:
        headers = {"Authorization": f"Bearer {KEYS['REAL_DEBRID']}"}
        # 1. إضافة الرابط
        add_res = requests.post("https://api.real-debrid.com/rest/1.0/unrestrict/link", headers=headers, data={"link": url}, timeout=15).json()
        if add_res.get("download"):
            return {"success": True, "url": add_res["download"]}
        return {"success": False, "error": add_res.get("error", "فشل Real-Debrid")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def resolve_alldebrid(url):
    try:
        res = requests.get(
            f"https://api.alldebrid.com/v4/link/unlock?agent=Elkomy&apikey={KEYS['ALLDEBRID']}&link={url}",
            timeout=15
        ).json()
        if res.get("status") == "success":
            return {"success": True, "url": res["data"]["link"]}
        return {"success": False, "error": res.get("error", {}).get("message", "فشل AllDebrid")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def resolve_premiumize(url):
    try:
        res = requests.post(
            "https://www.premiumize.me/api/transfer/create",
            data={"src": url, "apikey": KEYS['PREMIUMIZE']},
            timeout=15
        ).json()
        # Premiumize بيحتاج متابعة، فهنا هنجيب الرابط المباشر لو متاح فوراً (للملفات المحفوظة مسبقاً Cached)
        if res.get("status") == "success" and res.get("location"):
             return {"success": True, "url": res["location"]}
        return {"success": False, "error": res.get("message", "الرابط يحتاج وقت أو فشل Premiumize")}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ----------------- تشغيل السيرفر -----------------

@app.get("/")
def home():
    return "Elkomy Server (Multi-API) is Active! 🚀"

@app.post("/generate")
def generate():
    data = request.get_json() or {}
    q = (data.get("query") or data.get("magnet") or data.get("link") or "").strip()
    tid = data.get("torrent_id")
    if not q and not tid:
        return jsonify(error="مفيش رابط مبعوت"), 400

    try:
        # ================== 1. تورنت (TorBox فقط) ==================
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
                    params={"token": KEYS['TORBOX'], "torrent_id": tid, "file_id": f["id"]})
            if dl.get("success"):
                return jsonify(direct_link=dl["data"], name=f["name"])
            return jsonify(error="TorBox رفض إنشاء الرابط", detail=dl), 400

        # ================== 2. روابط الويب (التوجيه الذكي) ==================
        else:
            errors = []

            # محاولة 1Fichier
            if "1fichier.com" in q and KEYS["ONEFICHIER"] and "حط_توكن" not in KEYS["ONEFICHIER"]:
                res = resolve_1fichier(q)
                if res["success"]: return jsonify(direct_link=res["url"], name="1Fichier VIP 🚀")
                errors.append(res["error"])

            # محاولة Real-Debrid (لو التوكن موجود)
            if KEYS["REAL_DEBRID"] and "حط_توكن" not in KEYS["REAL_DEBRID"]:
                res = resolve_real_debrid(q)
                if res["success"]: return jsonify(direct_link=res["url"], name="Real-Debrid VIP 🚀")
                errors.append(res["error"])
                
            # محاولة AllDebrid (لو التوكن موجود)
            if KEYS["ALLDEBRID"] and "حط_توكن" not in KEYS["ALLDEBRID"]:
                res = resolve_alldebrid(q)
                if res["success"]: return jsonify(direct_link=res["url"], name="AllDebrid VIP 🚀")
                errors.append(res["error"])

            # محاولة Premiumize (لو التوكن موجود)
            if KEYS["PREMIUMIZE"] and "حط_توكن" not in KEYS["PREMIUMIZE"]:
                res = resolve_premiumize(q)
                if res["success"]: return jsonify(direct_link=res["url"], name="Premiumize VIP 🚀")
                errors.append(res["error"])

            # المحاولة الأخيرة: TorBox Web Download (الافتراضي)
            if KEYS["TORBOX"] and "حط_توكن" not in KEYS["TORBOX"]:
                add = tb("POST", "/webdownloads/createwebdownload", data={"link": q})
                if add.get("success"):
                    d = add["data"]
                    wid = d.get("webdownload_id") or d.get("id")
                    dl = tb("GET", "/webdownloads/requestdl", params={"token": KEYS['TORBOX'], "web_id": wid, "file_id": 0})
                    if dl.get("success"):
                        return jsonify(direct_link=dl["data"], name="TorBox Web VIP 🚀")
                    return jsonify(error="رابط الويب لسه بيجهز في TorBox", web_id=wid), 202
                else:
                    errors.append(add.get("detail") or add.get("error") or "TorBox لا يدعم هذا الرابط")

            return jsonify(error="فشل السحب من جميع السيرفرات ❌\n" + " | ".join(errors)), 400

    except requests.Timeout:
        return jsonify(error="السيرفرات بطيئة حالياً، جرب تاني"), 504
    except Exception as e:
        return jsonify(error=f"مشكلة في السيرفر: {e}"), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
