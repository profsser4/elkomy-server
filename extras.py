"""
extras.py: مسار التشغيل الجديد (اختيار النسخة، البث المحوَّل، التحميل).

بيتسجّل من wsgi.py من غير ما نعدّل bot.py، وبيستخدم دوال bot.py الموجودة.
الروابط:
  GET  /play/versions  نسخ العمل (فيلم أو حلقة) من إضافات Stremio مع علامة "فوري"
  POST /play/start     بيجهّز النسخة ويرجّع رابط البث المحوَّل (hls) والرابط الأصلي (direct)
  POST /play/diag      (أدمن) بيفحص كل خطوة ويرجّع الردود الخام عشان نعرف أي مشكلة
"""
import re
import time
from datetime import date, timedelta
from urllib.parse import quote

import requests
from flask import g, jsonify, request

RISKY_RX = re.compile(
    r"(?:\bDDP?\s?\d|\bDD\+|\bAC-?3\b|\bE-?AC-?3\b|\bDTS|\bTrueHD|\bAtmos"
    r"|\bHEVC|\bx265|\bH[. ]?265|\b10-?bit|\.avi\b)", re.I)
Q_RX = re.compile(r"2160p|4K|1080p|720p|480p", re.I)
HLS_KEYS = ("hls_url", "hlsUrl", "hls", "playlist", "stream_url", "url")
_MASK = re.compile(r"""(?i)(token['"]?\s*[:=]\s*['"]?)[^'"&\s,}]+""")


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
    STREAMERS = {"torbox": lambda d: torbox_stream(d["torrent_id"], d.get("file_id"))}

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
        d, code = _jr(bot.torrent_dispatch(magnet, tid))
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

    # ---------- النسخ ----------
    @app.get("/play/versions")
    def play_versions():
        d, code = _jr(bot.st_streams())
        res = d.get("results") or []
        if not res:
            return jsonify(versions=[], error=d.get("error") or "مفيش نسخ متاحة للعمل ده")
        hashes = list({(x.get("hash") or "").lower() for x in res if x.get("hash")})[:100]
        try:
            cached = bot.cached_hashes(hashes) if hashes else set()
        except Exception:
            cached = set()
        best = {}
        for x in res:
            h = (x.get("hash") or "").lower()
            title = x.get("title") or ""
            m = Q_RX.search(title)
            v = {
                "title": title, "size": x.get("size") or 0,
                "seeders": x.get("seeders") or 0, "hash": h,
                "ref": x.get("ref") or "",
                "instant": bool((h and h in cached) or x.get("ref")),
                "dub": bool(x.get("dub")), "eg": bool(x.get("eg")),
                "risky": bool(RISKY_RX.search(title.replace("_", " "))),
                "q": m.group(0).upper() if m else "",
            }
            key = h or v["ref"] or title
            o = best.get(key)
            if o is None or (v["instant"], v["seeders"]) > (o["instant"], o["seeders"]):
                best[key] = v
        out = sorted(best.values(),
                     key=lambda v: (not v["instant"], -v["seeders"]))[:60]
        return jsonify(versions=out)

    # ---------- تجهيز النسخة ----------
    @app.post("/play/start")
    def play_start():
        b = request.get_json(silent=True) or {}
        mode = b.get("mode") or "watch"
        h = str(b.get("hash") or "").lower()
        if not h:
            if not b.get("ref"):
                return jsonify(error="النسخة ناقصة"), 400
            d, code = _jr(bot.st_resolve())
            if not d.get("url"):
                return jsonify(error=d.get("error") or "فشل تجهيز الرابط"), code if code >= 400 else 502
            return jsonify(direct=d["url"], hls="", name=b.get("name") or "",
                           size=b.get("size") or 0, provider="Stremio", file_id=None)
        d, err = prepare(b)
        if err:
            return err
        resp = {"direct": d["direct_link"], "hls": "", "name": d.get("name") or "",
                "size": d.get("size") or 0, "provider": d.get("provider") or "",
                "torrent_id": d.get("torrent_id"), "file_id": d.get("file_id"),
                "stream_note": ""}
        if mode == "watch":
            resp["hls"], resp["stream_note"], _ = stream_for(d)
        return jsonify(resp)

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

    @app.get("/browse")
    def browse():
        k, skip = request.args.get("k", ""), _int(request.args.get("skip")) or 0
        d = bot.ensure_catalogs()
        e = next((x for x in bot.home_layout(d) if x.get("k") == k), None)
        if not e:
            return jsonify(error="الصف ده مش موجود"), 404
        if e.get("a") == "_dub":
            return jsonify(title=e.get("t"), items=bot.dub_items(), skip=0, more=False)
        a = next((x for x in d["addons"] if x["id"] == e.get("a")), None)
        if not a:
            return jsonify(error="الإضافة اتحذفت"), 404
        try:
            items, raw = catalog_page(a, e, skip)
        except Exception:
            return jsonify(error="فشل تحميل القائمة"), 502
        return jsonify(title=e.get("t"), items=items, skip=skip + raw, more=raw > 0)
