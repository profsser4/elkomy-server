"""
storage_sync.py: مرآة تخزين للباك إند.

بتحفظ ملفات البيانات (المستخدمين، المزوّدين، سجل المشاهدة...) في قاعدة Postgres
خارجية، وبترجّعها لما السيرفر يقوم من جديد (زي Render المجاني اللي بيمسح الملفات).
لو DATABASE_URL مش متضبط مبتعمل أي حاجة، والباك إند يشتغل بالملفات زي ما هو.

القاعدة هي المصدر الأساسي: عند التشغيل بنرجّع منها الملفات، وبعدها كل 15 ثانية
بنرفع أي ملف اتغيّر. وعند الإغلاق الطبيعي بنرفع آخر تغييرات.
"""
import atexit
import hashlib
import json
import os
import re
import threading
import time

DB_URL = os.environ.get("DATABASE_URL", "").strip()
INTERVAL = 15
NAMES = {
    "providers.json", "userdata.json", "stremio.json", "users.json",
    "home.json", "activity.json", "dubbed.json", "push.json",
    "vapid_private.pem", "vapid_public.txt",
}
UD_RX = re.compile(r"ud_[a-z0-9_.-]{3,20}\.json")


def ok_name(fn):
    return fn in NAMES or bool(UD_RX.fullmatch(fn))


def digest(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def pg_connect():
    import psycopg2
    return psycopg2.connect(DB_URL, connect_timeout=15)


class Mirror:
    def __init__(self, data_dir, connect=pg_connect):
        self.dir = data_dir
        self.connect = connect
        self.known = {}          # اسم الملف -> بصمة آخر نسخة متخزّنة في القاعدة
        self.lock = threading.Lock()

    def restore(self):
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                "name TEXT PRIMARY KEY, data TEXT NOT NULL, updated BIGINT NOT NULL)")
            cur.execute("SELECT name, data FROM kv")
            rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        n = 0
        for name, data in rows:
            if not ok_name(name):
                continue
            path = os.path.join(self.dir, name)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
            self.known[name] = digest(data)
            n += 1
        return n

    def sync_once(self):
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
                        continue        # الملف لسه بيتكتب، نعيد في الدورة الجاية
                h = digest(data)
                if self.known.get(fn) != h:
                    changes.append((fn, data, h))
            gone = [fn for fn in self.known if fn not in present]
            if not changes and not gone:
                return 0
            conn = self.connect()
            try:
                cur = conn.cursor()
                now = int(time.time())
                for fn, data, _ in changes:
                    cur.execute(
                        "INSERT INTO kv (name, data, updated) VALUES (%s, %s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET "
                        "data = EXCLUDED.data, updated = EXCLUDED.updated",
                        (fn, data, now))
                for fn in gone:
                    cur.execute("DELETE FROM kv WHERE name = %s", (fn,))
                conn.commit()
            finally:
                conn.close()
            for fn, _, h in changes:
                self.known[fn] = h
            for fn in gone:
                self.known.pop(fn, None)
            return len(changes) + len(gone)

    def safe_sync(self):
        try:
            self.sync_once()
        except Exception:
            pass

    def loop(self):
        while True:
            time.sleep(INTERVAL)
            self.safe_sync()


def start(data_dir):
    """بتتنادى مرة واحدة وقت تشغيل السيرفر، قبل أي طلب."""
    if not DB_URL:
        return None
    m = Mirror(data_dir)
    for i in range(6):
        try:
            m.restore()
            break
        except Exception:
            if i == 5:
                raise       # نوقف التشغيل بدل ما نشتغل بحالة فاضية ونكتب فوق البيانات
            time.sleep(5)
    m.safe_sync()           # أول مرة: لو في ملفات محلية والقاعدة فاضية بنرفعها
    threading.Thread(target=m.loop, daemon=True).start()
    atexit.register(m.safe_sync)
    return m
