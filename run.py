"""Container entrypoint: keep `python main.py` running, restart 10s after any exit (same as supervise.sh).
Output goes to stdout/stderr; secrets come only from the environment.
PORT set        -> stdlib /health keepalive in this process (survives main.py restarts).
DESK_DB_REMOTE=1 -> restore desk.db from private Supabase storage at boot if missing; back up every 5 min and on SIGTERM."""
import os, signal, subprocess, sys, threading, time

DB_PATH = os.environ.get("DESK_DB_PATH") or "desk.db"
BACKUP_EVERY_S = 300
child = None
stop = threading.Event()
remote_ctx = None  # (store, object_path)
lock = threading.Lock()


def do_backup(why):
    import remote
    if not remote_ctx:
        return
    with lock:
        try:
            ok = remote.backup(DB_PATH, *remote_ctx)
            remote.log(f"backup ({why}) {'ok' if ok else 'FAILED'}")
        except Exception as e:
            remote.log(f"backup ({why}) error {type(e).__name__}")


def backup_loop():
    while not stop.wait(BACKUP_EVERY_S):
        do_backup("periodic")


def on_term(signum, frame):
    stop.set()
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(4)
        except subprocess.TimeoutExpired:
            child.kill()
    t = threading.Thread(target=do_backup, args=("shutdown",), daemon=True); t.start(); t.join(6)
    sys.exit(0)


def setup_remote():
    global remote_ctx
    import remote
    if not remote.enabled():
        return
    try:
        store = remote.SupabaseStorage(); store.ensure_bucket()
        remote_ctx = (store, remote.object_path())
        if not os.path.exists(DB_PATH):
            remote.log("restored db from remote" if remote.restore(DB_PATH, *remote_ctx) else "no remote db; starting fresh")
        threading.Thread(target=backup_loop, name="backup", daemon=True).start()
    except Exception as e:
        remote.log(f"setup error {type(e).__name__}; continuing without remote backup")


if __name__ == "__main__":
    if os.environ.get("PORT"):
        import health
        health.serve(int(os.environ["PORT"]), DB_PATH)
        print(f"{time.strftime('%F %T')} health: listening on 0.0.0.0:{os.environ['PORT']}", flush=True)
    setup_remote()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    while not stop.is_set():
        child = subprocess.Popen([sys.executable, "main.py"])
        rc = child.wait()
        if stop.is_set():
            break
        print(f"{time.strftime('%F %T')} supervise: main.py exited rc={rc}; restarting in 10s", flush=True)
        stop.wait(10)
