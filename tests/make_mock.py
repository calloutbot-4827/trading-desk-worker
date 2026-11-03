"""Builds tests/mock_d/{full,empty}/ with api.py's real builders against a TEMP db seeded with
CLEARLY MARKED EXAMPLE data (names start with 'EXAMPLE'). Never touches the real desk.db.
Run: .venv/bin/python tests/make_mock.py"""
import asyncio, json, math, os, pathlib, shutil, sys, tempfile, time
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("SUPABASE_SECRET_KEY", "TG_TOKEN", "DATA_PATH", "OWNER_CHAT_ID"):
    os.environ.pop(k, None)  # builders only; never load real secrets
import api, db, links
from config import Settings

OUT = ROOT / "tests" / "mock_d"
EX = {  # EXAMPLE mints (fake, not real tokens)
    "dog": ("EXAMPLEdog1111111111111111111111111111111", "EXAMPLE Dog", "EXDOG"),
    "cat": ("EXAMPLEcat2222222222222222222222222222222", "EXAMPLE Cat", "EXCAT"),
    "rug": ("EXAMPLErug3333333333333333333333333333333", "EXAMPLE Rug", "EXRUG"),
    "bad": ("EXAMPLEbad4444444444444444444444444444444", "EXAMPLE Stop", "EXSTP"),
    "chs": ("EXAMPLEchs5555555555555555555555555555555", "EXAMPLE Chase", "EXCHS"),
}


class A:
    paused = False
    room = {}


class TG:
    def connected(self): return True


def seed(c, now):
    ns = now - 6 * 3600
    db.insert(c, "settings", key="night_start", value=json.dumps(ns), updated_at=now)
    # balance: 200 -> ~232 over the night (EXAMPLE numbers)
    for i in range(0, 6 * 60 + 1, 2):
        t = ns + i * 60
        v = 200 + 32 * (i / 360) + 3 * math.sin(i / 17)
        db.insert(c, "balance", ts=t, value=round(v, 2))
    for k, (m, n, s) in EX.items():
        db.upsert_coin(c, m, name=n, symbol=s, holders=(412 if k == "dog" else None))
    ok_vals = ["revoked", "revoked", "dev holds 0.8%", "top 10 hold 14%", "LP 100% burned", "pool 34 min old", "2 bundled", "0 delegated"]
    for k, t0 in (("dog", now - 3000), ("cat", ns + 3600), ("bad", ns + 7200), ("chs", now - 1500)):
        m = EX[k][0]
        db.insert(c, "scans", mint=m, ts=t0 - 200, liquidity_usd=61000, top10_pct=14, h1_change_pct=30, age_min=34, verdict="pass", reason="")
        for i in range(1, 9):
            db.insert(c, "audits", mint=m, ts=t0 + i, layer=str(i), name="x", value_text=ok_vals[i - 1], **{"pass": 1}, source="rugcheck")
    rug = EX["rug"][0]
    db.insert(c, "scans", mint=rug, ts=now - 2200, liquidity_usd=48000, top10_pct=22, h1_change_pct=40, age_min=15, verdict="pass", reason="")
    for i, (v, p) in enumerate([("revoked", 1), ("revoked", 1), ("dev holds 31%", 0)], 1):
        db.insert(c, "audits", mint=rug, ts=now - 2100 + i, layer=str(i), name="x", value_text=v, **{"pass": p}, source="rpc")
    db.insert(c, "scans", mint="EXAMPLEkil6666666666666666666666666666666", ts=now - 900, liquidity_usd=9000, top10_pct=60,
              h1_change_pct=10, age_min=12, verdict="kill", reason="liquidity $9,000 < $40,000")
    db.upsert_coin(c, "EXAMPLEkil6666666666666666666666666666666", name="EXAMPLE Thin", symbol="EXTHN")
    db.insert(c, "verdicts", mint=EX["chs"][0], ts=now - 1400, alive_6h=.8, organic=.7, flush=.1, chase=.62, score=58, **{"pass": 0}, reason="chase 0.62", input_tokens=0)
    for k, t0, sc in (("dog", now - 2900, 84), ("cat", ns + 3700, 77), ("bad", ns + 7300, 72)):
        db.insert(c, "verdicts", mint=EX[k][0], ts=t0, alive_6h=.8, organic=.8, flush=.1, chase=.1, score=sc, **{"pass": 1}, reason="", input_tokens=0)
    # trades
    trades = [("cat", ns + 3800, 0.0021, 16.0, ns + 3800 + 5400, 0.0030, "trail", 6.86),
              ("bad", ns + 7400, 0.011, 17.0, ns + 7400 + 1500, 0.01034, "stop", -1.02),
              ("dog", now - 2800, 0.00042, 18.5, None, None, None, None)]
    for k, t, ep, sz, ct, xp, why, pnl in trades:
        m = EX[k][0]
        db.insert(c, "signals", mint=m, ts=t, price=ep, size_usd=sz, stack_pct=8, link=links.coin_url(m), mode="PAPER", status="sent")
        db.insert(c, "positions", mint=m, opened_at=t, entry_price=ep, size_usd=sz, peak_price=(ep * 1.31 if k == "dog" else ep * 1.5),
                  stop_price=ep * 0.94, stop_pct=6, trail_active=1 if k == "dog" else 0, closed_at=ct, exit_price=xp,
                  exit_reason=why, pnl_usd=pnl, mode="PAPER")
    # EXAMPLE price path for the open coin, every 20s for ~46 min
    m, p, vol = EX["dog"][0], 0.00042, 1000.0
    for i in range(140):
        p *= 1 + 0.012 * math.sin(i / 6) + 0.004
        vol += 200 + 150 * abs(math.sin(i / 3))
        db.insert(c, "prices", mint=m, ts=now - 2800 + i * 20, price=p, volume_m5=vol)
    acts = [(ns + 3800, "bought", "cat", "executor", "Bought EXAMPLE Cat", "Executor · 20:47", "$16.00 · 8% of stack"),
            (ns + 7400, "bought", "bad", "executor", "Bought EXAMPLE Stop", "Executor · 21:47", "$17.00 · 8% of stack"),
            (ns + 8900, "stopped", "bad", "risk", "Stopped EXAMPLE Stop", "Risk · -6% stop", "-$1.02"),
            (ns + 9200, "sold", "cat", "risk", "Sold EXAMPLE Cat", "Risk · trailing stop", "+$6.86 · +42.9%"),
            (now - 2099, "rejected", "rug", "auditor", "Rejected EXAMPLE Rug", "Auditor · dev holds 31%", "Skipped"),
            (now - 2800, "bought", "dog", "executor", "Bought EXAMPLE Dog", "Executor · 00:00", "$18.50 · 8% of stack"),
            (now - 1400, "rejected", "chs", "analyst", "Rejected EXAMPLE Chase", "Analyst (rules) · chase 0.62", "Skipped")]
    for t, kind, k, ag, ti, sub, val in acts:
        i = db.activity(c, kind, EX[k][0], ag, ti, sub, val)
        c.execute("UPDATE activity SET ts=? WHERE id=?", (t, i))
    c.commit()


async def build(name, seeded):
    d = OUT / name
    shutil.rmtree(d, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        c = db.connect(os.path.join(tmp, "mock.db"))
        now = time.time()
        if seeded:
            seed(c, now)
        for a in api.ORDER:
            db.heartbeat(c, a)
        s = Settings(c)
        up = api.FakeUploader()
        pub = api.Publisher(c, s, {n: A() for n in api.ORDER}, up, net=None, telegram=TG() if seeded else None)
        if seeded:  # stats DexScreener would give (EXAMPLE numbers)
            pub.pairs[EX["dog"][0]] = {"marketCap": 512000, "liquidity": {"usd": 64000}, "volume": {"h24": 230000}}
        await pub.publish_once()
        for f, body in up.files.items():
            p = d / f; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(body)
    print(name, len(up.files), "files")


if __name__ == "__main__":
    asyncio.run(build("full", True)); asyncio.run(build("empty", False))
