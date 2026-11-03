"""Durable SQLite state without a disk: gzip'd consistent snapshot (sqlite backup API) in a PRIVATE
Supabase Storage bucket at {DATA_PATH}/desk.db.gz. Enabled by DESK_DB_REMOTE=1. Secrets come only from env
and are never logged. Uploads go to a .tmp object first, then replace the final object, so a partial upload
never clobbers the last good copy; restore verifies gzip CRC + sqlite integrity before using a download."""
import gzip, os, sqlite3, tempfile, time
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL") or "https://inuingicceojxdjfsnaa.supabase.co"
BUCKET = "desk-private"


class SupabaseStorage:
    """Minimal Storage REST client (httpx). Methods return bool / bytes|None; never raise secrets in messages."""
    def __init__(self, key=None, url=SUPABASE_URL, bucket=BUCKET, timeout=8.0):
        import httpx
        key = key or os.environ["SUPABASE_SECRET_KEY"]
        self.base, self.bucket = url.rstrip("/") + "/storage/v1", bucket
        self.h = httpx.Client(timeout=timeout, headers={"apikey": key, "Authorization": f"Bearer {key}"})

    def ensure_bucket(self):
        r = self.h.get(f"{self.base}/bucket/{self.bucket}")
        if r.status_code == 200:
            if r.json().get("public"):
                self.h.put(f"{self.base}/bucket/{self.bucket}", json={"id": self.bucket, "public": False}).raise_for_status()
            return
        self.h.post(f"{self.base}/bucket", json={"id": self.bucket, "name": self.bucket, "public": False}).raise_for_status()

    def put(self, path, data):
        r = self.h.post(f"{self.base}/object/{self.bucket}/{path}", content=data,
                        headers={"Content-Type": "application/gzip", "x-upsert": "true"})
        return r.status_code in (200, 201)

    def get(self, path):
        r = self.h.get(f"{self.base}/object/authenticated/{self.bucket}/{path}")
        return r.content if r.status_code == 200 else None

    def delete(self, path):
        r = self.h.request("DELETE", f"{self.base}/object/{self.bucket}", json={"prefixes": [path]})
        return r.status_code == 200

    def move(self, src, dst):
        r = self.h.post(f"{self.base}/object/move", json={"bucketId": self.bucket, "sourceKey": src, "destinationKey": dst})
        return r.status_code == 200


class MemoryStorage:
    """Local fake transport for tests."""
    def __init__(self): self.objs = {}
    def ensure_bucket(self): pass
    def put(self, p, d): self.objs[p] = bytes(d); return True
    def get(self, p): return self.objs.get(p)
    def delete(self, p): self.objs.pop(p, None); return True
    def move(self, s, d):
        if s not in self.objs or d in self.objs: return False
        self.objs[d] = self.objs.pop(s); return True


def object_path(prefix=None):
    prefix = (prefix if prefix is not None else os.environ.get("DATA_PATH", "")).strip("/")
    if not prefix:
        raise RuntimeError("remote: DATA_PATH missing")
    return f"{prefix}/desk.db.gz"


def snapshot(db_path):
    """Consistent gzip'd copy of a live WAL db via the sqlite backup API."""
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "snap.db")
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
        return gzip.compress(Path(tmp).read_bytes(), 6)


def _valid(blob):
    try:
        raw = gzip.decompress(blob)
    except Exception:
        return None
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "chk.db"); Path(p).write_bytes(raw)
        try:
            c = sqlite3.connect(p); ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; c.close()
        except sqlite3.DatabaseError:
            ok = False
    return raw if ok else None


def backup(db_path, store, path):
    if not Path(db_path).exists():
        return False
    blob = snapshot(db_path); tmp = path + ".tmp"
    if not store.put(tmp, blob):
        return False
    store.delete(path)
    if store.move(tmp, path):
        return True
    return store.put(path, blob)  # fallback: direct upsert (tmp copy stays as a spare)


def restore(db_path, store, path):
    """Only when the local db is missing. Tries the final object, then the .tmp spare. Returns True if restored."""
    if Path(db_path).exists():
        return False
    for p in (path, path + ".tmp"):
        blob = store.get(p)
        raw = _valid(blob) if blob else None
        if raw:
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
            part = str(db_path) + ".restore"
            Path(part).write_bytes(raw); os.replace(part, db_path)
            return True
    return False


def enabled():
    return os.environ.get("DESK_DB_REMOTE") == "1"


def log(msg):
    print(f"{time.strftime('%F %T')} remote: {msg}", flush=True)
