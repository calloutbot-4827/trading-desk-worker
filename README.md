# trading-desk-worker

PAPER trading desk worker. Entry point: `python run.py` (supervises `main.py`, restarts it on exit).

## Render free

Deploy as a Docker web service (free plan: no disk, ephemeral filesystem, sleeps after 15 min without inbound HTTP).

- `run.py` serves `GET /` and `GET /health` on `0.0.0.0:$PORT` (Render sets `PORT`) with a one-line JSON status
  (uptime, mode, agent heartbeat ages; no secrets). The server lives in the supervisor process, so it keeps
  answering while `main.py` restarts. Point an external uptime pinger at `/health` to stop the service sleeping.
- With `DESK_DB_REMOTE=1`, the SQLite db (including the owner chat id stored in settings) is snapshotted with the
  sqlite backup API, gzipped and uploaded every 5 min and on SIGTERM to the private Supabase Storage bucket
  `desk-private` at `{DATA_PATH}/desk.db.gz` (bucket created private if missing). At boot, if `DESK_DB_PATH` does
  not exist, the latest valid copy is restored. Uploads go to a `.tmp` object first; restores verify gzip + integrity.

Env var names (set values in the Render dashboard, never in the image or repo):

- `TG_TOKEN`
- `OWNER_CHAT_ID`
- `DATA_PATH`
- `SUPABASE_SECRET_KEY`
- `DESK_DB_REMOTE` (set to `1` on Render)
- `DESK_DB_PATH` (optional; image default `/data/desk.db`)
- `DESK_LOG_DIR` (optional; image default `/data/logs`)
- `PORT` (set by Render automatically)
- `SUPABASE_URL` (optional; defaults to the project URL)
