import os
import re
import json
import hashlib
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import yaml

STATE_FILE = "state.json"

# Matches article titles that signal actual game changes worth alerting on.
# Covers patches, seasons, new content, balance changes, new characters, UI/feature releases, etc.
_RELEVANT_RE = re.compile(
    r'\b(?:'
    r'update|patch|hotfix|bugfix|'
    r'dlc|expansion|'
    r'season|'
    r'character|hero|operator|champion|'
    r'playable|roster|'
    r'balance|nerf|buff|rework|tuning|'
    r'wipe|'
    r'changelog|'
    r'event|'
    r'launch|'
    r'version'
    r')\b'
    r'|battle\s*pass'
    r'|patch\s+notes|release\s+notes'
    r'|early\s+access'
    r'|new\s+(?:content|map|mode|feature|weapon|skin|character|hero|agent|class)',
    re.IGNORECASE,
)

# Sentinel returned when no relevant items are found — stable across runs so no alert fires.
_NO_RELEVANT_CONTENT = hashlib.sha256(b"no-relevant-content").hexdigest()


def is_relevant(text: str) -> bool:
    return bool(_RELEVANT_RE.search(text))


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

def fingerprint_rss(xml_text: str) -> str:
    """Hash only RSS/Atom item titles that signal actual game changes."""
    try:
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        titles = [t.text.strip() for t in root.findall(".//item/title") if t.text]
        if not titles:
            titles = [t.text.strip() for t in root.findall(".//atom:entry/atom:title", ns) if t.text]

        relevant = [t for t in titles if is_relevant(t)]
        if relevant:
            return hashlib.sha256(" | ".join(relevant).encode("utf-8")).hexdigest()
        if titles:
            return _NO_RELEVANT_CONTENT
    except ET.ParseError:
        pass

    # Fallback 1: regex title extraction (avoids hashing dynamic timestamps in raw XML)
    raw_titles = re.findall(
        r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text, re.DOTALL
    )
    item_titles = [t.strip() for t in raw_titles[1:] if t.strip()]  # skip channel title
    relevant = [t for t in item_titles if is_relevant(t)]
    if relevant:
        return hashlib.sha256(" | ".join(relevant).encode("utf-8")).hexdigest()
    if item_titles:
        return _NO_RELEVANT_CONTENT

    # Fallback 2: last resort — hash raw text (may be unstable if feed has dynamic headers)
    return hashlib.sha256(xml_text[:5000].encode("utf-8")).hexdigest()

def fingerprint_headlines(html: str) -> str:
    """Hash only headlines that signal actual game changes — ignores news, ads, nav, banners."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    parts = []
    for tag in soup.find_all(["h1", "h2", "h3", "a"]):
        text = tag.get_text(" ", strip=True)
        if len(text) > 10 and is_relevant(text):
            parts.append(text)
    if not parts:
        return _NO_RELEVANT_CONTENT
    return hashlib.sha256(" | ".join(parts[:100]).encode("utf-8")).hexdigest()

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
        mode = game.get("mode", "scrape")  # "rss" or "scrape"
        urls = game.get("urls") or [game["url"]]
        news_url = game.get("news_url", urls[0])
        last_error = None
        used_url = None

        try:
            content = None
            for u in urls:
                try:
                    content = fetch_page(u)
                    used_url = u
                    break
                except Exception as e:
                    last_error = e

            if content is None:
                raise last_error

            # Choose fingerprint strategy based on mode
            if mode == "rss":
                fp = fingerprint_rss(content)
            else:
                fp = fingerprint_headlines(content)

            prev_fp = state.get(name, {}).get("fingerprint")
            if prev_fp and prev_fp != fp:
                updates_found.append((name, news_url))

            state[name] = {
                "fingerprint": fp,
                "last_checked_utc": datetime.now(timezone.utc).isoformat(),
                "last_source": used_url,
                "mode": mode,
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
            lines.append(f"- {name}: <{url}|Check it out>")
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
