"""ACCEPTANCE: links in every rendered surface (repo, pushes, API payload JSON, web + Pages copy) and the notifier."""
import json, pathlib, re
import httpx, pytest
import api, db, links, notifier
from agents.executor import Executor
from agents.risk import Risk
from fakes import FakeNet, Clock, NOW, nosleep, env, pair

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = pathlib.Path("/workspace/pages")
FOMO = re.compile(r"https?://(?:www\.)?fomo\.family[^\s\"'<>)\\]*")
SHORT = re.compile(r"\b(t\.co|bit\.ly|tinyurl\.com|goo\.gl|ow\.ly|is\.gd|buff\.ly|rebrand\.ly|cutt\.ly|t\.ly|shorturl\.at)/")
MINT = "Mint1111111111111111111111111111111pump"


def check_text(label, text):
    assert not SHORT.search(text), f"shortener in {label}"
    for u in FOMO.findall(text):
        assert "r=savipww" in u or u == links.SIGNUP_URL, f"{label}: FOMO url without ref"
    return FOMO.findall(text)


def mock_notifier(monkeypatch, sent):
    monkeypatch.setenv("TG_TOKEN", "x"); monkeypatch.setenv("OWNER_CHAT_ID", "1")
    monkeypatch.setattr(notifier, "load_env", lambda *a, **k: None)
    def handler(req):
        sent.append(json.loads(req.content)); return httpx.Response(200, json={"ok": True})
    return notifier.Notifier("PAPER", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_notify_paper_push_exact_link(tmp_path, monkeypatch):
    sent = []; n = mock_notifier(monkeypatch, sent)
    c, s, bus = env(tmp_path); net = FakeNet(); net.pairs = [pair(MINT, price=0.001, liq=60000)]
    ex = Executor(c, s, bus, net, n, clock=Clock(NOW))
    await ex.handle({"mint": MINT, "score": 80, "scan": {"name": "Alpha", "liquidityUsd": 60000}})
    assert len(sent) == 1, "exactly one push"
    p = sent[0]
    assert p["text"].startswith("PAPER · Buy Alpha")
    btn = p["reply_markup"]["inline_keyboard"][0][0]
    assert btn["url"] == links.coin_url(MINT, "solana")
    check_text("push", json.dumps(p))


async def test_links_in_all_rendered_pushes(tmp_path, monkeypatch):
    sent = []; n = mock_notifier(monkeypatch, sent)
    for b in ([notifier.signup_button()], [notifier.profile_button("p", "@fulfilling")],
              [notifier.coin_button("x", MINT, ch) for ch in links.CHAIN_ID], [notifier.coin_button("x", MINT, "unknownchain")]):
        assert await n.send("t", b)
    urls = [u for p in sent for u in check_text("push", json.dumps(p))]
    assert links.SIGNUP_URL in urls  # unknown chain -> SIGNUP_URL
    assert sent[-1]["reply_markup"]["inline_keyboard"][0][0]["url"] == links.SIGNUP_URL
    assert len(urls) >= 7


async def test_links_in_api_payloads(tmp_path, monkeypatch):
    for k, v in {"SUPABASE_SECRET_KEY": "sb_secret_T", "TG_TOKEN": "1:TG", "DATA_PATH": "pathX", "OWNER_CHAT_ID": "99"}.items():
        monkeypatch.setenv(k, v)
    c, s, bus = env(tmp_path)
    db.insert(c, "settings", key="night_start", value=json.dumps(NOW - 3600), updated_at=NOW)
    db.upsert_coin(c, MINT, name="Alpha")
    db.insert(c, "signals", mint=MINT, ts=NOW - 100, price=1.0, size_usd=10, stack_pct=5, link=links.coin_url(MINT), mode="PAPER", status="sent")
    db.insert(c, "audits", mint=MINT, ts=NOW - 50, layer="1", name="mint authority", value_text="null", **{"pass": 1}, source="s")
    db.activity(c, "bought", MINT, "executor", "Bought Alpha", "", "")
    up = api.FakeUploader(); net = FakeNet(); net.pairs = [pair(MINT)]
    pub = api.Publisher(c, s, {}, up, net=net, clock=Clock(NOW), sleep=nosleep)
    await pub.publish_once()
    assert up.files
    urls = [u for f, body in up.files.items() for u in check_text(f, body)]
    assert links.coin_url(MINT) in urls, "coin payload carries the links.py URL"


def test_web_and_pages_and_repo_files():
    n = 0
    for base in (ROOT, PAGES):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not {".venv", "__pycache__", ".git", ".pytest_cache", "logs"} & set(p.parts) \
                    and p.name != ".secrets.env" and p.suffix in (".py", ".js", ".html", ".json", ".css", ".md", ".txt"):
                check_text(str(p), p.read_text(errors="ignore")); n += 1
    assert n > 20
