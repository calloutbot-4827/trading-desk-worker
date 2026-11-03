import db, links, report
from config import Settings


def test_report(tmp_path):
    c = db.connect(str(tmp_path / "r.db")); Settings(c).set("desk.mode", "PAPER")
    c.execute("INSERT INTO settings(key,value,updated_at) VALUES('night_start','100',0)"); c.commit()
    db.upsert_coin(c, "G", name="Green", migrated_at=150); db.upsert_coin(c, "R", name="Red", migrated_at=150)
    db.insert(c, "positions", mint="G", opened_at=200, entry_price=1, size_usd=10, closed_at=300, exit_price=1.5,
              exit_reason="trail", pnl_usd=5, fees_usd=0.36, mode="PAPER")
    db.insert(c, "positions", mint="R", opened_at=200, entry_price=1, size_usd=10, closed_at=300, exit_price=0.8,
              exit_reason="stop", pnl_usd=-2, fees_usd=0.36, mode="PAPER")
    db.insert(c, "positions", mint="OLD", opened_at=1, entry_price=1, size_usd=10, closed_at=50, pnl_usd=9, fees_usd=1)
    db.insert(c, "scans", mint="K", ts=160, verdict="kill", reason="liq $1.0K")
    db.insert(c, "scans", mint="K2", ts=160, verdict="kill", reason="liq $1.0K")
    db.insert(c, "audits", mint="A", ts=170, layer="8", name="delegations", value_text="no data: delegations", **{"pass": 0})
    db.insert(c, "verdicts", mint="V", ts=180, reason="alive_6h", score=40, input_tokens=0, **{"pass": 0})
    r = report.build(c, now=400)
    assert [t["mint"] for t in r["trades"]] == ["R", "G"]
    assert r["trades"][0]["pnl_pct"] == -20.0 and r["trades"][0]["coin_url"] == links.coin_url("R")
    assert r["scanner_kills"]["liq $1.0K"]["count"] == 2
    assert r["auditor_refusals"]["8 delegations"]["items"][0]["ts"] == 170
    assert r["analyst_rejects"][0]["failed_rule"] == "alive_6h" and r["jev_tokens"] == 0
    assert r["total_fees_usd"] == 0.72 and r["auditor_layer8_down_kills"] == 1
    assert r["signup_url"] == links.SIGNUP_URL and r["report_time"] == "08:00" and r["last_report_url"] is None
    assert r["coins_seen"] == 4
