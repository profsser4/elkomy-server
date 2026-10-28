"""
storage_sync.py: حفظ بيانات الباك إند في قاعدة Postgres خارجية (Neon).

مصمّم للاستضافات اللي مفيهاش ديسك دايم (زي Vercel):
- قبل كل طلب: بنسحب من القاعدة أي ملف اتغيّر (ومرة واحدة كل ثانيتين بالكتير).
- بعد كل طلب: بنرفع أي ملف اتغيّر.
لو DATABASE_URL مش متضبط مبنعملش حاجة، والباك إند يشتغل بالملفات زي ما هو.
"""
import hashlib
import json
import os
import re
import threading
import time

DB_URL = os.environ.get("DATABASE_URL", "").strip()
MIN_GAP = 2.0
NAMES = {
    "providers.json", "userdata.json", "stremio.json", "users.json",
    "home.json", "activity.json", "dubbed.json", "push.json",
    "vapid_private.pem", "vapid_public.txt",
    "clib.json",                       # قسم مكتبتي الخاص (أفلام/مسلسلات/كرتون)
    "series_map.json", "settings.json",   # كانوا بيضيعوا مع كل ريستارت على Vercel
}
UD_RX = re.compile(r"ud_[a-z0-9_.-]{3,20}\.json")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS kv ("
    "name TEXT PRIMARY KEY, data TEXT NOT NULL, "
    "sha TEXT NOT NULL DEFAULT '', updated BIGINT NOT NULL DEFAULT 0)")


def ok_name(fn):
    return fn in NAMES or bool(UD_RX.fullmatch(fn))


def digest(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def pg_connect():
    import psycopg2
    return psycopg2.connect(DB_URL, connect_timeout=10)


class Mirror:
    def __init__(self, data_dir, connect=pg_connect):
        self.dir = data_dir
        self.connect = connect
        self.known = {}          # اسم الملف -> بصمة النسخة اللي في القاعدة
        self.lock = threading.RLock()
        self.conn = None
        self.ready = False       # اتسحبت البيانات من القاعدة مرة على الأقل؟
        self.last = 0.0

    # ---- الاتصال: بيعيد المحاولة مرة لو Neon كان نايم أو قفل الاتصال
    def _drop(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def _run(self, work):
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    self.conn = self.connect()
                return work(self.conn)
            except Exception:
                self._drop()
                if attempt:
                    raise

    def _write(self, name, data):
        path = os.path.join(self.dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)

    # ---- من القاعدة للملفات
    def refresh(self, force=False):
        now = time.time()
        if not force and self.ready and now - self.last < MIN_GAP:
            return 0
        with self.lock:
            def listing(conn):
                cur = conn.cursor()
                if not self.ready:
                    cur.execute(SCHEMA)
                cur.execute("SELECT name, sha FROM kv")
                rows = cur.fetchall()
                conn.commit()
                return rows

            remote = {n: s for n, s in self._run(listing) if ok_name(n)}
            need = [n for n, s in remote.items() if self.known.get(n) != s]
            n_changed = 0
            if need:
                def fetch(conn):
                    cur = conn.cursor()
                    out = []
                    if len(need) > 4:
                        cur.execute("SELECT name, data FROM kv")
                        out = [r for r in cur.fetchall() if r[0] in need]
                    else:
                        for n in need:
                            cur.execute("SELECT name, data FROM kv WHERE name = %s", (n,))
                            out.extend(cur.fetchall())
                    conn.commit()
                    return out

                for n, data in self._run(fetch):
                    self._write(n, data)
                    self.known[n] = remote[n]
                    n_changed += 1
            for n in [k for k in self.known if k not in remote]:
                try:
                    os.remove(os.path.join(self.dir, n))
                except OSError:
                    pass
                self.known.pop(n, None)
                n_changed += 1
            self.ready = True
            self.last = now
            return n_changed

    # ---- من الملفات للقاعدة
    def push(self):
        if not self.ready:       # عمرنا ما سحبنا من القاعدة، ممنوع نكتب فوقها
            return 0
        with self.lock:
            present, changes = set(), []
            try:
                names = os.listdir(self.dir)
            except OSError:
                return 0
            for fn in names:
                if not ok_name(fn):
                    continue
                present.add(fn)
                try:
                    with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                        data = f.read()
                except OSError:
                    continue
                if fn.endswith(".json"):
                    try:
                        json.loads(data)
                    except ValueError:
                        continue
                h = digest(data)
                if self.known.get(fn) != h:
                    changes.append((fn, data, h))
            gone = [fn for fn in self.known if fn not in present]
            if not changes and not gone:
                return 0

            def work(conn):
                cur = conn.cursor()
                now = int(time.time())
                for fn, data, h in changes:
                    cur.execute(
                        "INSERT INTO kv (name, data, sha, updated) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, "
                        "sha = EXCLUDED.sha, updated = EXCLUDED.updated",
                        (fn, data, h, now))
                for fn in gone:
                    cur.execute("DELETE FROM kv WHERE name = %s", (fn,))
                conn.commit()

            self._run(work)
            for fn, _, h in changes:
                self.known[fn] = h
            for fn in gone:
                self.known.pop(fn, None)
            return len(changes) + len(gone)


def attach(app, data_dir, connect=pg_connect):
    """بتربط المرآة بتطبيق Flask: سحب قبل كل طلب، ورفع بعده."""
    if connect is pg_connect and not DB_URL:
        return None
    from flask import jsonify, request

    m = Mirror(data_dir, connect)

    def before():
        if request.method == "OPTIONS" or request.path == "/":
            return None
        try:
            m.refresh()
        except Exception:
            if not m.ready:
                return jsonify(error="قاعدة البيانات مش متاحة دلوقتي، جرّب بعد شوية"), 503
        return None

    def after(resp):
        try:
            m.push()
        except Exception:
            pass
        return resp

    app.before_request_funcs.setdefault(None, []).insert(0, before)
    app.after_request(after)
    return m
