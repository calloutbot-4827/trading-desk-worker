"""Playwright screenshots of web/ against local mock data (tests/mock_d). Writes /workspace/desk/shots/*.png"""
import subprocess, sys, time, pathlib
from playwright.sync_api import sync_playwright
ROOT = pathlib.Path(__file__).resolve().parents[1]; OUT = ROOT / "shots"; OUT.mkdir(exist_ok=True)
PY = sys.executable
srv = [subprocess.Popen([PY, str(ROOT / "tests/serve_mock.py"), str(d), str(p)]) for d, p in
       ((ROOT / "web", 8700), (ROOT / "tests/mock_d/full", 8701), (ROOT / "tests/mock_d/empty", 8702))]
time.sleep(1)
DOG, RUG, CHS = "EXAMPLEdog1111111111111111111111111111111", "EXAMPLErug3333333333333333333333333333333", "EXAMPLEchs5555555555555555555555555555555"
def app(port): return f"http://127.0.0.1:8700/index.html?base=http://127.0.0.1:{port}/"
try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        def page(port, reduced=False):
            ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, reduced_motion="reduce" if reduced else "no-preference")
            pg = ctx.new_page(); pg.on("console", lambda m: print("console:", m.text) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: print("PAGEERROR", e)); pg.goto(app(port)); pg.wait_for_timeout(1500); return pg
        def shot(pg, name, full=True):
            if full:  # tall viewport instead of full_page so the fixed tab bar sits at the real bottom
                h = pg.evaluate("document.documentElement.scrollHeight"); pg.set_viewport_size({"width": 390, "height": max(844, h)}); pg.wait_for_timeout(400)
            pg.screenshot(path=str(OUT / f"{name}.png")); print("shot", name)
            pg.set_viewport_size({"width": 390, "height": 844}); pg.wait_for_timeout(200)
        def overlay(pg, name):  # overlay is fixed: capture its full scroll height
            h = pg.evaluate("document.querySelector('#overlay').scrollHeight")
            pg.set_viewport_size({"width": 390, "height": max(844, h)}); pg.wait_for_timeout(400); shot(pg, name, False)
        for kind, port in (("", 8701), ("empty-", 8702)):
            pg = page(port)
            shot(pg, kind + "home")
            if kind == "": pg.click("[data-range=ALL]"); pg.wait_for_timeout(500); shot(pg, "home-all-range", False); pg.click("[data-range=LIVE]")
            for i, n in enumerate(["scanner", "auditor", "analyst", "executor", "risk"]):
                pg.evaluate(f"document.querySelectorAll('.acard')[{i}].scrollIntoView({{block:'center',inline:'start'}})"); pg.wait_for_timeout(300); pg.evaluate("document.querySelector('.tabbar').style.display='none'")
                pg.locator(".acard").nth(i).screenshot(path=str(OUT / f"{kind}card-{n}.png")); print("shot card", n)
            if kind:
                for i, n in enumerate(["scanner", "auditor", "analyst", "executor", "risk"]):
                    pg.evaluate(f"document.querySelectorAll('.acard')[{i}].scrollIntoView({{block:'center',inline:'start'}})"); pg.wait_for_timeout(300); pg.evaluate("document.querySelector('.tabbar').style.display='none'")
                    pg.locator(".acard").nth(i).screenshot(path=str(OUT / f"{kind}card-{n}.png"))
            pg.evaluate("document.querySelector('.tabbar').style.display=''")
            pg.click("[data-tab=agents]"); pg.wait_for_timeout(300); shot(pg, kind + "agents")
            pg.click("[data-tab=activity]"); pg.wait_for_timeout(300); shot(pg, kind + "activity")
            pg.click("[data-tab=account]"); pg.wait_for_timeout(300); shot(pg, kind + "account")
            if kind == "":
                pg.click("[data-tab=activity]"); pg.click(f"[data-audit='{RUG}']"); pg.wait_for_timeout(4600); overlay(pg, "auditor-rejected")
                pg.evaluate(f"desk.open('auditor','{DOG}')"); pg.wait_for_timeout(4600); overlay(pg, "auditor-cleared")
                pg.click("[data-back]"); pg.click("[data-tab=activity]"); pg.click(f".row[data-coin='{DOG}']"); pg.wait_for_timeout(800); overlay(pg, "coin")
                pg.evaluate(f"desk.open('coin','{CHS}')"); pg.wait_for_timeout(800); overlay(pg, "coin-no-position")
                pg.click("[data-back]"); pg.click("[data-tab=home]"); pg.click("[data-search]"); pg.fill("#q", "dog"); pg.wait_for_timeout(300); shot(pg, "home-search", False)
            else:
                pg.evaluate(f"desk.open('auditor','{RUG}')"); pg.wait_for_timeout(800); overlay(pg, "empty-auditor")
                pg.evaluate(f"desk.open('coin','{DOG}')"); pg.wait_for_timeout(800); overlay(pg, "empty-coin")
        # loading: data server never answers -> skeleton
        ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2); pg = ctx.new_page()
        pg.route(lambda u: u.startswith("http://127.0.0.1:8701/"), lambda r: None)  # hang
        pg.goto(app(8701)); pg.wait_for_timeout(800); shot(pg, "loading-home")
        # error: last good numbers kept, then server goes down
        pg = page(8701)
        pg.route(lambda u: u.startswith("http://127.0.0.1:8701/"), lambda r: r.abort())
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))"); pg.wait_for_timeout(2500)
        shot(pg, "error-home", False)
        for t in ("agents", "activity", "account"):
            pg.click(f"[data-tab={t}]"); pg.wait_for_timeout(300); shot(pg, f"error-{t}", False)
        pg.evaluate(f"desk.open('coin','{DOG}')"); pg.wait_for_timeout(800); shot(pg, "error-coin", False)
        pg.evaluate(f"desk.open('auditor','{RUG}')"); pg.wait_for_timeout(4600); shot(pg, "error-auditor", False)
        # error with no data ever: server down from the start
        pg = page(8709); shot(pg, "error-home-never-loaded", False)
        # reduced motion
        pg = page(8701, reduced=True); pg.evaluate(f"desk.open('auditor','{RUG}')"); pg.wait_for_timeout(300); shot(pg, "reduced-auditor", False)
        b.close()
finally:
    for s in srv: s.kill()
