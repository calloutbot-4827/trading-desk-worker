"""Shared plumbing for agents: injectable HTTP/RPC, rate limiter, per-agent log files, helpers.
Never logs URLs with secrets or env values."""
import asyncio, json, logging, logging.handlers, os, time
from pathlib import Path
import httpx

LOG_DIR = Path(os.environ.get("DESK_LOG_DIR") or Path(__file__).resolve().parents[1] / "logs")
LOG_MAX_BYTES, LOG_BACKUPS = 5 * 1024 * 1024, 3


def cap_dir(d, max_files=200, max_bytes=20 * 1024 * 1024):
    """Keep only the newest files in d (e.g. logs/raw, logs/replay) within max_files and max_bytes."""
    d = Path(d)
    if not d.is_dir():
        return 0
    fs = sorted((f for f in d.rglob("*") if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
    total, removed = 0, 0
    for i, f in enumerate(fs):
        total += f.stat().st_size
        if i >= max_files or total > max_bytes:
            try:
                f.unlink(); removed += 1
            except OSError:
                pass
    return removed
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class RateLimited(Exception):
    """HTTP 429."""


class NetError(Exception):
    pass


def logger(agent):
    lg = logging.getLogger(f"desk.{agent}")
    if not lg.handlers:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(LOG_DIR / f"{agent}.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h); lg.setLevel(logging.INFO); lg.propagate = False
    return lg


class RateLimiter:
    """Sliding window: at most `n` calls per `per` seconds."""
    def __init__(self, n=250, per=60.0, clock=time.monotonic, sleep=asyncio.sleep):
        self.n, self.per, self.t, self.clock, self.sleep = n, per, [], clock, sleep

    async def acquire(self):
        while True:
            now = self.clock()
            self.t = [x for x in self.t if now - x < self.per]
            if len(self.t) < self.n:
                self.t.append(now); return
            await self.sleep(self.per - (now - self.t[0]) + 0.01)


DEX_LIMIT = RateLimiter(250, 60.0)  # shared by every agent that reads DexScreener


class Net:
    """All network I/O goes through here; tests pass a fake with the same methods."""
    TOTAL_TIMEOUT = 30.0  # hard cap per request (httpx timeouts are per-phase, a trickling body can exceed them)

    def __init__(self, rpc_url, client=None, dex_limiter=DEX_LIMIT):
        self.rpc_url, self._c, self.lim = rpc_url, client, dex_limiter

    async def _req(self, coro):
        try:
            return await asyncio.wait_for(coro, self.TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            raise NetError("timeout") from None

    def _client(self):
        if self._c is None:
            self._c = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10, pool=10))
        return self._c

    async def get_json(self, url):
        r = await self._req(self._client().get(url))
        if r.status_code == 429:
            raise RateLimited(url)
        if r.status_code != 200:
            raise NetError(f"HTTP {r.status_code}")
        return r.json()

    async def dex_tokens(self, mints):
        """List of pairs for up to 30 mints in one request."""
        out = []
        for i in range(0, len(mints), 30):
            await self.lim.acquire()
            out += await self.get_json(DEX + ",".join(mints[i:i + 30])) or []
        return out

    async def rugcheck_report(self, mint):
        return await self.get_json(f"{RUGCHECK}{mint}/report")

    async def rugcheck_graph(self, mint):
        return await self.get_json(f"{RUGCHECK}{mint}/insiders/graph")

    async def rpc(self, method, params):
        r = await self._req(self._client().post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
        if r.status_code == 429:
            raise RateLimited("rpc")
        if r.status_code != 200:
            raise NetError(f"rpc HTTP {r.status_code}")
        j = r.json()
        if "error" in j:
            raise NetError(f"rpc error {j['error'].get('code')}")
        return j.get("result")


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def g(d, *path):
    """Safe nested get; unknown/missing -> None."""
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and -len(d) <= k < len(d):
            d = d[k]
        else:
            return None
    return d


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pick_pumpswap(pairs, mint):
    """Pair with dexId pumpswap for mint (most liquidity if several), else None (coin waits)."""
    c = [p for p in pairs or [] if g(p, "dexId") == "pumpswap" and g(p, "baseToken", "address") == mint]
    if not c:
        return None
    return max(c, key=lambda p: fnum(g(p, "liquidity", "usd")) or 0)


def pool_exclusions(report):
    """Pool + bonding-curve accounts from a RugCheck report (fields.py: knownAccounts type AMM, markets[i] vaults/pubkey)."""
    ex = set()
    for addr, v in (g(report, "knownAccounts") or {}).items():
        if g(v, "type") == "AMM":
            ex.add(addr)
    for m in g(report, "markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB"):
            if g(m, k):
                ex.add(m[k])
    return ex


async def _holder_accounts(net, mint):
    """Every token account of mint via getProgramAccounts on BOTH programs (Token: dataSize 165; Token-2022: no size)."""
    t = await net.rpc("getProgramAccounts", [TOKEN_PROGRAM, {"encoding": "jsonParsed", "filters": [
        {"dataSize": 165}, {"memcmp": {"offset": 0, "bytes": mint}}]}])
    t22 = await net.rpc("getProgramAccounts", [TOKEN_2022, {"encoding": "jsonParsed", "filters": [
        {"memcmp": {"offset": 0, "bytes": mint}}]}])
    return (t or []) + (t22 or [])


async def top10_pct(net, mint, exclude, report=None):
    """Top-10 holders % of supply via RPC getTokenLargestAccounts + getTokenSupply, pool/curve excluded.
    A largest-account address is excluded when it, or its RugCheck topHolders owner, is in `exclude`.
    Replay 2026-11-03: the public mainnet-beta RPC answered 429 to every getTokenLargestAccounts, so top10 was
    always null (scanner never 'ready', auditor L4 always 'no data'). On RateLimited/NetError we fall back to
    getProgramAccounts (worked on every real coin) and read owner + amount from each account."""
    try:
        amt = fnum(g(await net.rpc("getTokenSupply", [mint]), "value", "amount"))
    except (RateLimited, NetError):
        amt = fnum(g(await net.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]), "value", "data", "parsed", "info", "supply"))
    if not amt:
        return None
    owner = {h.get("address"): h.get("owner") for h in (g(report, "topHolders") or [])}
    try:
        largest = g(await net.rpc("getTokenLargestAccounts", [mint]), "value") or []
        keep = [fnum(a.get("amount")) or 0 for a in largest
                if a.get("address") not in exclude and owner.get(a.get("address")) not in exclude]
    except (RateLimited, NetError):
        keep = []
        for a in await _holder_accounts(net, mint):
            info = g(a, "account", "data", "parsed", "info") or {}
            if a.get("pubkey") in exclude or info.get("owner") in exclude or owner.get(a.get("pubkey")) in exclude:
                continue
            keep.append(fnum(g(info, "tokenAmount", "amount")) or 0)
    return round(100 * sum(sorted(keep, reverse=True)[:10]) / amt, 2)


async def creator_balance(net, creator, mint):
    """Raw token amount the creator wallet holds of mint (getTokenAccountsByOwner, jsonParsed)."""
    r = await net.rpc("getTokenAccountsByOwner", [creator, {"mint": mint}, {"encoding": "jsonParsed"}])
    return sum(fnum(g(a, "account", "data", "parsed", "info", "tokenAmount", "amount")) or 0 for a in g(r, "value") or [])


def short(addr):
    return f"{addr[:4]}...{addr[-3:]}" if addr and len(addr) > 8 else str(addr)


def kfmt(usd):
    return f"${usd/1000:.1f}K" if usd < 1e6 else f"${usd/1e6:.1f}M"
