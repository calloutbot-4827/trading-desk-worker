import json
import pytest
import db, links, telegram
from agents.executor import Executor
from agents.risk import Risk
from fakes import FakeNet, FakeNotifier, Clock, NOW, nosleep, env

OWNER = "4242"
M1, M2, M3 = "Mint1111111111111111111111111111111", "Mint2222222222222222222222222222222", "Mint3333333333333333333333333333333"


class R:
    def __init__(self, j): self.status_code = 200; self._j = j
    def json(self): return self._j


class TGClient:
    """Mocked Telegram HTTP: records sendMessage payloads, serves queued getUpdates."""
    def __init__(self): self.sent = []; self.updates = []; self.calls = []
    async def post(self, url, json=None):
        method = url.rsplit("/", 1)[1]; self.calls.append(method)
        if method == "getUpdates":
            u, self.updates = self.updates, []
            return R({"ok": True, "result": u})
        if method == "sendMessage":
            self.sent.append(json)
        return R({"ok": True, "result": {}})


class FakeAuditor:
    def __init__(self): self.calls = []
    async def audit(self, scan):
        self.calls.append(scan)
        return {"layers": [{"n": i, "name": f"L{i}", "pass": True, "value": f"ok{i}"} for i in range(1, 9)]}


class Ag:
    paused = False


@pytest.fixture
def T(tmp_path, monkeypatch):
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    c, s, bus = env(tmp_path); clock = Clock(NOW)
    db.insert(c, "settings", key="night_start", value=json.dumps(NOW - 3600), updated_at=NOW)
    db.upsert_coin(c, M1, name="Alpha", symbol="ALP"); db.upsert_coin(c, M2, name="Dup", symbol="D1"); db.upsert_coin(c, M3, name="Dup", symbol="D2")
    n = FakeNotifier(); net = FakeNet()
    agents = {k: Ag() for k in ("scanner", "auditor", "analyst")}
    agents["executor"] = Executor(c, s, bus, net, n, clock=clock, sleep=nosleep)
    agents["risk"] = Risk(c, s, net, n, clock=clock, sleep=nosleep)
    cl = TGClient(); fa = FakeAuditor()
    t = telegram.Telegram(c, s, bus, agents, client=cl, token="T", owner=OWNER, clock=clock, sleep=nosleep,
                          auditor_factory=lambda: fa, secrets_path=tmp_path / "s.env")
    return t, cl, c, s, fa, agents, n


def msg(chat, text, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


def last(cl):
    return cl.sent[-1]


def urls(p):
    return [r[0].get("url") for r in p.get("reply_markup", {}).get("inline_keyboard", [])]


async def test_owner_lock_ignores_strangers(T):
    t, cl, *_ = T
    await t.handle_update(msg("999", "status"))
    assert cl.sent == []
    await t.handle_update(msg(OWNER, "status"))
    assert last(cl)["chat_id"] == OWNER


async def test_first_start_claims_owner(T, tmp_path, monkeypatch):
    t, cl, *_ = T
    t.owner = None
    (tmp_path / "s.env").write_text("TG_TOKEN=T\n")  # local mode: an existing secrets file also gets the owner
    await t.handle_update(msg("555", "hello"))
    assert cl.sent == [] and t.owner is None
    await t.handle_update(msg("555", "/start"))
    assert t.owner == "555" and "OWNER_CHAT_ID=555" in (tmp_path / "s.env").read_text()
    await t.handle_update(msg("666", "/start"))
    assert len(cl.sent) == 1


async def test_poll_once_offset(T):
    t, cl, *_ = T
    cl.updates = [msg(OWNER, "invite", 7)]
    assert await t.poll_once() and t.offset == 8 and urls(last(cl)) == [links.SIGNUP_URL]
    assert t.connected()


async def test_unknown_text_shows_commands(T):
    t, cl, *_ = T
    await t.command("what is up")
    assert "Commands:" in last(cl)["text"] and last(cl)["text"].startswith("PAPER · ")


async def test_set_stop_and_out_of_range(T):
    t, cl, c, s, *_ = T
    await t.command("set stop to -8%")
    assert last(cl)["text"] == "PAPER · stop -6% -> -8%" and s.get("risk.stop_pct") == 8
    await t.command("/set stop -80%")
    assert "Refused" in last(cl)["text"] and "-1%" in last(cl)["text"] and "-50%" in last(cl)["text"]
    assert s.get("risk.stop_pct") == 8
    await t.command("set analyst.min_score to 150")
    assert "between 0 and 100" in last(cl)["text"]
    await t.command("set nonsense to 3")
    assert "No setting" in last(cl)["text"]


async def test_ticket_warnings(T):
    t, cl, c, s, *_ = T
    await t.command("stack 100")  # PAPER stack 100 * 8% = 8 < 47.5 -> bumped
    assert "stack 200 -> 100" in last(cl)["text"] and "tickets bumped to $47.50" in last(cl)["text"]
    await t.command("set executor.below_min to skip")
    assert "every coin will be skipped" in last(cl)["text"]
    await t.command("set stack to 50")
    assert "8% -> 50%" in last(cl)["text"] and "⚠️" not in last(cl)["text"]


async def test_pause_resume_and_risk_lock(T):
    t, cl, c, s, fa, agents, n = T
    await t.command("pause Scanner")
    assert agents["scanner"].paused and "paused" in last(cl)["text"]
    await t.command("resume scanner")
    assert not agents["scanner"].paused
    db.insert(c, "positions", mint=M1, opened_at=NOW, entry_price=1, size_usd=50, peak_price=1, stop_price=.94, stop_pct=6, mode="PAPER")
    await t.command("pause Risk")
    assert "can't pause" in last(cl)["text"] and not agents["risk"].paused
    await t.command("pause Bob")
    assert "No agent" in last(cl)["text"]


async def test_status_buttons(T):
    t, cl, c, *_ = T
    db.insert(c, "positions", mint=M1, opened_at=NOW, entry_price=1, size_usd=50, peak_price=1, stop_price=.94, stop_pct=6, mode="PAPER")
    await t.command("status")
    p = last(cl)
    assert "PAPER · balance $200.00" in p["text"] and "1 open position" in p["text"]
    assert urls(p) == [links.coin_url(M1)] and "Sell Alpha on FOMO" == p["reply_markup"]["inline_keyboard"][0][0]["text"]


async def test_buy_pushed_tonight_resends(T):
    t, cl, c, s, fa, *_ = T
    db.insert(c, "signals", mint=M1, ts=NOW - 60, price=1, size_usd=47.5, stack_pct=23.8, link=links.coin_url(M1), mode="PAPER", status="sent")
    await t.command("buy ALP")
    p = last(cl)
    assert p["text"].startswith("PAPER · Buy Alpha · $47.50") and urls(p)[0] == links.coin_url(M1)
    assert [r[0].get("callback_data") for r in p["reply_markup"]["inline_keyboard"]][1:] == [f"bought:{M1}", f"skip:{M1}"]
    assert fa.calls == []


async def test_buy_never_pushed_audits_now(T):
    t, cl, c, s, fa, *_ = T
    await t.command("buy alpha")
    p = last(cl)
    assert len(fa.calls) == 1 and "ok8" in p["text"] and "not sized, not on the desk" in p["text"]
    assert urls(p) == [links.coin_url(M1)] and p["reply_markup"]["inline_keyboard"][0][0]["text"] == "Buy on FOMO"
    assert c.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0 and not db.open_positions(c)


async def test_buy_unknown_and_ambiguous(T):
    t, cl, *_ = T
    await t.command("buy zzz")
    assert "Send me its mint" in last(cl)["text"]
    await t.command("buy Dup")
    p = last(cl)
    assert "2 coins match" in p["text"] and set(urls(p)) == {links.coin_url(M2), links.coin_url(M3)}


async def test_why_and_sell(T):
    t, cl, c, *_ = T
    db.insert(c, "scans", mint=M1, ts=NOW, verdict="kill", reason="liq $1.0K")
    await t.command("why " + M1)
    assert "Scanner: kill · liq $1.0K" in last(cl)["text"] and urls(last(cl)) == [links.coin_url(M1)]
    await t.command("sell alpha")
    assert "No open position" in last(cl)["text"]
    db.insert(c, "positions", mint=M1, opened_at=NOW, entry_price=1, size_usd=50, peak_price=1, stop_price=.94, stop_pct=6, mode="PAPER")
    await t.command("sell alpha")
    p = last(cl)
    assert urls(p)[0] == links.coin_url(M1) and p["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == f"sold:{M1}"


async def test_callbacks(T):
    t, cl, c, s, fa, agents, n = T
    db.insert(c, "positions", mint=M1, opened_at=NOW, entry_price=1, size_usd=50, peak_price=1, stop_price=.94, stop_pct=6, mode="PAPER")
    db.insert(c, "prices", mint=M1, ts=NOW, price=1.1, volume_m5=1)
    u = {"update_id": 3, "callback_query": {"id": "q", "data": f"sold:{M1}", "message": {"chat": {"id": int(OWNER)}}}}
    assert await t.handle_update(u) == "Sold, position closed"
    assert not db.open_positions(c) and c.execute("SELECT exit_reason FROM positions").fetchone()[0] == "manual"
    assert "Sold Alpha" in n.sent[-1][0]
    assert await t.on_callback(f"skip:{M2}") == "Skipped for 24h" and db.is_skipped(c, M2)
    u["callback_query"]["message"]["chat"]["id"] = 1
    assert await t.handle_update(u) is None  # stranger's callback ignored


async def test_paper_live(T):
    t, cl, c, s, *_ = T
    await t.command("live")
    assert "Type LIVE" in last(cl)["text"]
    await t.command("LIVE")
    assert "Refused" in last(cl)["text"] and s.get("desk.mode") == "PAPER"
    db.insert(c, "balance", ts=NOW - 9 * 3600, value=200); db.insert(c, "balance", ts=NOW, value=210)
    await t.command("LIVE")  # not armed anymore
    assert s.get("desk.mode") == "PAPER"
    await t.command("live"); await t.command("LIVE")
    assert s.get("desk.mode") == "LIVE" and last(cl)["text"] == "mode PAPER -> LIVE"
    await t.command("paper")
    assert s.get("desk.mode") == "PAPER"


async def test_report_and_invite(T):
    t, cl, c, *_ = T
    await t.command("report")
    assert "No morning report" in last(cl)["text"]
    db.insert(c, "settings", key="last_report_url", value=json.dumps("https://example.com/r"), updated_at=NOW)
    await t.command("/report")
    assert urls(last(cl)) == ["https://example.com/r"]
