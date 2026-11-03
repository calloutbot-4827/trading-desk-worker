"""Tiny stdlib HTTP keepalive for hosts that need an open port (Render free web service).
Runs in run.py's process so it keeps answering while main.py restarts. Body: uptime, mode, heartbeat ages. No secrets."""
import json, os, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTED = time.time()


def status(db_path):
    out = {"ok": True, "uptime_s": round(time.time() - STARTED), "mode": "PAPER", "heartbeat_age_s": {}}
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            r = c.execute("SELECT value FROM settings WHERE key='desk.mode'").fetchone()
            if r and json.loads(r[0]) in ("PAPER", "LIVE"):
                out["mode"] = json.loads(r[0])
            now = time.time()
            out["heartbeat_age_s"] = {a: round(now - ts) for a, ts in c.execute("SELECT agent, ts FROM heartbeats")
                                      if isinstance(a, str) and ts is not None}
        finally:
            c.close()
    except Exception:
        out["db"] = "unavailable"
    return out


def serve(port, db_path, host="0.0.0.0"):
    class H(BaseHTTPRequestHandler):
        def _send(self, body_ok):
            if self.path.split("?")[0] not in ("/", "/health"):
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            body = (json.dumps(status(db_path), separators=(",", ":")) + "\n").encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            if body_ok:
                self.wfile.write(body)

        def do_GET(self): self._send(True)
        def do_HEAD(self): self._send(False)
        def log_message(self, *a): pass

    srv = ThreadingHTTPServer((host, int(port)), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="health", daemon=True).start()
    return srv
