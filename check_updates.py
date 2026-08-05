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

# Consecutive failed daily checks before a source is called "likely broken"
# rather than just having a transient blip. One bad day is noise; a streak is
# a real problem worth surfacing loudly.
BROKEN_STREAK = 3

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
    r'launch|'
    r'version|'
    r'notes|'
    r'gameplay|'
    r'preview'
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


def is_excluded(game_name: str, text: str) -> bool:
    """True if the text contains a per-game 'exclude' term (e.g. leaks/rumors
    that shouldn't count as real updates)."""
    rules = keyword_rules.get(game_name, {})
    low = text.lower()
    return any(word.lower() in low for word in rules.get("exclude", []))


# --- data hygiene helpers -------------------------------------------------

# An ISO timestamp embedded inside a title string (some feeds prepend one).
_ISO_IN_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?"
)
_MONTH_DAY_RE = re.compile(r"[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}")

# Titles that are page chrome / navigation, never a real update.
_JUNK_TITLES = {
    "skip to main content",
    "skip to content",
    "main content",
    "read more",
    "news",
}


def clean_title(raw: str) -> str:
    """Strip embedded timestamps and feed labels from a title."""
    if not raw:
        return ""
    t = _ISO_IN_TEXT.sub(" ", raw)
    t = re.sub(r"^\s*Game Updates\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def is_junk_title(raw: str) -> bool:
    t = (raw or "").strip().lower()
    return (not t) or len(t) < 5 or t in _JUNK_TITLES


def _iso_utc(dt) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def to_iso(raw):
    """Normalize any known date format (ISO / RFC-822 RSS / 'Month Day, Year'
    or a string containing one) to an ISO-8601 UTC string, or None."""
    if not raw:
        return None
    raw = raw.strip()

    try:
        return _iso_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return _iso_utc(dt)
    except (TypeError, ValueError):
        pass

    m = _MONTH_DAY_RE.search(raw)
    if m:
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return _iso_utc(
                    datetime.strptime(m.group(0), fmt).replace(
                        tzinfo=timezone.utc
                    )
                )
            except ValueError:
                continue

    return None


def record_key(rec: dict):
    """Identity of a history record for dedup: same article-day (preferred)
    or, lacking a date, same primary title."""
    ad = rec.get("article_date")
    if ad:
        return ("d", ad[:10])
    return ("t", (rec.get("titles") or [""])[0].strip().lower())


def already_recorded(records: list, rec: dict) -> bool:
    key = record_key(rec)
    return any(record_key(r) == key for r in records)


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


def fingerprint_build(json_text: str):
    """Track a game by its live client build/CL (e.g. Fortnite via
    fortnite-api.com). The CL changes on every patch, so a changed
    fingerprint == a real game update.

    Returns: fingerprint, latest_title, latest_date
    """
    data = json.loads(json_text)
    build = (data.get("data") or {}).get("build") or ""

    # e.g. "++Fortnite+Release-41.20-CL-55550516"
    ver = re.search(r"Release-([\d.]+)", build)
    cl = re.search(r"CL-(\d+)", build)

    version = ver.group(1) if ver else "?"
    cl_num = cl.group(1) if cl else "?"

    title = f"Fortnite v{version} (build CL-{cl_num})"
    fp = hashlib.sha256(build.encode("utf-8")).hexdigest()

    return fp, title, None


def fingerprint_minecraft(json_text: str):
    """Mojang's official Java patch-notes JSON. Every entry is a real
    release/snapshot, so the newest one is the latest update.

    Returns: fingerprint, latest_title, latest_date
    """
    data = json.loads(json_text)
    entries = data.get("entries") or []

    if not entries:
        return _NO_RELEVANT_CONTENT, None, None

    latest = entries[0]
    title = (latest.get("title") or "").strip()
    version = latest.get("version") or title
    date = latest.get("date")  # already ISO-8601

    fp = hashlib.sha256(str(version).encode("utf-8")).hexdigest()
    return fp, title, date


def fingerprint_appstore(json_text: str):
    """Apple App Store lookup API. A changed version == a real update.
    Works for any mobile game (e.g. Diablo Immortal).

    Returns: fingerprint, latest_title, latest_date
    """
    data = json.loads(json_text)
    results = data.get("results") or []

    if not results:
        return _NO_RELEVANT_CONTENT, None, None

    app = results[0]
    name = (app.get("trackName") or "").strip()
    version = app.get("version") or ""
    date = app.get("currentVersionReleaseDate")  # already ISO-8601

    title = f"{name} v{version}".strip() if version else name
    fp = hashlib.sha256(str(version).encode("utf-8")).hexdigest()
    return fp, title, date


def fingerprint_genshin(json_text: str, game_name: str):
    """HoYoLAB news API (notices). Mixed content, so keep the newest item
    that looks like a real version/update notice.

    Returns: fingerprint, latest_title, latest_date
    """
    data = json.loads(json_text)
    news = (data.get("data") or {}).get("list") or []

    relevant = []
    latest_title = None
    latest_date = None

    for item in news:
        post = item.get("post") or {}
        subject = (post.get("subject") or "").strip()
        if not subject or is_excluded(game_name, subject):
            continue

        date_iso = None
        ts = post.get("created_at")
        if ts:
            try:
                date_iso = _iso_utc(
                    datetime.fromtimestamp(int(ts), tz=timezone.utc)
                )
            except (ValueError, OSError, OverflowError):
                pass

        if is_relevant(subject) or detect_keywords(game_name, subject):
            relevant.append(subject)
            if latest_title is None:
                latest_title = subject
                latest_date = date_iso

    if relevant:
        fp = hashlib.sha256(
            " | ".join(relevant).encode("utf-8")
        ).hexdigest()
        return fp, latest_title, latest_date

    return _NO_RELEVANT_CONTENT, None, None


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

            if is_excluded(game_name, title):
                continue

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

        if len(text) > 10 and not is_excluded(game_name, text) and (
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

def extract_game_update(
    game_name: str,
    html: str,
    default_title: str = None
):
    """
    Returns:
        latest_title, latest_date
    """

    soup = BeautifulSoup(html, "lxml")

    # ==========================================================
    # Riot Games (Valorant / League / TFT)
    # ==========================================================
    if game_name in (
        "VALORANT",
        "League of Legends",
        "Teamfight Tactics"
    ):

        title = default_title
        date = None

        title_tag = soup.find(
            attrs={"data-testid": "card-title"}
        )

        if title_tag:
            title = title_tag.get_text(
                " ",
                strip=True
            )

        date_container = soup.find(
            attrs={"data-testid": "card-date"}
        )

        if date_container:

            time_tag = date_container.find("time")

            if time_tag and time_tag.get("datetime"):
                date = time_tag["datetime"]

        return title, date

    # ==========================================================
    # World of Warcraft
    # ==========================================================
    if game_name == "World of Warcraft":

        title = default_title
        date = None

        title_tag = soup.find(
            "div",
            class_="NewsBlog-title"
        )

        if title_tag:
            title = title_tag.get_text(
                " ",
                strip=True
            )

        time_tag = soup.find("time")

        if time_tag and time_tag.get("datetime"):
            date = time_tag["datetime"]

        return title, date

    # ==========================================================
    # Diablo Immortal
    # ==========================================================
    blz_time = soup.find("blz-timestamp")

    if blz_time and blz_time.get("timestamp"):
        return default_title, blz_time["timestamp"]

    # ==========================================================
    # Generic <time datetime="">
    # ==========================================================
    time_tag = soup.find("time")

    if time_tag and time_tag.get("datetime"):
        return default_title, time_tag["datetime"]

    # ==========================================================
    # Hearthstone
    # ==========================================================
    hs_time = soup.find(
        "time",
        class_=lambda x: x and "ArticleTime" in x
    )

    if hs_time:
        return default_title, hs_time.get_text(strip=True)

    # ==========================================================
    # Call of Duty / Black Ops
    # ==========================================================
    news_date = soup.find(
        "div",
        class_="news-published"
    )

    if news_date and news_date.get("data-date"):
        return default_title, news_date["data-date"]

    # ==========================================================
    # Minecraft
    # ==========================================================
    minecraft_date = soup.find(
        "div",
        class_="MC_listingF_timestamp"
    )

    if minecraft_date:
        return default_title, minecraft_date.get_text(strip=True)

    # ==========================================================
    # Genshin Impact
    # ==========================================================
    genshin_date = soup.find(
        "div",
        class_="news__date"
    )

    if genshin_date:
        return default_title, genshin_date.get_text(strip=True)

    # ==========================================================
    # Hytale
    # ==========================================================
    date_span = soup.find(
        "span",
        class_="inline-block h-[26px]"
    )

    if date_span:
        return default_title, date_span.get_text(strip=True)

    # ==========================================================
    # Generic Month Day, Year fallback
    # ==========================================================
    for span in soup.find_all("span"):

        text = span.get_text(
            " ",
            strip=True
        )

        if re.search(
            r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}",
            text
        ):
            return default_title, text

    return default_title, None


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

        # Carry the current failure streak forward from the last run so a
        # success can reset it and a failure can extend it.
        prev_entry = state.get(name, {})
        prev_streak = prev_entry.get("error_streak", 0)

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
            if mode == "build":

                fp, latest_title, latest_date = fingerprint_build(content)

                titles = [latest_title] if latest_title else []

            elif mode == "minecraft":

                fp, latest_title, latest_date = fingerprint_minecraft(content)

                titles = [latest_title] if latest_title else []

            elif mode == "appstore":

                fp, latest_title, latest_date = fingerprint_appstore(content)

                titles = [latest_title] if latest_title else []

            elif mode == "genshin":

                fp, latest_title, latest_date = fingerprint_genshin(
                    content, name
                )

                titles = [latest_title] if latest_title else []

            elif mode == "rss":

                fp, latest_title, latest_date = fingerprint_rss(content,name)

                titles = [latest_title] if latest_title else []

            else:

                fp, titles = fingerprint_headlines(content, name)

                default_title = titles[0] if titles else None
                latest_title, latest_date = extract_game_update(name,content,default_title)

            prev_title = state.get(name, {}).get("latest_title")

            if latest_title and latest_title != prev_title:

                joined_titles = " ".join(titles)

                clean = clean_title(latest_title)

                if mode == "build":
                    # A changed build/CL is itself the update signal; no
                    # keyword matching needed.
                    detected = ["build change"]
                    is_update = prev_title is not None
                elif mode in ("minecraft", "appstore"):
                    # Authoritative version/patch feed: every new entry is a
                    # real update.
                    detected = ["patch note"]
                    is_update = True
                else:
                    detected = detect_keywords(name, joined_titles)
                    is_update = bool(detected) and not is_excluded(
                        name, joined_titles
                    )

                # Never record page chrome / empty titles as an update.
                if is_junk_title(clean):
                    is_update = False

                if is_update:

                    print(
                        f"NEW UPDATE DETECTED -> {name} | "
                        f"title={clean} | "
                        f"prev={prev_title}"
                    )

                    supporting = [
                        clean_title(t)
                        for t in titles[1:3]
                        if not is_junk_title(clean_title(t))
                    ]

                    record = {
                        "date_detected": datetime.now(timezone.utc).isoformat(),
                        "article_date": to_iso(latest_date),
                        "titles": [clean] + supporting,
                        "url": news_url,
                    }

                    if name not in history:
                        history[name] = []

                    # Skip if we've already logged this update (same article
                    # day, or same title when undated).
                    if not already_recorded(history[name], record):

                        history[name].append(record)

                        updates_found.append({
                            "name": name,
                            "url": news_url,
                            "detected": detected,
                            "titles": [clean],
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
                "error_streak": 0,
            }

        except Exception as e:

            now_iso = datetime.now(timezone.utc).isoformat()
            streak = prev_streak + 1
            # Timestamp the start of the streak so we can report "broken since".
            error_since = prev_entry.get("error_since") if prev_streak else now_iso

            failures.append((
                name,
                used_url or urls[0],
                str(e),
                streak,
            ))

            state[name] = {
                "error": str(e),
                "last_checked_utc": now_iso,
                "last_source": used_url or urls[0],
                "error_streak": streak,
                "error_since": error_since,
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

    # Escalate sources that have failed several days running — those are
    # broken, not blips, and are the ones worth chasing down early.
    broken = [f for f in failures if f[3] >= BROKEN_STREAK]
    transient = [f for f in failures if f[3] < BROKEN_STREAK]

    if broken:

        lines.append("")
        lines.append(
            f"🔴 *Likely broken (failing {BROKEN_STREAK}+ days — check the source):*"
        )

        for name, url, err, streak in broken:

            short = err[:180].replace("\n", " ")

            lines.append(
                f"- {name} ({streak}d): {url} — `{short}`"
            )

    if transient:

        lines.append("")
        lines.append(
            "*Errors on check (url down/changed):*"
        )

        for name, url, err, streak in transient:

            short = err[:180].replace("\n", " ")

            lines.append(
                f"- {name}: {url} — `{short}`"
            )

    send_slack("\n".join(lines))
    #print("\n".join(lines))


if __name__ == "__main__":
    main()