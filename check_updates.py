import os
import re
import json
import hashlib
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import yaml
from email.utils import parsedate_to_datetime

with open("game_keywords.yaml", "r", encoding="utf-8") as f:
    keyword_rules = yaml.safe_load(f)

STATE_FILE = "state.json"
HISTORY_FILE = "update_history.json"

# Matches article titles that signal actual game changes worth alerting on.
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
    r'version|'
    r'notes|'
    r'gameplay|'
    r'preview|'
    r')\b'
    r'|battle\s*pass'
    r'|patch\s+notes'
    r'|release\s+notes'
    r'|content\s+update'
    r'|early\s+access'
    r'|new\s+(?:content|map|mode|feature|weapon|skin|character|hero|agent|class)',
    re.IGNORECASE,
)

# Sentinel returned when no relevant items are found
_NO_RELEVANT_CONTENT = hashlib.sha256(
    b"no-relevant-content"
).hexdigest()


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

def load_history() -> dict:

    if os.path.exists(HISTORY_FILE):

        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    return {}


def save_history(history: dict) -> None:

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


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
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        },
    )

    r.raise_for_status()
    return r.text


def fingerprint_rss(xml_text: str, game_name: str):
    """Extract RSS fingerprint + latest entry info."""
    try:
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        items = root.findall(".//item")

        if not items:
            items = root.findall(".//atom:entry", ns)

        titles = []
        all_titles = []

        latest_title = None
        latest_date = None

        for item in items:

            title_el = item.find("title")

            if title_el is None:
                title_el = item.find("atom:title", ns)

            if title_el is None or not title_el.text:
                continue

            title = title_el.text.strip()

            all_titles.append(title)


            if is_relevant(title) or detect_keywords(game_name, title):

                titles.append(title)

                if latest_title is None:

                    latest_title = title

                    date_el = item.find("pubDate")

                    if date_el is None:
                        date_el = item.find("updated")

                    if date_el is None:
                        date_el = item.find("atom:updated", ns)

                    if date_el is not None and date_el.text:
                        latest_date = date_el.text.strip()

        if titles:

            fp = hashlib.sha256(
                " | ".join(titles).encode("utf-8")
            ).hexdigest()

            return fp, latest_title, latest_date

        # DEBUG:
        if all_titles:

            first_item = items[0]

            fallback_date = None

            date_el = first_item.find("pubDate")

            if date_el is None:
                date_el = first_item.find("updated")

            if date_el is None:
                date_el = first_item.find("atom:updated", ns)

            if date_el is not None and date_el.text:
                fallback_date = date_el.text.strip()

            fp = hashlib.sha256(
                all_titles[0].encode("utf-8")
            ).hexdigest()

            return fp, all_titles[0], fallback_date

        return _NO_RELEVANT_CONTENT, None, None

    except ET.ParseError:
        return _NO_RELEVANT_CONTENT, None, None


def fingerprint_headlines(html: str, game_name: str):
    """
    Hash only headlines that signal actual game changes.
    """

    soup = BeautifulSoup(html, "lxml")

    for tag in soup([
        "script",
        "style",
        "noscript",
        "nav",
        "footer",
        "header"
    ]):
        tag.decompose()

    parts = []

    for tag in soup.find_all(["h1", "h2", "h3", "a"]):
        text = tag.get_text(" ", strip=True)

        if len(text) > 10 and (
            is_relevant(text) or detect_keywords(game_name, text)
        ):
            parts.append(text)

    if not parts:
        return _NO_RELEVANT_CONTENT, []

    clean_titles = []

    for p in parts:
        if p not in clean_titles:
            clean_titles.append(p)

    return (
        hashlib.sha256(
            " | ".join(clean_titles[:100]).encode("utf-8")
        ).hexdigest(),
        clean_titles[:10]
    )


def detect_keywords(game_name: str, text: str):
    detected = []

    game_rules = keyword_rules.get(game_name, {})

    for word in game_rules.get("high", []):

        if word.lower() in text.lower():
            detected.append(word)

    return list(set(detected))


def send_slack(message: str) -> None:
    url = os.environ["SLACK_WEBHOOK_URL"]

    requests.post(
        url,
        json={"text": message},
        timeout=15
    )

def extract_date_from_html(html: str):

    soup = BeautifulSoup(html, "lxml")

    # Diablo Immortal
    blz_time = soup.find("blz-timestamp")

    if blz_time and blz_time.get("timestamp"):
        return blz_time["timestamp"]

    # WoW / Valorant / League / TFT
    time_tag = soup.find("time")

    if time_tag and time_tag.get("datetime"):
        return time_tag["datetime"]

    # Hearthstone
    hs_time = soup.find(
        "time",
        class_=lambda x: x and "ArticleTime" in x
    )

    if hs_time:
        return hs_time.get_text(strip=True)

    # Call of Duty / Black Ops
    news_date = soup.find(
        "div",
        class_="news-published"
    )

    if news_date and news_date.get("data-date"):
        return news_date["data-date"]

    # Minecraft
    minecraft_date = soup.find(
        "div",
        class_="MC_listingF_timestamp"
    )

    if minecraft_date:
        return minecraft_date.get_text(strip=True)

    # Genshin
    genshin_date = soup.find(
        "div",
        class_="news__date"
    )

    if genshin_date:
        return genshin_date.get_text(strip=True)

    # Hytale
    date_span = soup.find(
        "span",
        class_="inline-block h-[26px]"
    )

    if date_span:
        return date_span.get_text(strip=True)

    # Fallback: "May 28, 2026"
    for span in soup.find_all("span"):

        text = span.get_text(" ", strip=True)

        if re.search(
            r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}",
            text
        ):
            return text

    return None


def main() -> None:

    if not os.getenv("SLACK_WEBHOOK_URL"):
        raise RuntimeError(
            "Missing SLACK_WEBHOOK_URL env var"
        )

    with open("games.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    state = load_state()
    history = load_history()

    updates_found = []
    failures = []

    for game in config["games"]:

        name = game["name"]
        mode = game.get("mode", "scrape")

        urls = game.get("urls") or [game["url"]]

        news_url = game.get(
            "news_url",
            urls[0]
        )

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

            # fingerprint strategy
            if mode == "rss":

                fp, latest_title, latest_date = fingerprint_rss(content,name)

                titles = [latest_title] if latest_title else []

            else:

                fp, titles = fingerprint_headlines(content, name)

                latest_title = titles[0] if titles else None
                latest_date = extract_date_from_html(content)

            prev_title = state.get(name, {}).get("latest_title")

            if latest_title and latest_title != prev_title:

                if name not in history:
                    history[name] = []

                history[name].append({
                    "date_detected": datetime.now(timezone.utc).isoformat(),
                    "article_date": latest_date,
                    "titles": titles[:3],
                    "url": news_url
                })

                history[name] = history[name][-50:]

                joined_titles = " ".join(titles)

                detected = detect_keywords(
                    name,
                    joined_titles
                )

                if detected:

                    updates_found.append({
                        "name": name,
                        "url": news_url,
                        "detected": detected,
                        "titles": titles[:3]
                    })

            print(f"{name} | title={latest_title} | date={latest_date}")

            state[name] = {
                "fingerprint": fp,
                "latest_title": latest_title,
                "latest_date": latest_date,
                "last_checked_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
                "last_source": used_url,
                "mode": mode,
            }

        except Exception as e:

            failures.append((
                name,
                used_url or urls[0],
                str(e)
            ))

            state[name] = {
                "error": str(e),
                "last_checked_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
                "last_source": used_url or urls[0],
            }

    save_state(state)
    save_history(history)

    lines = [
        "*Daily Game Update Check (6:00 PM Fortaleza, Brasil)*"
    ]

    if updates_found:

        lines.append(
            "*High priority updates detected:*"
        )

        for update in updates_found:

            lines.append(
                f"- {update['name']}: <{update['url']}|Check it out>"
            )

    else:
        lines.append("No high priority updates today ✅")

    if failures:

        lines.append("")
        lines.append(
            "*Errors on check (url down/changed):*"
        )

        for name, url, err in failures:

            short = err[:180].replace("\n", " ")

            lines.append(
                f"- {name}: {url} — `{short}`"
            )

    send_slack("\n".join(lines))
    #print("\n".join(lines))


if __name__ == "__main__":
    main()