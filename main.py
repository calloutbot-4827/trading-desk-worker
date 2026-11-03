"""One process: agents + plan-B JSON publisher (api.py) + Telegram long polling (telegram.py)."""
import asyncio, faulthandler, io, os, signal, sys, time, traceback
from pathlib import Path
import db, notifier
from bus import Bus
from config import Settings
from agents.common import Net, LOG_DIR, LOG_MAX_BYTES, cap_dir
from agents.scanner import Scanner
from agents.auditor import Auditor
from agents.analyst import Analyst
from agents.executor import Executor
from agents.risk import Risk


DB_PATH = os.environ.get("DESK_DB_PATH") or "desk.db"


def build(path=None):
    path = path or DB_PATH
    Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
    c = db.connect(path); s = Settings(c); bus = Bus()
    net = Net(s.get("desk.rpc_url")); n = notifier.Notifier(s.get("desk.mode"))
    agents = {"scanner": Scanner(c, s, bus, net), "auditor": Auditor(c, s, bus, net, notifier=n),
              "analyst": Analyst(c, s, bus, net), "executor": Executor(c, s, bus, net, n), "risk": Risk(c, s, net, n)}
    return c, s, bus, agents


STALL_LOG = LOG_DIR / "stall.log"
HB_STALE_S = 120        # an agent heartbeat older than this gets its task restarted
LOOP_BLOCK_S = 60       # event loop blocked this long -> all thread stacks dumped to stall.log
MAX_RESTARTS = 5        # per agent within RESTART_WINDOW_S, then exit non-zero (supervisor restarts process)
RESTART_WINDOW_S = 1800


def stall(msg, path=None):
    p = Path(path or STALL_LOG); p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.stat().st_size > LOG_MAX_BYTES:  # keep one previous file, bounded
            os.replace(p, p.with_name(p.name + ".1"))
    except OSError:
        pass
    with open(p, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def task_stack(task):
    buf = io.StringIO(); task.print_stack(limit=30, file=buf); return buf.getvalue()


def dump_all_tasks(path=None):
    lines = [f"--- {t.get_name()}\n{task_stack(t)}" for t in asyncio.all_tasks()]
    stall("asyncio task dump\n" + "\n".join(lines), path)


async def loop_block_watchdog(path=None, block_s=LOOP_BLOCK_S, every=5):
    """Re-arms faulthandler while the loop is responsive; if the loop blocks > block_s, the C-level
    timer thread dumps every thread's stack to stall.log (works even when Python code is wedged)."""
    Path(path or STALL_LOG).parent.mkdir(parents=True, exist_ok=True)
    f = open(path or STALL_LOG, "a")
    try:
        while True:
            faulthandler.dump_traceback_later(block_s, repeat=False, file=f, exit=False)
            await asyncio.sleep(every)
    finally:
        faulthandler.cancel_dump_traceback_later(); f.close()


class Supervisor:
    """One task per agent; dead or heartbeat-stale tasks are logged (with stack) and restarted."""
    def __init__(self, conn, factories, stale_s=HB_STALE_S, clock=time.time, stall_path=None,
                 max_restarts=MAX_RESTARTS, window_s=RESTART_WINDOW_S):
        self.c, self.f, self.stale_s, self.clock, self.path = conn, factories, stale_s, clock, stall_path
        self.max_restarts, self.window_s = max_restarts, window_s
        self.tasks, self.restarts, self.started = {}, {}, {}

    def start(self, name):
        self.tasks[name] = asyncio.create_task(self.f[name](), name=name); self.started[name] = self.clock()

    def hb_age(self, name):
        r = self.c.execute("SELECT ts FROM heartbeats WHERE agent=?", (name,)).fetchone()
        return self.clock() - r[0] if r else None

    async def restart(self, name, why):
        t = self.tasks[name]
        stall(f"{name}: {why}; restarting\n{task_stack(t) if not t.done() else ''}", self.path)
        if not t.done():
            t.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(t), 10)
            except BaseException:
                pass
        now = self.clock(); hist = [x for x in self.restarts.get(name, []) if now - x < self.window_s] + [now]
        self.restarts[name] = hist
        if len(hist) > self.max_restarts:
            stall(f"{name}: {len(hist)} restarts in {self.window_s}s; exiting for process supervisor", self.path)
            raise SystemExit(3)
        self.start(name)

    async def check(self):
        for name, t in list(self.tasks.items()):
            if t.done():
                exc = None if t.cancelled() else t.exception()
                await self.restart(name, f"task ended ({type(exc).__name__ if exc else 'no exception'})"
                                   + ("\n" + "".join(traceback.format_exception(exc)) if exc else ""))
                continue
            age = self.hb_age(name)
            if age is None:
                age = self.clock() - self.started[name]
            if age > self.stale_s and self.clock() - self.started[name] > self.stale_s:
                await self.restart(name, f"heartbeat {age:.0f}s old")

    async def run(self, every=15):
        for n in self.f:
            self.start(n)
        n = 0
        while True:
            await asyncio.sleep(every)
            await self.check()
            n += 1
            if n % 240 == 1:
                prune_logs()


def prune_logs():
    for sub in ("raw", "replay"):
        cap_dir(LOG_DIR / sub)


async def main(api_hook=None):
    prune_logs()
    c, s, bus, a = build()
    factories = {"scanner": a["scanner"].run, "auditor": a["auditor"].run, "analyst": a["analyst"].run,
                 "executor": a["executor"].run, "risk": lambda: a["risk"].run(bus)}
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, dump_all_tasks)  # kill -USR1 <pid>
    except (NotImplementedError, RuntimeError):
        pass
    tasks = [Supervisor(c, factories).run(), loop_block_watchdog()]
    if api_hook is not None:  # HOOK for the API writer: api_hook(conn, settings, bus, agents) -> coroutine
        tasks.append(api_hook(c, s, bus, a))
    await asyncio.gather(*tasks)


def default_hook():
    import api, telegram
    return api.make_api_hook(telegram_factory=lambda c, s, bus, a: telegram.Telegram(c, s, bus, a))


if __name__ == "__main__":
    asyncio.run(main(default_hook()))
