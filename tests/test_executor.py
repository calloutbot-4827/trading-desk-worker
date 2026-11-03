from agents.executor import size_for, start_of_night_check, Executor
from config import Settings
from tests.fakes import env, FakeNet, FakeNotifier, pair, Clock
import links


def S(**kw):
    s = Settings()
    for k, v in kw.items(): s.set(k.replace("__", "."), v)
    return s.get


def test_200_bump_and_skip():
    assert size_for(200, 0, None, S()) == (47.5, "")
    assert size_for(200, 0, None, S(executor__below_min="skip")) == (None, "under $47.50")
    assert 200 * 8 / 100 == 16


def test_bump_needs_free():
    assert size_for(200, 160, None, S()) == (None, "no free stack")


def test_pool_cap_800():
    assert size_for(100000, 0, 40000, S()) == (800.0, "")
    assert size_for(10**6, 0, 40000, S()) == (800.0, "")


def test_night_check_warning(tmp_path):
    c, s, bus = env(tmp_path); s.set("executor.below_min", "skip")
    w = start_of_night_check(c, s.get)
    assert w.startswith("every coin will be skipped")


async def test_paper_push_and_fill(tmp_path):
    c, s, bus = env(tmp_path); net = FakeNet(); net.pairs = [pair("M", price=2.0, liq=50000)]; n = FakeNotifier()
    ex = Executor(c, s, bus, net, n, clock=Clock())
    out = await ex.handle({"mint": "M", "score": 76, "scan": {"name": "Coin", "liquidityUsd": 50000}})
    assert out["status"] == "filled" and out["sizeUsd"] == 47.5 and out["link"] == links.coin_url("M", "solana")
    text, buttons = n.sent[0]
    assert text == "PAPER · Buy Coin · $47.50 · 23.8% of stack\nAuditor 8/8 · Analyst 76 · stop -6% · trail 12%"
    assert buttons[0]["url"] == links.coin_url("M", "solana") and [b["text"] for b in buttons] == ["Buy on FOMO", "Bought", "Skip"]
    p = c.execute("SELECT * FROM positions").fetchone()
    assert p["stop_price"] == 2.0 * 0.94 and p["stop_pct"] == 6
    assert c.execute("SELECT status FROM signals").fetchone()[0] == "sent"
    assert (await ex.handle({"mint": "M", "scan": {}}))["status"] == "skipped"  # 1 push per coin


async def test_skip_mode_200_skips(tmp_path):
    c, s, bus = env(tmp_path); s.set("executor.below_min", "skip"); net = FakeNet(); net.pairs = [pair("M")]; n = FakeNotifier()
    out = await Executor(c, s, bus, net, n, clock=Clock()).handle({"mint": "M", "scan": {"name": "Coin"}})
    assert out["reason"] == "under $47.50" and not n.sent


async def test_push_failed_paper_fills(tmp_path):
    c, s, bus = env(tmp_path); net = FakeNet(); net.pairs = [pair("M")]; n = FakeNotifier(ok=False)
    out = await Executor(c, s, bus, net, n, clock=Clock()).handle({"mint": "M", "scan": {"name": "Coin"}})
    assert len(n.sent) == 3 and out["status"] == "filled" and out["reason"] == "push failed"


async def test_rechecks(tmp_path):
    c, s, bus = env(tmp_path); net = FakeNet(); net.pairs = [pair("M", liq=39000)]; n = FakeNotifier()
    ex = Executor(c, s, bus, net, n, clock=Clock())
    assert (await ex.handle({"mint": "M", "scan": {"liquidityUsd": 50000}}))["reason"] == "liquidity dropping"
    net.pairs = []
    assert (await ex.handle({"mint": "N", "scan": {}}))["reason"] == "no price"
