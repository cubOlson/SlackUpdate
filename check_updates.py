import os
import json
import hashlib
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import yaml

STATE_FILE = "state.json"

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_page(url: str) -> str:
    r = requests.get(
        url,
        timeout=45,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
        },
    )
    r.raise_for_status()
    return r.text

def stable_text_fingerprint(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = " ".join(text.split())
    text = text[:20000]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def send_slack(message: str) -> None:
    url = os.environ["SLACK_WEBHOOK_URL"]
    requests.post(url, json={"text": message}, timeout=15)

def main() -> None:
    if not os.getenv("SLACK_WEBHOOK_URL"):
        raise RuntimeError("Missing SLACK_WEBHOOK_URL env var (configure as GitHub Secret)")

    with open("games.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    state = load_state()
    updates_found = []
    failures = []

    for game in config["games"]:
        name = game["name"]
        urls = game.get("urls") or [game["url"]]
        last_error = None
        used_url = None
        try:
            html = None
            for u in urls:
                try:
                    html = fetch_page(u)
                    used_url = u
                    break
                except Exception as e:
                    last_error = e
            if html is None:
                raise last_error
            fp = stable_text_fingerprint(html)
            prev_fp = state.get(name, {}).get("fingerprint")
            if prev_fp and prev_fp != fp:
                updates_found.append((name, used_url))
            state[name] = {
                "fingerprint": fp,
                "last_checked_utc": datetime.now(timezone.utc).isoformat(),
                "last_source": used_url,
            }
        except Exception as e:
            failures.append((name, used_url or urls[0], str(e)))
            state[name] = {
                "error": str(e),
                "last_checked_utc": datetime.now(timezone.utc).isoformat(),
                "last_source": used_url or urls[0],
            }

    save_state(state)

    lines = ["*Daily Game Update Check (6:00 PM Fortaleza, Brasil)*"]
    if updates_found:
        lines.append("*Updates detected (changes since yesterday):*")
        for name, url in updates_found:
            lines.append(f"- {name}: {url}")
    else:
        lines.append("No updates today ✅")

    if failures:
        lines.append("")
        lines.append("*Errors on check (url down/changed):*")
        for name, url, err in failures:
            short = err[:180].replace("\n", " ")
            lines.append(f"- {name}: {url} — `{short}`")

    send_slack("\n".join(lines))

if __name__ == "__main__":
    main()
