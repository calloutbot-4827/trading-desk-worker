import re, pathlib
import links

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP = {".venv", "logs", "__pycache__", ".git"}


def files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not SKIP & set(p.relative_to(ROOT).parts) and p.name != ".secrets.env" and p.suffix in (".py", ".md", ".txt", ".json", ".html", ".js", ".toml", ".cfg", ".env.example"):
            yield p


def test_coin_and_profile_have_ref():
    assert "r=savipww" in links.coin_url("X")
    assert "r=savipww" in links.profile_url("@fulfilling")
    for ch in links.CHAIN_ID:
        assert "r=savipww" in links.coin_url("X", ch)


def test_signup():
    assert links.SIGNUP_URL.endswith("/r/savipww")


def test_unknown_chain():
    assert links.coin_url("X", "nope") == links.SIGNUP_URL


def test_no_shorteners():
    bad = re.compile(r"\b(t\.co|bit\.ly|tinyurl\.com|goo\.gl|ow\.ly|is\.gd|buff\.ly|rebrand\.ly|cutt\.ly)/")
    for p in files():
        assert not bad.search(p.read_text(errors="ignore")), p


def test_fomo_urls_only_in_links():
    url = re.compile(r"fomo\.family[^\s\"')]*")
    for p in files():
        rel = p.relative_to(ROOT)
        if rel.parts[0] == "tests" or p.name == "links.py":
            continue
        assert not url.search(p.read_text(errors="ignore")), f"FOMO URL literal in {rel}"


def test_every_fomo_url_in_repo_has_ref():
    url = re.compile(r"https?://fomo\.family[^\s\"']*")
    for p in files():
        for u in url.findall(p.read_text(errors="ignore")):
            assert "r=savipww" in u or u.endswith("/r/savipww"), (p, u)
