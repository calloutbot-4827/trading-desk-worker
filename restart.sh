#!/usr/bin/env bash
# Idempotent: stop this desk's main.py (if any), start a fresh one, verify heartbeats + a fresh publish.
set -u
DESK=/workspace/desk
cd "$DESK" || exit 1
mkdir -p logs
# stop the auto-restart loop first so it can't respawn the desk we're about to kill
for pid in $(pgrep -f 'bash (\./|/workspace/desk/)?supervise\.sh'); do
  [ "$(readlink /proc/$pid/cwd)" = "$DESK" ] && kill "$pid"
done
# exact cmdline match of this desk's process (cwd checked too, so other main.py's are untouched)
for pid in $(pgrep -fx '\.venv/bin/python main\.py|/workspace/desk/\.venv/bin/python /workspace/desk/main\.py'); do
  [ "$(readlink /proc/$pid/cwd)" = "$DESK" ] && kill "$pid"
done
for i in $(seq 1 20); do
  pgrep -fx '\.venv/bin/python main\.py' >/dev/null || break; sleep 0.5
done
pgrep -fx '\.venv/bin/python main\.py' >/dev/null && { echo "old desk still running after 10s; SIGKILL"; pkill -9 -fx '\.venv/bin/python main\.py'; sleep 1; }
START=$(date +%s)
set -a; source .secrets.env; set +a
nohup setsid bash ./supervise.sh >/dev/null 2>&1 < /dev/null &
SUP=$!
echo "started supervisor pid $SUP"
sleep 20
NEW=$(pgrep -fx '\.venv/bin/python main\.py' | head -1)
if [ -z "$NEW" ] || ! kill -0 "$SUP" 2>/dev/null; then echo "FAIL: desk exited"; tail -20 logs/main.out; exit 1; fi
echo "desk pid $NEW"
pgrep -af 'main\.py'
.venv/bin/python - "$START" <<'PY'
import sqlite3, sys, time
start = float(sys.argv[1]); c = sqlite3.connect("desk.db"); now = time.time(); bad = []
for a, t in c.execute("SELECT agent, ts FROM heartbeats ORDER BY agent"):
    fresh = t >= start
    print(f"heartbeat {a:9s} age {now-t:5.1f}s {'fresh' if fresh else 'STALE (before restart)'}")
    if not fresh: bad.append(a)
sys.exit(1 if bad else 0)
PY
RC=$?
# publish check: public state.json must have updated_at after restart (path never printed)
.venv/bin/python - "$START" <<'PY' || RC=1
import json, os, sys, time, httpx, api
u = api.SupabaseUploader().public_url("state.json")
try:
    j = httpx.get(u, headers={"cache-control": "no-cache"}, params={"t": time.time()}, timeout=15).json()
    age = time.time() - float(j["updated_at"])
    ok = float(j["updated_at"]) >= float(sys.argv[1])
    print(f"public state.json updated_at age {age:.1f}s {'fresh' if ok else 'STALE'}")
    sys.exit(0 if ok else 1)
except Exception as e:
    print(f"public state.json check failed ({type(e).__name__})"); sys.exit(1)
PY
[ $RC -eq 0 ] && echo "OK: all heartbeats fresh" || echo "FAIL: stale heartbeats"
exit $RC
