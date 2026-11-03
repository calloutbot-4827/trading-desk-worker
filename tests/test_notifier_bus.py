import asyncio, pytest, httpx
import links, notifier
from bus import Bus


class FakeClient:
    def __init__(self): self.calls = []
    async def post(self, url, json):
        self.calls.append(json); return httpx.Response(200)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "x"); monkeypatch.setenv("OWNER_CHAT_ID", "1")
    monkeypatch.setattr(notifier, "load_env", lambda *a, **k: None)


async def test_paper_prefix_and_buttons():
    fc = FakeClient(); n = notifier.Notifier("PAPER", client=fc)
    assert await n.send("bought X", [notifier.coin_button("Open", "MINT")])
    p = fc.calls[0]
    assert p["text"] == "PAPER · bought X"
    assert p["reply_markup"]["inline_keyboard"][0][0]["url"] == links.coin_url("MINT")


async def test_rejects_handbuilt_url():
    n = notifier.Notifier("PAPER", client=FakeClient())
    with pytest.raises(ValueError):
        await n.send("x", [{"text": "a", "url": "https://" + "fomo.family/coin?address=M"}])


async def test_bus_chain():
    b = Bus()
    await b.publish("scanner", 1); assert await b.consume("auditor") == 1
    await b.publish("executor", 2); assert await b.consume("risk") == 2
    with pytest.raises(ValueError):
        await b.publish("risk", 3)
