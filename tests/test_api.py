import json, os
import pytest
import api, db, links
from agents.risk import Risk
from fakes import FakeNet, FakeNotifier, Clock, NOW, nosleep, env, pair

SECRETS = {"SUPABASE_SECRET_KEY": "sb_secret_TESTKEY_abcdef", "TG_TOKEN": "123:TGTOKENxyz", "DATA_PATH": "pathSECRET987",
           "OWNER_CHAT_ID": "998877665"}
M1, M2 = "Mint1111111111111111111111111111111", "Mint2222222222222222222222222222222"


class A:  # stand-in agent
    paused = False
    room = {}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    c, s, bus = env(tmp_path)
    clock = Clock(NOW)
    db.insert(c, "balance", ts=NOW - 7200, value=200.0)
    db.upsert_coin(c, M1, name="Alpha", symbol="ALP", holders=321)
    db.upsert_coin(c, M2, name="Beta", symbol="BET")
    db.insert(c, "settings", key="night_start", value=json.dumps(NOW - 3600), updated_at=NOW)
    for i in range(1, 9):
        db.insert(c, "audits", mint=M1, ts=NOW - 3000 + i, layer=str(i), name="x", value_text=f"v{i}", **{"pass": 1}, source="s")
    db.insert(c, "audits", mint=M2, ts=NOW - 2900, layer="1", name="mint authority", value_text="set", **{"pass": 0}, source="s")
    db.insert(c, "scans", mint=M1, ts=NOW - 3100, liquidity_usd=50000, top10_pct=10, h1_change_pct=5, age_min=12, verdict="pass", reason="")
    db.insert(c, "verdicts", mint=M1, ts=NOW - 2800, alive_6h=.8, organic=.8, flush=.1, chase=.1, score=82, **{"pass": 1}, reason="", input_tokens=0)
    db.insert(c, "signals", mint=M1, ts=NOW - 2700, price=1.0, size_usd=47.5, stack_pct=23.8, link=links.coin_url(M1), mode="PAPER", status="sent")
    pid = db.insert(c, "positions", mint=M1, opened_at=NOW - 2700, entry_price=1.0, size_usd=47.5, peak_price=1.0,
                    stop_price=0.94, stop_pct=6, trail_active=0, mode="PAPER")
    for k, (p, v) in enumerate([(1.0, 100), (1.02, 160), (0.99, 150), (1.05, 400)]):
        db.insert(c, "prices", mint=M1, ts=NOW - 2700 + k * 30, price=p, volume_m5=v)
    db.activity(c, "bought", M1, "executor", "Bought Alpha", "Executor · 01:00", "$47.50 · 23.8% of stack")
    db.activity(c, "rejected", M2, "auditor", "Rejected Beta", "Auditor · set", "Skipped")
    for a in api.ORDER:
        db.heartbeat(c, a)
    agents = {n: A() for n in api.ORDER}
    up = api.FakeUploader()
    net = FakeNet(); net.pairs = [pair(M1, price=1.05, liq=60000), pair(M2, price=2.0)]
    net.pairs[0]["marketCap"] = 900000; net.pairs[0]["volume"]["h24"] = 123456
    pub = api.Publisher(c, s, agents, up, net=net, clock=clock, sleep=nosleep)
    return c, s, pub, up, clock, pid, net


async def test_publish_shapes_and_no_secrets(setup):
    c, s, pub, up, clock, pid, net = setup
    await pub.prices_once()
    res = await pub.publish_once()
    assert all(res.values()), res
    files = up.files
    for f in ["state.json", "chart.json", "activity.json", "activity/1.json", f"coin/{M1}.json", f"coin/{M2}.json"] + \
             [f"agents/{n}.json" for n in api.ORDER]:
        assert f in files, f
    for f, body in files.items():
        for v in SECRETS.values():
            assert v not in body, f
        json.loads(body)
    st = json.loads(files["state.json"])
    assert [a["name"] for a in st["agents"]] == list(api.ORDER)
    assert st["mode"] == "PAPER" and st["paper"] and st["badge"] == "PAPER"
    assert st["balance"] == 200.0 and st["multiplier"] == 1.0
    au = st["agents"][1]
    assert au["headline"] == "2 taken apart" and au["rejected_text"] == "1 rejected"
    assert any(x["strip"] == [True] * 8 for x in au["strips"])
    assert st["agents"][0]["headline"] == "1 pools"
    assert st["agents"][2]["headline"] == "Scoring" and st["agents"][2]["score"] == 82
    assert st["agents"][3]["headline"] == "1 trades" and st["agents"][3]["stack_pct"] == 23.8
    assert st["agents"][4]["headline"] == "Guarding" and st["agents"][4]["pills"] == ["stop -6%", "trail 12%"]
    assert st["account"]["profile_url"] == links.profile_url(s.get("desk.fomo_handle"))
    assert st["account"]["signup_url"] == links.SIGNUP_URL
    assert st["account"]["telegram"]["connected"] is False
    coin = json.loads(files[f"coin/{M1}.json"])
    assert coin["coin_url"] == links.coin_url(M1) and coin["action"] == "Sell on FOMO"
    assert coin["stats"] == {"market_cap": 900000.0, "liquidity_usd": 60000.0, "volume_24h": 123456.0, "holders": 321}
    assert len(coin["audit"]) == 8 and coin["audit"][0]["value"] == "v1"
    assert coin["exits"]["stop_pct"] == 6 and coin["exits"]["time_left_s"] == pytest.approx(3 * 3600 - 2700)
    assert coin["candles"][0]["o"] == 1.0 and coin["candles"][0]["v"] == 60  # vol delta 100->160, then 160->150 clipped 0
    c2 = json.loads(files[f"coin/{M2}.json"])
    assert c2["stats"]["holders"] == "n/a" and c2["action"] == "Buy on FOMO" and c2["audit"][1]["value"] == "not checked"
    act = json.loads(files["activity.json"])
    assert act["page"] == 1 and act["pages"] == 1 and act["rows"][0]["title"] == "Rejected Beta"
    assert act["rows"][1]["coin_url"] == links.coin_url(M1)
    ag = json.loads(files["agents/executor.json"])
    assert ag["decisions"][0]["coin_url"] == links.coin_url(M1)


async def test_prices_only_for_uncovered(setup):
    c, s, pub, up, clock, pid, net = setup
    n0 = c.execute("SELECT COUNT(*) FROM prices WHERE mint=?", (M2,)).fetchone()[0]
    n1 = c.execute("SELECT COUNT(*) FROM prices WHERE mint=?", (M1,)).fetchone()[0]
    calls = []
    orig = net.dex_tokens
    async def spy(m):
        calls.append(m); return await orig(m)
    net.dex_tokens = spy
    await pub.prices_once()
    assert len(calls) == 1 and set(calls[0]) == {M1, M2}  # one batch
    assert c.execute("SELECT COUNT(*) FROM prices WHERE mint=?", (M2,)).fetchone()[0] == n0 + 1
    assert c.execute("SELECT COUNT(*) FROM prices WHERE mint=?", (M1,)).fetchone()[0] == n1  # Risk covers open M1


async def test_chart_marks_and_balance(setup, tmp_path):
    c, s, pub, up, clock, pid, net = setup
    risk = Risk(c, s, net, FakeNotifier(), clock=clock, sleep=nosleep)
    p = db.open_positions(c)[0]
    clock.t = NOW + 10
    await risk.exit(p, 0.9, "stop", clock.t)
    assert pub.write_balance() == pytest.approx(200 + c.execute("SELECT pnl_usd FROM positions").fetchone()[0])
    ch = pub.chart()
    assert set(ch["ranges"]) == {"LIVE", "1H", "4H", "1D", "1W", "ALL"}
    marks = ch["ranges"]["1H"]["marks"]
    assert marks[0]["label"] == "Bought Alpha" and marks[0]["color"] == "blue"
    assert marks[1]["color"] == "red" and marks[1]["label"].startswith("-6% · -$")
    st = pub.state()
    assert st["agents"][4]["headline"] == "Stopped"
    s.set("risk.stop_pct", 8)
    assert pub.state()["agents"][4]["pills"][0] == "stop -8%"


async def test_activity_pages(setup):
    c, s, pub, up, *_ = setup
    for i in range(65):
        db.activity(c, "rejected", M2, "auditor", f"Rejected {i}", "x", "Skipped")
    pages = pub.activity_pages()
    assert len(pages) == 3 and pages[0]["pages"] == 3 and len(pages[0]["rows"]) == 30 and len(pages[2]["rows"]) == 7


async def test_upload_failure_keeps_last_good_and_never_raises(setup):
    c, s, pub, up, clock, *_ = setup
    await pub.publish_once(full=False)
    good = up.files["state.json"]
    up.ok = False; clock.t += 15
    res = await pub.publish_once(full=False)
    assert res["state.json"] is False and up.files["state.json"] == good and "state.json" in pub.pending


async def test_secret_leak_refused(setup, monkeypatch):
    c, s, pub, up, *_ = setup
    db.upsert_coin(c, M1, name=SECRETS["TG_TOKEN"])  # pretend a secret got into the data
    res = await pub.publish_once()
    for f, body in up.files.items():
        assert SECRETS["TG_TOKEN"] not in body
    assert res[f"coin/{M1}.json"] is False


def test_rpc_url_key_not_published(setup):
    c, s, pub, *_ = setup
    s.values["desk.rpc_url"] = "https://rpc.example.com/?api-key=SECRETKEY1"
    assert "SECRETKEY1" not in json.dumps(pub.state())


class FakeResp:
    def __init__(self, code): self.status_code = code


class FakeClient:
    def __init__(self, codes): self.codes = list(codes); self.calls = []
    async def post(self, url, content=None, headers=None):
        self.calls.append((url, headers)); return FakeResp(self.codes.pop(0))


async def test_supabase_uploader_retry_and_headers():
    cl = FakeClient([500, 200])
    up = api.SupabaseUploader(url="https://x.supabase.co", key="K", data_path="P", client=cl, sleep=nosleep)
    assert await up.put("state.json", "{}") is True
    url, h = cl.calls[1]
    assert url == "https://x.supabase.co/storage/v1/object/desk-data/d/P/state.json"
    assert h["x-upsert"] == "true" and h["apikey"] == "K" and h["Authorization"] == "Bearer K" and h["cache-control"] == "max-age=0"
    cl2 = FakeClient([500, 500, 500])
    up2 = api.SupabaseUploader(url="https://x", key="K", data_path="P", client=cl2, sleep=nosleep)
    assert await up2.put("a.json", "{}") is False and up2.failures == 1


async def test_api_hook_signature(tmp_path, monkeypatch):
    import main
    hook = api.make_api_hook(uploader_factory=lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    c, s, bus = env(tmp_path)
    await hook(c, s, bus, {})  # publisher disabled, no telegram -> returns without raising
