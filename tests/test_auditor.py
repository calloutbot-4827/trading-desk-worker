import db
from agents.auditor import Auditor, parse_graph, NoData
from tests.fakes import env, FakeNet, FakeNotifier, Clock, nosleep
import pytest


def mint_acct(freeze):
    return {"value": {"data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": freeze, "supply": "1000"}}}}}


async def test_freeze_authority_red_at_2(tmp_path):
    c, s, bus = env(tmp_path); net = FakeNet(); net.rpc_map["getAccountInfo"] = mint_acct("FrZ1111111111111111111111111111")
    seen = []
    a = Auditor(c, s, bus, net, sleep=nosleep, clock=Clock(), on_layer=lambda m, l: seen.append(l["n"]))
    out = await a.audit({"mint": "MOCK", "name": "Mock", "pool": "P", "liquidityUsd": 50000})
    L = out["layers"]
    assert L[0]["pass"] is True and L[1]["pass"] is False and out["passed"] is False
    assert out["layersChecked"] == 2 and seen == [1, 2]
    assert all(l["value"] == "not checked" and l["pass"] is None for l in L[2:]) and len(L) == 8
    assert out["reason"].startswith("set ")
    row = c.execute("SELECT title, subtitle, value FROM activity").fetchone()
    assert row["title"] == "Rejected Mock" and row["subtitle"] == f"Auditor · {out['reason']}" and row["value"] == "Skipped"
    assert await a.audit({"mint": "MOCK"}) is None  # never re-audit


async def test_rpc_failure_is_no_data(tmp_path):
    c, s, bus = env(tmp_path); net = FakeNet(); net.rpc_map["getAccountInfo"] = RuntimeError("down")
    out = await Auditor(c, s, bus, net, sleep=nosleep, clock=Clock()).audit({"mint": "X"})
    assert out["layers"][0]["value"] == "no data: mint authority" and not out["passed"]
    assert sum(1 for m, p in net.rpc_calls if m == "getAccountInfo") == 3


def test_empty_graph_is_no_data():
    with pytest.raises(NoData):
        parse_graph([])
    with pytest.raises(NoData):
        parse_graph({"nodes": []})


async def test_top10_falls_back_to_gpa_when_largest_rate_limited():
    from agents.common import top10_pct, RateLimited, TOKEN_2022
    net = FakeNet()
    net.rpc_map["getTokenSupply"] = {"value": {"amount": "1000"}}
    net.rpc_map["getTokenLargestAccounts"] = RateLimited("rpc")
    acct = lambda pk, own, amt: {"pubkey": pk, "account": {"data": {"parsed": {"info": {"owner": own, "tokenAmount": {"amount": str(amt)}}}}}}
    net.rpc_map["getProgramAccounts"] = lambda p: ([acct("POOLV", "POOL", 800)] + [acct(f"a{i}", f"o{i}", 10) for i in range(12)]
                                                   if p[0] == TOKEN_2022 else [])
    assert await top10_pct(net, "M", {"POOL"}) == 10.0  # pool owner excluded, top 10 of 12 x 10
    assert any(c[0] == "getProgramAccounts" and c[1][0] == TOKEN_2022 for c in net.rpc_calls)


def _all_pass_env(tmp_path, monkeypatch, mode, graph):
    import agents.auditor as au
    from tests.fakes import NOW
    async def zero(*a, **k): return 0
    async def ten(*a, **k): return 10.0
    monkeypatch.setattr(au, "creator_balance", zero); monkeypatch.setattr(au, "top10_pct", ten)
    monkeypatch.setattr(au, "pick_pumpswap", lambda d, m: {"liquidity": {"usd": 50000}, "pairCreatedAt": (NOW - 3600) * 1000})
    c, s, bus = env(tmp_path); s.set("desk.mode", mode)
    db.upsert_coin(c, "M", creator="CREATOR")
    net = FakeNet(); net.rpc_map["getAccountInfo"] = mint_acct(None); net.rpc_map["getProgramAccounts"] = []
    net.report = {"markets": [{"pubkey": "P", "lp": {"lpLockedPct": 100}}], "topHolders": [{"owner": f"w{i}"} for i in range(9)]}
    net.graph = graph
    return Auditor(c, s, bus, net, sleep=nosleep, clock=Clock())


async def test_bundles_no_data_paper_is_warning_not_red(tmp_path, monkeypatch):
    out = await _all_pass_env(tmp_path, monkeypatch, "PAPER", []).audit({"mint": "M", "pool": "P", "liquidityUsd": 50000})
    L7 = out["layers"][6]
    assert L7["pass"] is True and L7["warn"] is True and L7["value"] == "bundles: no data (warning)"
    assert out["layers"][7]["pass"] is True and out["layersChecked"] == 8  # layer 8 still decides
    assert out["passed"] is True and out["verdict"] == "amber" and out["score"] == 90
    assert "bundles: no data (warning)" in out["reason"]


async def test_bundles_no_data_live_is_red(tmp_path, monkeypatch):
    out = await _all_pass_env(tmp_path, monkeypatch, "LIVE", []).audit({"mint": "M", "pool": "P", "liquidityUsd": 50000})
    L7 = out["layers"][6]
    assert L7["pass"] is False and L7["value"] == "no data: bundles" and out["passed"] is False
    assert out["verdict"] == "red" and out["layers"][7]["pass"] is None


async def test_bundles_setting_red_in_paper_is_strict(tmp_path, monkeypatch):
    a = _all_pass_env(tmp_path, monkeypatch, "PAPER", []); a.s.set("auditor.bundles_no_data", "red")
    out = await a.audit({"mint": "M", "pool": "P", "liquidityUsd": 50000})
    assert out["layers"][6]["pass"] is False and not out["passed"]


async def test_real_bundles_over_threshold_red_in_paper(tmp_path, monkeypatch):
    edges = [{"source": f"w{i}", "target": f"w{i+1}"} for i in range(8)]  # 9 wallets in one group
    from agents.common import TOKEN_PROGRAM
    a = _all_pass_env(tmp_path, monkeypatch, "PAPER", [{"nodes": [], "edges": edges}])
    accts = [{"account": {"data": {"parsed": {"info": {"owner": f"w{i}", "tokenAmount": {"amount": "5"}}}}}} for i in range(9)]
    a.net.rpc_map["getProgramAccounts"] = lambda p: accts if p[0] == TOKEN_PROGRAM else []
    out = await a.audit({"mint": "M", "pool": "P", "liquidityUsd": 50000})
    L7 = out["layers"][6]
    assert L7["pass"] is False and L7["value"] == "biggest group 9" and out["verdict"] == "red" and not out.get("warnings")
