"""
clib.py: قسم "مكتبتي" الخاص (أفلام / مسلسلات / كرتون) - الأدمن بس هو اللي بيضيف.

  GET  /clib/list      (أي عضو)  القايمة من غير أي روابط
  GET  /clib/item      (أدمن)    عنصر كامل بروابطه للتعديل
  POST /clib/save      (أدمن)    إضافة / تعديل
  POST /clib/delete    (أدمن)    حذف
  POST /clib/play      (أي عضو)  السيرفر بيجرّب الروابط بالترتيب لحد ما واحد يشتغل
                                  والروابط الأصلية عمرها ما بتتبعت للمتصفح غير للأدمن

بيتسجّل من آخر extras.register من غير تعديل wsgi.py.
بيتخزّن في clib.json جنب series_map.json في DATA_DIR.
"""
import json
import os
import re
import threading
import time
import uuid

from flask import g, jsonify, request, make_response

CATS = ("movie", "series", "cartoon")
LINK_RX = re.compile(r"(magnet:\?[^\s|]+|https?://[^\s|]+)", re.I)
EP_PREFIX = re.compile(r"^\s*[sS](\d{1,2})\s*[eE](\d{1,3})\b[\s:|\-]*")
SEASON_ONLY = re.compile(r"^\s*[sS](\d{1,2})\s*$")
MAX_LINKS = 20
MAX_EPS = 400
BUDGET = 35          # ثواني نجرّب فيها روابط في طلب واحد قبل ما نرجّع للعميل يكمّل


def parse_links(text):
    out = []
    for m in LINK_RX.findall(str(text or "")):
        m = m.strip()[:2000]
        if m not in out:
            out.append(m)
    return out[:MAX_LINKS]


def parse_eps(text):
    """كل سطر = حلقة، وأكتر من رابط في نفس السطر = بدائل. S02E05 في أول السطر اختياري."""
    eps, s, e = {}, 1, 0
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        ms = SEASON_ONLY.match(line)
        if ms:
            s, e = int(ms.group(1)), 0
            continue
        m = EP_PREFIX.match(line)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            rest = line[m.end():]
        else:
            e += 1
            rest = line
        links = parse_links(rest)
        if not links:
            continue
        k = (s, e)
        if k in eps:
            eps[k]["links"] = (eps[k]["links"] + [x for x in links if x not in eps[k]["links"]])[:MAX_LINKS]
        else:
            eps[k] = {"s": s, "e": e, "links": links}
    return [eps[k] for k in sorted(eps)][:MAX_EPS]


def eps_to_text(eps):
    return "\n".join("S%02dE%02d %s" % (x["s"], x["e"], " | ".join(x["links"])) for x in eps)


def register(app, bot):
    FILE = os.path.join(bot.DATA_DIR, "clib.json")
    LOCK = threading.Lock()

    def load():
        try:
            with open(FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def save(d):
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, FILE)

    def public(it):
        return {"id": it["id"], "cat": it["cat"], "kind": it["kind"], "title": it["title"],
                "poster": it.get("poster") or "", "year": it.get("year") or "",
                "eps": [{"s": x["s"], "e": x["e"]} for x in it.get("eps") or []],
                "t": it.get("t") or 0}

    def admin_only():
        if g.role != "admin":
            return jsonify(error="للأدمن بس"), 403
        return None

    @app.get("/clib/list")
    def clib_list():
        cat = request.args.get("cat") or ""
        items = [public(x) for x in load().values() if not cat or x.get("cat") == cat]
        items.sort(key=lambda x: -x["t"])
        return jsonify(items=items)

    @app.get("/clib/item")
    def clib_item():
        bad = admin_only()
        if bad:
            return bad
        it = load().get(str(request.args.get("id") or ""))
        if not it:
            return jsonify(error="مش موجود"), 404
        out = public(it)
        out["links_text"] = "\n".join(it.get("links") or [])
        out["eps_text"] = eps_to_text(it.get("eps") or [])
        return jsonify(out)

    @app.post("/clib/save")
    def clib_save():
        bad = admin_only()
        if bad:
            return bad
        b = request.get_json(silent=True) or {}
        cat = b.get("cat") if b.get("cat") in CATS else "movie"
        kind = "tv" if b.get("kind") == "tv" else "movie"
        title = str(b.get("title") or "").strip()[:120]
        poster = str(b.get("poster") or "").strip()[:1000]
        if poster and not poster.lower().startswith(("http://", "https://")):
            return jsonify(error="رابط الصورة لازم يبدأ بـ http"), 400
        links = parse_links(b.get("links_text"))
        eps = parse_eps(b.get("eps_text")) if kind == "tv" else []
        if not title:
            return jsonify(error="اكتب الاسم"), 400
        if not links and not eps:
            return jsonify(error="ضيف رابط واحد على الأقل"), 400
        with LOCK:
            d = load()
            iid = str(b.get("id") or "")
            old = d.get(iid)
            if not old:
                iid = "c" + uuid.uuid4().hex[:8]
            d[iid] = {"id": iid, "cat": cat, "kind": kind, "title": title, "poster": poster,
                      "year": str(b.get("year") or "").strip()[:8], "links": links, "eps": eps,
                      "t": (old or {}).get("t") or int(time.time())}
            save(d)
        return jsonify(ok=True, id=iid)

    @app.post("/clib/delete")
    def clib_delete():
        bad = admin_only()
        if bad:
            return bad
        iid = str((request.get_json(silent=True) or {}).get("id") or "")
        with LOCK:
            d = load()
            d.pop(iid, None)
            save(d)
        return jsonify(ok=True)

    # ---------- تشغيل: السيرفر بيجرّب الروابط بالترتيب ----------
    def gen(user, role, data):
        with app.test_request_context("/generate", method="POST", json=data):
            g.user, g.role = user, role
            resp = make_response(bot._generate())
        return resp.status_code, (resp.get_json(silent=True) or {})

    def order_of(links, li, skip):
        idx = [i for i in range(len(links)) if i not in skip]
        slow = set()
        for i in idx:                      # magnet مش جاهز على المزوّد بيتأجّل لآخر الطابور
            try:
                if links[i].startswith("magnet:"):
                    h = bot.magnet_hash(links[i])
                    if h and h not in bot.cached_hashes([h]):
                        slow.add(i)
            except Exception:
                pass
        idx.sort(key=lambda i: i in slow)
        if li is not None and li in idx:
            idx.remove(li)
            idx.insert(0, li)
        return idx

    @app.post("/clib/play")
    def clib_play():
        if g.role != "admin":
            lk, msg = bot.lock_of(g.user)
            if lk >= 2:
                return jsonify(error=msg, nodl=True, lock=2), 403
        b = request.get_json(silent=True) or {}
        it = load().get(str(b.get("id") or ""))
        if not it:
            return jsonify(error="العنصر ده اتحذف"), 404
        links = it.get("links") or []
        if it.get("kind") == "tv" and it.get("eps"):
            try:
                s, e = int(b.get("s")), int(b.get("e"))
            except (TypeError, ValueError):
                s, e = it["eps"][0]["s"], it["eps"][0]["e"]
            ep = next((x for x in it["eps"] if x["s"] == s and x["e"] == e), None)
            if not ep:
                return jsonify(error="الحلقة دي مش موجودة"), 404
            links = ep["links"]
        if not links:
            return jsonify(error="مفيش روابط"), 404
        try:
            li = int(b["li"]) if b.get("li") is not None else None
        except (TypeError, ValueError):
            li = None
        skip = {int(x) for x in (b.get("skip") or []) if str(x).isdigit()}
        tid = b.get("torrent_id") or None
        tried, done_idx, t0 = [], [], time.time()
        for i in order_of(links, li, skip):
            if done_idx and time.time() - t0 > BUDGET:     # حدّ وقت الطلب في Vercel: العميل يكمّل بباقي الروابط
                return jsonify(more=True, skip=sorted(skip | set(done_idx))), 408
            data = {"query": links[i]}
            if tid and i == li:
                data["torrent_id"] = tid
            try:
                code, d = gen(g.user, g.role, data)
            except Exception as ex:
                code, d = 500, {"error": str(ex)[:120]}
            if code == 202:
                d.pop("tried", None)
                return jsonify(dict(d, li=i, n=len(links))), 202
            if code == 200 and d.get("direct_link"):
                try:
                    bot.act_log(g.user, "torrent" if d.get("torrent_id") else "link",
                                d.get("name"), d.get("size"), d.get("provider"))
                except Exception:
                    pass
                d.pop("tried", None)
                return jsonify(dict(d, li=i, n=len(links)))
            done_idx.append(i)
            tried.append("#%d: %s" % (i + 1, str(d.get("error") or code)[:120]))
        out = {"error": "كل الروابط فشلت (%d)" % len(links), "n": len(links)}
        if g.role == "admin":
            out["tried"] = tried
        return jsonify(out), 400
