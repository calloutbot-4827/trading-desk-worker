"""web/ is published publicly: no secrets, no data path, no hard-coded FOMO URLs."""
import os, pathlib, re
import pytest
ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
pytestmark = pytest.mark.skipif(not WEB.is_dir(), reason="web/ lives in the other repo")
FILES = [p for p in WEB.rglob("*") if p.is_file() and p.suffix in (".html", ".js", ".css", ".json")]


def secret_values():
    vals = []
    env = ROOT / ".secrets.env"  # values only compared in memory, never printed
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(v) >= 4:
                    vals.append(v)
    vals += [v for v in (os.environ.get(k) for k in ("SUPABASE_SECRET_KEY", "TG_TOKEN", "DATA_PATH", "OWNER_CHAT_ID")) if v and len(v) >= 4]
    return vals


def test_web_files_exist():
    names = {p.relative_to(WEB).as_posix() for p in WEB.rglob("*") if p.is_file()}
    for f in ("index.html", "app.js", "chart.js", "styles.css", "manifest.json", "sw.js",
              "icons/apple-touch-icon.png", "icons/icon-192.png", "icons/icon-512.png"):
        assert f in names, f


def test_no_secret_names_or_values():
    vals = secret_values()
    for p in FILES:
        body = p.read_text()
        for k in ("TG_TOKEN", "SUPABASE_SECRET_KEY", "DATA_PATH", "OWNER_CHAT_ID", "sb_secret_", "service_role"):
            assert k not in body, f"{p.name} mentions {k}"
        for i, v in enumerate(vals):
            assert v not in body, f"{p.name} contains secret value #{i}"  # never echo the value


def test_no_fomo_url_literals():
    for p in FILES:
        body = p.read_text().lower()
        assert "fomo.family" not in body, p.name
        assert not re.search(r"https?://[^\"'\s]*fomo", body), p.name


def test_relative_paths_only():
    html = (WEB / "index.html").read_text()
    assert not re.search(r'(src|href)="/', html)
