"""Freeze fixes: ws supervision, keepalive config, net hard timeouts, agent supervisor, loop-block watchdog."""
import asyncio, time
import pytest
import db, main
import agents.scanner as scmod
from agents.scanner import Scanner
from agents.common import Net, NetError
from tests.fakes import env, FakeNet, nosleep


class HangCM:
    """Async CM whose open (or recv) never returns: simulates a wedged websocket."""
    def __init__(self, hang_open=False, msgs=()):
        self.hang_open, self.msgs, self.closed = hang_open, list(msgs), False
    async def __aenter__(self):
        if self.hang_open:
            await asyncio.Event().wait()
        return self
    async def __aexit__(self, *a):
        self.closed = True
    async def send(self, m): pass
    async def recv(self):
        if self.msgs:
            return self.msgs.pop(0)
        await asyncio.Event().wait()


def test_keepalive_config():
    assert scmod.WS_KEEPALIVE["ping_interval"] == 20 and scmod.WS_KEEPALIVE["ping_timeout"] == 20
    assert scmod.WS_KEEPALIVE["open_timeout"] and scmod.WS_KEEPALIVE["close_timeout"]
    assert scmod.WS_SILENCE_S >= 600


async def test_hung_ws_open_times_out_and_reconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(scmod, "WS_OPEN_TIMEOUT_S", 0.05)
    c, s, bus = env(tmp_path); cms = []
    def connect():
        cms.append(HangCM(hang_open=True)); return cms[-1]
    sc = Scanner(c, s, bus, FakeNet(), ws_connect=connect, sleep=nosleep)
    t = asyncio.create_task(sc.ws_loop()); await asyncio.sleep(0.3); t.cancel()
    assert len(cms) >= 3 and sc.reconnects >= 3  # kept retrying instead of hanging forever


async def test_hung_ws_does_not_stall_other_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(scmod, "WS_SILENCE_S", 0.05)
    c, s, bus = env(tmp_path)
    sc = Scanner(c, s, bus, FakeNet(), ws_connect=lambda: HangCM(), sleep=nosleep)
    beats = []
    async def other():
        while True:
            beats.append(time.time()); await asyncio.sleep(0.01)
    t1, t2 = asyncio.create_task(sc.ws_loop()), asyncio.create_task(other())
    await asyncio.sleep(0.3); t1.cancel(); t2.cancel()
    assert len(beats) > 15 and sc.reconnects >= 2


async def test_net_hard_timeout(monkeypatch):
    class Slow:
        async def get(self, url):
            await asyncio.sleep(10)
    n = Net("http://x", client=Slow()); n.TOTAL_TIMEOUT = 0.05
    with pytest.raises(NetError):
        await n.get_json("http://y")


async def test_supervisor_restarts_stale_and_dead(tmp_path):
    c = db.connect(str(tmp_path / "d.db")); runs = {"stale": 0, "dead": 0, "ok": 0}
    async def stale():
        runs["stale"] += 1; await asyncio.Event().wait()   # never heartbeats
    async def dead():
        runs["dead"] += 1; raise RuntimeError("boom")
    async def ok():
        runs["ok"] += 1
        while True:
            db.heartbeat(c, "ok"); await asyncio.sleep(0.01)
    sp = main.Supervisor(c, {"stale": stale, "dead": dead, "ok": ok}, stale_s=0.1,
                         stall_path=tmp_path / "stall.log", max_restarts=100)
    t = asyncio.create_task(sp.run(every=0.05)); await asyncio.sleep(0.6); t.cancel()
    assert runs["stale"] >= 2 and runs["dead"] >= 2 and runs["ok"] == 1
    log = (tmp_path / "stall.log").read_text()
    assert "heartbeat" in log and "RuntimeError" in log


async def test_supervisor_exits_after_too_many_restarts(tmp_path):
    c = db.connect(str(tmp_path / "d.db"))
    async def dead():
        raise RuntimeError("boom")
    sp = main.Supervisor(c, {"dead": dead}, stall_path=tmp_path / "s.log", max_restarts=2)
    with pytest.raises(SystemExit):
        await asyncio.wait_for(sp.run(every=0.01), 2)


async def test_loop_block_watchdog_dumps_stacks(tmp_path):
    p = tmp_path / "stall.log"
    t = asyncio.create_task(main.loop_block_watchdog(p, block_s=0.2, every=0.05))
    await asyncio.sleep(0.1)
    time.sleep(0.5)  # deliberately block the loop
    await asyncio.sleep(0); t.cancel()
    await asyncio.sleep(0.05)
    assert "Thread" in p.read_text() or "Stack" in p.read_text()
