"""LIVE replay (not part of pytest, not inside the running desk).
Connects to PumpPortal subscribeMigration for up to REPLAY_S seconds and feeds graduated coins through the REAL
Scanner -> Auditor -> Analyst -> Executor -> Risk in PAPER with a temp DB, a fake notifier (no Telegram) and a fake
uploader. Temp config only: scanner.min_pool_age_min=0, scanner.poll_s=10.
Also, per coin, a PROBE records raw field presence (DexScreener pumpswap liquidity.usd, RugCheck insiders graph keys,
getProgramAccounts counts for Token and Token-2022) and a SHADOW Auditor (own temp DB, auditor.min_pool_age_min=0)
audits every coin the scanner saw so all 8 layers get exercised on real data. Output: logs/replay/*.json(l)."""
import asyncio, json, os, pathlib, sys, tempfile, time
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
import db, api
from bus import Bus
from config import Settings
from agents.common import Net, g, pick_pumpswap, TOKEN_PROGRAM, TOKEN_2022
from agents.scanner import Scanner
from agents.auditor import Auditor
from agents.analyst import Analyst
from agents.executor import Executor
from agents.risk import Risk

REPLAY_S = float(os.environ.get("REPLAY_S", 600))
OUT = ROOT / "logs" / "replay"; OUT.mkdir(parents=True, exist_ok=True)
probes = open(OUT / "probes.jsonl", "a")


class FakeNotifier:
    def __init__(self): self.sent = []
    async def send(self, text, buttons=None):
        self.sent.append({"text": text, "buttons": buttons}); return True


def keyset(objs):
    ks = set()
    for o in objs:
        if isinstance(o, dict): ks |= set(o)
    return sorted(ks)


async def probe(net, mint):
    rec = {"mint": mint, "ts": time.time()}
    try:
        pairs = await net.dex_tokens([mint]); p = pick_pumpswap(pairs, mint)
        rec["dex_pairs"] = [{"dexId": g(x, "dexId"), "liquidity": g(x, "liquidity"), "has_pairCreatedAt": g(x, "pairCreatedAt") is not None,
                             "priceUsd": g(x, "priceUsd")} for x in pairs]
        rec["pumpswap_liquidity_usd"] = g(p, "liquidity", "usd") if p else "NO_PUMPSWAP_PAIR"
        rec["pumpswap_null_fields"] = [k for k in ("priceUsd", "liquidity", "pairCreatedAt", "volume", "txns", "priceChange", "marketCap")
                                       if p and g(p, k) is None]
        if p: rec["pumpswap_priceChange_keys"] = sorted((g(p, "priceChange") or {}).keys())
    except Exception as e: rec["dex_error"] = type(e).__name__
    try:
        rep = await net.rugcheck_report(mint)
        rec["rc_null"] = [k for k in ("creator", "markets", "topHolders", "knownAccounts", "token") if g(rep, k) is None]
        rec["rc_markets"] = [{"type": g(m, "marketType"), "has_lp": g(m, "lp") is not None, "lpLockedPct": g(m, "lp", "lpLockedPct")}
                             for m in g(rep, "markets") or []]
        rec["rc_graphInsidersDetected"] = g(rep, "graphInsidersDetected")
    except Exception as e: rec["rc_error"] = type(e).__name__
    try:
        gr = await net.rugcheck_graph(mint)
        rec["graph_type"] = type(gr).__name__
        objs = gr if isinstance(gr, list) else [gr]
        rec["graph_len"] = len(gr) if isinstance(gr, (list, dict)) else None
        rec["graph_top_keys"] = keyset(objs)
        nodes = [n for o in objs if isinstance(o, dict) for n in (o.get("nodes") or [])]
        edges = [e for o in objs if isinstance(o, dict) for e in (o.get("edges") or o.get("links") or [])]
        rec["graph_node_keys"], rec["graph_edge_keys"] = keyset(nodes), keyset(edges)
        rec["graph_nodes"], rec["graph_edges"] = len(nodes), len(edges)
        if gr and (nodes or edges or not isinstance(gr, list)):
            (OUT / f"graph_{mint}.json").write_text(json.dumps(gr)[:200000])
    except Exception as e: rec["graph_error"] = type(e).__name__
    for label, prog, flt in (("token", TOKEN_PROGRAM, [{"dataSize": 165}]), ("token2022", TOKEN_2022, [])):
        try:
            r = await net.rpc("getProgramAccounts", [prog, {"encoding": "jsonParsed", "filters": flt + [{"memcmp": {"offset": 0, "bytes": mint}}]}])
            rec[f"gpa_{label}"] = len(r or [])
        except Exception as e: rec[f"gpa_{label}_error"] = str(e)[:80]
    try:
        acc = await net.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]); rec["mint_owner_program"] = g(acc, "value", "owner")
    except Exception as e: rec["acct_error"] = type(e).__name__
    probes.write(json.dumps(rec) + "\n"); probes.flush()
    return rec


async def main():
    tmp = tempfile.mkdtemp(prefix="replay_")
    c = db.connect(f"{tmp}/replay.db"); s = Settings(c); bus = Bus()
    s.set("scanner.min_pool_age_min", 0, who="replay"); s.set("scanner.poll_s", 10, who="replay"); s.set("desk.mode", "PAPER", who="replay")
    net = Net(s.get("desk.rpc_url")); fn = FakeNotifier()
    sc = Scanner(c, s, bus, net); au = Auditor(c, s, bus, net, notifier=fn); an = Analyst(c, s, bus, net)
    ex = Executor(c, s, bus, net, fn); rk = Risk(c, s, net, fn)
    up = api.FakeUploader(); pub = api.Publisher(c, s, {"scanner": sc, "auditor": au}, up, net=net)
    # shadow auditor: separate DB + bus, auditor pool age 0 so layers 7/8 run on real coins
    c2 = db.connect(f"{tmp}/shadow.db"); s2 = Settings(c2); s2.set("auditor.min_pool_age_min", 0, who="replay")
    sh = Auditor(c2, s2, Bus(), net, notifier=FakeNotifier())
    shadow = {}
    orig = sc._decide
    async def decide(mint, p, verdict, reason, m, now):
        out = await orig(mint, p, verdict, reason, m, now)
        async def side():
            await probe(net, mint)
            row = c.execute("SELECT creator, creator_balance_at_migration FROM coins WHERE mint=?", (mint,)).fetchone()
            db.upsert_coin(c2, mint, creator=row[0], creator_balance_at_migration=row[1])
            try:
                shadow[mint] = await sh.audit(out)
            except Exception as e:
                shadow[mint] = {"error": type(e).__name__}
            # drain shadow bus so it never blocks
            while not sh.bus.q["analyst"].empty(): sh.bus.q["analyst"].get_nowait()
        asyncio.create_task(side())
        return out
    sc._decide = decide

    async def publisher():
        while True:
            await pub.publish_once(); await asyncio.sleep(30)
    tasks = [asyncio.create_task(x) for x in (sc.run(), au.run(), an.run(), ex.run(), rk.run(bus), publisher())]
    await asyncio.sleep(REPLAY_S)
    for t in tasks: t.cancel()
    await asyncio.sleep(5)  # let side probes finish
    q = lambda sql: [dict(r) for r in c.execute(sql)]
    summary = {"tmp_db": tmp, "seconds": REPLAY_S,
               "coins_in_room_at_end": len(sc.room),
               "scans": q("SELECT mint,verdict,reason,liquidity_usd,top10_pct,h1_change_pct,age_min FROM scans"),
               "audits": q("SELECT mint,layer,name,value_text,pass,source FROM audits"),
               "verdicts": q("SELECT mint,score,pass,reason FROM verdicts"),
               "signals": q("SELECT mint,price,size_usd,mode,status FROM signals"),
               "positions": q("SELECT mint,entry_price,size_usd,closed_at,exit_reason FROM positions"),
               "activity": q("SELECT kind,agent,title,subtitle,value FROM activity"),
               "fake_pushes": fn.sent, "uploaded_files": sorted(up.files),
               "shadow": {m: {"passed": r.get("passed"), "reason": r.get("reason"), "layers": [(l["n"], l["pass"], l["value"], l["source"]) for l in r.get("layers", [])]} if isinstance(r, dict) and "layers" in r else r for m, r in shadow.items()}}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print("done", len(summary["scans"]), "scans")

asyncio.run(main())
