"""AUDITOR: 8 layers in order, stop at first red; later layers grey ("not checked").

Layer 7 data note: RugCheck /insiders/graph returned `[]` for the sampled mint (logs/raw/rugcheck_insiders_graph.json),
so its node/edge key names are NOT observed (fields.py: nodes=None, edges=None). We accept the spec's shape
{"nodes":[...],"edges":[...]} (or a list of such objects). Node id key and edge endpoint keys below are UNVERIFIED
assumptions (GRAPH_* constants). An empty [] response, or a graph without an "edges" key, is NO DATA -> red
"no data: bundles". No data is never a pass.

Exception (owner decision, PAPER only): with desk.mode == PAPER and auditor.bundles_no_data == "warn", a null/empty
bundles graph is a WARNING (amber): layer 7 value "bundles: no data (warning)", pass stays True with warn=True, the
audit score drops by auditor.bundles_no_data_penalty (default 10 of 100) and layer 8 still decides. In LIVE mode
no data on bundles is always red. Real bundle data over auditor.max_bundled is red in every mode.
"""
import asyncio, time
import db
from fields import FIELDS
from agents.common import (Net, logger, g, fnum, pick_pumpswap, pool_exclusions, top10_pct, creator_balance, short,
                           TOKEN_PROGRAM, TOKEN_2022)

RC = FIELDS["rugcheck_report"]
GRAPH_NODE_ID = ("id", "address")          # UNVERIFIED
GRAPH_EDGE_ENDS = (("source", "target"), ("from", "to"))  # UNVERIFIED
NAMES = ["mint authority", "freeze authority", "dev wallet", "top 10", "liquidity", "pool age", "bundled", "delegations"]


class NoData(Exception):
    pass


def components(edges, keep):
    parent = {}
    def f(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for a, b in edges:
        if a in keep and b in keep:
            parent[f(a)] = f(b)
    groups = {}
    for x in list(parent):
        groups.setdefault(f(x), set()).add(x)
    return sorted(groups.values(), key=len, reverse=True)


def parse_graph(graph):
    """-> list of (a,b) edges, or raise NoData."""
    objs = graph if isinstance(graph, list) else [graph]
    objs = [o for o in objs if isinstance(o, dict) and "edges" in o]
    if not objs:
        raise NoData("bundles")
    edges = []
    for o in objs:
        for e in o.get("edges") or []:
            for a, b in GRAPH_EDGE_ENDS:
                if isinstance(e, dict) and e.get(a) and e.get(b):
                    edges.append((e[a], e[b])); break
    return edges


class Auditor:
    name = "auditor"

    def __init__(self, conn, settings, bus, net: Net, notifier=None, sleep=asyncio.sleep, clock=time.time, on_layer=None):
        self.c, self.s, self.bus, self.net, self.notifier = conn, settings, bus, net, notifier
        self.sleep, self.clock = sleep, clock
        self.on_layer = on_layer or (lambda mint, layer: None)  # hook for later SSE "audit_layer"
        self.log = logger("auditor")
        self.rejected = set()
        self.audited = set()
        self.layer8_down = False
        self.layer8_killed = 0   # for the morning report

    async def _retry(self, fn, label):
        n = self.s.get("auditor.retries")
        for i in range(n):
            try:
                return await fn()
            except Exception as e:
                self.log.info(f"{label} attempt {i+1}/{n} failed ({type(e).__name__})")
                if i < n - 1:
                    await self.sleep(2)
        raise NoData(label)

    async def audit(self, scan):
        mint = scan["mint"]; S = self.s.get
        if mint in self.rejected or mint in self.audited:
            return None
        self.audited.add(mint)
        cache = {}

        async def once(key, fn):
            if key not in cache:
                try:
                    cache[key] = await self._retry(fn, key)
                except NoData:
                    cache[key] = NoData
            if cache[key] is NoData:
                raise NoData(key)
            return cache[key]

        report = lambda: once("rugcheck_report", lambda: self.net.rugcheck_report(mint))
        acct = lambda: once("mint_account", lambda: self.net.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]))

        async def holders_accts():
            async def gpa():
                t = await self.net.rpc("getProgramAccounts", [TOKEN_PROGRAM, {"encoding": "jsonParsed", "filters": [
                    {"dataSize": 165}, {"memcmp": {"offset": 0, "bytes": mint}}]}])
                t22 = await self.net.rpc("getProgramAccounts", [TOKEN_2022, {"encoding": "jsonParsed", "filters": [
                    {"memcmp": {"offset": 0, "bytes": mint}}]}])
                return (t or []) + (t22 or [])
            return await once("gpa", gpa)

        def info_of(a):
            return g(a, "account", "data", "parsed", "info") or {}

        async def L1():
            v = g(await acct(), "value", "data", "parsed", "info")
            if v is None or "mintAuthority" not in v:
                raise NoData("mint authority")
            ma = v["mintAuthority"]
            return ma is None, ("null" if ma is None else f"set {short(ma)}"), "rpc getAccountInfo"

        async def L2():
            v = g(await acct(), "value", "data", "parsed", "info")
            if v is None or "freezeAuthority" not in v:
                raise NoData("freeze authority")
            fa = v["freezeAuthority"]
            return fa is None, ("null" if fa is None else f"set {short(fa)}"), "rpc getAccountInfo"

        async def L3():
            row = self.c.execute("SELECT creator, creator_balance_at_migration FROM coins WHERE mint=?", (mint,)).fetchone()
            creator = row["creator"] if row else None
            if not creator:
                return False, "no data: dev", "rugcheck creator"
            supply = fnum(g(await acct(), "value", "data", "parsed", "info", "supply"))
            if not supply:
                return False, "no supply", "rpc getAccountInfo"
            bal = await once("creator_bal", lambda: creator_balance(self.net, creator, mint))
            pct = 100 * bal / supply
            then = row["creator_balance_at_migration"]
            if then is not None and bal < then:
                return False, "dev sold", "rpc getTokenAccountsByOwner"
            if pct >= S("auditor.max_dev_pct"):
                return False, f"dev holds {pct:.0f}%", "rpc getTokenAccountsByOwner"
            return True, f"{pct:.1f}% · sold 0", "rpc getTokenAccountsByOwner"

        async def L4():
            rep = await report()
            t = await once("top10", lambda: top10_pct(self.net, mint, pool_exclusions(rep) | {scan.get("pool")}, rep))
            if t is None:
                raise NoData("top 10")
            return t < S("auditor.max_top10_pct"), f"top 10 hold {t:.0f}%", "rpc getTokenLargestAccounts"

        async def dex():
            return await once("dex", lambda: self.net.dex_tokens([mint]))

        async def L5():
            rep = await report()
            mk = [m for m in g(rep, "markets") or [] if g(m, "pubkey") == scan.get("pool")]  # fields: markets[i].pubkey
            locked = fnum(g(mk[0], "lp", "lpLockedPct")) if mk else None  # fields: markets[i].lp.lpLockedPct
            if locked is None:
                return False, "no data: liquidity", "rugcheck markets"
            if locked < S("auditor.min_lp_locked_pct"):
                return False, f"LP locked {locked:.0f}%", "rugcheck markets"
            p = pick_pumpswap(await dex(), mint)
            now_liq, then = fnum(g(p, "liquidity", "usd")), fnum(scan.get("liquidityUsd"))
            if now_liq is None or not then:
                return False, "no data: liquidity", "dexscreener"
            drop = 100 * (then - now_liq) / then
            if drop > S("auditor.max_liq_drop_pct"):
                return False, f"liquidity down {drop:.0f}%", "dexscreener"
            return True, f"LP locked {locked:.0f}%", "rugcheck markets + dexscreener"

        async def L6():
            p = pick_pumpswap(await dex(), mint)
            created = fnum(g(p, "pairCreatedAt"))
            if not created:
                raise NoData("pool age")
            age = (self.clock() - created / 1000) / 60
            return age >= S("auditor.min_pool_age_min"), f"{age:.0f} min", "dexscreener pairCreatedAt"

        async def L7():
            graph = await once("graph", lambda: self.net.rugcheck_graph(mint))
            try:
                edges = parse_graph(graph)
            except NoData:
                if S("desk.mode") == "PAPER" and S("auditor.bundles_no_data") == "warn":
                    return True, "bundles: no data (warning)", "rugcheck insiders graph", "warn"
                return False, "no data: bundles", "rugcheck insiders graph", 0
            try:
                keep = {info_of(a).get("owner") for a in await holders_accts()
                        if fnum(g(info_of(a), "tokenAmount", "amount"))}
                src = "rugcheck graph + holders"
            except NoData:
                keep = {h.get("owner") for h in g(await report(), "topHolders") or []}
                src = "rugcheck graph + rugcheck topHolders"
            groups = components(edges, keep)
            big = len(groups[0]) if groups else 0
            return big < S("auditor.max_bundled"), f"biggest group {big}", src, big

        async def L8():
            try:
                accts = await holders_accts()
            except NoData:
                await self._layer8_down()
                return False, "no data: delegations", "rpc getProgramAccounts"
            by = {}
            holders = 0
            for a in accts:
                inf = info_of(a)
                if fnum(g(inf, "tokenAmount", "amount")):
                    holders += 1
                if inf.get("delegate"):
                    by.setdefault(inf["delegate"], 0); by[inf["delegate"]] += 1
            db.upsert_coin(self.c, mint, holders=holders)
            result["holders"] = holders
            if by:
                d, n = max(by.items(), key=lambda kv: kv[1])
                if n > S("auditor.max_delegated_accounts"):
                    return False, f"{n} wallets → {short(d)}", "rpc getProgramAccounts"
            return True, f"max {max(by.values()) if by else 0} per delegate", "rpc getProgramAccounts"

        result = {"holders": None}
        layers, reason, red = [], "", False
        warnings, score = [], 100
        for i, fn in enumerate([L1, L2, L3, L4, L5, L6, L7, L8], 1):
            if red:
                layers.append({"n": i, "name": NAMES[i-1], "pass": None, "value": "not checked", "source": None}); continue
            extra = None
            try:
                r = await fn()
                ok, val, src = r[:3]; extra = r[3] if len(r) > 3 else None
            except NoData:
                ok, val, src = False, f"no data: {NAMES[i-1]}", None
            layer = {"n": i, "name": NAMES[i-1], "pass": bool(ok), "value": val, "source": src}
            if extra == "warn":
                layer["warn"] = True; warnings.append(val); score -= S("auditor.bundles_no_data_penalty")
            elif extra is not None:
                layer["bundled"] = extra
            layers.append(layer)
            db.insert(self.c, "audits", mint=mint, ts=self.clock(), layer=str(i), name=NAMES[i-1], value_text=val,
                      **{"pass": int(bool(ok))}, source=src)
            self.on_layer(mint, layer)
            if not ok:
                red, reason = True, val
                if i == 8 and val == "no data: delegations":
                    self.layer8_killed += 1
        passed = not red and len(layers) == 8 and all(l["pass"] for l in layers)
        if warnings:
            reason = ("passed with warning · " if passed else f"{reason} · ") + "; ".join(warnings)
        checked = sum(1 for l in layers if l["pass"] is not None)
        out = {"agent": "auditor", "mint": mint, "ts": self.clock(), "passed": passed, "layersChecked": checked,
               "layers": layers, "holders": result["holders"], "reason": reason, "score": score,
               "verdict": "red" if not passed else ("amber" if warnings else "green"), "warnings": warnings}
        name = scan.get("name") or mint
        self.log.info(f"{mint} passed={passed} checked={checked} reason={reason!r}")
        if passed:
            await self.bus.publish("auditor", {**out, "scan": scan})
        else:
            self.rejected.add(mint)
            db.activity(self.c, "rejected", mint, "auditor", f"Rejected {name}", f"Auditor · {reason}", "Skipped")
        return out

    async def _layer8_down(self):
        if self.layer8_down:
            return
        self.layer8_down = True
        self.log.info("layer 8 down: getProgramAccounts refused")
        if self.notifier:
            try:
                await self.notifier.send("layer 8 down, every coin is red until rpc_url changes")
            except Exception as e:
                self.log.info(f"notify failed ({type(e).__name__})")

    def rpc_url_changed(self):
        self.layer8_down = False

    async def run(self):
        while True:
            db.heartbeat(self.c, "auditor")
            if getattr(self, "paused", False):
                await self.sleep(5); continue
            try:
                scan = await asyncio.wait_for(self.bus.consume("auditor"), 30)
            except asyncio.TimeoutError:
                continue
            if not self.s.get("auditor.enabled"):
                self.log.info(f"{scan['mint']} auditor disabled: not audited, not forwarded")
                continue
            try:
                await self.audit(scan)
            except Exception as e:
                self.log.info(f"{scan.get('mint')} audit error {type(e).__name__}")
