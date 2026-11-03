"""SCANNER: PumpPortal migrations -> waiting room -> DexScreener kill/pass -> bus.auditor."""
import asyncio, json, time, unicodedata
import db
from fields import FIELDS
from agents.common import Net, RateLimited, logger, g, fnum, pick_pumpswap, pool_exclusions, top10_pct, creator_balance, kfmt

WS_URL = "wss://pumpportal.fun/api/data"
PP = FIELDS["pumpportal"]
WS_SILENCE_S = 600      # feed is quiet between graduations; keepalive pings detect dead sockets
WS_OPEN_TIMEOUT_S = 20  # connect + subscribe hard cap
WS_KEEPALIVE = dict(ping_interval=20, ping_timeout=20, open_timeout=15, close_timeout=5)
EVAL_TIMEOUT_S = 60     # one mint's evaluate (DexScreener/RugCheck/RPC) hard cap


def default_ws_connect():
    import websockets
    return websockets.connect(WS_URL, **WS_KEEPALIVE)


def norm_name(name):
    """lowercase, strip spaces, digits and emoji/symbols."""
    out = []
    for ch in (name or "").lower():
        if ch.isspace() or ch.isdigit() or unicodedata.category(ch) in ("So", "Sk", "Cs", "Mn", "Cf") or ord(ch) > 0xFFFF:
            continue
        out.append(ch)
    return "".join(out)


def copycat_of(name, runners):
    """runners: {display_name: norm}. Exact or one inside the other."""
    n = norm_name(name)
    if not n:
        return None
    for disp, r in runners.items():
        if r and (n == r or n in r or r in n):
            return disp
    return None


class Scanner:
    name = "scanner"

    def __init__(self, conn, settings, bus, net: Net, ws_connect=None, clock=time.time, sleep=asyncio.sleep):
        self.c, self.s, self.bus, self.net = conn, settings, bus, net
        self.ws_connect, self.clock, self.sleep = ws_connect, clock, sleep
        self.log = logger("scanner")
        self.room = {}           # mint -> {migrated_at, pool_label, signature, exclude, report}
        self.decided = set()     # mints decided (pass/kill/drop) this night: never twice
        self.runners = {}        # today's coins with h24 >= runner pct: name -> normalized
        self.runner_mints = set()

    # ---------- intake ----------
    async def on_migration(self, msg):
        mint = msg.get(PP["mint"])
        if not mint or mint in self.room or mint in self.decided:
            return
        creator = bal = None
        report = None
        try:
            report = await self.net.rugcheck_report(mint)
            creator = g(report, FIELDS["rugcheck_report"]["creator"])  # PumpPortal has no creator key
        except Exception as e:
            self.log.info(f"{mint} rugcheck report unavailable at migration ({type(e).__name__}); creator=null")
        if creator:
            try:
                bal = await creator_balance(self.net, creator, mint)
            except Exception as e:
                self.log.info(f"{mint} creator balance unreadable ({type(e).__name__}); null")
        now = self.clock()
        db.upsert_coin(self.c, mint, migrated_at=now, creator=creator, creator_balance_at_migration=bal)
        self.room[mint] = {"migrated_at": now, "signature": msg.get(PP["signature"]), "pool_label": msg.get(PP["pool"]),
                           "report": report, "exclude": pool_exclusions(report) if report else set()}
        self.log.info(f"{mint} waiting room (sig {msg.get(PP['signature'])}, creator {'known' if creator else 'null'})")

    # ---------- one poll ----------
    async def poll(self):
        if not self.room:
            return []
        mints = list(self.room)
        try:
            pairs = await self.net.dex_tokens(mints)
        except RateLimited:
            self.log.info(f"DexScreener 429: waiting {self.s.get('scanner.backoff_429_s')}s, room kept ({len(mints)})")
            await self.sleep(self.s.get("scanner.backoff_429_s"))
            return []
        out = []
        now = self.clock()
        for p in pairs:  # track today's runners from everything we see
            h24 = fnum(g(p, "priceChange", "h24"))
            if h24 is not None and h24 >= self.s.get("scanner.runner_h24_pct"):
                nm = g(p, "baseToken", "name")
                if nm:
                    self.runners[nm] = norm_name(nm); self.runner_mints.add(g(p, "baseToken", "address"))
        for mint in mints:
            db.heartbeat(self.c, "scanner")  # a long room must not look like a frozen scanner
            if mint not in self.room:
                continue
            try:
                r = await asyncio.wait_for(self.evaluate(mint, pairs, now), EVAL_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.log.info(f"{mint} evaluate timed out; retry next poll"); r = None
            if r:
                out.append(r)
        return out

    async def evaluate(self, mint, pairs, now):
        S = self.s.get
        w = self.room[mint]
        age_room = (now - w["migrated_at"]) / 60
        p = pick_pumpswap(pairs, mint)
        if p is None:
            if age_room >= S("scanner.max_wait_min"):
                return await self._decide(mint, None, "kill", "never qualified", {}, now)
            return None  # no pumpswap pair yet: waits
        price, vm5 = fnum(p.get("priceUsd")), fnum(g(p, "volume", "m5"))
        if price is not None:
            db.insert(self.c, "prices", mint=mint, ts=now, price=price, volume_m5=vm5)
        liq = fnum(g(p, "liquidity", "usd"))
        h1 = fnum(g(p, "priceChange", "h1"))
        created = fnum(p.get("pairCreatedAt"))
        age = (now - created / 1000) / 60 if created else None
        name = g(p, "baseToken", "name"); sym = g(p, "baseToken", "symbol")
        try:
            top10 = await top10_pct(self.net, mint, w["exclude"] | {p.get("pairAddress")}, w["report"])
        except Exception as e:
            self.log.info(f"{mint} top10 unreadable ({type(e).__name__})"); top10 = None
        m = {"name": name, "symbol": sym, "pool": p.get("pairAddress"), "liq": liq, "top10": top10, "h1": h1, "age": age}
        reason = None
        if liq is not None and liq < S("scanner.min_liquidity_usd"):
            reason = f"liq {kfmt(liq)}"
        elif top10 is not None and top10 > S("scanner.max_top10_pct"):
            reason = f"top 10 hold {top10:.0f}%"
        elif h1 is not None and h1 > S("scanner.max_h1_change_pct"):
            reason = f"+{h1:.0f}% in 1h"
        else:
            cc = copycat_of(name, {k: v for k, v in self.runners.items() if k != name})
            if cc:
                reason = f"copycat of {cc}"
        if reason:
            return await self._decide(mint, p, "kill", reason, m, now)
        ready = None not in (liq, top10, h1, age) and age >= S("scanner.min_pool_age_min")
        if ready:
            return await self._decide(mint, p, "pass", "", m, now)
        if age_room >= S("scanner.max_wait_min"):
            return await self._decide(mint, p, "kill", "never qualified", m, now)
        return None

    async def _decide(self, mint, p, verdict, reason, m, now):
        self.room.pop(mint, None); self.decided.add(mint)
        out = {"agent": "scanner", "mint": mint, "name": m.get("name"), "symbol": m.get("symbol"), "pool": m.get("pool"),
               "liquidityUsd": m.get("liq"), "top10Pct": m.get("top10"), "h1ChangePct": m.get("h1"),
               "ageMin": round(m["age"], 1) if m.get("age") is not None else None,
               "verdict": verdict, "reason": reason, "ts": now}
        if m.get("name") or m.get("pool"):
            db.upsert_coin(self.c, mint, name=m.get("name"), symbol=m.get("symbol"), pool=m.get("pool"))
        db.insert(self.c, "scans", mint=mint, ts=now, liquidity_usd=out["liquidityUsd"], top10_pct=out["top10Pct"],
                  h1_change_pct=out["h1ChangePct"], age_min=out["ageMin"], verdict=verdict, reason=reason)
        self.log.info(f"ts={now:.0f} mint={mint} verdict={verdict} reason={reason!r} liq={out['liquidityUsd']} "
                      f"top10={out['top10Pct']} h1={out['h1ChangePct']} age={out['ageMin']}")
        if verdict == "pass":
            await self.bus.publish("scanner", out)
        return out

    # ---------- loops ----------
    async def ws_loop(self):
        connect = self.ws_connect or default_ws_connect
        delay = 1
        while True:
            self.ws_beat = self.clock()
            cm = None
            try:
                cm = connect()
                ws = await asyncio.wait_for(cm.__aenter__(), WS_OPEN_TIMEOUT_S)
                try:
                    await asyncio.wait_for(ws.send(json.dumps({"method": "subscribeMigration"})), WS_OPEN_TIMEOUT_S)
                    self.log.info("ws open, subscribeMigration sent"); delay = 1
                    while True:
                        self.ws_beat = self.clock()
                        raw = await asyncio.wait_for(ws.recv(), WS_SILENCE_S)
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict) and msg.get(PP["mint"]) and msg.get(PP["txType"]) == "migrate":
                            try:
                                await asyncio.wait_for(self.on_migration(msg), EVAL_TIMEOUT_S)
                            except asyncio.TimeoutError:
                                self.log.info(f"{msg.get(PP['mint'])} migration intake timed out; skipped")
                finally:
                    try:  # bounded close: a hung close must never wedge the loop
                        await asyncio.wait_for(cm.__aexit__(None, None, None), 10)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                kind = f"silent/timeout {WS_SILENCE_S}s" if isinstance(e, asyncio.TimeoutError) else f"closed ({type(e).__name__})"
                self.reconnects = getattr(self, "reconnects", 0) + 1
                self.log.info(f"ws {kind}; reconnect in {delay}s")
                await self.sleep(delay); delay = min(delay * 2, 60)

    async def poll_loop(self):
        while True:
            db.heartbeat(self.c, "scanner")
            if getattr(self, "paused", False):
                await self.sleep(self.s.get("scanner.poll_s")); continue
            try:
                await self.poll()
            except Exception as e:
                self.log.info(f"poll error {type(e).__name__}")
            db.heartbeat(self.c, "scanner")
            await self.sleep(self.s.get("scanner.poll_s"))

    async def run(self):
        await asyncio.gather(self.ws_loop(), self.poll_loop())
