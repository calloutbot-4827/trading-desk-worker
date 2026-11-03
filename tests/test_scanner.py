import pytest
from agents.scanner import Scanner, norm_name
from tests.fakes import env, FakeNet, pair, Clock, nosleep, NOW


def rpc(top_amounts, supply=1000):
    return {"getTokenSupply": {"value": {"amount": str(supply)}},
            "getTokenLargestAccounts": {"value": [{"address": f"A{i}", "amount": str(a)} for i, a in enumerate(top_amounts)]},
            "getTokenAccountsByOwner": {"value": []}}


async def run_one(tmp_path, p, top=(10,), runners=None, mint="M"):
    c, s, bus = env(tmp_path); net = FakeNet(); net.rpc_map = rpc(top); net.report = {"creator": "CR"}
    net.pairs = [p] + (runners or [])
    sc = Scanner(c, s, bus, net, clock=Clock(), sleep=nosleep)
    for r in runners or []:  # runners are coins this scanner saw today
        await sc.on_migration({"mint": r["baseToken"]["address"], "txType": "migrate"})
    await sc.on_migration({"mint": mint, "signature": "sig", "txType": "migrate", "pool": "pump-amm"})
    outs = await sc.poll()
    return c, bus, sc, [o for o in outs if o["mint"] == mint]


@pytest.mark.parametrize("p,top,reason", [
    (pair("M", liq=31200), (10,), "liq $31.2K"),
    (pair("M"), (380,), "top 10 hold 38%"),
    (pair("M", h1=112), (10,), "+112% in 1h"),
])
async def test_kills(tmp_path, p, top, reason):
    c, bus, sc, outs = await run_one(tmp_path, p, top)
    assert outs[0]["verdict"] == "kill" and outs[0]["reason"] == reason
    assert c.execute("SELECT reason FROM scans").fetchone()[0] == reason and bus.q["auditor"].empty()


async def test_copycat(tmp_path):
    runner = pair("R", name="Bear Trump 🐻", h24=400)
    c, bus, sc, outs = await run_one(tmp_path, pair("M", name="bear trump 2"), runners=[runner])
    assert outs[0]["reason"] == "copycat of Bear Trump 🐻"
    assert norm_name("Bear Trump 🐻 2") == "beartrump"


async def test_pass_once(tmp_path):
    c, bus, sc, outs = await run_one(tmp_path, pair("M"))
    assert outs[0]["verdict"] == "pass" and not bus.q["auditor"].empty()
    assert c.execute("SELECT creator FROM coins").fetchone()[0] == "CR"
    await sc.on_migration({"mint": "M", "txType": "migrate"}); assert "M" not in sc.room


async def test_no_pumpswap_waits_then_never_qualified(tmp_path):
    c, bus, sc, outs = await run_one(tmp_path, pair("M", dex="pumpfun"))
    assert outs == [] and "M" in sc.room
    sc.clock.t = NOW + 3600
    outs = await sc.poll()
    assert outs[0]["reason"] == "never qualified"


async def test_young_pool_waits(tmp_path):
    c, bus, sc, outs = await run_one(tmp_path, pair("M", age_min=5))
    assert outs == [] and "M" in sc.room
