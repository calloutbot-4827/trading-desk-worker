import json, os, sqlite3
import db, notifier, telegram
from config import Settings

def test_env_wins_over_secrets_file(tmp_path, monkeypatch):
    f = tmp_path / "s.env"; f.write_text("TG_TOKEN=fromfile\nDATA_PATH=filepath\nOWNER_CHAT_ID=111\n")
    monkeypatch.setenv("TG_TOKEN", "fromenv"); monkeypatch.delenv("DATA_PATH", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    notifier.load_env(f)
    assert os.environ["TG_TOKEN"] == "fromenv" and os.environ["DATA_PATH"] == "filepath"

def test_missing_secrets_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "x"); notifier.load_env(tmp_path / "nope.env")
    assert os.environ["TG_TOKEN"] == "x"

def test_owner_persists_in_db_without_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, "load_env", lambda *a, **k: None)  # never read the real .secrets.env
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    c = db.connect(str(tmp_path / "d.db")); s = Settings(c)
    t = telegram.Telegram(c, s, None, {}, token="t", secrets_path=tmp_path / "absent.env")
    assert t.owner is None
    t._claim_owner(42)
    assert not (tmp_path / "absent.env").exists()
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    t2 = telegram.Telegram(c, Settings(c), None, {}, token="t", secrets_path=tmp_path / "absent.env")
    assert t2.owner == "42"
    monkeypatch.setenv("OWNER_CHAT_ID", "99")  # env takes priority over stored owner
    assert telegram.Telegram(c, Settings(c), None, {}, token="t", secrets_path=tmp_path / "absent.env").owner == "99"
    assert Settings(c).get("desk.mode") == "PAPER"

def test_cap_dir(tmp_path):
    from agents.common import cap_dir
    for i in range(5):
        (tmp_path / f"{i}.json").write_text("x" * 10)
    assert cap_dir(tmp_path, max_files=2) == 3 and len(list(tmp_path.iterdir())) == 2
