#!/usr/bin/env bash
# Keeps main.py running: if it exits (crash, watchdog SystemExit 3), restart it after 10s.
# Started by restart.sh (which kills this loop first, so there's never two desks).
cd /workspace/desk || exit 1
while true; do
  .venv/bin/python main.py >> logs/main.out 2>&1
  rc=$?
  echo "$(date '+%F %T') supervise: main.py exited rc=$rc; restarting in 10s" >> logs/main.out
  sleep 10
done
