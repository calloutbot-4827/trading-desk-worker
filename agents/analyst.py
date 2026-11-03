"""ANALYST: RULES mode active. Jev mode is a stub (no key): it never falls back silently to rules."""
import asyncio, os, time
import db
from agents.common import Net, RateLimited, logger, g, fnum, clamp, pick_pumpswap

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
QUESTIONS = {  # closed "noul" questions; answers are numbers 0..1. No thresholds, balance or "buy" in the prompt.
    "alive_6h": "How likely is this coin to still be trading actively 6 hours from now? Answer 0 to 1.",
    "organic": "How organic is the buying (many independent wallets, not a few bundled ones)? Answer 0 to 1.",
    "flush": "Has a recent dip already flushed out weak sellers while buyers kept coming? Answer 0, 0.5 or 1.",
    "chase": "How much would entering now be chasing a move that already happened? Answer 0 to 1.",
}


def rules_answers(pair, bundled, S):
    vh1 = fnum(g(pair, "volume", "h1")) or 0
    bh1 = fnum(g(pair, "txns", "h1", "buys")) or 0
    m5 = fnum(g(pair, "priceChange", "m5"))
    bm5, sm5 = fnum(g(pair, "txns", "m5", "buys")) or 0, fnum(g(pair, "txns", "m5", "sells")) or 0
    h1 = fnum(g(pair, "priceChange", "h1")) or 0
    alive = clamp(vh1 / S("analyst.alive_volume_h1"))
    organic = clamp(bh1 / S("analyst.organic_buys_h1"))
    if bundled is not None and bundled >= S("analyst.bundled_penalty_from_wallets"):
        organic *= S("analyst.bundled_penalty")
    if m5 is not None and m5 <= -S("analyst.flush_drop_m5_pct") and bm5 >= sm5:
        flush = 1.0
    elif m5 is not None and m5 < 0:
        flush = 0.5
    else:
        flush = 0.0
    chase = clamp(h1 / S("analyst.chase_h1_pct"))
    return {"alive_6h": round(alive, 4), "organic": round(organic, 4), "flush": flush, "chase": round(chase, 4)}


def score_of(a):
    return round(100 * (a["alive_6h"] + a["organic"] + a["flush"] + (1 - a["chase"])) / 4)


def gate(a, score, S):
    """Hard gate in code. Returns list of failed rules '<rule> <value>' (empty = pass)."""
    f = []
    if a["alive_6h"] < S("analyst.min_alive_6h"): f.append(f"alive_6h {a['alive_6h']:.2f}")
    if a["organic"] < S("analyst.min_organic"): f.append(f"organic {a['organic']:.2f}")
    if a["chase"] > S("analyst.max_chase"): f.append(f"chase {a['chase']:.2f}")
    if score < S("analyst.min_score"): f.append(f"score {score}")
    return f


def jev_state_text(scan, pair, audit):
    """Facts only. Never thresholds, balance or the word buy."""
    return (f"Coin {scan.get('name')} ({scan.get('symbol')}) graduated to PumpSwap {scan.get('ageMin')} min ago. "
            f"Liquidity ${fnum(g(pair,'liquidity','usd'))}. Volume 1h ${fnum(g(pair,'volume','h1'))}, 5m ${fnum(g(pair,'volume','m5'))}. "
            f"Txns 1h {g(pair,'txns','h1')}, 5m {g(pair,'txns','m5')}. Price change 5m {g(pair,'priceChange','m5')}%, "
            f"1h {g(pair,'priceChange','h1')}%. Holders {audit.get('holders')}. Top 10 hold {scan.get('top10Pct')}%.")


async def jev_answers(client, key, state):
    """Stub: inactive without a key. Raises on any error; caller rejects 'no answer' (no silent fallback)."""
    if not key:
        raise RuntimeError("no Jev key")
    r = await client.post(JEV_URL, headers={"Authorization": f"Bearer {key}"}, json={
        "model": JEV_MODEL, "state": state, "questions": [{"id": k, "type": "noul", "text": v} for k, v in QUESTIONS.items()]})
    r.raise_for_status()
    ans = r.json().get("answers")  # response shape not verified (no key)
    return {k: float(ans[k]) for k in QUESTIONS}


class Analyst:
    name = "analyst"

    def __init__(self, conn, settings, bus, net: Net, clock=time.time, sleep=asyncio.sleep, jev_client=None):
        self.c, self.s, self.bus, self.net, self.clock, self.sleep = conn, settings, bus, net, clock, sleep
        self.jev_client = jev_client
        self.log = logger("analyst"); self.scored = set()

    async def score(self, audit):
        mint = audit["mint"]; S = self.s.get
        if mint in self.scored:
            return None
        scan = audit.get("scan") or {}
        name = scan.get("name") or mint
        mode = S("analyst.mode")
        try:
            pair = pick_pumpswap(await self.net.dex_tokens([mint]), mint)
        except RateLimited:
            await self.sleep(30); pair = pick_pumpswap(await self.net.dex_tokens([mint]), mint)
        self.scored.add(mint)
        now = self.clock()
        if pair and fnum(pair.get("priceUsd")) is not None:
            db.insert(self.c, "prices", mint=mint, ts=now, price=fnum(pair["priceUsd"]), volume_m5=fnum(g(pair, "volume", "m5")))
        bundled = next((l.get("bundled") for l in audit.get("layers", []) if l.get("n") == 7), None)
        model = "rules" if mode == "rules" else JEV_MODEL
        answers, failed, score = None, [], 0
        if pair is None:
            failed = ["no price"]
        elif mode == "rules":
            answers = rules_answers(pair, bundled, S)
        else:
            try:
                answers = await jev_answers(self.jev_client, os.environ.get("JEV_API_KEY"), jev_state_text(scan, pair, audit))
            except Exception as e:
                self.log.info(f"{mint} jev error {type(e).__name__}")
                failed = ["no answer"]
        if answers:
            score = score_of(answers); failed = gate(answers, score, S)
        passed = not failed
        out = {"agent": "analyst", "mint": mint, "answers": answers, "score": score, "passed": passed, "failed": failed,
               "model": model, "inputTokens": 0}
        a = answers or {}
        db.insert(self.c, "verdicts", mint=mint, ts=now, alive_6h=a.get("alive_6h"), organic=a.get("organic"),
                  flush=a.get("flush"), chase=a.get("chase"), score=score, **{"pass": int(passed)},
                  reason=failed[0] if failed else "", input_tokens=0)
        label = "Analyst (rules)" if model == "rules" else "Analyst (Jev)"
        self.log.info(f"{mint} score={score} passed={passed} failed={failed} model={model}")
        if passed:
            await self.bus.publish("analyst", {**out, "scan": scan, "audit": {k: audit.get(k) for k in ("passed", "layersChecked", "warnings", "score")}})
        else:
            db.activity(self.c, "rejected", mint, "analyst", f"Rejected {name}", f"{label} · {failed[0]}", "Skipped")
        return out

    async def run(self):
        while True:
            db.heartbeat(self.c, "analyst")
            if getattr(self, "paused", False):
                await self.sleep(5); continue
            try:
                item = await asyncio.wait_for(self.bus.consume("analyst"), 30)
            except asyncio.TimeoutError:
                continue
            try:
                await self.score(item)
            except Exception as e:
                self.log.info(f"{item.get('mint')} analyst error {type(e).__name__}")
