import os
import json
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

HISTORY_FILE = "update_history.json"


def send_slack(message: str) -> None:
    url = os.environ["SLACK_WEBHOOK_URL"]

    requests.post(
        url,
        json={"text": message},
        timeout=15
    )


def parse_date(raw_date):
    if not raw_date:
        return None

    try:
        return datetime.fromisoformat(raw_date.replace("Z", "+00:00"))

    except ValueError:

        try:
            return parsedate_to_datetime(raw_date)

        except Exception:

            try:
                return datetime.strptime(
                    raw_date,
                    "%B %d, %Y"
                ).replace(tzinfo=timezone.utc)

            except Exception:
                return None


with open(HISTORY_FILE, "r", encoding="utf-8") as f:
    history = json.load(f)

now = datetime.now(timezone.utc)
week_ago = now - timedelta(days=7)

#print("NOW =", now)
#print("WEEK AGO =", week_ago)

lines = [
    "🎮 *WEEKLY GAME UPDATE CALENDAR*\n"
]

total_updates = 0

#print("TOTAL GAMES:", len(history))

for game, updates in history.items():

    #print(game, len(updates))

    recent_updates = []
    seen_titles = set()

    for update in updates:

        #print(update)

        title = (update.get("titles") or ["No title"])[0]

        # Não contar o mesmo título duas vezes
        if title in seen_titles:
            continue

        seen_titles.add(title)

        article_dt = parse_date(update.get("article_date"))
        detected_dt = parse_date(update.get("date_detected"))
        fallback_dt = parse_date(update.get("date"))

        # Prioridade:
        # article_date -> date_detected -> date
        dt = article_dt or detected_dt or fallback_dt

        if dt is None:
            continue

        #print(
            "COMPARE:",
            game,
            dt,
            "week_ago=",
            week_ago,
            "result=",
            dt >= week_ago
        #)

        if dt >= week_ago:

            recent_updates.append({
                "date": dt,
                "title": title,
                "article": article_dt,
            })

    if not recent_updates:
        continue

    recent_updates.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    total_updates += len(recent_updates)

    latest = recent_updates[0]

    lines.append(f"*{game}*")
    lines.append(
        f"• Updates this week: {len(recent_updates)}"
    )
    lines.append(
        f"• {latest['title']}"
    )

    lines.append(
        "• Published: "
        + latest["date"].strftime("%Y-%m-%d")
    )

    lines.append("")

if total_updates == 0:

    lines.append(
        "No game updates detected this week."
    )

else:

    lines.append(
        f"Total updates last week: {total_updates}"
    )

message = "\n".join(lines)

#print(message)

send_slack(message)