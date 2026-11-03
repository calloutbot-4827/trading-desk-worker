"""Telegram sender. Secrets read from real env vars first, .secrets.env only as fallback; never logged. FOMO URLs only via links.py."""
import os
from pathlib import Path
import httpx
import links
import urllib.parse as _up
FOMO_HOST = _up.urlparse(links.SIGNUP_URL).netloc

SECRETS = Path(__file__).with_name(".secrets.env")


def load_env(path=SECRETS):
    """Env-first: real environment variables always win; .secrets.env (if present) only fills gaps."""
    path = Path(path)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def coin_button(text, mint, chain="solana"):
    return {"text": text, "url": links.coin_url(mint, chain)}


def callback_button(text, data):
    return {"text": text, "callback_data": data}


def profile_button(text, handle):
    return {"text": text, "url": links.profile_url(handle)}


def signup_button(text="Join FOMO"):
    return {"text": text, "url": links.SIGNUP_URL}


class Notifier:
    def __init__(self, mode="PAPER", client=None):
        load_env()
        self.mode = mode
        self._client = client

    def _prefix(self, text):
        return ("PAPER · " + text) if self.mode == "PAPER" and not text.startswith("PAPER · ") else text

    async def send(self, text, buttons=None):
        token, chat = os.environ.get("TG_TOKEN"), os.environ.get("OWNER_CHAT_ID")
        if not token or not chat:
            raise RuntimeError("notifier: TG_TOKEN/OWNER_CHAT_ID missing")
        for b in buttons or []:
            if "callback_data" in b:
                continue
            u = b["url"]
            if _up.urlparse(u).netloc == FOMO_HOST and u not in _allowed(b):
                raise ValueError("FOMO button URL not built by links.py")
        payload = {"chat_id": chat, "text": self._prefix(text), "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": b["text"], **({"callback_data": b["callback_data"]} if "callback_data" in b else {"url": b["url"]})}] for b in buttons]}
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            ok = r.status_code == 200
        except httpx.HTTPError as e:  # avoid echoing URL (contains token)
            raise RuntimeError(f"notifier: send failed ({type(e).__name__})") from None
        finally:
            if self._client is None:
                await client.aclose()
        return ok


def _allowed(b):
    # a FOMO url is accepted only if links.py reproduces it exactly
    u = b["url"]
    if u == links.SIGNUP_URL:
        return {u}
    if b.get("_src"):
        return {b["_src"]}
    import urllib.parse as up
    p = up.urlparse(u); q = up.parse_qs(p.query)
    out = set()
    if p.path == "/coin" and "address" in q:
        for ch in links.CHAIN_ID:
            out.add(links.coin_url(q["address"][0], ch))
    if p.path.startswith("/profile/"):
        out.add(links.profile_url(p.path.split("/profile/", 1)[1]))
    return out
