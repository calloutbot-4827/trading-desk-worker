import pytest
from agents.analyst import gate, score_of, rules_answers, Analyst
from tests.fakes import env, FakeNet, pair, Clock
from config import Settings

S = Settings().get
OK = {"alive_6h": 0.70, "organic": 0.65, "flush": 1.0, "chase": 0.30}


@pytest.mark.parametrize("k,v,rule", [("alive_6h", 0.69, "alive_6h 0.69"), ("organic", 0.64, "organic 0.64"), ("chase", 0.31, "chase 0.31")])
def test_each_rule_rejects(k, v, rule):
    a = {**OK, k: v}
    assert gate(a, 80, S) == [rule]


def test_score_69_rejects_70_passes():
    assert gate(OK, 69, S) == ["score 69"]
    assert gate(OK, 70, S) == []


def test_all_at_threshold_pass():
    assert score_of(OK) == 76 and gate(OK, score_of(OK), S) == []


def test_note_threshold_without_flush_cannot_pass():
    a = {**OK, "flush": 0.0}
    assert score_of(a) == 51 and gate(a, 51, S) == ["score 51"]


def mk(**kw):  # pair whose rules answers are the threshold values; flush=1 (m5 -5, buys>=sells)
    base = dict(vh1=17500, bh1=98, h1=24, m5=-5, bm5=10, sm5=10)
    base.update(kw); return pair("M", **base)


@pytest.mark.parametrize("kw,first", [({"vh1": 17250}, "alive_6h 0.69"), ({"bh1": 96}, "organic 0.64"),
                                      ({"h1": 24.8}, "chase 0.31"), ({"m5": 1, "vh1": 25000, "bh1": 150, "h1": 19.2}, "score 69")])
async def test_rules_mode_rejects(tmp_path, kw, first):
    c, s, bus = env(tmp_path); net = FakeNet(); net.pairs = [mk(**kw)]
    an = Analyst(c, s, bus, net, clock=Clock())
    out = await an.score({"mint": "M", "layers": [], "scan": {"name": "Coin"}})
    assert out["passed"] is False and out["failed"][0] == first and out["model"] == "rules" and out["inputTokens"] == 0
    row = c.execute("SELECT title, subtitle, value FROM activity").fetchone()
    assert tuple(row) == ("Rejected Coin", f"Analyst (rules) · {first}", "Skipped")
    assert bus.q["executor"].empty()
    assert await an.score({"mint": "M", "layers": []}) is None  # never re-score


async def test_rules_mode_at_threshold_passes(tmp_path):
    c, s, bus = env(tmp_path); net = FakeNet(); net.pairs = [mk(h1=24)]
    out = await Analyst(c, s, bus, net, clock=Clock()).score({"mint": "M", "layers": [], "scan": {"name": "Coin"}})
    assert out["answers"] == {"alive_6h": 0.7, "organic": 0.6533, "flush": 1.0, "chase": 0.3}
    assert out["passed"] and out["score"] == 76 and not bus.q["executor"].empty()


def test_bundled_penalty():
    a = rules_answers(pair("M", bh1=150), 3, S)
    assert a["organic"] == 0.5


async def test_jev_mode_without_key_rejects_no_answer(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    c, s, bus = env(tmp_path); s.set("analyst.mode", "llm"); net = FakeNet(); net.pairs = [mk()]
    out = await Analyst(c, s, bus, net, clock=Clock()).score({"mint": "M", "layers": [], "scan": {"name": "Coin"}})
    assert out["failed"] == ["no answer"] and not out["passed"]
