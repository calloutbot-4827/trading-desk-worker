import pytest
import config, db
from config import Settings, SettingError, ticket_check


def test_defaults_sample():
    s = Settings()
    assert s.get("scanner.min_liquidity_usd") == 40000
    assert s.get("executor.min_ticket_usd") == 47.50
    assert s.get("desk.mode") == "PAPER" and s.get("analyst.mode") == "rules"
    assert s.get("desk.rpc_url") == "https://api.mainnet-beta.solana.com"
    assert s.get("risk.stop_pct") == 6 and s.get("desk.fees_ref_discount") == 0.10


def test_alias_and_change_log(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    s = Settings(c)
    assert s.set("stop", 8) == (6, 8)
    assert s.get("risk.stop_pct") == 8
    row = c.execute("SELECT key,old,new,who FROM change_log").fetchone()
    assert tuple(row) == ("risk.stop_pct", "6", "8", "owner")
    assert Settings(c).get("stop") == 8  # persisted


def test_out_of_range_refused():
    s = Settings()
    with pytest.raises(SettingError, match="between 1 and 50"):
        s.set("stop", 99)
    assert s.get("stop") == 6


def test_enum_and_unknown():
    s = Settings()
    with pytest.raises(SettingError):
        s.set("executor.below_min", "maybe")
    with pytest.raises(SettingError):
        s.set("nope", 1)
    with pytest.raises(SettingError):
        s.set("desk.report_time", "25:00")


def test_ticket_check():
    assert 47.50 / 0.08 == pytest.approx(593.75)
    t, w = ticket_check(200, 8, 47.50, "bump")
    assert t == 47.50 and w == "tickets bumped to $47.50 = 23.8% of stack"
    t, w = ticket_check(200, 8, 47.50, "skip")
    assert t is None and w == "every coin will be skipped, stack under $593.75"
    t, w = ticket_check(1000, 8, 47.50, "bump")
    assert t == 80 and w is None


def test_wal(tmp_path):
    c = db.connect(str(tmp_path / "w.db"))
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.activity(c, "bought", "M", "executor", "t")
    with pytest.raises(Exception):
        db.activity(c, "bogus", "M", "x", "t")
