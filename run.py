"""Container entrypoint: keep `python main.py` running, restart 10s after any exit (same as supervise.sh).
Output goes to stdout/stderr (Railway captures it); secrets come only from the environment."""
import subprocess, sys, time

if __name__ == "__main__":
    while True:
        rc = subprocess.call([sys.executable, "main.py"])
        print(f"{time.strftime('%F %T')} supervise: main.py exited rc={rc}; restarting in 10s", flush=True)
        time.sleep(10)
