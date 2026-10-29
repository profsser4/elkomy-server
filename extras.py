"""
extras.py: مسار التشغيل الجديد (اختيار النسخة، البث المحوَّل، التحميل).

بيتسجّل من wsgi.py من غير ما نعدّل bot.py، وبيستخدم دوال bot.py الموجودة.
الروابط:
  GET  /play/versions  نسخ العمل (فيلم أو حلقة) من إضافات Stremio مع علامة "فوري"
  POST /play/start     بيجهّز النسخة ويرجّع رابط البث المحوَّل (hls) والرابط الأصلي (direct)
  POST /play/diag      (أدمن) بيفحص كل خطوة ويرجّع الردود الخام عشان نعرف أي مشكلة
  POST /play/probe     (أدمن) بيفتح أول 1MB من الملف ويقول الحاوية والكودكات والسبب
"""
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import quote, urlparse

import requests
from flask import g, jsonify, request

RISKY_RX = re.compile(
    r"(?:\bDDP?\s?\d|\bDD\+|\bAC-?3\b|\bE-?AC-?3\b|\bDTS|\bTrueHD|\bAtmos"
    r"|\bHEVC|\bx265|\bH[. ]?265|\b10-?bit|\.avi\b)", re.I)
Q_RX = re.compile(r"2160p|4K|1080p|720p|480p", re.I)
# ترتيب النسخ: H.264/x264 + AAC أول، وDDP/DTS/HEVC/x265 آخر
GOOD_V_RX = re.compile(r"\b(?:x264|H[. ]?264|AVC)\b", re.I)
GOOD_A_RX = re.compile(r"\bAAC", re.I)


def ver_score(title, risky):
    """0 = خطرة (DDP/DTS/HEVC/x265..) | 1 = عادية | 2 = H.264 أو AAC | 3 = H.264 + AAC"""
    if risky:
        return 0
    t = title.replace("_", " ")
    return 1 + bool(GOOD_V_RX.search(t)) + bool(GOOD_A_RX.search(t))
HLS_KEYS = ("hls_url", "hlsUrl", "hls", "playlist", "stream_url", "url")
_MASK = re.compile(r"""(?i)(token['"]?\s*[:=]\s*['"]?)[^'"&\s,}]+""")

# كودكات بنبحث عنها جوه أول 1MB من الملف (MKV و MP4)
CODECS = [(b"V_MPEGH/ISO/HEVC", "HEVC"), (b"V_MPEG4/ISO/AVC", "H264"), (b"V_AV1", "AV1"),
          (b"A_EAC3", "EAC3"), (b"A_AC3", "AC3"), (b"A_DTS", "DTS"), (b"A_TRUEHD", "TrueHD"),
          (b"A_AAC", "AAC"), (b"A_OPUS", "OPUS"), (b"A_FLAC", "FLAC"),
          (b"hvc1", "HEVC"), (b"hev1", "HEVC"), (b"avc1", "H264"),
          (b"mp4a", "AAC"), (b"ec-3", "EAC3"), (b"ac-3", "AC3"), (b"dtsc", "DTS")]
BAD_CODECS = ("HEVC", "AC3", "EAC3", "DTS", "TrueHD", "AV1")


def mask(x):
    """بيخفي قيم token من أي نص قبل ما يتعرض في صفحة الفحص."""
    return _MASK.sub(r"\1***", str(x))


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _jr(x):
    """بيحوّل رد Flask (Response أو (Response, code)) لـ (dict, code)."""
    code = None
    if isinstance(x, tuple):
        code, x = x[1], x[0]
    if code is None:
        code = getattr(x, "status_code", 200)
    d = x.get_json(silent=True) if hasattr(x, "get_json") else None
    return (d if isinstance(d, dict) else {}), code


def find_hls(obj):
    """بيدوّر في رد TorBox عن رابط HLS من غير ما نعتمد على شكل محدد."""
    if isinstance(obj, dict):
        for k in HLS_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            r = find_hls(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_hls(v)
            if r:
                return r
    elif isinstance(obj, str) and obj.startswith("http") and ".m3u8" in obj:
        return obj
    return ""


def sniff(u, nbytes=1048576):
    """بيحمّل أول nbytes من الرابط ويحدد: الحاوية، الكودكات، والسبب المتوقع لو المتصفح مش هيشغّله.
    بيرجّع dict فيه: status, type, total, ranges, host, container, codecs, bad, fix. أو error."""
    try:
        r = requests.get(u, headers={"Range": "bytes=0-%d" % (nbytes - 1), "User-Agent": "Mozilla/5.0"},
                         timeout=20, stream=True, allow_redirects=True)
        buf = b""
        for ch in r.iter_content(65536):
            buf += ch
            if len(buf) >= nbytes:
                break
        r.close()
    except Exception as ex:
        return {"error": "الفحص فشل: " + mask(ex)[:150]}
    m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
    total = int(m.group(1)) if m else int(r.headers.get("Content-Length") or 0)
    ctype = (r.headers.get("content-type") or "").lower()
    if buf[:4] == b"\x1a\x45\xdf\xa3":
        cont = "MKV"
    elif b"ftyp" in buf[:16]:
        cont = "MP4"
    elif buf[:3] == b"\x47\x40\x00" or buf[:1] == b"\x47" and len(buf) > 188 and buf[188:189] == b"\x47":
        cont = "TS"
    elif ctype.startswith("text/") or "json" in ctype or "html" in ctype or buf.lstrip()[:1] in (b"<", b"{"):
        cont = "HTML/JSON (مش فيديو)"
    else:
        cont = "غير معروف"
    found = sorted({n for k, n in CODECS if k in buf}) if not cont.startswith("HTML") else []
    if cont == "MP4" and b"moov" not in buf:
        found.append("moov في آخر الملف (بيحمّل ببطء)")
    bad = [c for c in found if c in BAD_CODECS]
    if cont.startswith("HTML"):
        fix = "الرابط مش فيديو: " + mask(buf[:150].decode("utf-8", "ignore"))
    elif r.status_code not in (200, 206):
        fix = "السيرفر رد %s" % r.status_code
    elif bad:
        fix = "المتصفح مش بيفك: " + ", ".join(bad) + " ← لازم HLS محوَّل أو نسخة تانية"
    elif cont == "MKV":
        fix = "حاوية MKV: بعض المتصفحات بترفضها حتى لو الكودكات سليمة ← HLS أو نسخة MP4"
    elif cont == "غير معروف":
        fix = "صيغة الملف مش معروفة (Content-Type: %s)" % (ctype or "؟")
    else:
        fix = "الكودكات تمام، شوف الـ Content-Type / الـ CORS"
    return {"status": r.status_code, "type": ctype, "total": total, "ranges": r.headers.get("accept-ranges"),
            "host": urlparse(r.url).netloc, "container": cont, "codecs": found, "bad": bad, "fix": fix}


def register(app, bot):

    # ---------- مساعدات ----------
    def is_instant(h, magnet):
        try:
            if h in bot.cached_hashes([h]):
                return True
        except Exception:
            pass
        p = bot.tb_provider()
        if p:
            try:
                return bool(bot.find_existing(p["key"], magnet))
            except Exception:
                return False
        return False

    def torbox_stream(tid, fid):
        p = bot.tb_provider()
        if not p:
            return "", "TorBox مش مضاف في المزوّدين", {}
        try:
            r = bot.tb(p["key"], "GET", "/stream/createstream",
                       params={"id": tid, "file_id": fid, "type": "torrent"})
        except Exception as ex:
            return "", "createstream: " + str(ex)[:150], {}
        if not isinstance(r, dict) or not r.get("success"):
            msg = (r or {}).get("detail") or (r or {}).get("error") or "createstream فشل"
            return "", str(msg)[:200], r if isinstance(r, dict) else {}
        url = find_hls(r.get("data"))
        return url, ("" if url else "TorBox رجّع رد من غير رابط HLS"), r

    # مزوّد -> دالة بتجيب رابط HLS محوَّل. بنضيف مزوّد لما نراجع توثيقه ونجرّبه.
    def rd_stream(d):
        """Real-Debrid: رابط التحميل غير رابط البث. البث المحوَّل (m3u8) من /streaming/transcode/{id}"""
        parts = str(d.get("torrent_id") or "").split(":", 3)     # x:realdebrid:<pid>:<rid>
        p = next((x for x in bot.load() if len(parts) > 2 and x["id"] == parts[2]), None)
        if not p:
            return "", "Real-Debrid: المزوّد مش لاقيه", {}
        rid = getattr(bot, "_RD_IDS", {}).get(d.get("direct_link"))
        if not rid:
            # الذاكرة اتمسحت/السيرفر اتعمله ريستارت: نرجّع نفك رابط الملف من التورنت نفسه
            try:
                info = bot.rd_info(p["key"], parts[3])
                fl = info.get("files") or []
                fid = d.get("file_id")
                f = next((x for x in fl if str(x["id"]) == str(fid)), None) if fid is not None else None
                if not f and fl:
                    f = max(fl, key=lambda x: x.get("size") or 0)
                if f and f.get("link"):
                    c0, r0 = bot.rd(p["key"], "POST", "/unrestrict/link", data={"link": f["link"]})
                    if c0 == 200 and isinstance(r0, dict):
                        rid = r0.get("id")
            except Exception:
                rid = None
        if not rid:
            return "", "Real-Debrid: مفيش id للفك، بيتشغّل الملف الأصلي", {}
        try:
            code, r = bot.rd(p["key"], "GET", "/streaming/transcode/" + str(rid))
        except Exception as ex:
            return "", "Real-Debrid transcode: " + str(ex)[:150], {}
        if code != 200 or not isinstance(r, dict):
            return "", "Real-Debrid transcode فشل (%s)" % code, r if isinstance(r, dict) else {}
        a = r.get("apple")
        url = ""
        if isinstance(a, str):
            url = a
        elif isinstance(a, dict):
            url = a.get("full") or next((v for v in a.values() if isinstance(v, str) and v.startswith("http")), "")
        return url, ("" if url else "Real-Debrid رجّع رد من غير رابط HLS"), r

    STREAMERS = {"torbox": lambda d: torbox_stream(d["torrent_id"], d.get("file_id")),
                 "realdebrid": rd_stream}

    def stream_for(d):
        """بيرجّع (رابط_hls, ملاحظة, الرد_الخام) حسب المزوّد اللي جهّز الملف."""
        tid = str(d.get("torrent_id") or "")
        kind = "torbox" if tid.isdigit() else (tid.split(":")[1] if tid.startswith("x:") else "")
        fn = STREAMERS.get(kind)
        if not fn:
            return "", "بث محوَّل مش متضاف للمزوّد ده (%s) لسه، بيتشغّل الملف الأصلي" % (kind or d.get("provider") or "؟"), {}
        return fn(d)

    def prepare(b):
        """بيجهّز الملف على المزوّد. بيرجّع (data, None) أو (None, (response, code))."""
        s, e = _int(b.get("s")), _int(b.get("e"))
        h = str(b.get("hash") or "").lower()
        name = str(b.get("name") or "")[:200]
        magnet = "magnet:?xt=urn:btih:" + h + ("&dn=" + quote(name) if name else "")
        tid = b.get("torrent_id") or None
        if not tid and not b.get("slow") and not is_instant(h, magnet):
            return None, (jsonify(
                error="النسخة دي مش فورية على المزوّد. اختار نسخة عليها ⚡ أو اضغط (سحب بطيء)."), 409)
        pf, stc = bot.prov_pref(g.user, "download" if b.get("mode") == "download" else "watch")
        if b.get("prefer"):                       # مزوّد محدّد (مثلاً نفس مزوّد رابط Stremio)
            pf, stc = str(b["prefer"]), True
        d, code = _jr(bot.torrent_dispatch(magnet, tid, prefer=pf, strict=stc))
        if code == 202:
            return None, (jsonify(d), 202)
        if code != 200 or not d.get("direct_link"):
            return None, (jsonify(error=d.get("error") or "فشل تجهيز النسخة"), 400)
        if s is not None and e is not None:
            files = d.get("files") or []
            if files:
                pick = next((f for f in files
                             if f.get("season") == s and f.get("episode") == e), None) \
                    or next((f for f in files
                             if f.get("season") is None and f.get("episode") == e), None)
                if not pick:
                    return None, (jsonify(
                        error="الحلقة S%02dE%02d مش موجودة في النسخة دي" % (s, e)), 400)
                if str(pick["id"]) != str(d.get("file_id")):
                    d, code = _jr(bot.torrent_dispatch(
                        magnet, d.get("torrent_id"), file_id=pick["id"]))
                    if code != 200 or not d.get("direct_link"):
                        return None, (jsonify(error=d.get("error") or "فشل تجهيز الحلقة"),
                                      code if code in (202, 400) else 400)
            else:
                _, ep = bot.parse_ep(d.get("name") or "")
                if ep is not None and ep != e:
                    return None, (jsonify(error="الملف ده مش الحلقة المطلوبة"), 400)
        return d, None

    # ---------- بحث TorBox (بيجيب النسخ وعلامة "فوري" من TorBox نفسه) ----------
    TBS = "https://search-api.torbox.app"

    def tb_items(path, params):
        p = bot.tb_provider()
        if not p:
            return []
        try:
            r = requests.get(TBS + path, params=params, timeout=20,
                             headers={"Authorization": "Bearer " + p["key"], "User-Agent": "Mozilla/5.0"})
            data = (r.json() or {}).get("data")
        except Exception:
            return []
        out = []
        for x in (data.get("torrents") if isinstance(data, dict) else None) or []:
            if not isinstance(x, dict):
                continue
            mag = x.get("magnet") or ""
            m = re.search(r"btih:([a-zA-Z0-9]{40})", mag)
            h = str(x.get("hash") or (m.group(1) if m else "")).lower()
            title = str(x.get("raw_title") or x.get("title") or "")
            if len(h) != 40 or not title:
                continue
            eg, dub = bot.dub_flags(title)
            out.append({"title": title, "size": x.get("size") or 0,
                        "seeders": x.get("last_known_seeders") or x.get("seeders") or 0,
                        "hash": h, "ref": "", "dub": dub, "eg": eg, "cached": bool(x.get("cached")),
                        "magnet": mag, "indexer": str(x.get("tracker") or "TorBox")})
        return out

    def tb_search(imdb, s, e):
        if not re.fullmatch(r"tt\d+", imdb or ""):
            return []
        q = {"check_cache": "true"}
        if s:
            q.update(season=s, episode=e or 1)
        return tb_items("/torrents/imdb:" + imdb, q)

    AR_KEEP = re.compile(r"[\u0600-\u06FF]|arab|dubbed|\bdub\b|\bHC\b|hard.?cod|hard.?sub|egypt", re.I)

    def ar_search(q):
        """بحث إضافي عن نسخ مدبلجة/مترجمة بالعربي باسم العمل (بيتضاف لنتايج الـ imdb)."""
        qs = [q + " مدبلج مصري", q + " arabic dubbed"]
        out = []
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                for items in ex.map(lambda t: tb_items("/torrents/search/" + quote(t, safe=""),
                                                       {"check_cache": "true"}), qs):
                    out += [x for x in items if AR_KEEP.search(x["title"])]
        except Exception:
            pass
        return out

    def bad_media(url, expected):
        """بيرجّع سبب نصي لو الرابط مش فيديو سليم (صفحة HTML/JSON أو فيديو صغير بدل الملف)، وإلا فاضي.
        بعض الإضافات بترجّع فيديو صغير أو صفحة خطأ لما النسخة مش متخزّنة (not cached yet)."""
        try:
            r = requests.get(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"}, timeout=12, stream=True)
            if r.status_code not in (200, 206):
                r.close()
                return ""   # رد خطأ من السيرفر مش دليل إن الملف صغير
            ct = (r.headers.get("Content-Type") or "").lower()
            m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
            total = int(m.group(1)) if m else int(r.headers.get("Content-Length") or 0)
            r.close()
        except Exception:
            return ""
        if ct.startswith("text/") or "json" in ct or "html" in ct:
            return "الرابط مش فيديو (رجّع %s). جرّب نسخة تانية." % ct.split(";")[0]
        if total and total < 40000000 and (not expected or total < expected * 0.1):
            return "النسخة دي لسه مش متخزّنة على خدمة الدابريد (بتتحمّل). جرّب نسخة تانية."
        return ""

    def resolve_checked():
        resp = bot.st_resolve()
        d, _ = _jr(resp)
        size = _int((request.get_json(silent=True) or {}).get("size"))
        if d.get("url"):
            why = bad_media(d["url"], size)
            if why:
                return jsonify(error=why), 409
        return resp

    if "st_resolve" in app.view_functions:
        app.view_functions["st_resolve"] = resolve_checked

    # ---------- النسخ ----------
    @app.get("/play/versions")
    def play_versions():
        try:
            d, code = _jr(bot.st_streams())
        except Exception:
            d = {}
        tbs = tb_search(request.args.get("imdb", ""), _int(request.args.get("s")), _int(request.args.get("e")))
        qn = (request.args.get("q") or "").strip()[:80]
        if qn:
            tbs = tbs + ar_search(qn)
        res = (d.get("results") or []) + tbs
        if not res:
            return jsonify(versions=[], error=d.get("error") or "مفيش نسخ متاحة للعمل ده")
        hashes = list({(x.get("hash") or "").lower() for x in res if x.get("hash")})[:100]
        try:
            cached = bot.cached_hashes(hashes) if hashes else set()
        except Exception:
            cached = set()
        cached |= {x["hash"] for x in tbs if x["cached"]}
        best = {}
        for x in res:
            h = (x.get("hash") or "").lower()
            title = x.get("title") or ""
            m = Q_RX.search(title)
            risky = bool(RISKY_RX.search(title.replace("_", " ")))
            v = {
                "title": title, "size": x.get("size") or 0,
                "seeders": x.get("seeders") or 0, "hash": h,
                "ref": x.get("ref") or "",
                # نسخ Stremio (ref) رابطها جاهز من الإضافة: تتحسب فورية. التورنت بس هو اللي بيتفحص كاش.
                "instant": bool((h and h in cached) or x.get("ref")),
                "dub": bool(x.get("dub")), "eg": bool(x.get("eg")),
                "risky": risky, "score": ver_score(title, risky),
                "q": m.group(0).upper() if m else "",
            }
            key = h or v["ref"] or title
            o = best.get(key)
            if o is None or (v["instant"], v["score"], v["seeders"]) > (o["instant"], o["score"], o["seeders"]):
                best[key] = v
        out = sorted(best.values(),
                     key=lambda v: (not v["instant"], -v["score"], -v["seeders"]))[:120]
        return jsonify(versions=out)

    def hls_fallback(h, b):
        """مزوّد المشاهدة مفيهوش بث محوَّل (صوت AC3/DTS مثلاً): لو الملف فوري على TorBox نجيب منه الـ HLS."""
        try:
            tp = bot.tb_provider()
            if not tp or not h or h not in bot.cached_for(tp, [h]):
                return ""
            name = str(b.get("name") or "")[:200]
            magnet = "magnet:?xt=urn:btih:" + h + ("&dn=" + quote(name) if name else "")
            d, code = _jr(bot.torrent_dispatch(magnet, None, prefer="torbox", strict=True))
            if code != 200 or not d.get("direct_link"):
                return ""
            s, e = _int(b.get("s")), _int(b.get("e"))
            files = d.get("files") or []
            if s is not None and e is not None and files:
                pick = next((f for f in files if f.get("season") == s and f.get("episode") == e), None) \
                    or next((f for f in files if f.get("season") is None and f.get("episode") == e), None)
                if not pick:
                    return ""
                if str(pick["id"]) != str(d.get("file_id")):
                    d, code = _jr(bot.torrent_dispatch(magnet, d.get("torrent_id"), file_id=pick["id"]))
                    if code != 200 or not d.get("direct_link"):
                        return ""
            return stream_for(d)[0]
        except Exception:
            return ""

    # ---------- تجهيز النسخة ----------
    HASH_RX = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])")

    def hash_from_ref(ref, url):
        for u in (bot.ref_get(str(ref or "")) or "", url or ""):
            m = HASH_RX.search(str(u))
            if m:
                return m.group(1).lower()
        return ""

    def rd_hls_from_url(u):
        """رابط Real-Debrid جاي من Stremio من غير hash: id الفك جوه الرابط نفسه (/d/<id>/)."""
        m = re.search(r"real-debrid\.com/d/([A-Za-z0-9]+)", u or "")
        if not m:
            return "", "Real-Debrid: مش لاقي id الفك في الرابط"
        for p in bot.load():
            if p.get("type") != "realdebrid":
                continue
            try:
                code, r = bot.rd(p["key"], "GET", "/streaming/transcode/" + m.group(1))
            except Exception:
                continue
            if code == 200 and isinstance(r, dict):
                a = r.get("apple")
                url = a if isinstance(a, str) else (
                    (a.get("full") or next((v for v in a.values() if isinstance(v, str) and v.startswith("http")), ""))
                    if isinstance(a, dict) else "")
                if url:
                    return url, ""
        return "", "Real-Debrid transcode مش متاح للرابط ده"

    def webdl_hls(url):
        """نضيف رابط Stremio المباشر لحساب TorBox بتاعك (تحميل ويب) ونطلب منه بث HLS محوَّل.
        لو لسه بيتحمّل نرجّع سبب وبنفس الرابط المرة الجاية هيلاقيه جاهز في المجلة."""
        if os.environ.get("WEBDL_HLS", "1") != "1":   # مفعّل افتراضياً. WEBDL_HLS=0 يقفله
            return "", ""
        p = bot.tb_provider()
        if not p:
            return "", "TorBox مش مضاف في المزوّدين (مفيش تحويل من حسابك)"
        k = p["key"]
        try:
            wid = None
            items = bot.tb(k, "GET", "/webdl/mylist", params={"bypass_cache": "true"}).get("data") or []
            for x in (items if isinstance(items, list) else []):
                if str(x.get("original_url") or "") == url:
                    wid = x.get("id")
                    break
            if not wid:
                add = bot.tb(k, "POST", "/webdl/createwebdownload", data={"link": url})
                msg = str(add.get("detail") or add.get("error") or "").lower()
                if not add.get("success") and "limit" in msg:
                    bot.clean_torbox(k)
                    add = bot.tb(k, "POST", "/webdl/createwebdownload", data={"link": url})
                if not add.get("success"):
                    return "", "TorBox رفض الرابط: " + str(add.get("detail") or add.get("error") or "؟")[:120]
                dd = add.get("data") or {}
                wid = dd.get("webdownload_id") or dd.get("id")
            info = {}
            for _ in range(12):
                info = bot.as_dict(bot.tb(k, "GET", "/webdl/mylist", params={"id": wid, "bypass_cache": "true"}).get("data"))
                st = str(info.get("download_state", "")).lower()
                if st in ("error", "failed", "expired"):
                    return "", "تحميل الرابط في حسابك فشل (%s)" % st
                if bot.is_ready(info):
                    break
                time.sleep(1.5)
            if not bot.is_ready(info):
                return "", "الملف لسه بيتحمّل في حسابك (%s%%)، جرّب تاني بعد شوية" % (info.get("progress") if info.get("progress") is not None else "؟")
            last = ""
            for typ in ("webdl", "webdownload"):
                r = bot.tb(k, "GET", "/stream/createstream", params={"id": wid, "file_id": 0, "type": typ})
                if isinstance(r, dict) and r.get("success"):
                    u = find_hls(r.get("data"))
                    if u:
                        return u, ""
                    last = "TorBox رجّع رد من غير رابط HLS"
                else:
                    last = str((r or {}).get("detail") or (r or {}).get("error") or "createstream فشل")[:150]
            return "", "بث حسابك: " + last
        except Exception as ex:
            return "", "تحويل من حسابك: " + str(ex)[:120]

    # ---------- سيرفر التحويل الخاص (اختياري): بيحوّل أي رابط مالوش بث محوَّل لـHLS صوته AAC ----------
    XC_URL = os.environ.get("TRANSCODE_URL", "").rstrip("/")
    XC_KEY = os.environ.get("TRANSCODE_KEY", "")

    def _xcode_hls(url, start=0):
        if not XC_URL or not XC_KEY or not url:
            return "", ""
        try:
            r = requests.post(XC_URL + "/hls/start", json={"url": url, "start": start},
                              headers={"X-Api-Key": XC_KEY}, timeout=90)
            j = r.json() if r.content else {}
            if r.ok and j.get("hls"):
                return j["hls"], ""
            return "", "سيرفر التحويل: " + str(j.get("error") or r.status_code)[:140]
        except Exception as ex:
            return "", "سيرفر التحويل مش بيرد: " + str(ex)[:100]

    @app.post("/play/start")
    def play_start():
        b = request.get_json(silent=True) or {}
        mode = b.get("mode") or "watch"
        h = str(b.get("hash") or "").lower()
        if not h:
            if not b.get("ref"):
                return jsonify(error="النسخة ناقصة"), 400
            d, code = _jr(resolve_checked())
            if not d.get("url"):
                return jsonify(error=d.get("error") or "فشل تجهيز الرابط"), code if code >= 400 else 502
            resp = {"direct": d["url"], "hls": "", "name": b.get("name") or "",
                    "size": b.get("size") or 0, "provider": "Stremio", "file_id": None, "stream_note": ""}
            if mode == "watch":
                # نسخ Stremio: نحاول نجيب بث HLS محوَّل (الصوت والصورة يشتغلوا في المتصفح). التحميل بيفضل على الرابط الأصلي.
                # لو أي خطوة فشلت نرجع للتشغيل المباشر ونرجّع السبب في stream_note (قبل كده كان بيتبلع من غير ما نعرف).
                try:
                    host = (urlparse(d["url"]).netloc or "").lower()
                    kind = ("realdebrid" if "real-debrid" in host else "alldebrid" if "alldebrid" in host
                            else "torbox" if "torbox" in host else "debridlink" if "debrid-link" in host else "")
                    hh = hash_from_ref(b.get("ref"), d["url"])
                    if hh:
                        b2 = dict(b, hash=hh, torrent_id=None, slow=bool(kind))
                        if kind:
                            b2["prefer"] = kind
                        dd, err2 = prepare(b2)
                        if not err2 and dd:
                            url, note, _ = stream_for(dd)
                            if url:
                                resp.update(hls=url, torrent_id=dd.get("torrent_id"), file_id=dd.get("file_id"))
                            else:
                                resp["stream_note"] = note or ""
                        elif err2:
                            resp["stream_note"] = str((err2[0].get_json(silent=True) or {}).get("error") or "")[:200]
                    if not resp["hls"] and hh:
                        alt = hls_fallback(hh, b)
                        if alt:
                            resp["hls"], resp["stream_note"], resp["provider"] = alt, "", "TorBox (بث محوَّل)"
                    if not resp["hls"]:
                        # رابط TorBox فيه torrent_id / file_id (إضافات زي Aradeb): بث محوَّل مباشرة من حسابك من غير تحميل تاني
                        for src_u in (bot.ref_get(str(b.get("ref") or "")) or "", d["url"]):
                            from urllib.parse import unquote as _uq
                            src_u = _uq(str(src_u))
                            mt = re.search(r"torrent_?id=(\d+)", src_u, re.I)
                            if mt:
                                mf = re.search(r"file_?id=(\d+)", src_u, re.I)
                                u4, n4, _r = torbox_stream(int(mt.group(1)), int(mf.group(1)) if mf else 0)
                                if u4:
                                    resp.update(hls=u4, stream_note="", provider="TorBox (بث محوَّل)")
                                    break
                                resp["stream_note"] = n4 or resp["stream_note"]
                    if not resp["hls"] and kind == "realdebrid":
                        u2, n2 = rd_hls_from_url(d["url"])
                        if u2:
                            resp["hls"], resp["stream_note"] = u2, ""
                        elif not resp["stream_note"]:
                            resp["stream_note"] = n2
                    if not resp["hls"] and not resp["stream_note"]:
                        resp["stream_note"] = ("البث المحوَّل مش متاح للنسخة دي (مفيش hash في الرابط أو المزوّد "
                                               "%s ملوش بث محوَّل)، بيتشغّل الملف الأصلي" % (kind or "؟"))
                except Exception as ex:
                    resp["stream_note"] = "تعذّر تجهيز البث المحوَّل: " + str(ex)[:120]
                if not resp["hls"]:
                    u3, n3 = webdl_hls(bot.ref_get(str(b.get("ref") or "")) or d["url"])   # الرابط الأصلي ثابت: مايتكررش التحميل
                    if u3:
                        resp["hls"], resp["stream_note"], resp["provider"] = u3, "", "TorBox (حسابك)"
                    elif n3:
                        resp["stream_note"] = ((resp["stream_note"] + " | ") if resp["stream_note"] else "") + n3
                if not resp["hls"] and XC_URL:
                    u5, n5 = _xcode_hls(d["url"], int(b.get("start") or 0))
                    if u5:
                        resp["hls"], resp["stream_note"], resp["provider"] = u5, "", "سيرفر التحويل"
                    elif n5:
                        resp["stream_note"] = ((resp["stream_note"] + " | ") if resp["stream_note"] else "") + n5
                # مفيش HLS: هنفحص الملف الأصلي قبل ما نسلّمه للمتصفح.
                # لو مش فيديو أصلاً نرجّع 409 فالواجهة تجرّب النسخة اللي بعدها لوحدها بدل ما تعرض "الصيغة مش مدعومة".
                if not resp["hls"]:
                    try:
                        sn = sniff(d["url"], 524288)
                    except Exception:
                        sn = None
                    if sn and not sn.get("error"):
                        if sn["container"].startswith("HTML"):
                            return jsonify(error="النسخة دي رجّعت صفحة مش فيديو. " + sn["fix"][:120]), 409
                        if sn["bad"]:
                            resp["stream_note"] = ((resp["stream_note"] + " | ") if resp["stream_note"] else "") + sn["fix"]
                            resp["bad_codecs"] = sn["bad"]
            return jsonify(resp)
        d, err = prepare(b)
        if err:
            return err
        resp = {"direct": d["direct_link"], "hls": "", "name": d.get("name") or "",
                "size": d.get("size") or 0, "provider": d.get("provider") or "",
                "torrent_id": d.get("torrent_id"), "file_id": d.get("file_id"),
                "stream_note": ""}
        # لينك المشاهدة (HLS) بيتجاب في المشاهدة والتحميل: لكل ملف لينكين مختلفين (direct للتحميل و hls للمشاهدة)
        resp["hls"], resp["stream_note"], _ = stream_for(d)
        if mode == "watch":
            if not resp["hls"]:
                alt = hls_fallback(h, b)
                if alt:
                    resp["hls"], resp["stream_note"] = alt, ""
            if not resp["hls"] and XC_URL:
                u5, n5 = _xcode_hls(d["direct_link"], int(b.get("start") or 0))
                if u5:
                    resp["hls"], resp["stream_note"], resp["provider"] = u5, "", "سيرفر التحويل"
                elif n5:
                    resp["stream_note"] = ((resp["stream_note"] + " | ") if resp["stream_note"] else "") + n5
        return jsonify(resp)

    @app.post("/play/ext")
    def play_ext():
        """بث HLS لمشغّل خارجي (VLC / Just Player...): الصوت AAC + الترجمة العربية جوه البث نفسه."""
        if not XC_URL or not XC_KEY:
            return jsonify(error="سيرفر التحويل مش مفعّل"), 501
        b = request.get_json(silent=True) or {}
        url = str(b.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return jsonify(error="رابط غير صالح"), 400
        sub = b.get("sub") or {}
        sub_url = ""
        if isinstance(sub, dict) and str(sub.get("u") or "").startswith("/subtitle/"):
            try:
                p = {"u": sub["u"], "shift": float(sub.get("shift") or 0), "scale": float(sub.get("scale") or 1),
                     "s": str(b.get("s") if b.get("s") is not None else "")[:6],
                     "e": str(b.get("e") if b.get("e") is not None else "")[:6],
                     "lang": str(sub.get("lang") or "")[:20], "exp": int(time.time()) + 6 * 3600, "f": "vtt"}
                tok = bot._b64e(json.dumps(p, separators=(",", ":")).encode())
                proto = request.headers.get("X-Forwarded-Proto", "https").split(",")[0].strip()
                base = (os.environ.get("PUBLIC_URL") or f"{proto}://{request.host}").rstrip("/")
                sub_url = f"{base}/subx/{tok}.{bot.sign('subx|' + tok)[:40]}/sub.vtt"
            except Exception:
                sub_url = ""
        try:
            r = requests.post(XC_URL + "/hls/start", headers={"X-Api-Key": XC_KEY}, timeout=120,
                              json={"url": url, "start": int(b.get("start") or 0), "sub": sub_url,
                                    "sub_name": str(sub.get("name") or "العربية")[:40] if isinstance(sub, dict) else "العربية",
                                    "sub_lang": "ar"})
            j = r.json() if r.content else {}
        except Exception as ex:
            return jsonify(error="سيرفر التحويل مش بيرد: " + str(ex)[:100]), 502
        if not r.ok or not j.get("hls"):
            return jsonify(error="سيرفر التحويل: " + str(j.get("error") or r.status_code)[:140]), 502
        return jsonify(hls=j["hls"], subs=bool(j.get("subs")), mode=j.get("mode"))

    # ---------- تسلسل المسلسل المتضاف (مشترك لكل الأعضاء): tmdb -> {"س:ح": مصدر الحلقة} ----------
    SM_FILE = os.path.join(bot.DATA_DIR, "series_map.json")
    SM_LOCK = threading.Lock()

    def _sm_load():
        try:
            with open(SM_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _sm_save(m):
        tmp = SM_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        os.replace(tmp, SM_FILE)

    @app.get("/series/map")
    def series_map_get():
        tid = str(request.args.get("id") or "")
        m = _sm_load()
        if tid:
            return jsonify(eps=(m.get(tid) or {}).get("eps") or {})
        return jsonify(counts={k: len(v.get("eps") or {}) for k, v in m.items()})

    @app.post("/series/map")
    def series_map_set():
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        b = request.get_json(silent=True) or {}
        tid, sn, en = _int(b.get("id")), _int(b.get("s")), _int(b.get("e"))
        sp = b.get("spec") or {}
        h = str(sp.get("hash") or "").lower()
        clean = {"hash": h if re.fullmatch(r"[0-9a-f]{40}", h) else "",
                 "name": str(sp.get("name") or "")[:200], "src": str(sp.get("src") or "")[:2000],
                 "ref": str(sp.get("ref") or "")[:2000], "fid": _int(sp.get("fid"))}
        if not tid or sn is None or en is None or not (clean["hash"] or clean["ref"] or clean["src"]):
            return jsonify(error="بيانات الحلقة ناقصة"), 400
        with SM_LOCK:
            m = _sm_load()
            ent = m.setdefault(str(tid), {"eps": {}})
            ent.setdefault("eps", {})["%d:%d" % (sn, en)] = dict(clean, by=g.user, ts=int(time.time()))
            _sm_save(m)
        return jsonify(ok=True, eps=ent["eps"])


    @app.post("/series/map/bulk")
    def series_map_bulk():
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        b = request.get_json(silent=True) or {}
        tid, sn, cnt = _int(b.get("id")), _int(b.get("s")), _int(b.get("count"))
        sp = b.get("spec") or {}
        h = str(sp.get("hash") or b.get("hash") or "").lower()
        name = str(sp.get("name") or b.get("name") or "")[:200]
        if not tid or not re.fullmatch(r"[0-9a-f]{40}", h):
            return jsonify(error="بيانات الموسم ناقصة"), 400
        pairs = []
        lst = b.get("eps")
        if isinstance(lst, list) and lst:                      # قايمة حلقات محددة (من ملفات الباك نفسها)
            for x in lst[:200]:
                if not isinstance(x, dict):
                    continue
                s_, e_ = _int(x.get("s")), _int(x.get("e"))
                if s_ is not None and e_ is not None:
                    pairs.append((s_, e_, _int(x.get("fid"))))
        elif sn is not None and cnt and 0 < cnt <= 120:        # موسم كامل بالعدد
            pairs = [(sn, en, None) for en in range(1, cnt + 1)]
        if not pairs:
            return jsonify(error="بيانات الموسم ناقصة"), 400
        added = 0
        with SM_LOCK:
            m = _sm_load()
            ent = m.setdefault(str(tid), {"eps": {}})
            eps = ent.setdefault("eps", {})
            for s_, e_, fid in pairs:
                k = "%d:%d" % (s_, e_)
                if k in eps and not b.get("force"):          # اللي اتضاف قبل كده مابيتغيّرش
                    continue
                eps[k] = {"hash": h, "name": name, "src": "", "ref": "",
                          "fid": fid, "by": g.user, "ts": int(time.time())}
                added += 1
            _sm_save(m)
        return jsonify(ok=True, added=added, eps=eps)

    @app.post("/series/unmap")
    def series_map_del():
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        b = request.get_json(silent=True) or {}
        tid, sn, en = _int(b.get("id")), _int(b.get("s")), _int(b.get("e"))
        if not tid:
            return jsonify(error="بيانات ناقصة"), 400
        with SM_LOCK:
            m = _sm_load()
            ent = m.get(str(tid)) or {"eps": {}}
            if sn is not None and en is not None:
                ent.get("eps", {}).pop("%d:%d" % (sn, en), None)
            else:
                ent["eps"] = {}
            if ent.get("eps"):
                m[str(tid)] = ent
            else:
                m.pop(str(tid), None)
            _sm_save(m)
        return jsonify(ok=True, eps=ent.get("eps") or {})

    # ---------- بلاغات فحص الفيديو (بتتبعت تلقائي من المشغّل لما فيه مشكلة) ----------
    REP_FILE = os.path.join(bot.DATA_DIR, "play_reports.jsonl")
    REP_LAST = {}

    @app.post("/play/report")
    def play_report():
        b = request.get_json(silent=True) or {}
        row = {"t": int(time.time()), "u": g.user, "msg": str(b.get("msg") or "")[:300],
               "title": str(b.get("title") or "")[:120], "s": _int(b.get("s")), "e": _int(b.get("e")),
               "mode": str(b.get("mode") or "")[:20], "f": str(b.get("f") or "")[:200],
               "ua": str(b.get("ua") or "")[:160]}
        if not row["msg"]:
            return jsonify(ok=False)
        k = (g.user, row["title"], re.sub(r"\d+", "#", row["msg"]))
        if time.time() - REP_LAST.get(k, 0) < 300:      # نفس البلاغ من نفس المستخدم خلال 5 دقايق: تجاهل
            return jsonify(ok=True, dup=True)
        REP_LAST[k] = time.time()
        try:
            with open(REP_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return jsonify(ok=True)

    @app.get("/play/reports")
    def play_reports():
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        rows = []
        try:
            with open(REP_FILE, encoding="utf-8") as f:
                for ln in f.readlines()[-40:]:
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
        except Exception:
            pass
        return jsonify(items=rows[::-1])

    # ---------- رابط HLS لملف جاهز (للمسارات اللي بتجيب الرابط من /generate: حلقات الباك وغيرها) ----------
    @app.post("/play/hls")
    def play_hls():
        try:
            lk, msg = bot.lock_of(g.user)
        except Exception:
            lk, msg = 0, ""
        if g.role != "admin" and lk >= 2:
            return jsonify(error=msg, nodl=True, lock=2), 403
        b = request.get_json(silent=True) or {}
        tid = str(b.get("torrent_id") or "")
        if not tid:
            return jsonify(hls="", note="torrent_id ناقص")
        url, note, _ = stream_for({"torrent_id": tid, "file_id": b.get("file_id"),
                                   "provider": b.get("provider") or "",
                                   "direct_link": str(b.get("direct_link") or "")[:2000]})
        return jsonify(hls=url or "", note=note or "")

    # ---------- فحص (أدمن) ----------
    @app.post("/play/diag")
    def play_diag():
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        b = request.get_json(silent=True) or {}
        b["slow"] = True
        steps = []
        d, err = prepare(b)
        if err:
            body = err[0].get_json(silent=True) or {}
            return jsonify(steps=[{"step": "prepare", "status": err[1], "body": body}]), 200
        steps.append({"step": "prepare", "name": d.get("name"), "size": d.get("size"),
                      "provider": d.get("provider"), "torrent_id": d.get("torrent_id"),
                      "file_id": d.get("file_id")})
        url, note, raw = stream_for(d)
        steps.append({"step": "stream", "provider": d.get("provider"), "hls_found": bool(url),
                      "hls": url, "note": note, "raw": mask(raw)[:2500]})
        if True:
            if url:
                origin = request.headers.get("Origin") or "https://profsser4.github.io"
                try:
                    r = requests.get(url, timeout=15, headers={"Origin": origin})
                    steps.append({
                        "step": "manifest", "status": r.status_code,
                        "type": r.headers.get("content-type"),
                        "cors": r.headers.get("access-control-allow-origin"),
                        "head": mask(r.text[:300])})
                except Exception as ex:
                    steps.append({"step": "manifest", "error": mask(ex)[:200]})
        return jsonify(steps=steps)

    # ---------- فحص عميق للملف (أدمن): الحاوية والكودكات والسبب ----------
    @app.post("/play/probe")
    def play_probe():
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        u = str((request.get_json(silent=True) or {}).get("url") or "")
        if not u.startswith("http"):
            return jsonify(error="رابط ناقص"), 400
        return jsonify(sniff(u))

    # ---------- بانر الرئيسية: أحدث 5 أعمال ----------
    HERO = {"t": 0, "items": []}

    @app.get("/hero")
    def hero():
        if HERO["items"] and time.time() - HERO["t"] < 1800:
            return jsonify(items=HERO["items"])
        iso = lambda n: (date.today() + timedelta(days=n)).isoformat()
        try:
            mv = bot.tmdb_get("/discover/movie", language="ar", include_adult="false", sort_by="popularity.desc",
                              **{"primary_release_date.gte": iso(-60), "primary_release_date.lte": iso(0),
                                 "vote_count.gte": "3"}).get("results", [])
            tv = bot.tmdb_get("/discover/tv", language="ar", include_adult="false", sort_by="popularity.desc",
                              **{"first_air_date.gte": iso(-120), "first_air_date.lte": iso(0)}).get("results", [])
        except Exception as ex:
            return jsonify(items=[], error=str(ex)[:120])

        def pack(x, t):
            if not (x.get("backdrop_path") and x.get("overview")):
                return None
            return {"id": x["id"], "type": t, "title": x.get("title") or x.get("name") or "",
                    "overview": x["overview"], "year": (x.get("release_date") or x.get("first_air_date") or "")[:4],
                    "rating": round(x.get("vote_average") or 0, 1),
                    "backdrop": "https://image.tmdb.org/t/p/w1280" + x["backdrop_path"]}

        a = [p for p in (pack(x, "movie") for x in mv) if p][:3]
        b = [p for p in (pack(x, "tv") for x in tv) if p][:2]
        items = [p for pair in zip(a, b) for p in pair] + a[len(b):] + b[len(a):]
        if items:
            HERO["items"], HERO["t"] = items, time.time()
        return jsonify(items=items)

    # ---------- "المزيد": صفحات أكتر من نفس الصف ----------
    def catalog_page(a, e, skip):
        parts = (["genre=" + quote(e["g"], safe="")] if e.get("g") else []) + (["skip=%d" % skip] if skip else [])
        path = "/catalog/%s/%s" % (e["ty"], quote(e["c"], safe="")) + ("/" + "&".join(parts) if parts else "")
        r = requests.get(a["base"] + path + ".json", timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        metas = r.json().get("metas") or []
        out = []
        for m in metas:
            iid = str(m.get("id", ""))
            if not m.get("poster"):
                continue
            it = {"type": "tv" if (m.get("type") or e["ty"]) == "series" else "movie",
                  "title": m.get("name") or "", "poster": m["poster"],
                  "year": str(m.get("year") or m.get("releaseInfo") or "")[:4]}
            if re.fullmatch(r"tt\d+", iid):
                it["imdb"] = iid
            elif iid.startswith("tmdb:") and iid[5:].isdigit():
                it["id"] = int(iid[5:])
            elif it["title"]:
                it["lk"] = True
            else:
                continue
            out.append(it)
        return out, len(metas)

    # ---------- دمج الصفوف + "المزيد" + التصنيفات ----------
    def srcs(e):
        return [{"a": e.get("a"), "ty": e.get("ty"), "c": e.get("c"), "g": e.get("g", "")}] + list(e.get("m") or [])

    def interleave(lists, cap=60):
        out, seen = [], set()
        for i in range(max((len(x) for x in lists), default=0)):
            for lst in lists:
                if i < len(lst):
                    x = lst[i]
                    k = x.get("imdb") or x.get("id") or (x.get("title"), x.get("year"))
                    if k not in seen:
                        seen.add(k)
                        out.append(x)
        return out[:cap]

    def row_of(e, amap):
        lists = []
        for sc in srcs(e):
            if sc["a"] == "_dub":
                lists.append(bot.dub_items())
            elif sc["a"] in amap:
                try:
                    lists.append(bot.cat_items(amap[sc["a"]], sc))
                except Exception:
                    pass
        items = interleave(lists, 20)
        return {"id": e["k"], "title": e["t"], "items": items} if items else None

    def my_rows():
        d = bot.ensure_catalogs()
        amap = {a["id"]: a for a in d["addons"]}
        ents = [e for e in bot.home_layout(d)
                if e.get("on") and (e.get("a") in amap or e.get("a") == "_dub")]
        if not ents:
            return bot.tmdb_rows()
        with ThreadPoolExecutor(max_workers=6) as ex:
            data = [x for x in ex.map(lambda e: row_of(e, amap), ents) if x]
        return jsonify(rows=data) if data else bot.tmdb_rows()

    def my_layout_set():
        d = bot.st_load()
        ids = {a["id"] for a in d["addons"]} | {"_dub"}
        rows_in = (request.get_json(silent=True) or {}).get("rows")
        if not isinstance(rows_in, list):
            return jsonify(error="بيانات ناقصة"), 400
        ok_src = lambda r: isinstance(r, dict) and r.get("a") in ids and r.get("ty") in ("movie", "series")
        out, used = [], set()
        for r in rows_in[:300]:
            if not ok_src(r):
                continue
            k = re.sub(r"[^a-z0-9]", "", str(r.get("k", "")).lower())[:16] or uuid.uuid4().hex[:8]
            if k in used:
                k = uuid.uuid4().hex[:8]
            used.add(k)
            ent = {"k": k, "a": r["a"], "ty": r["ty"], "c": str(r.get("c", ""))[:80],
                   "g": str(r.get("g", ""))[:40], "t": str(r.get("t", ""))[:60] or "صف", "on": bool(r.get("on"))}
            m = [{"a": x["a"], "ty": x["ty"], "c": str(x.get("c", ""))[:80], "g": str(x.get("g", ""))[:40]}
                 for x in (r.get("m") or [])[:8] if ok_src(x)]
            if m:
                ent["m"] = m
            out.append(ent)
        seen = {"dubeg"} | {bot.cat_key(a["id"], c["type"], c["id"])
                            for a in d["addons"] for c in a.get("catalogs") or []}
        tmp = bot.HOME_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rows": out, "seen": sorted(seen)}, f, ensure_ascii=False)
        os.replace(tmp, bot.HOME_FILE)
        return jsonify(ok=True)

    app.view_functions["rows"] = my_rows
    app.view_functions["home_layout_set"] = my_layout_set

    # ---------- دمج الصفوف ----------
    def src_list(e):
        return [{"a": e.get("a"), "ty": e.get("ty"), "c": e.get("c"), "g": e.get("g", "")}] + (e.get("m") or [])

    def ikey(x):
        return x.get("imdb") or x.get("id") or ((x.get("title") or "") + (x.get("year") or ""))

    def mix(lists, cap=20):
        out, seen = [], set()
        for i in range(max((len(l) for l in lists), default=0)):
            for l in lists:
                if i < len(l) and ikey(l[i]) not in seen:
                    seen.add(ikey(l[i]))
                    out.append(l[i])
        return out[:cap]

    def rows2():
        d = bot.ensure_catalogs()
        amap = {a["id"]: a for a in d["addons"]}
        ents = [e for e in bot.home_layout(d) if e.get("on") and (e.get("a") in amap or e.get("a") == "_dub")]
        if not ents:
            return bot.tmdb_rows()

        def fetch(s_):
            if s_.get("a") == "_dub":
                return bot.dub_items()
            ad = amap.get(s_.get("a"))
            try:
                return bot.cat_items(ad, s_) if ad else []
            except Exception as ex:
                bot.CAT_STATS[(s_["a"], s_["ty"], s_["c"], s_.get("g", ""))] = {"err": bot.short_err(ex)}
                return []

        def one(e):
            items = mix([fetch(x) for x in src_list(e)])
            return {"id": e["k"], "title": e["t"], "items": items} if items else None

        with ThreadPoolExecutor(max_workers=6) as ex:
            data = [x for x in ex.map(one, ents) if x]
        return jsonify(rows=data) if data else bot.tmdb_rows()

    def layout_set():
        d = bot.st_load()
        ids = {a["id"] for a in d["addons"]} | {"_dub"}
        rows_in = (request.get_json(silent=True) or {}).get("rows")
        if not isinstance(rows_in, list):
            return jsonify(error="بيانات ناقصة"), 400
        ok = lambda r: isinstance(r, dict) and r.get("a") in ids and r.get("ty") in ("movie", "series")
        out, used = [], set()
        for r in rows_in[:300]:
            if not ok(r):
                continue
            k = re.sub(r"[^a-z0-9]", "", str(r.get("k", "")).lower())[:16] or uuid.uuid4().hex[:8]
            if k in used:
                k = uuid.uuid4().hex[:8]
            used.add(k)
            ent = {"k": k, "a": r["a"], "ty": r["ty"], "c": str(r.get("c", ""))[:80], "g": str(r.get("g", ""))[:40],
                   "t": str(r.get("t", ""))[:60] or "صف", "on": bool(r.get("on"))}
            ms = [{"a": m["a"], "ty": m["ty"], "c": str(m.get("c", ""))[:80], "g": str(m.get("g", ""))[:40]}
                  for m in (r.get("m") or []) if ok(m)][:8]
            if ms:
                ent["m"] = ms
            out.append(ent)
        seen = {"dubeg"}
        for ad in d["addons"]:
            for c in ad.get("catalogs") or []:
                seen.add(bot.cat_key(ad["id"], c["type"], c["id"]))
        tmp = bot.HOME_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rows": out, "seen": sorted(seen)}, f, ensure_ascii=False)
        os.replace(tmp, bot.HOME_FILE)
        return jsonify(ok=True)

    if "rows" in app.view_functions:
        app.view_functions["rows"] = rows2
    if "home_layout_set" in app.view_functions:
        app.view_functions["home_layout_set"] = layout_set

    # ---------- التصنيفات والاهتمامات (من TMDB) ----------
    GENRES = {"action": (28, 10759), "drama": (18, 18), "comedy": (35, 35), "crime": (80, 80), "scifi": (878, 10765),
              "horror": (27, 9648), "romance": (10749, 18), "cartoon": (16, 16), "family": (10751, 10751), "doc": (99, 99)}
    L = lambda t, **q: (t, q)
    INTERESTS = {
        "cartoon": ("كرتون وأنيميشن", [L("movie", with_genres="16"), L("tv", with_genres="16")]),
        "kids": ("أطفال وعائلة", [L("movie", with_genres="10751"), L("tv", with_genres="10762")]),
        "ar": ("عربي", [L("movie", with_original_language="ar"), L("tv", with_original_language="ar")]),
        "hi": ("هندي", [L("movie", with_original_language="hi"), L("tv", with_original_language="hi")]),
        "tr": ("تركي", [L("tv", with_original_language="tr"), L("movie", with_original_language="tr")]),
        "ko": ("كوري", [L("tv", with_original_language="ko"), L("movie", with_original_language="ko")]),
        "anime": ("أنمي", [L("tv", with_genres="16", with_original_language="ja"),
                           L("movie", with_genres="16", with_original_language="ja")]),
        "act": ("أكشن", [L("movie", with_genres="28"), L("tv", with_genres="10759")]),
        "com": ("كوميدي", [L("movie", with_genres="35"), L("tv", with_genres="35")]),
        "hor": ("رعب", [L("movie", with_genres="27")]),
        "sci": ("خيال علمي", [L("movie", with_genres="878"), L("tv", with_genres="10765")]),
        "doc": ("وثائقي", [L("movie", with_genres="99"), L("tv", with_genres="99")]),
        "dub": ("🇪🇬 مدبلج مصري", None),
    }
    ICACHE = {}

    def tmdb_mix(specs, page, drop_ar=False, sort="popularity.desc", votes="20"):
        lists, more = [], False
        for typ, q in specs:
            params = {"vote_count.gte": votes}
            params.update(q)
            try:
                r = bot.tmdb_get("/discover/" + typ, language="ar", include_adult="false", sort_by=sort,
                                 page=page, **params)
            except Exception:
                continue
            more = more or page < min(int(r.get("total_pages") or 1), 40)
            lists.append([{"id": x["id"], "type": typ, "title": x.get("title") or x.get("name") or "",
                           "poster": "https://image.tmdb.org/t/p/w342" + x["poster_path"],
                           "year": (x.get("release_date") or x.get("first_air_date") or "")[:4]}
                          for x in r.get("results", [])
                          if x.get("poster_path") and not (drop_ar and x.get("original_language") == "ar")])
        return mix(lists, 40), more

    # ---------- أحدث الإضافات: عربي/أجنبي × أفلام/مسلسلات (بيتجدد كل 20 دقيقة) ----------
    FRESH = {"ar_movie": "🆕 أحدث الأفلام العربي", "ar_tv": "🆕 جديد المسلسلات العربي",
             "fr_movie": "🆕 أحدث الأفلام الأجنبي", "fr_tv": "🆕 جديد المسلسلات الأجنبي"}
    FCACHE = {}

    def fresh_specs(k):
        iso = lambda n: (date.today() + timedelta(days=n)).isoformat()
        is_movie = k.endswith("movie")
        q = ({"primary_release_date.gte": iso(-45), "primary_release_date.lte": iso(0)} if is_movie
             else {"air_date.gte": iso(-14), "air_date.lte": iso(0)})
        if k.startswith("ar"):
            q["with_original_language"] = "ar"
        return [("movie" if is_movie else "tv", q)], k.startswith("fr")

    @app.get("/fresh")
    def fresh():
        def one(k):
            hit = FCACHE.get(k)
            if hit and time.time() - hit[0] < 1200:
                return k, hit[1]
            specs, drop = fresh_specs(k)
            items = tmdb_mix(specs, 1, drop, votes="1")[0][:20]
            if items:
                FCACHE[k] = (time.time(), items)
            return k, items
        with ThreadPoolExecutor(max_workers=4) as ex:
            got = dict(ex.map(one, FRESH))
        return jsonify(rows=[{"id": "fresh:" + k, "title": t, "items": got[k]} for k, t in FRESH.items() if got.get(k)])

    # ---------- التريلر ----------
    TCACHE = {}

    @app.get("/trailer")
    def trailer():
        typ, tid = request.args.get("type"), _int(request.args.get("id"))
        if typ not in ("movie", "tv") or not tid:
            return jsonify(error="بيانات ناقصة"), 400
        if (typ, tid) in TCACHE:
            return jsonify(key=TCACHE[(typ, tid)])
        vid = ""
        for lang in ("ar", "en-US"):
            try:
                res = bot.tmdb_get("/%s/%s/videos" % (typ, tid), language=lang).get("results", [])
            except Exception:
                continue
            yt = [v for v in res if v.get("site") == "YouTube" and v.get("key")]
            best = (next((v for v in yt if v.get("type") == "Trailer" and v.get("official")), None)
                    or next((v for v in yt if v.get("type") == "Trailer"), None) or (yt[0] if yt else None))
            if best:
                vid = best["key"]
                break
        TCACHE[(typ, tid)] = vid
        return jsonify(key=vid)

    @app.get("/interest/list")
    def interest_list():
        return jsonify(items=[{"k": k, "name": v[0]} for k, v in INTERESTS.items()])

    @app.get("/interest")
    def interest_rows():
        rows = []
        for k in [x for x in request.args.get("k", "").split(",") if x in INTERESTS][:8]:
            name, specs = INTERESTS[k]
            hit = ICACHE.get(k)
            if hit and time.time() - hit[0] < 1800:
                items = hit[1]
            else:
                items = bot.dub_items() if specs is None else tmdb_mix(specs, 1)[0][:20]
                if items:
                    ICACHE[k] = (time.time(), items)
            if items:
                rows.append({"id": "int:" + k, "title": name, "items": items})
        return jsonify(rows=rows)

    # ---------- "ليك أنت": صفوف لكل بروفايل على حسب اللي هو شافه (زي نتفلكس) ----------
    # كل عضو/بروفايل ليه صفوفه من سجل مشاهدته هو بس. مفيش حاجة بتتشارك بين الأعضاء.
    TMETA = {}
    FYC = {}
    TREND = {"t": 0, "items": []}
    FY_FLAG = {"hi": "🇮🇳 ", "ar": "🎬 ", "tr": "🇹🇷 ", "ko": "🇰🇷 ", "foreign": "🌍 "}
    FY_LABEL = {"hi": "الهندي", "ar": "العربي", "tr": "التركي", "ko": "الكوري", "foreign": "الأجنبي"}
    FY_GEN = {  # التصنيف: (رقم تصنيف الفيلم, رقم تصنيف المسلسل, الاسم)
        "act": ("28", "10759", "أكشن"), "com": ("35", "35", "كوميدي"), "hor": ("27", "9648", "رعب"),
        "sci": ("878", "10765", "خيال علمي"), "doc": ("99", "99", "وثائقي"), "crime": ("80", "80", "جريمة"),
        "romance": ("10749", "18", "رومانسي")}
    FY_GSET = {k: {int(v[0]), int(v[1])} for k, v in FY_GEN.items()}
    FY_VOTES = {("movie", "hi"): "300", ("movie", "ar"): "100", ("movie", "tr"): "200", ("movie", "ko"): "300",
                ("tv", "hi"): "30", ("tv", "ar"): "30", ("tv", "tr"): "100", ("tv", "ko"): "200",
                ("movie", "foreign"): "8000", ("tv", "foreign"): "5000"}

    def fy_meta(ty, tid):
        k = (ty, tid)
        if k not in TMETA:
            try:
                a = bot.tmdb_get("/%s/%s" % (ty, tid), language="en-US")
            except Exception:
                return None
            TMETA[k] = {"lang": a.get("original_language") or "", "g": {x.get("id") for x in a.get("genres", [])}}
        return TMETA[k]

    def fy_history(user, pid):
        ud = bot.ud_load(user)
        pr = (ud.get("profiles") or {}).get(pid)
        if not isinstance(pr, dict) or pr.get("del"):
            return {}
        out = {}
        for k, it in ((ud.get("items") or {}).get(pid) or {}).items():
            if (not isinstance(it, dict) or it.get("del") or not str(it.get("tmdb", "")).isdigit()
                    or it.get("type") not in ("movie", "tv")):
                continue
            is_list = str(k).startswith("l")
            dur, pos = it.get("dur") or 0, it.get("pos") or 0
            w = 1.0 if is_list else (2.0 if it.get("done") else (1.0 if dur and pos / dur > 0.15 else 0.4))
            o = out.setdefault((it["type"], int(it["tmdb"])),
                               {"t": 0, "w": 0.0, "title": "", "txt": "", "seen": False})
            o["t"] = max(o["t"], bot.tnum(it))
            o["w"] += w
            o["title"] = o["title"] or it.get("title") or ""
            o["seen"] = o["seen"] or not is_list
            o["txt"] += " " + " ".join(str(it.get(f) or "") for f in ("title", "fname", "qn"))
        return out

    def fy_best(cat, ty):
        """صف "أفضل ... في التاريخ" لتصنيف معين"""
        noun = "الأفلام" if ty == "movie" else "المسلسلات"
        if cat in FY_LABEL:
            q = {} if cat == "foreign" else {"with_original_language": cat}
            title = "%sأفضل %s %s في التاريخ" % (FY_FLAG[cat], noun, FY_LABEL[cat])
            items = tmdb_mix([L(ty, **q)], 1, drop_ar=(cat == "foreign"), sort="vote_average.desc",
                             votes=FY_VOTES[(ty, cat)])[0]
        elif cat == "anime":
            title = "🎌 أفضل الأنمي في التاريخ"
            items = tmdb_mix([L("tv", with_genres="16", with_original_language="ja"),
                              L("movie", with_genres="16", with_original_language="ja")], 1,
                             sort="vote_average.desc", votes="500")[0]
        else:
            m, t, name = FY_GEN[cat]
            title = "⭐ أفضل %s %s في التاريخ" % ("أفلام" if ty == "movie" else "مسلسلات", name)
            items = tmdb_mix([L(ty, with_genres=m if ty == "movie" else t)], 1, sort="vote_average.desc",
                             votes="3000" if ty == "movie" else "1000")[0]
        return title, items

    def fy_because(ty, tid, title):
        try:
            res = bot.tmdb_get("/%s/%s/recommendations" % (ty, tid), language="ar").get("results", [])
        except Exception:
            return None
        items = [{"id": x["id"], "type": ty, "title": x.get("title") or x.get("name") or "",
                  "poster": "https://image.tmdb.org/t/p/w342" + x["poster_path"],
                  "year": (x.get("release_date") or x.get("first_air_date") or "")[:4]}
                 for x in res if x.get("poster_path")]
        return {"id": "fy:b:%s:%s" % (ty, tid), "title": "لأنك شاهدت " + title, "items": items}

    def fy_trending():
        if TREND["items"] and time.time() - TREND["t"] < 1800:
            return TREND["items"]
        try:
            items = bot.row_items({"type": "movie", "path": "/trending/all/day", "q": {}})
        except Exception:
            items = []
        if items:
            TREND["items"], TREND["t"] = items, time.time()
        return TREND["items"]

    def fy_build(user, pid, skip, lite=False):
        hist = fy_history(user, pid)
        seen = set(hist)
        rows = []
        recent = sorted(hist.items(), key=lambda kv: kv[1]["t"], reverse=True)[:30]
        with ThreadPoolExecutor(max_workers=6) as ex:
            metas = list(ex.map(lambda kv: fy_meta(*kv[0]), recent))
        score = {}
        for idx, ((key, info), m) in enumerate(zip(recent, metas)):
            if not m:
                continue
            ty, lang, gs = key[0], m["lang"], m["g"]
            w = info["w"] * (1.0 - idx / 60.0)
            cats = []
            if 16 in gs or 10751 in gs:
                dub = bool(bot.DUB_RX.search(info["txt"]))
                if lang == "ja" and 16 in gs and not dub:
                    cats.append((("anime", "tv"), w))
                else:
                    cats.append((("cartoon", "movie"), w * (1.5 if dub else 1.0)))
            if lang in FY_LABEL and lang != "foreign":
                cats.append(((lang, ty), w))
            elif lang and lang != "ja" and 16 not in gs:
                cats.append((("foreign", ty), w))
            for c, gset in FY_GSET.items():
                if gs & gset:
                    cats.append(((c, ty), w * 0.5))
            for c, s in cats:
                score[c] = score.get(c, 0) + s
        # "لأنك شاهدت": آخر عملين خلّصهم أو بدأهم فعلاً
        tops = [(k, i) for k, i in recent if i["seen"] and i["w"] >= 1][:1 if lite else 2]
        if tops:
            with ThreadPoolExecutor(max_workers=2) as ex:
                for r in ex.map(lambda ki: fy_because(ki[0][0], ki[0][1], ki[1]["title"]), tops):
                    if r:
                        r["items"] = [x for x in r["items"] if (x["type"], x["id"]) not in seen][:20]
                        if len(r["items"]) >= 4:
                            rows.append(r)
        tr = [x for x in fy_trending() if (x.get("type"), x.get("id")) not in seen]
        if len(tr) >= 4:
            rows.append({"id": "fy:trend", "title": "🔥 الأكثر مشاهدة النهارده", "items": tr[:20]})
        picked, gen_n = [], 0
        for (cat, ty), s in sorted(score.items(), key=lambda kv: kv[1], reverse=True):
            gen = cat in FY_GEN
            if cat in skip or (gen and gen_n >= 2):
                continue
            gen_n += gen
            picked.append((cat, ty))
            if len(picked) >= (2 if lite else 5):
                break

        def one(ct):
            cat, ty = ct
            if cat == "cartoon":
                items = mix([bot.dub_items(), tmdb_mix([L("movie", with_genres="16|10751")], 1)[0]], 40)
                title = "🇪🇬 كرتون مدبلج"
            else:
                title, items = fy_best(cat, ty)
            items = [x for x in items if (x.get("type"), x.get("id")) not in seen][:20]
            return {"id": "fy:%s:%s" % (cat, ty), "title": title, "items": items} if len(items) >= 4 else None

        if picked:
            with ThreadPoolExecutor(max_workers=5) as ex:
                rows += [r for r in ex.map(one, picked) if r]
        return rows

    @app.get("/for-you")
    def for_you():
        pid = request.args.get("pid", "")[:24]
        skip = {x for x in request.args.get("skip", "").split(",") if x}
        lite = request.args.get("lite") == "1"
        ck = (g.user, pid, ",".join(sorted(skip)), lite)
        hit = FYC.get(ck)
        if hit and time.time() - hit[0] < 300:
            return jsonify(rows=hit[1])
        try:
            rows = fy_build(g.user, pid, skip, lite)
        except Exception:
            rows = hit[1] if hit else []
        FYC[ck] = (time.time(), rows)
        return jsonify(rows=rows)

    # ---------- آخر الإضافات (مسلسلات قائمتك + أحدث الأفلام) ----------
    LAT = {}

    @app.get("/latest")
    def latest():
        hit = LAT.get(g.user)
        if hit and time.time() - hit[0] < 900:
            return jsonify(items=hit[1])
        limit = (date.today() - timedelta(days=30)).isoformat()

        def one(kv):
            tid, title = kv
            try:
                info = bot.tmdb_get("/tv/%s" % tid, language="ar")
            except Exception:
                return None
            le = info.get("last_episode_to_air") or {}
            ad = le.get("air_date") or ""
            if not ad or ad < limit or not info.get("poster_path"):
                return None
            return ad, {"id": tid, "type": "tv", "title": info.get("name") or title,
                        "poster": "https://image.tmdb.org/t/p/w342" + info["poster_path"],
                        "year": (info.get("first_air_date") or "")[:4],
                        "badge": "S%02dE%02d" % (le.get("season_number") or 0, le.get("episode_number") or 0)}

        with ThreadPoolExecutor(max_workers=6) as ex:
            got = sorted([x for x in ex.map(one, list(bot.follows_of(g.user).items())[:20]) if x],
                         key=lambda x: x[0], reverse=True)
        items = [x[1] for x in got]
        LAT[g.user] = (time.time(), items[:20])
        return jsonify(items=items[:20])

    # ---------- "المزيد": صفحات أكتر (صفوف الموقع، التصنيفات، الاهتمامات) ----------
    @app.get("/browse")
    def browse():
        gen, it, fr = request.args.get("genre"), request.args.get("int"), request.args.get("fresh")
        if fr in FRESH:
            page = (_int(request.args.get("skip")) or 0) + 1
            specs, drop = fresh_specs(fr)
            items, more = tmdb_mix(specs, page, drop, votes="1")
            return jsonify(items=items, skip=str(page), more=more)
        if gen or it:
            page = (_int(request.args.get("skip")) or 0) + 1
            if it == "dub":
                return jsonify(items=bot.dub_items(), skip="0", more=False)
            if gen in GENRES:
                specs = [L("movie", with_genres=str(GENRES[gen][0])), L("tv", with_genres=str(GENRES[gen][1]))]
            elif it in INTERESTS and INTERESTS[it][1]:
                specs = INTERESTS[it][1]
            else:
                return jsonify(error="القسم ده مش موجود"), 404
            items, more = tmdb_mix(specs, page)
            return jsonify(items=items, skip=str(page), more=more)
        d = bot.ensure_catalogs()
        e = next((x for x in bot.home_layout(d) if x.get("k") == request.args.get("k", "")), None)
        if not e:
            return jsonify(error="الصف ده مش موجود"), 404
        srcs = src_list(e)
        skips = [(_int(x) or 0) for x in str(request.args.get("skip", "")).split(",")]
        skips += [0] * (len(srcs) - len(skips))
        lists, new = [], []
        for s_, sk in zip(srcs, skips):
            if s_.get("a") == "_dub":
                its = bot.dub_items() if sk == 0 else []
                lists.append(its)
                new.append(sk + len(its))
                continue
            ad = next((x for x in d["addons"] if x["id"] == s_.get("a")), None)
            try:
                its, raw = catalog_page(ad, s_, sk) if ad else ([], 0)
            except Exception:
                its, raw = [], 0
            lists.append(its)
            new.append(sk + raw)
        items = mix(lists, 80)
        if not items and not any(skips):
            return jsonify(error="فشل تحميل القائمة"), 502
        return jsonify(title=e.get("t"), items=items, skip=",".join(str(n) for n in new),
                       more=any(n > sk for n, sk in zip(new, skips)))

    def search2():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify(results=[])
        if bot.PROWLARR_URL and bot.PROWLARR_KEY:
            return orig_search()
        items = tb_items("/torrents/search/" + quote(q, safe=""), {"check_cache": "true"})
        if not items:
            return jsonify(error="مفيش نتايج من بحث TorBox. جرّب اسم تاني (بالإنجليزي من غير سنة)."), 200
        items.sort(key=lambda i: (not i["cached"], -(i["seeders"] or 0)))
        return jsonify(results=items[:60])

    if "search" in app.view_functions:
        orig_search = app.view_functions["search"]
        app.view_functions["search"] = search2

    # ---------- قسم مكتبتي الخاص (أفلام / مسلسلات / كرتون) ----------
    try:
        import clib
        clib.register(app, bot)
    except Exception as ex:
        print("clib مش اتسجّل:", ex)
