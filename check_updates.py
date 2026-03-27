import os
import json
import hashlib
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
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

def fingerprint_rss(xml_text: str) -> str:
    """Extract the title of the most recent item in an RSS/Atom feed."""
    import re
    try:
        root = ET.fromstring(xml_text)
        # Handle both RSS and Atom namespaces
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        # Try RSS first
        item = root.find(".//item/title")
        if item is None:
            # Try Atom
            item = root.find(".//atom:entry/atom:title", ns)
        if item is not None and item.text:
            return hashlib.sha256(item.text.strip().encode("utf-8")).hexdigest()
    except ET.ParseError:
        pass
    # Fallback 1: extract first item title with regex to avoid hashing dynamic timestamps
    m = re.search(r"<item[^>]*>.*?<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text, re.DOTALL)
    if m:
        return hashlib.sha256(m.group(1).strip().encode("utf-8")).hexdigest()
    # Fallback 2: hash the raw text (last resort — may be unstable if feed has dynamic headers)
    return hashlib.sha256(xml_text[:5000].encode("utf-8")).hexdigest()

def fingerprint_headlines(html: str) -> str:
    """Fingerprint only headlines and article links — ignores ads, nav, banners."""
    soup = BeautifulSoup(html, "lxml")
    # Remove noise
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    # Collect only headings and anchor text (article titles live here)
    parts = []
    for tag in soup.find_all(["h1", "h2", "h3", "a"]):
        text = tag.get_text(" ", strip=True)
        if len(text) > 10:  # skip tiny/empty tags
            parts.append(text)
    combined = " | ".join(parts[:100])  # cap at 100 items
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()

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
                updates_found.append((name, used_url))

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
