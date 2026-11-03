import json, os, socket, sqlite3, urllib.request
import db, health, remote

SECRETS = {"TG_TOKEN": "fake-tg-token-value", "OWNER_CHAT_ID": "998877665",
           "DATA_PATH": "fakeprefix-zzz", "SUPABASE_SECRET_KEY": "sb_secret_TESTfakefakefake"}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_health_endpoint(tmp_path, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    p = tmp_path / "d.db"; c = db.connect(str(p)); db.heartbeat(c, "scanner")
    c.execute("INSERT INTO settings VALUES('_owner_chat_id', ?, 0)", (json.dumps(SECRETS["OWNER_CHAT_ID"]),)); c.commit()
    port = free_port(); srv = health.serve(port, str(p), host="127.0.0.1")
    try:
        for path in ("/", "/health"):
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
            body = r.read().decode()
            assert r.status == 200 and body.count("\n") == 1
            d = json.loads(body)
            assert d["mode"] == "PAPER" and "scanner" in d["heartbeat_age_s"] and "uptime_s" in d
            for v in SECRETS.values():
                assert v not in body
    finally:
        srv.shutdown()


def test_health_without_db(tmp_path):
    port = free_port(); srv = health.serve(port, str(tmp_path / "missing.db"), host="127.0.0.1")
    try:
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).status == 200
    finally:
        srv.shutdown()


def test_backup_restore_roundtrip(tmp_path):
    src = tmp_path / "a.db"; c = db.connect(str(src))
    c.execute("INSERT INTO settings VALUES('_owner_chat_id', '\"42\"', 0)"); db.heartbeat(c, "risk"); c.commit()
    st = remote.MemoryStorage(); path = remote.object_path("pfx")
    assert remote.backup(str(src), st, path) and path in st.objs and path + ".tmp" not in st.objs
    assert remote.backup(str(src), st, path)  # second upload replaces the first
    dst = tmp_path / "sub" / "b.db"
    assert remote.restore(str(dst), st, path)
    r = sqlite3.connect(dst).execute("SELECT value FROM settings WHERE key='_owner_chat_id'").fetchone()
    assert r[0] == '"42"'
    assert not remote.restore(str(dst), st, path)  # never overwrites an existing local db


def test_restore_rejects_corrupt_and_uses_spare(tmp_path):
    src = tmp_path / "a.db"; db.connect(str(src)).close()
    st = remote.MemoryStorage(); path = "p/desk.db.gz"
    good = remote.snapshot(str(src)); st.objs[path] = good[: len(good) // 2]; st.objs[path + ".tmp"] = good
    assert remote.restore(str(tmp_path / "c.db"), st, path)
    st.objs = {path: b"garbage"}
    assert not remote.restore(str(tmp_path / "d.db"), st, path) and not (tmp_path / "d.db").exists()
