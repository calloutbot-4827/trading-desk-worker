"""EXECUTOR: size, re-check, push (Telegram via notifier), PAPER fill / LIVE confirm."""
import asyncio, time
import db, links, notifier as nt
from config import ticket_check
from agents.common import Net, logger, g, fnum, pick_pumpswap


def stack_of(c, S):
    if S("desk.mode") == "PAPER":
        return db.latest_balance(c, S("desk.paper_start_stack"))
    return S("desk.live_stack")


def open_money(c):
    return sum(p["size_usd"] or 0 for p in db.open_positions(c))


def size_for(stack, used, liq, S):
    """-> (size or None, skip reason)."""
    free = stack - used
    size = stack * S("executor.stack_pct") / 100
    if liq is not None:
        size = min(size, liq * S("executor.max_pool_pct") / 100)
    mn = S("executor.min_ticket_usd")
    if size < mn:
        if S("executor.below_min") == "skip":
            return None, f"under ${mn:.2f}"
        if free < mn:
            return None, "no free stack"
        size = mn
    if size > free:
        return None, "no free stack"
    return round(size, 2), ""


def start_of_night_check(c, S, log=None):
    """Ticket check from config. Returns warning text or None."""
    _, warn = ticket_check(stack_of(c, S), S("executor.stack_pct"), S("executor.min_ticket_usd"), S("executor.below_min"))
    if warn and log:
        log.warning(warn)
    return warn


class Executor:
    name = "executor"

    def __init__(self, conn, settings, bus, net: Net, notifier, clock=time.time, sleep=asyncio.sleep):
        self.c, self.s, self.bus, self.net, self.n, self.clock, self.sleep = conn, settings, bus, net, notifier, clock, sleep
        self.log = logger("executor"); self.pushed = set(); self.pending = {}  # LIVE: mint -> signal info

    def _out(self, mint, price, size, pct, mode, status, reason):
        return {"agent": "executor", "mint": mint, "signalPrice": price, "sizeUsd": size, "stackPct": pct,
                "link": links.coin_url(mint, "solana"), "mode": mode, "status": status, "reason": reason}

    def _skip(self, mint, mode, reason, price=None, size=None, pct=None):
        self.log.info(f"{mint} skip: {reason}")
        return self._out(mint, price, size, pct, mode, "skipped", reason)

    async def handle(self, v):
        mint = v["mint"]; S = self.s.get; mode = S("desk.mode")
        scan = v.get("scan") or {}; name = scan.get("name") or mint
        if mint in self.pushed or any(p["mint"] == mint for p in db.open_positions(self.c)):
            return self._skip(mint, mode, "already pushed tonight")
        self.pushed.add(mint)
        stack = stack_of(self.c, S)
        pair = pick_pumpswap(await self.net.dex_tokens([mint]), mint)
        price = fnum(g(pair, "priceUsd")); liq = fnum(g(pair, "liquidity", "usd"))
        if price is None:
            return self._skip(mint, mode, "no price")
        size, why = size_for(stack, open_money(self.c), liq, S)
        if size is None:
            return self._skip(mint, mode, why, price)
        pct = round(100 * size / stack, 1)
        # re-check right before the push
        liq0 = fnum(scan.get("liquidityUsd"))
        if liq0 and liq is not None and 100 * (liq0 - liq) / liq0 > S("executor.recheck_liq_drop_pct"):
            return self._skip(mint, mode, "liquidity dropping", price, size, pct)
        r = self.c.execute("SELECT price FROM prices WHERE mint=? AND ts<=(SELECT MAX(ts) FROM verdicts WHERE mint=?) "
                           "ORDER BY ts DESC LIMIT 1", (mint, mint)).fetchone()
        p_an = r[0] if r else None
        if p_an and 100 * (price - p_an) / p_an > S("executor.recheck_price_up_pct"):
            return self._skip(mint, mode, "ran before the push", price, size, pct)
        db.insert(self.c, "prices", mint=mint, ts=self.clock(), price=price, volume_m5=fnum(g(pair, "volume", "m5")))
        link = links.coin_url(mint, "solana")
        text = (f"{'PAPER · ' if mode == 'PAPER' else ''}Buy {name} · ${size:.2f} · {pct:g}% of stack\n"
                f"Auditor 8/8{''.join(' · ' + w for w in (v.get('audit') or {}).get('warnings') or [])} · Analyst {v.get('score')} · stop -{S('risk.stop_pct'):g}% · trail {S('risk.trail_pct'):g}%")
        buttons = [nt.coin_button("Buy on FOMO", mint, "solana"), nt.callback_button("Bought", f"bought:{mint}"),
                   nt.callback_button("Skip", f"skip:{mint}")]
        ok = await self._push(text, buttons)
        sid = db.insert(self.c, "signals", mint=mint, ts=self.clock(), price=price, size_usd=size, stack_pct=pct,
                        link=link, mode=mode, status="sent" if ok else "push failed")
        if mode == "PAPER":
            if not ok:
                self.log.info(f"{mint} push failed; PAPER fills anyway")
            self.open_position(mint, name, price, size, pct, mode)
            return self._out(mint, price, size, pct, mode, "filled", "" if ok else "push failed")
        if not ok:
            return self._out(mint, price, size, pct, mode, "push failed", "push failed")
        self.pending[mint] = {"ts": self.clock(), "price": price, "size": size, "pct": pct, "name": name, "sid": sid}
        return self._out(mint, price, size, pct, mode, "sent", "")

    async def _push(self, text, buttons):
        for i in range(3):
            try:
                if await self.n.send(text, buttons):
                    return True
            except Exception as e:
                self.log.info(f"push attempt {i+1} failed ({type(e).__name__})")
        return False

    def open_position(self, mint, name, price, size, pct, mode):
        sp = self.s.get("risk.stop_pct")
        pid = db.insert(self.c, "positions", mint=mint, opened_at=self.clock(), entry_price=price, size_usd=size,
                        peak_price=price, stop_price=price * (1 - sp / 100), stop_pct=sp, trail_active=0, mode=mode)
        db.activity(self.c, "bought", mint, "executor", f"Bought {name}",
                    f"Executor · {time.strftime('%H:%M', time.localtime(self.clock()))}", f"${size:.2f} · {pct:g}% of stack")
        self.log.info(f"{mint} opened position {pid} at {price} size ${size:.2f} stop -{sp}%")
        return pid

    # callbacks from Telegram buttons (receiver wired later by the API writer)
    def on_callback(self, action, mint):
        if action == "skip":
            db.skip(self.c, mint, 24 * 3600)
            self.pending.pop(mint, None); return "skipped"
        if action == "bought" and mint in self.pending:
            p = self.pending.pop(mint)
            if self.clock() - p["ts"] > self.s.get("executor.confirm_window_min") * 60:
                self.c.execute("UPDATE signals SET status='missed' WHERE id=?", (p["sid"],)); self.c.commit()
                return "missed"
            if not any(x["mint"] == mint for x in db.open_positions(self.c)):
                self.open_position(mint, p["name"], p["price"], p["size"], p["pct"], "LIVE")
            return "opened"
        return "ignored"

    def expire_pending(self):
        for mint, p in list(self.pending.items()):
            if self.clock() - p["ts"] > self.s.get("executor.confirm_window_min") * 60:
                self.pending.pop(mint)
                self.c.execute("UPDATE signals SET status='missed' WHERE id=?", (p["sid"],)); self.c.commit()

    async def run(self):
        start_of_night_check(self.c, self.s.get, self.log)
        while True:
            db.heartbeat(self.c, "executor"); self.expire_pending()
            if getattr(self, "paused", False):
                await self.sleep(5); continue
            try:
                v = await asyncio.wait_for(self.bus.consume("executor"), 30)
            except asyncio.TimeoutError:
                continue
            try:
                out = await self.handle(v)
                if out and out["status"] in ("filled", "sent"):
                    await self.bus.publish("executor", out)
            except Exception as e:
                self.log.info(f"{v.get('mint')} executor error {type(e).__name__}")
