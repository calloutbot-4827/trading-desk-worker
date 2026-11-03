"""PLAN B publisher: builds the spec API JSON and uploads it to a public Supabase Storage bucket under a secret path.
No listening port. Secrets (SUPABASE_SECRET_KEY, TG_TOKEN, DATA_PATH, OWNER_CHAT_ID) are never logged or written into
any payload; every payload is scanned for them before upload. FOMO links only via links.py."""
import asyncio, json, os, time
import httpx
import db, links, notifier as nt
from agents.common import Net, RateLimited, logger, g, fnum, pick_pumpswap

SUPABASE_URL = "https://inuingicceojxdjfsnaa.supabase.co"
BUCKET = "desk-data"
SECRET_KEYS = ("SUPABASE_SECRET_KEY", "TG_TOKEN", "DATA_PATH", "OWNER_CHAT_ID")
ORDER = ("scanner", "auditor", "analyst", "executor", "risk")
RANGES = {"LIVE": 1800, "1H": 3600, "4H": 4 * 3600, "1D": 86400, "1W": 7 * 86400, "ALL": None}
PAGE = 30
STATE_EVERY_S, PRICES_EVERY_S, BALANCE_EVERY_S = 15, 60, 60
HEARTBEAT_STALE_S = 120
log = logger("api")


class SecretLeak(Exception):
    pass


def secret_values():
    return [v for v in (os.environ.get(k) for k in SECRET_KEYS) if v and len(v) >= 4]


def check_no_secrets(body: str, secrets=None):
    for s in (secret_values() if secrets is None else secrets):
        if s in body:
            raise SecretLeak("payload contains a secret value")  # never say which / never echo it


def money(x):
    return f"+${x:.2f}" if x >= 0 else f"-${-x:.2f}"


class SupabaseUploader:
    """POST {URL}/storage/v1/object/{bucket}/d/{DATA_PATH}/{file}, x-upsert. Retries; never raises."""

    def __init__(self, url=SUPABASE_URL, key=None, data_path=None, client=None, retries=3, sleep=asyncio.sleep):
        nt.load_env()
        self.url, self.key = url, key or os.environ.get("SUPABASE_SECRET_KEY")
        self.data_path = data_path or os.environ.get("DATA_PATH")
        if not self.key or not self.data_path:
            raise RuntimeError("uploader: SUPABASE_SECRET_KEY or DATA_PATH missing")
        self._c, self.retries, self.sleep = client, retries, sleep
        self.failures = 0

    def public_url(self, file):
        return f"{self.url}/storage/v1/object/public/{BUCKET}/d/{self.data_path}/{file}"

    async def put(self, file, body: str) -> bool:
        c = self._c or httpx.AsyncClient(timeout=15)
        self._c = c
        hdr = {"Authorization": f"Bearer {self.key}", "apikey": self.key, "x-upsert": "true",
               "Content-Type": "application/json", "cache-control": "max-age=0"}
        for i in range(self.retries):
            try:
                r = await c.post(f"{self.url}/storage/v1/object/{BUCKET}/d/{self.data_path}/{file}",
                                 content=body.encode(), headers=hdr)
                if r.status_code in (200, 201):
                    return True
                log.info(f"upload {file.split('/')[0]} HTTP {r.status_code} (try {i+1})")  # no path, no key
            except httpx.HTTPError as e:
                log.info(f"upload {file.split('/')[0]} failed {type(e).__name__} (try {i+1})")
            await self.sleep(min(2 ** i, 10))
        self.failures += 1
        return False


class FakeUploader:
    def __init__(self, ok=True):
        self.files, self.ok, self.calls = {}, ok, 0

    async def put(self, file, body):
        self.calls += 1
        if self.ok:
            self.files[file] = body
        return self.ok


class Publisher:
    def __init__(self, conn, settings, agents, uploader, net=None, clock=time.time, sleep=asyncio.sleep, telegram=None):
        self.c, self.s, self.a, self.up, self.net = conn, settings, agents or {}, uploader, net
        self.clock, self.sleep, self.tg = clock, sleep, telegram
        self.pairs = {}          # mint -> last DexScreener pair (stats)
        self.last_good = {}      # file -> last body uploaded OK
        self.pending = {}        # file -> body that failed; retried next cycle
        self.night_start, self.night_balance = self._night()

    # ---------- night / balance ----------
    def _night(self):
        r = self.c.execute("SELECT value FROM settings WHERE key='night_start'").fetchone()
        now = self.clock()
        if r and now - float(json.loads(r[0])) < 16 * 3600:
            ns = float(json.loads(r[0]))
        else:
            ns = now
            self.c.execute("INSERT INTO settings(key,value,updated_at) VALUES('night_start',?,?) ON CONFLICT(key) DO UPDATE "
                           "SET value=excluded.value, updated_at=excluded.updated_at", (json.dumps(ns), now)); self.c.commit()
        b = self.c.execute("SELECT value FROM balance WHERE ts<=? ORDER BY ts DESC LIMIT 1", (ns,)).fetchone()
        return ns, (b[0] if b else self.sim_balance())

    def sim_balance(self):
        S = self.s.get
        if S("desk.mode") == "PAPER":
            pnl = self.c.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM positions WHERE closed_at IS NOT NULL AND "
                                 "(mode='PAPER' OR mode IS NULL)").fetchone()[0]
            return round(S("desk.paper_start_stack") + pnl, 2)
        return float(S("desk.live_stack"))

    def write_balance(self):
        v = self.sim_balance()
        db.insert(self.c, "balance", ts=self.clock(), value=v)
        return v

    # ---------- helpers ----------
    def _name(self, mint):
        r = self.c.execute("SELECT name, symbol FROM coins WHERE mint=?", (mint,)).fetchone()
        return (r["name"] or r["symbol"] or mint) if r else mint

    def _q(self, sql, *a):
        return self.c.execute(sql, a).fetchall()

    def _count(self, sql, *a):
        return self.c.execute(sql, a).fetchone()[0]

    def tonight_mints(self):
        ns = self.night_start
        rows = self._q("SELECT mint FROM signals WHERE ts>=? UNION SELECT mint FROM audits WHERE ts>=? "
                       "UNION SELECT mint FROM positions WHERE opened_at>=? OR closed_at IS NULL", ns, ns, ns)
        return sorted({r[0] for r in rows if r[0]})

    def covered_mints(self):
        sc = self.a.get("scanner"); room = set(getattr(sc, "room", {}) or {})
        return room | {p["mint"] for p in db.open_positions(self.c)}

    def is_paused(self, name):
        return bool(getattr(self.a.get(name), "paused", False))

    # ---------- prices (one DexScreener batch) ----------
    async def prices_once(self):
        mints = self.tonight_mints()
        if not mints or self.net is None:
            return 0
        try:
            pairs = await self.net.dex_tokens(mints[:30])  # ONE batch (<=30 mints)
        except RateLimited:
            log.info("DexScreener 429 in api prices"); return 0
        except Exception as e:
            log.info(f"api prices error {type(e).__name__}"); return 0
        covered, now, n = self.covered_mints(), self.clock(), 0
        for m in mints[:30]:
            p = pick_pumpswap(pairs, m)
            if p is None:
                continue
            self.pairs[m] = p
            price = fnum(p.get("priceUsd"))
            if m not in covered and price is not None:
                db.insert(self.c, "prices", mint=m, ts=now, price=price, volume_m5=fnum(g(p, "volume", "m5"))); n += 1
        return n

    # ---------- agents ----------
    def _decisions(self, name, limit=20):
        ns = self.night_start; out = []
        if name == "scanner":
            for r in self._q("SELECT * FROM scans ORDER BY ts DESC LIMIT ?", limit):
                out.append({"ts": r["ts"], "mint": r["mint"], "name": self._name(r["mint"]), "verdict": r["verdict"],
                            "text": f"{'Passed' if r['verdict']=='pass' else 'Killed'} {self._name(r['mint'])}"
                                    + (f" · {r['reason']}" if r["reason"] else ""),
                            "liquidity_usd": r["liquidity_usd"], "top10_pct": r["top10_pct"]})
        elif name == "auditor":
            for r in self._q("SELECT mint, MAX(ts) ts, SUM(pass) passed, COUNT(*) n FROM audits GROUP BY mint "
                             "ORDER BY ts DESC LIMIT ?", limit):
                red = self.c.execute("SELECT value_text FROM audits WHERE mint=? AND pass=0 ORDER BY ts LIMIT 1", (r["mint"],)).fetchone()
                ok = red is None and r["passed"] == 8
                out.append({"ts": r["ts"], "mint": r["mint"], "name": self._name(r["mint"]), "verdict": "pass" if ok else "reject",
                            "text": f"{'8/8' if ok else 'Rejected'} {self._name(r['mint'])}" + ("" if ok else f" · {red[0] if red else ''}"),
                            "layers": self.layers(r["mint"])})
        elif name == "analyst":
            for r in self._q("SELECT * FROM verdicts ORDER BY ts DESC LIMIT ?", limit):
                out.append({"ts": r["ts"], "mint": r["mint"], "name": self._name(r["mint"]), "verdict": "pass" if r["pass"] else "reject",
                            "score": r["score"], "text": f"{self._name(r['mint'])} · score {r['score'] if r['score'] is not None else 'n/a'}"
                            + (f" · {r['reason']}" if r["reason"] else "")})
        elif name == "executor":
            for r in self._q("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", limit):
                out.append({"ts": r["ts"], "mint": r["mint"], "name": self._name(r["mint"]), "verdict": r["status"],
                            "size_usd": r["size_usd"], "stack_pct": r["stack_pct"],
                            "text": f"Buy {self._name(r['mint'])} · ${r['size_usd']:.2f} · {r['stack_pct']:g}% of stack"})
        elif name == "risk":
            for r in self._q("SELECT * FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?", limit):
                out.append({"ts": r["closed_at"], "mint": r["mint"], "name": self._name(r["mint"]), "verdict": r["exit_reason"],
                            "pnl_usd": r["pnl_usd"], "text": f"{r['exit_reason']} {self._name(r['mint'])} · {money(r['pnl_usd'] or 0)}"})
        for d in out:
            d["coin_url"] = links.coin_url(d["mint"], "solana")
        return out

    def agent_card(self, name):
        S, ns = self.s.get, self.night_start
        hb = self.c.execute("SELECT ts FROM heartbeats WHERE agent=?", (name,)).fetchone()
        stale = not hb or self.clock() - hb[0] > HEARTBEAT_STALE_S
        card = {"name": name, "title": name.capitalize(), "status": "paused" if self.is_paused(name) else "running",
                "stale": stale, "heartbeat_at": hb[0] if hb else None,
                "last": [d["text"] for d in self._decisions(name, 3)]}
        if name == "scanner":
            n = self._count("SELECT COUNT(DISTINCT mint) FROM scans WHERE ts>=?", ns)
            card.update(today=n, headline=f"{n} pools",
                        feed=[{k: d[k] for k in ("mint", "name", "verdict", "text", "coin_url", "ts")} for d in self._decisions(name, 8)])
        elif name == "auditor":
            n = self._count("SELECT COUNT(DISTINCT mint) FROM audits WHERE ts>=?", ns)
            rej = self._count("SELECT COUNT(DISTINCT mint) FROM audits WHERE ts>=? AND pass=0", ns)
            strips = [{"mint": d["mint"], "name": d["name"], "coin_url": d["coin_url"],
                       "strip": [l["pass"] for l in d["layers"]]} for d in self._decisions(name, 5)]
            card.update(today=n, headline=f"{n} taken apart", rejected=rej, rejected_text=f"{rej} rejected", strips=strips)
        elif name == "analyst":
            n = self._count("SELECT COUNT(*) FROM verdicts WHERE ts>=?", ns)
            r = self.c.execute("SELECT score FROM verdicts ORDER BY ts DESC LIMIT 1").fetchone()
            card.update(today=n, headline="Scoring", score=r[0] if r else None, min_score=S("analyst.min_score"))
        elif name == "executor":
            n = self._count("SELECT COUNT(*) FROM signals WHERE ts>=?", ns)
            r = self.c.execute("SELECT size_usd, stack_pct FROM signals ORDER BY ts DESC LIMIT 1").fetchone()
            card.update(today=n, headline=f"{n} trades", size_usd=r[0] if r else None,
                        stack_pct=r[1] if r else S("executor.stack_pct"))
        elif name == "risk":
            n = self._count("SELECT COUNT(*) FROM positions WHERE closed_at>=?", ns)
            last = self.c.execute("SELECT exit_reason FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1").fetchone()
            if db.open_positions(self.c) or not last:
                head = "Guarding"
            else:
                head = {"trail": "Trailed out", "stop": "Stopped"}.get(last[0], "Guarding")
            card.update(today=n, headline=head, open=len(db.open_positions(self.c)),
                        pills=[f"stop -{S('risk.stop_pct'):g}%", f"trail {S('risk.trail_pct'):g}%"],
                        stop_pct=S("risk.stop_pct"), trail_pct=S("risk.trail_pct"))
        return card

    # ---------- state ----------
    def public_settings(self):
        out = dict(self.s.values)  # DEFAULTS keys only
        import urllib.parse as up
        u = up.urlparse(str(out.get("desk.rpc_url", "")))  # rpc urls often carry an api key: publish host only
        out["desk.rpc_url"] = f"{u.scheme}://{u.hostname}" if u.hostname else ""
        return out

    def state(self):
        S = self.s.get
        bal = db.latest_balance(self.c, self.sim_balance())
        nb = self.night_balance or bal
        mode = S("desk.mode")
        return {"balance": bal, "today_pct": round(100 * (bal / nb - 1), 2) if nb else 0.0,
                "multiplier": round(bal / nb, 4) if nb else 1.0, "mode": mode, "paper": mode == "PAPER",
                "badge": "PAPER" if mode == "PAPER" else "LIVE", "updated_at": self.clock(), "night_start": self.night_start,
                "agents": [self.agent_card(n) for n in ORDER], "settings": self.public_settings(),
                "account": {"mode": mode, "stack": bal if mode == "PAPER" else S("desk.live_stack"),
                            "profile_url": links.profile_url(S("desk.fomo_handle")), "signup_url": links.SIGNUP_URL,
                            "telegram": {"connected": bool(self.tg and self.tg.connected())}}}

    # ---------- chart ----------
    def chart(self):
        now, out = self.clock(), {}
        pos = self._q("SELECT * FROM positions ORDER BY opened_at")
        for rng, span in RANGES.items():
            lo = now - span if span else 0
            pts = [[r[0], r[1]] for r in self._q("SELECT ts, value FROM balance WHERE ts>=? ORDER BY ts", lo)]
            if len(pts) > 400:
                step = len(pts) / 400; pts = [pts[int(i * step)] for i in range(400)] + [pts[-1]]
            marks = []
            for p in pos:
                nm = self._name(p["mint"]); url = links.coin_url(p["mint"], "solana")
                if p["opened_at"] >= lo:
                    marks.append({"ts": p["opened_at"], "kind": "bought", "color": "blue", "label": f"Bought {nm}", "mint": p["mint"], "coin_url": url})
                if p["closed_at"] and p["closed_at"] >= lo:
                    pnl = p["pnl_usd"] or 0
                    if p["exit_reason"] == "stop":
                        marks.append({"ts": p["closed_at"], "kind": "stopped", "color": "red",
                                      "label": f"-{p['stop_pct']:g}% · -${abs(pnl):.2f}", "mint": p["mint"], "coin_url": url})
                    else:
                        marks.append({"ts": p["closed_at"], "kind": "sold", "color": "green" if pnl >= 0 else "red",
                                      "label": money(pnl), "mint": p["mint"], "coin_url": url})
            marks.sort(key=lambda m: m["ts"])
            gap = (span or max(now - (pts[0][0] if pts else now), 3600)) / 30
            for i, m in enumerate(marks):  # later marks that would overlap move up a row
                m["row"] = marks[i - 1]["row"] + 1 if i and m["ts"] - marks[i - 1]["ts"] < gap else 0
            out[rng] = {"points": pts, "marks": marks}
        return {"updated_at": now, "ranges": out}

    # ---------- activity ----------
    def activity_pages(self):
        rows = []
        for r in self._q("SELECT * FROM activity ORDER BY ts DESC, id DESC"):
            rows.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "agent": r["agent"], "title": r["title"],
                         "subtitle": r["subtitle"], "value": r["value"], "mint": r["mint"],
                         "coin_url": links.coin_url(r["mint"], "solana") if r["mint"] else None})
        pages = max(1, -(-len(rows) // PAGE))
        return [{"page": i + 1, "pages": pages, "per_page": PAGE, "total": len(rows), "rows": rows[i * PAGE:(i + 1) * PAGE]}
                for i in range(pages)]

    # ---------- coin ----------
    def layers(self, mint):
        last = self.c.execute("SELECT MAX(ts) FROM audits WHERE mint=? AND layer='1'", (mint,)).fetchone()[0]
        got = {r["layer"]: r for r in self._q("SELECT * FROM audits WHERE mint=? AND ts>=? ORDER BY ts", mint, last or 0)}
        from agents.auditor import NAMES
        return [{"n": i, "name": NAMES[i - 1], "pass": (bool(got[str(i)]["pass"]) if str(i) in got else None),
                 "value": got[str(i)]["value_text"] if str(i) in got else "not checked",
                 "source": got[str(i)]["source"] if str(i) in got else None} for i in range(1, 9)]

    def candles(self, mint):
        rows = self._q("SELECT ts, price, volume_m5 FROM prices WHERE mint=? AND price IS NOT NULL ORDER BY ts", mint)
        out, prev_v = [], None
        for r in rows:
            t = int(r["ts"] // 60 * 60); p = r["price"]; v = r["volume_m5"]
            dv = max(0.0, v - prev_v) if v is not None and prev_v is not None else 0.0
            if v is not None:
                prev_v = v
            if out and out[-1]["t"] == t:
                c = out[-1]; c["h"] = max(c["h"], p); c["l"] = min(c["l"], p); c["c"] = p; c["v"] += dv
            else:
                out.append({"t": t, "o": p, "h": p, "l": p, "c": p, "v": dv})
        return out[-720:]

    def coin(self, mint):
        S = self.s.get
        cr = self.c.execute("SELECT * FROM coins WHERE mint=?", (mint,)).fetchone()
        last = self.c.execute("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        pair = self.pairs.get(mint) or {}
        pos = self.c.execute("SELECT * FROM positions WHERE mint=? ORDER BY opened_at DESC LIMIT 1", (mint,)).fetchone()
        position, exits = None, None
        if pos:
            open_ = pos["closed_at"] is None
            position = {"id": pos["id"], "open": open_, "entry_price": pos["entry_price"], "size_usd": pos["size_usd"],
                        "opened_at": pos["opened_at"], "peak_price": pos["peak_price"], "closed_at": pos["closed_at"],
                        "exit_price": pos["exit_price"], "exit_reason": pos["exit_reason"], "pnl_usd": pos["pnl_usd"],
                        "mode": pos["mode"]}
            if open_:
                trail = pos["peak_price"] * (1 - S("risk.trail_pct") / 100) if pos["trail_active"] else None
                exits = {"stop_price": pos["stop_price"], "stop_pct": pos["stop_pct"], "trail_active": bool(pos["trail_active"]),
                         "trail_price": trail, "trail_pct": S("risk.trail_pct"),
                         "trail_start_price": pos["entry_price"] * (1 + S("risk.trail_start_pct") / 100),
                         "timeout_at": pos["opened_at"] + S("risk.timeout_h") * 3600,
                         "time_left_s": max(0, pos["opened_at"] + S("risk.timeout_h") * 3600 - self.clock())}
        holders = cr["holders"] if cr and cr["holders"] is not None else "n/a"
        is_open = bool(position and position["open"])
        return {"mint": mint, "name": cr["name"] if cr else None, "symbol": cr["symbol"] if cr else None,
                "price": last[0] if last else fnum(pair.get("priceUsd")), "updated_at": self.clock(),
                "candles": self.candles(mint),
                "stats": {"market_cap": fnum(pair.get("marketCap")), "liquidity_usd": fnum(g(pair, "liquidity", "usd")),
                          "volume_24h": fnum(g(pair, "volume", "h24")), "holders": holders},
                "position": position, "exits": exits, "coin_url": links.coin_url(mint, "solana"),
                "audit": self.layers(mint), "action": "Sell on FOMO" if is_open else "Buy on FOMO"}

    # ---------- publish ----------
    async def _put(self, file, obj):
        body = json.dumps(obj, separators=(",", ":"), default=str)
        check_no_secrets(body)
        if self.last_good.get(file) == body:
            return True
        ok = await self.up.put(file, body)
        if ok:
            self.last_good[file] = body; self.pending.pop(file, None)
        else:
            self.pending[file] = body  # last good stays online; retry next cycle
        return ok

    async def publish_once(self, full=True):
        files = [("state.json", self.state)]
        if full:
            files += [("chart.json", self.chart)]
            pages = self.activity_pages()
            files += [("activity.json", lambda: pages[0])] + [(f"activity/{p['page']}.json", (lambda p=p: p)) for p in pages]
            files += [(f"coin/{m}.json", (lambda m=m: self.coin(m))) for m in self.tonight_mints()]
            files += [(f"agents/{n}.json", (lambda n=n: {"name": n, "decisions": self._decisions(n, 20)})) for n in ORDER]
        res = {}
        for f, build in files:
            try:
                res[f] = await self._put(f, build())
            except SecretLeak:
                log.info(f"refused to upload {f.split('/')[0]}: secret in payload"); res[f] = False
            except Exception as e:
                log.info(f"build/upload {f.split('/')[0]} error {type(e).__name__}"); res[f] = False
        return res

    async def run(self):
        last_prices = last_bal = 0
        while True:
            now = self.clock()
            try:
                if now - last_bal >= BALANCE_EVERY_S:
                    self.write_balance(); last_bal = now
                if now - last_prices >= PRICES_EVERY_S:
                    await self.prices_once(); last_prices = now
                await self.publish_once()
            except Exception as e:  # never crash the agents
                log.info(f"publisher loop error {type(e).__name__}")
            await self.sleep(STATE_EVERY_S)


def make_api_hook(uploader_factory=SupabaseUploader, telegram_factory=None):
    """Returns api_hook(conn, settings, bus, agents) -> coroutine, as main.py expects."""
    def api_hook(conn, settings, bus, agents):
        async def go():
            tasks = []
            tg = telegram_factory(conn, settings, bus, agents) if telegram_factory else None
            try:
                up = uploader_factory()
            except Exception as e:
                log.info(f"publisher disabled: {e}"); up = None
            if up is not None:
                pub = Publisher(conn, settings, agents, up, net=Net(settings.get("desk.rpc_url")), telegram=tg)
                tasks.append(pub.run())
            if tg is not None:
                tasks.append(tg.run())
            if tasks:
                await asyncio.gather(*tasks)
        return go()
    return api_hook


api_hook = make_api_hook()
