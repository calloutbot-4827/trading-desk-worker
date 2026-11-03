"""RISK: poll open positions, stop / trail / timeout / pulled exits."""
import asyncio, time
import db, fees, notifier as nt
from agents.common import Net, RateLimited, logger, g, fnum, pick_pumpswap


def pill(pos):
    return f"stop -{pos['stop_pct']:g}%"


class Risk:
    name = "risk"

    def __init__(self, conn, settings, net: Net, notifier, clock=time.time, sleep=asyncio.sleep):
        self.c, self.s, self.net, self.n, self.clock, self.sleep = conn, settings, net, notifier, clock, sleep
        self.log = logger("risk"); self.no_price = {}; self.paused = False

    def pause(self):
        if db.open_positions(self.c):
            raise RuntimeError("Risk can't be paused while a position is open")
        self.paused = True

    def _name(self, mint):
        r = self.c.execute("SELECT name FROM coins WHERE mint=?", (mint,)).fetchone()
        return (r[0] if r and r[0] else mint)

    def _tighten(self, pos):
        """A new stop setting applies to an open position only if tighter. Never moves a stop down."""
        new = self.s.get("risk.stop_pct")
        if pos["stop_pct"] is not None and new < pos["stop_pct"]:
            sp = pos["entry_price"] * (1 - new / 100)
            if sp > pos["stop_price"]:
                self.c.execute("UPDATE positions SET stop_pct=?, stop_price=? WHERE id=?", (new, sp, pos["id"])); self.c.commit()

    async def poll(self):
        S = self.s.get
        positions = db.open_positions(self.c)
        if not positions:
            return []
        for p in positions:
            self._tighten(p)
        positions = db.open_positions(self.c)
        mints = sorted({p["mint"] for p in positions})
        try:
            pairs = await self.net.dex_tokens(mints)  # one call for all open mints (<=30)
        except RateLimited:
            self.log.info(f"DexScreener 429: backing off {S('risk.backoff_s')}s")
            await self.sleep(S("risk.backoff_s")); return []
        now = self.clock(); outs = []
        for p in positions:
            pair = pick_pumpswap(pairs, p["mint"])
            price, liq = fnum(g(pair, "priceUsd")), fnum(g(pair, "liquidity", "usd"))
            if price is not None:
                db.insert(self.c, "prices", mint=p["mint"], ts=now, price=price, volume_m5=fnum(g(pair, "volume", "m5")))
            o = await self.check(p, price, liq, now)
            if o:
                outs.append(o)
        return outs

    async def check(self, p, price, liq, now):
        S = self.s.get; mint = p["mint"]; name = self._name(mint)
        if liq is not None and liq == 0:
            last = self.c.execute("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
            return await self.exit(p, last[0] if last else p["entry_price"], "pulled", now)
        if price is None:
            k = self.no_price.get(p["id"], 0) + 1; self.no_price[p["id"]] = k
            self.log.info(f"pos={p['id']} mint={mint} price=None miss={k}")
            if k == 4:
                await self._push(f"{name}: no price for 1 min", [nt.coin_button("Sell on FOMO", mint, "solana")])
            return None
        self.no_price[p["id"]] = 0
        entry = p["entry_price"]; peak = max(p["peak_price"] or entry, price)
        trail = bool(p["trail_active"]) or price >= entry * (1 + S("risk.trail_start_pct") / 100)
        self.c.execute("UPDATE positions SET peak_price=?, trail_active=? WHERE id=?", (peak, int(trail), p["id"])); self.c.commit()
        self.log.info(f"pos={p['id']} mint={mint} price={price} entry={entry} peak={peak} stop={p['stop_price']} "
                      f"(-{p['stop_pct']}%) trail={trail}")
        reason = None
        if price <= entry * (1 - p["stop_pct"] / 100):
            reason = "stop"
        elif trail and price <= peak * (1 - S("risk.trail_pct") / 100):
            reason = "trail"
        elif now - p["opened_at"] >= S("risk.timeout_h") * 3600 and price < entry * (1 + S("risk.timeout_min_gain_pct") / 100):
            reason = "timeout"
        if reason:
            return await self.exit(p, price, reason, now)
        return None

    async def exit(self, p, price, reason, now):
        S = self.s.get; mint = p["mint"]; name = self._name(mint)
        size = p["size_usd"]; value = size * price / p["entry_price"]
        fee = fees.round_trip_fees(size, value, S("desk.fees_ref_discount"))
        pnl = round(value - size - fee, 2); pct = round(100 * (price / p["entry_price"] - 1), 1)
        sp = p["stop_pct"]
        mode = p["mode"] or S("desk.mode")
        if mode == "PAPER":
            self.c.execute("UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, pnl_usd=?, fees_usd=? WHERE id=?",
                           (now, price, reason, pnl, fee, p["id"])); self.c.commit()
            db.insert(self.c, "balance", ts=now, value=round(db.latest_balance(self.c, S("desk.paper_start_stack")) + pnl, 2))
        money = f"+${pnl:.2f}" if pnl >= 0 else f"-${-pnl:.2f}"
        mark = None
        if reason == "stop":
            mark = f"-{sp:g}%"
            db.activity(self.c, "stopped", mint, "risk", f"Stopped {name}", f"Risk · -{sp:g}% stop", money)
            text = f"PAPER · {name} hit the -{sp:g}% stop · -${abs(pnl):.2f}"
        elif reason == "trail":
            db.activity(self.c, "sold", mint, "risk", f"Sold {name}", "Risk · trailing stop", money)
            text = f"PAPER · Sold {name} · {money} ({pct:+g}%)"
        elif reason == "manual":
            db.activity(self.c, "sold", mint, "risk", f"Sold {name}", "Risk · sold by owner", money)
            text = f"PAPER · Sold {name} · {money} ({pct:+g}%)"
        elif reason == "timeout":
            db.activity(self.c, "sold", mint, "risk", f"Sold {name}", "Risk · timeout", money)
            text = f"PAPER · Sell {name}, dead money · {pct:g}% in {S('risk.timeout_h'):g}h"
        else:
            db.activity(self.c, "sold", mint, "risk", f"Sold {name}", "Risk · liquidity pulled", money)
            text = f"PAPER · Sell {name}, liquidity pulled · {money}"
        if mode != "PAPER":
            text = text.replace("PAPER · ", "", 1)
        await self._push(text, [nt.coin_button("Sell on FOMO", mint, "solana"), nt.callback_button("Sold", f"sold:{mint}")])
        out = {"agent": "risk", "mint": mint, "positionId": p["id"], "exitPrice": price, "reason": reason,
               "pnlUsd": pnl, "pnlPct": pct, "stopPct": sp, "mark": mark, "mode": mode, "text": text}
        self.log.info(f"exit {out}")
        return out

    async def _push(self, text, buttons):
        for i in range(3):
            try:
                if await self.n.send(text, buttons):
                    return True
            except Exception as e:
                self.log.info(f"push attempt {i+1} failed ({type(e).__name__})")
        self.log.info("push failed")
        return False

    async def run(self, bus=None):
        while True:
            db.heartbeat(self.c, "risk")
            if bus is not None:  # drain executor outputs (positions are already in db)
                while not bus.q["risk"].empty():
                    bus.q["risk"].get_nowait()
            if self.paused:
                await self.sleep(self.s.get("risk.poll_s")); continue
            try:
                await self.poll()
            except Exception as e:
                self.log.info(f"poll error {type(e).__name__}")
            await self.sleep(self.s.get("risk.poll_s"))
