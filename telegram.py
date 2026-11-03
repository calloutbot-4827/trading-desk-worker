"""Telegram long-polling receiver. OWNER ONLY. Commands as /command or plain text. Never logs the token or chat id.
FOMO links only via links.py (notifier button helpers)."""
import asyncio, json, os, re, time
from pathlib import Path
import httpx
import db, links, notifier as nt
from config import ticket_check, resolve, SettingError, ALIASES
from agents.common import logger, short
from agents.executor import stack_of

log = logger("telegram")
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")
TICKET_KEYS = {"executor.stack_pct", "executor.min_ticket_usd", "executor.below_min", "desk.paper_start_stack", "desk.live_stack"}
FULL_NIGHT_H = 8
HELP = ("Commands:\nbuy X · sell X · why X\npause <Agent> · resume <Agent>\nset stop to -8% (any setting by name or alias)\n"
        "stack 1240 · status · paper · live\ninvite · report")


class _NoBus:  # Auditor run from "buy X": never forwards to Analyst
    async def publish(self, *a, **k):
        pass


def _fmt(key, v):
    if key == "risk.stop_pct":
        return f"-{v:g}%"
    if key.endswith("_pct") and isinstance(v, (int, float)):
        return f"{v:g}%"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class Telegram:
    def __init__(self, conn, settings, bus, agents, client=None, token=None, owner=None, sleep=asyncio.sleep,
                 clock=time.time, auditor_factory=None, secrets_path=nt.SECRETS):
        nt.load_env()
        self.c, self.s, self.bus, self.a = conn, settings, bus, agents or {}
        self.token = token if token is not None else os.environ.get("TG_TOKEN")
        self.owner = str(owner if owner is not None else os.environ.get("OWNER_CHAT_ID") or "") or None
        if not self.owner:  # env OWNER_CHAT_ID wins; else the owner learned via /start, stored on the volume db
            self.owner = self._stored_owner()
            if self.owner:
                os.environ["OWNER_CHAT_ID"] = self.owner
        self._c, self.sleep, self.clock, self.secrets_path = client, sleep, clock, Path(secrets_path)
        self.auditor_factory = auditor_factory
        self.offset = 0; self.ok = False; self.awaiting_live = False

    def connected(self):
        return bool(self.token and self.owner and self.ok)

    # ---------- transport ----------
    def _client(self):
        if self._c is None:
            self._c = httpx.AsyncClient(timeout=40)
        return self._c

    async def _api(self, method, payload):
        try:
            r = await self._client().post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as e:  # never echo the URL (has the token)
            log.info(f"{method} failed ({type(e).__name__})"); return None

    def _paper(self, text):
        return "PAPER · " + text if self.s.get("desk.mode") == "PAPER" and not text.startswith("PAPER · ") else text

    async def reply(self, text, buttons=None, chat=None):
        payload = {"chat_id": chat or self.owner, "text": self._paper(text), "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{k: b[k] for k in ("text", "url", "callback_data") if k in b}] for b in buttons]}
        return await self._api("sendMessage", payload)

    # ---------- owner ----------
    OWNER_KEY = "_owner_chat_id"  # not a user setting (Settings ignores keys outside DEFAULTS)

    def _stored_owner(self):
        try:
            r = self.c.execute("SELECT value FROM settings WHERE key=?", (self.OWNER_KEY,)).fetchone()
            return str(json.loads(r[0])) if r and r[0] else None
        except Exception:
            return None

    def _claim_owner(self, chat):
        self.owner = str(chat); os.environ["OWNER_CHAT_ID"] = self.owner
        try:  # always persist in the db settings table (survives on the /data volume)
            self.c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE "
                           "SET value=excluded.value, updated_at=excluded.updated_at",
                           (self.OWNER_KEY, json.dumps(self.owner), time.time()))
            self.c.commit()
        except Exception as e:
            log.info(f"could not store owner in db ({type(e).__name__})")
        if not self.secrets_path.exists():  # env-var deployment: no secrets file to write
            log.info("owner claimed via /start"); return
        try:
            lines = self.secrets_path.read_text().splitlines() if self.secrets_path.exists() else []
            lines = [l for l in lines if not l.startswith("OWNER_CHAT_ID=")] + [f"OWNER_CHAT_ID={self.owner}"]
            self.secrets_path.write_text("\n".join(lines) + "\n"); os.chmod(self.secrets_path, 0o600)
        except OSError as e:
            log.info(f"could not persist owner ({type(e).__name__})")
        log.info("owner claimed via /start")

    async def handle_update(self, u):
        if "callback_query" in u:
            q = u["callback_query"]; chat = str((q.get("message") or {}).get("chat", {}).get("id") or q.get("from", {}).get("id"))
            if not self.owner or chat != self.owner:
                return None
            out = await self.on_callback(q.get("data") or "")
            await self._api("answerCallbackQuery", {"callback_query_id": q.get("id"), "text": out})
            return out
        m = u.get("message") or {}
        chat = str(m.get("chat", {}).get("id", "")); text = (m.get("text") or "").strip()
        if not chat or not text:
            return None
        if not self.owner:
            if text.split()[0].lower() in ("/start", "start"):
                self._claim_owner(chat)
                return await self.reply("You own this desk now.\n" + HELP)
            return None
        if chat != self.owner:
            return None  # ignore everyone else, silently
        return await self.command(text)

    # ---------- coins ----------
    def find(self, x):
        x = x.strip()
        if MINT_RE.match(x):
            return [x]
        rows = self.c.execute("SELECT mint FROM coins WHERE lower(name)=lower(?) OR lower(symbol)=lower(?) OR "
                              "lower('$'||symbol)=lower(?)", (x, x, x)).fetchall()
        return [r[0] for r in rows]

    def name(self, mint):
        r = self.c.execute("SELECT name, symbol FROM coins WHERE mint=?", (mint,)).fetchone()
        return (r["name"] or r["symbol"]) if r and (r["name"] or r["symbol"]) else short(mint)

    async def _resolve(self, x):
        ms = self.find(x)
        if not ms:
            await self.reply(f"I don't know {x}. Send me its mint address.")
            return None
        if len(ms) > 1:
            await self.reply(f"{len(ms)} coins match {x}. Which one? Send the mint.",
                             [nt.coin_button(f"{self.name(m)} · {short(m)}", m, "solana") for m in ms])
            return None
        return ms[0]

    def night_start(self):
        r = self.c.execute("SELECT value FROM settings WHERE key='night_start'").fetchone()
        return float(json.loads(r[0])) if r else self.clock() - 16 * 3600

    # ---------- commands ----------
    async def command(self, text):
        t = text.lstrip("/").strip(); low = t.lower()
        if t == "LIVE" and self.awaiting_live:
            return await self.go_live()
        self.awaiting_live = False
        m = re.match(r"^(buy|sell|why)\s+(.+)$", t, re.I)
        if m:
            return await getattr(self, "cmd_" + m.group(1).lower())(m.group(2))
        m = re.match(r"^(pause|resume)\s+(\w+)$", t, re.I)
        if m:
            return await self.cmd_pause(m.group(2).lower(), m.group(1).lower() == "pause")
        m = re.match(r"^stack\s+\$?([\d,]+(?:\.\d+)?)$", t, re.I)
        if m:
            key = "desk.paper_start_stack" if self.s.get("desk.mode") == "PAPER" else "desk.live_stack"
            return await self.cmd_set(key, m.group(1).replace(",", ""), label="stack")
        m = re.match(r"^set\s+([\w.]+)\s+(?:to\s+|=\s*)?(\S+)$", t, re.I)
        if m:
            return await self.cmd_set(m.group(1).lower(), m.group(2))
        if low in ("status", "start", "help", "commands"):
            return await (self.cmd_status() if low == "status" else self.reply(HELP))
        if low == "paper":
            return await self.cmd_paper()
        if low in ("live", "live live") or t == "live LIVE":
            if t.lower() == "live live" and t.endswith(" LIVE"):
                self.awaiting_live = True; return await self.go_live()
            self.awaiting_live = True
            return await self.reply("Going LIVE means real money signals. Type LIVE to confirm.")
        if low == "invite":
            return await self.reply("Invite a friend to FOMO:", [nt.signup_button("Join FOMO")])
        if low == "report":
            r = self.c.execute("SELECT value FROM settings WHERE key='last_report_url'").fetchone()
            if not r:
                return await self.reply("No morning report yet.")
            url = json.loads(r[0]) if r[0].startswith('"') else r[0]
            return await self.reply(f"Last morning report: {url}", [{"text": "Open report", "url": url}])
        return await self.reply(HELP)

    async def cmd_buy(self, x):
        mint = await self._resolve(x)
        if not mint:
            return None
        url = links.coin_url(mint, "solana")
        sig = self.c.execute("SELECT * FROM signals WHERE mint=? AND ts>=? ORDER BY ts DESC LIMIT 1", (mint, self.night_start())).fetchone()
        S = self.s.get
        if sig:
            text = (f"Buy {self.name(mint)} · ${sig['size_usd']:.2f} · {sig['stack_pct']:g}% of stack\n"
                    f"stop -{S('risk.stop_pct'):g}% · trail {S('risk.trail_pct'):g}%")
            return await self.reply(text, [nt.coin_button("Buy on FOMO", mint, "solana"),
                                           nt.callback_button("Bought", f"bought:{mint}"), nt.callback_button("Skip", f"skip:{mint}")])
        aud = self.auditor_factory() if self.auditor_factory else self._default_auditor()
        try:
            out = await aud.audit({"mint": mint, "name": self.name(mint)})
        except Exception as e:
            log.info(f"manual audit error {type(e).__name__}"); out = None
        lines = [f"{self.name(mint)} · Auditor now:"]
        for l in (out or {}).get("layers", []):
            mark = "✅" if l["pass"] else ("❌" if l["pass"] is False else "▫️")
            lines.append(f"{mark} {l['n']} {l['name']}: {l['value']}")
        if not out:
            lines.append("audit unavailable")
        lines.append("not sized, not on the desk")
        return await self.reply("\n".join(lines), [nt.coin_button("Buy on FOMO", mint, "solana")])

    def _default_auditor(self):
        from agents.auditor import Auditor
        from agents.common import Net
        return Auditor(self.c, self.s, _NoBus(), Net(self.s.get("desk.rpc_url")))

    async def cmd_sell(self, x):
        mint = await self._resolve(x)
        if not mint:
            return None
        pos = [p for p in db.open_positions(self.c) if p["mint"] == mint]
        if not pos:
            return await self.reply(f"No open position in {self.name(mint)}.", [nt.coin_button("Open on FOMO", mint, "solana")])
        return await self.reply(f"Sell {self.name(mint)} · ${pos[0]['size_usd']:.2f} in",
                                [nt.coin_button("Sell on FOMO", mint, "solana"), nt.callback_button("Sold", f"sold:{mint}")])

    async def cmd_why(self, x):
        mint = await self._resolve(x)
        if not mint:
            return None
        c = self.c; lines = [self.name(mint)]
        s = c.execute("SELECT * FROM scans WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        if s:
            lines.append(f"Scanner: {s['verdict']}" + (f" · {s['reason']}" if s["reason"] else ""))
        red = c.execute("SELECT name, value_text FROM audits WHERE mint=? AND pass=0 ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        na = c.execute("SELECT COUNT(*) FROM audits WHERE mint=?", (mint,)).fetchone()[0]
        if na:
            lines.append(f"Auditor: rejected · {red['name']}: {red['value_text']}" if red else "Auditor: 8/8")
        v = c.execute("SELECT * FROM verdicts WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        if v:
            lines.append(f"Analyst: score {v['score']}" + (f" · {v['reason']}" if v["reason"] else ""))
        g = c.execute("SELECT * FROM signals WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        if g:
            lines.append(f"Executor: ${g['size_usd']:.2f} · {g['stack_pct']:g}% · {g['status']}")
        p = c.execute("SELECT * FROM positions WHERE mint=? ORDER BY opened_at DESC LIMIT 1", (mint,)).fetchone()
        if p:
            lines.append(f"Risk: {'open' if p['closed_at'] is None else p['exit_reason']}" +
                         (f" · ${p['pnl_usd']:+.2f}" if p["pnl_usd"] is not None else ""))
        if len(lines) == 1:
            lines.append("The desk hasn't seen this coin.")
        return await self.reply("\n".join(lines), [nt.coin_button("Open on FOMO", mint, "solana")])

    async def cmd_pause(self, who, pause):
        if who not in AGENTS or who not in self.a:
            return await self.reply(f"No agent called {who}. Agents: {', '.join(a.capitalize() for a in AGENTS)}")
        ag = self.a[who]
        if pause:
            if who == "risk" and db.open_positions(self.c):
                return await self.reply("Risk can't pause while a position is open.")
            ag.paused = True
            return await self.reply(f"{who.capitalize()} paused.")
        ag.paused = False
        return await self.reply(f"{who.capitalize()} running.")

    async def cmd_set(self, name, raw, label=None):
        try:
            key = resolve(name)
        except SettingError:
            return await self.reply(f"No setting called {name}. Aliases: {', '.join(ALIASES)}")
        val = raw.strip().rstrip("%").replace("$", "")
        if key in ("risk.stop_pct",) and val.startswith("-"):
            val = val[1:]
        if key == "desk.mode":
            return await self.reply("Use paper or live to switch mode.")
        try:
            old, new = self.s.set(key, val, who="telegram")
        except SettingError as e:
            msg = str(e)
            if key == "risk.stop_pct" and "between" in msg:
                from config import RANGES
                lo, hi = RANGES[key]; msg = f"stop must be between -{lo:g}% and -{hi:g}%"
            return await self.reply(f"Refused: {msg}")
        if key == "desk.rpc_url" and "auditor" in self.a:
            self.a["auditor"].rpc_url_changed()
        text = f"{label or name} {_fmt(key, old)} -> {_fmt(key, new)}"
        if key in TICKET_KEYS:
            S = self.s.get
            _, warn = ticket_check(stack_of(self.c, S), S("executor.stack_pct"), S("executor.min_ticket_usd"), S("executor.below_min"))
            if warn:
                text += f"\n⚠️ {warn}"
        return await self.reply(text)

    async def cmd_status(self):
        S = self.s.get; mode = S("desk.mode")
        bal = db.latest_balance(self.c, S("desk.paper_start_stack")) if mode == "PAPER" else S("desk.live_stack")
        pos = db.open_positions(self.c)
        lines = [f"{mode} · balance ${bal:,.2f}", f"{len(pos)} open position{'s' if len(pos) != 1 else ''}"]
        paused = [a for a in AGENTS if getattr(self.a.get(a), "paused", False)]
        if paused:
            lines.append("paused: " + ", ".join(paused))
        buttons = []
        for p in pos:
            lines.append(f"· {self.name(p['mint'])} ${p['size_usd']:.2f} @ {p['entry_price']:g} · stop -{p['stop_pct']:g}%")
            buttons.append(nt.coin_button(f"Sell {self.name(p['mint'])} on FOMO", p["mint"], "solana"))
        return await self.reply("\n".join(lines), buttons or None)

    async def cmd_paper(self):
        old, _ = self.s.set("desk.mode", "PAPER", who="telegram")
        return await self.reply(f"mode {old} -> PAPER")

    def full_paper_night(self):
        rows = self.c.execute("SELECT ts FROM balance ORDER BY ts").fetchall()
        live = self.c.execute("SELECT MIN(ts) FROM change_log WHERE key='desk.mode' AND new='\"LIVE\"'").fetchone()[0]
        ts = [r[0] for r in rows if live is None or r[0] < live]
        return len(ts) > 1 and ts[-1] - ts[0] >= FULL_NIGHT_H * 3600

    async def go_live(self):
        self.awaiting_live = False
        if not self.full_paper_night():
            return await self.reply(f"Refused: LIVE needs at least 1 full PAPER night ({FULL_NIGHT_H}h) in the db first.")
        old, _ = self.s.set("desk.mode", "LIVE", who="telegram")
        for a in self.a.values():
            if hasattr(a, "n") and hasattr(a.n, "mode"):
                a.n.mode = "LIVE"
        return await self.reply(f"mode {old} -> LIVE")

    # ---------- callbacks ----------
    async def on_callback(self, data):
        action, _, mint = data.partition(":")
        if action in ("bought", "skip") and "executor" in self.a:
            r = self.a["executor"].on_callback(action, mint)
            return {"opened": "Bought, Risk is guarding", "skipped": "Skipped for 24h", "missed": "Too late, missed"}.get(r, r)
        if action == "sold":
            pos = [p for p in db.open_positions(self.c) if p["mint"] == mint]
            if not pos:
                return "Already closed"
            last = self.c.execute("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
            await self.a["risk"].exit(pos[0], last[0] if last else pos[0]["entry_price"], "manual", self.clock())
            return "Sold, position closed"
        return "ignored"

    # ---------- loop ----------
    async def poll_once(self):
        r = await self._api("getUpdates", {"offset": self.offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]})
        self.ok = bool(r and r.get("ok"))
        for u in (r or {}).get("result", []):
            self.offset = max(self.offset, u["update_id"] + 1)
            try:
                await self.handle_update(u)
            except Exception as e:
                log.info(f"update error {type(e).__name__}")
        return self.ok

    async def run(self):
        if not self.token:
            log.info("telegram disabled: no TG_TOKEN"); return
        delay = 1
        while True:
            ok = await self.poll_once()
            if ok:
                delay = 1
            else:
                await self.sleep(delay); delay = min(delay * 2, 60)
