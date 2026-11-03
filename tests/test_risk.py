import db
from agents.risk import Risk, pill
from agents.executor import Executor
from tests.fakes import env, FakeNet, FakeNotifier, pair, Clock, nosleep, NOW


def setup(tmp_path):
    c, s, bus = env(tmp_path); net = FakeNet(); n = FakeNotifier(); clk = Clock()
    db.upsert_coin(c, "M", name="Coin")
    ex = Executor(c, s, bus, net, n, clock=clk)
    return c, s, net, n, clk, ex, Risk(c, s, net, n, clock=clk, sleep=nosleep)


def pos(c, pid): return c.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()


async def test_trail_exit(tmp_path):
    c, s, net, n, clk, ex, r = setup(tmp_path)
    pid = ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    net.pairs = [pair("M", price=1.25)]; assert await r.poll() == []
    assert pos(c, pid)["trail_active"] == 1
    net.pairs = [pair("M", price=1.25 * 0.87)]
    out = (await r.poll())[0]
    assert out["reason"] == "trail" and out["exitPrice"] == 1.25 * 0.87
    assert n.sent[-1][0].startswith("PAPER · Sold Coin · +$") and "(+8.7%)" in n.sent[-1][0]
    assert [b["text"] for b in n.sent[-1][1]] == ["Sell on FOMO", "Sold"]
    assert n.sent[-1][1][0]["url"] == __import__("links").coin_url("M", "solana")
    a = c.execute("SELECT title, subtitle FROM activity WHERE kind='sold'").fetchone()
    assert tuple(a) == ("Sold Coin", "Risk · trailing stop")


async def test_stop_exit(tmp_path):
    c, s, net, n, clk, ex, r = setup(tmp_path)
    ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    net.pairs = [pair("M", price=0.939)]
    out = (await r.poll())[0]
    assert out["reason"] == "stop" and "hit the -6% stop" in n.sent[-1][0]


async def test_timeout(tmp_path):
    c, s, net, n, clk, ex, r = setup(tmp_path)
    ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    clk.t = NOW + 3 * 3600; net.pairs = [pair("M", price=1.04)]
    out = (await r.poll())[0]
    assert out["reason"] == "timeout" and n.sent[-1][0] == "PAPER · Sell Coin, dead money · 4% in 3h"


async def test_loosening_never_moves_stop_down(tmp_path):
    c, s, net, n, clk, ex, r = setup(tmp_path)
    pid = ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    s.set("risk.stop_pct", 10)
    net.pairs = [pair("M", price=0.93)]
    out = (await r.poll())[0]
    assert out["reason"] == "stop" and out["stopPct"] == 6
    assert pos(c, pid)["stop_price"] == 1.0 * 0.94


async def test_tightening_applies(tmp_path):
    c, s, net, n, clk, ex, r = setup(tmp_path)
    pid = ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    s.set("risk.stop_pct", 4); net.pairs = [pair("M", price=1.0)]
    await r.poll()
    assert pos(c, pid)["stop_pct"] == 4


async def test_stop_8_new_position_and_old_keeps_6(tmp_path):
    c, s, net, n, clk, ex, r = setup(tmp_path)
    db.upsert_coin(c, "OLD", name="Old")
    old = ex.open_position("OLD", "Old", 1.0, 50, 25, "PAPER")
    s.set("risk.stop_pct", 8)
    new = ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    assert pill(pos(c, new)) == "stop -8%" and pill(pos(c, old)) == "stop -6%"
    net.pairs = [pair("M", price=0.919), pair("OLD", price=0.95)]
    outs = await r.poll()
    assert len(outs) == 1 and outs[0]["mint"] == "M" and outs[0]["mark"] == "-8%"
    assert n.sent[-1][0].startswith("PAPER · Coin hit the -8% stop · -$")
    a = c.execute("SELECT subtitle FROM activity WHERE kind='stopped'").fetchone()[0]
    assert a == "Risk · -8% stop"
    assert pill(pos(c, old)) == "stop -6%" and pos(c, old)["closed_at"] is None


async def test_no_price_4_polls_and_pulled(tmp_path):
    c, s, net, n, clk, ex, r = setup(tmp_path)
    ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    net.pairs = []
    for _ in range(4):
        assert await r.poll() == []
    assert n.sent[-1][0] == "Coin: no price for 1 min"
    db.insert(c, "prices", mint="M", ts=NOW, price=0.8, volume_m5=0)
    net.pairs = [pair("M", price=0.8, liq=0)]
    out = (await r.poll())[0]
    assert out["reason"] == "pulled" and out["exitPrice"] == 0.8


async def test_cannot_pause_with_open_position(tmp_path):
    import pytest
    c, s, net, n, clk, ex, r = setup(tmp_path)
    ex.open_position("M", "Coin", 1.0, 50, 25, "PAPER")
    with pytest.raises(RuntimeError):
        r.pause()
