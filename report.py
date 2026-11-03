"""Morning report JSON for the last night (from settings.night_start). FOMO links only via links.py.
Usage: python report.py [db_path]"""
import json, sys, time
import db, links
from config import Settings


def _setting(c, key):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def build(c, now=None):
    now = time.time() if now is None else now
    ns = _setting(c, "night_start")
    ns = float(ns) if ns is not None else now - 16 * 3600
    q = lambda sql, *a: c.execute(sql, a).fetchall()
    names = {r["mint"]: (r["name"] or r["symbol"]) for r in q("SELECT mint,name,symbol FROM coins")}
    trades = []
    for p in q("SELECT * FROM positions WHERE closed_at IS NOT NULL AND closed_at>=? ORDER BY closed_at", ns):
        size = p["size_usd"] or 0; pnl = p["pnl_usd"] or 0
        trades.append({"mint": p["mint"], "name": names.get(p["mint"]), "mode": p["mode"],
                       "entry": p["entry_price"], "exit": p["exit_price"], "size_usd": size,
                       "pnl_usd": round(pnl, 2), "pnl_pct": round(100 * pnl / size, 2) if size else None,
                       "exit_reason": p["exit_reason"], "fees_usd": round(p["fees_usd"] or 0, 4),
                       "opened_at": p["opened_at"], "closed_at": p["closed_at"], "coin_url": links.coin_url(p["mint"])})
    trades.sort(key=lambda t: (t["pnl_usd"] >= 0, t["pnl_usd"] if t["pnl_usd"] < 0 else -t["pnl_usd"]))  # red first (worst), then green (best)
    seen = {r[0] for r in q("SELECT mint FROM coins WHERE migrated_at>=?", ns)} | {r[0] for r in q("SELECT mint FROM scans WHERE ts>=?", ns)}
    kills = {}
    for r in q("SELECT mint,ts,reason FROM scans WHERE ts>=? AND verdict='kill'", ns):
        kills.setdefault(r["reason"] or "", []).append(r["mint"])
    refusals = {}
    for r in q("SELECT mint,ts,layer,name,value_text FROM audits WHERE ts>=? AND pass=0 ORDER BY ts", ns):
        refusals.setdefault(f"{r['layer']} {r['name']}", []).append({"mint": r["mint"], "name": names.get(r["mint"]),
                                                                        "ts": r["ts"], "value": r["value_text"]})
    rejects = [{"mint": r["mint"], "name": names.get(r["mint"]), "ts": r["ts"], "failed_rule": r["reason"], "score": r["score"]}
               for r in q("SELECT mint,ts,reason,score FROM verdicts WHERE ts>=? AND pass=0 ORDER BY ts", ns)]
    jev = c.execute("SELECT COALESCE(SUM(input_tokens),0) FROM verdicts WHERE ts>=?", (ns,)).fetchone()[0]
    l8 = c.execute("SELECT COUNT(*) FROM audits WHERE ts>=? AND layer='8' AND pass=0 AND value_text='no data: delegations'",
                   (ns,)).fetchone()[0]
    out = {"night_start": ns, "generated_at": now,
           "trades": trades, "coins_seen": len(seen),
           "scanner_kills": {k: {"count": len(v), "mints": v} for k, v in sorted(kills.items(), key=lambda kv: -len(kv[1]))},
           "auditor_refusals": {k: {"count": len(v), "items": v} for k, v in sorted(refusals.items())},
           "analyst_rejects": rejects, "jev_tokens": int(jev),
           "total_fees_usd": round(sum(t["fees_usd"] for t in trades), 4),
           "signup_url": links.SIGNUP_URL, "report_time": Settings(c).get("desk.report_time"),
           "last_report_url": _setting(c, "last_report_url")}
    if l8:
        out["auditor_layer8_down_kills"] = l8
    return out


if __name__ == "__main__":
    print(json.dumps(build(db.connect(sys.argv[1] if len(sys.argv) > 1 else (__import__("os").environ.get("DESK_DB_PATH") or "desk.db"))), indent=1, default=str))
