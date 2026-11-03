"""Live acceptance (network). Prints file NAMES only, never values or DATA_PATH.
Run: set -a; . ./.secrets.env; set +a; .venv/bin/python tests/acceptance_live.py"""
import json, os, pathlib, re, sys, time
import httpx
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import api, links

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEYS = ("TG_TOKEN", "SUPABASE_SECRET_KEY", "DATA_PATH", "OWNER_CHAT_ID")
vals = {k: os.environ.get(k) for k in KEYS}
missing = [k for k, v in vals.items() if not v]
print("secret values loaded:", {k: bool(v) for k, v in vals.items()})
FOMO = re.compile(r"https?://(?:www\.)?fomo\.family[^\s\"'<>)\\]*")
hits, fails = [], []


def scan(label, text):
    for k, v in vals.items():
        if v and len(v) >= 4 and v in text:
            hits.append(f"{label}: {k}")


n = 0
for base in (ROOT, pathlib.Path("/workspace/pages")):
    for p in base.rglob("*"):
        if p.is_file() and not {".venv", ".git", "__pycache__", ".pytest_cache"} & set(p.parts) and p.name != ".secrets.env" \
                and not p.name.startswith("desk.db"):
            scan(str(p.relative_to(base.parent)), p.read_text(errors="ignore")); n += 1
print(f"scanned {n} local files (repo incl. web/ logs/ tests/, /workspace/pages)")

up = api.SupabaseUploader()
pub_base = f"{api.SUPABASE_URL}/storage/v1/object/public/{api.BUCKET}"
for f in ("state.json", "chart.json", "activity.json"):
    r = httpx.get(up.public_url(f), params={"t": time.time()}, timeout=20)
    print(f"live {f}: HTTP {r.status_code}, {len(r.content)} bytes")
    if r.status_code != 200:
        fails.append(f"live {f} HTTP {r.status_code}"); continue
    scan(f"live {f}", r.text)
    for u in FOMO.findall(r.text):
        if not ("r=savipww" in u or u == links.SIGNUP_URL):
            fails.append(f"live {f}: FOMO url without ref")
    json.loads(r.text)

r = httpx.get(f"{pub_base}/d/wrong-path-{int(time.time())}/state.json", timeout=20)
print(f"wrong path: HTTP {r.status_code}")
if r.status_code < 400: fails.append("wrong path did not error")
for path in ("d/", "d"):
    r = httpx.get(f"{pub_base}/{path}", timeout=20)
    scan(f"bare /{path} body", r.text)
    print(f"bare /{path}: HTTP {r.status_code}, body {len(r.content)} bytes")
    if r.status_code < 400: fails.append(f"bare /{path} returned {r.status_code}")
# anonymous list attempt (no key): must list nothing
r = httpx.post(f"{api.SUPABASE_URL}/storage/v1/object/list/{api.BUCKET}", json={"prefix": "d/", "limit": 100}, timeout=20)
try:
    listed = r.json() if r.status_code == 200 else []
except ValueError:
    listed = []
print(f"anonymous list d/: HTTP {r.status_code}, entries {len(listed) if isinstance(listed, list) else 'n/a'}")
if isinstance(listed, list) and listed: fails.append("anonymous listing returned entries")
scan("anonymous list body", r.text)

print("SECRET HITS:", hits or "none")
print("FAILS:", fails or "none")
if missing: print("WARNING: not loaded:", missing)
sys.exit(1 if hits or fails or missing else 0)
