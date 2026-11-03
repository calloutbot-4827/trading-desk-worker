import time
import db
from bus import Bus
from config import Settings
from agents.common import TOKEN_PROGRAM, TOKEN_2022

NOW = 1_800_000_000.0


def pair(mint, price=1.0, liq=50000, h1=10, h24=10, m5=0, vh1=0, bh1=0, bm5=0, sm5=0, age_min=15, name="Coin", dex="pumpswap", pool="POOL"):
    return {"dexId": dex, "pairAddress": pool, "baseToken": {"address": mint, "name": name, "symbol": "C"},
            "priceUsd": str(price), "liquidity": {"usd": liq}, "volume": {"m5": 1, "h1": vh1},
            "txns": {"m5": {"buys": bm5, "sells": sm5}, "h1": {"buys": bh1, "sells": 0}},
            "priceChange": {"m5": m5, "h1": h1, "h24": h24}, "pairCreatedAt": (NOW - age_min * 60) * 1000}


class FakeNet:
    def __init__(self):
        self.pairs = []; self.rpc_calls = []; self.rpc_map = {}; self.report = {}; self.graph = []
    async def dex_tokens(self, mints):
        return [p for p in self.pairs if p["baseToken"]["address"] in mints]
    async def rugcheck_report(self, mint): return self.report
    async def rugcheck_graph(self, mint): return self.graph
    async def rpc(self, method, params):
        self.rpc_calls.append((method, params))
        v = self.rpc_map.get(method)
        if isinstance(v, Exception): raise v
        return v(params) if callable(v) else v


class FakeNotifier:
    def __init__(self, ok=True): self.sent = []; self.ok = ok
    async def send(self, text, buttons=None):
        self.sent.append((text, buttons)); return self.ok


class Clock:
    def __init__(self, t=NOW): self.t = t
    def __call__(self): return self.t


async def nosleep(*a): pass


def env(tmp_path):
    c = db.connect(str(tmp_path / "t.db")); return c, Settings(c), Bus()
