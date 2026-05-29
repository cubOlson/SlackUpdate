import os
import json
from datetime import datetime, timedelta, timezone

import requests

HISTORY_FILE = "update_history.json"


def send_slack(message: str) -> None:

    url = os.environ["SLACK_WEBHOOK_URL"]

    requests.post(
        url,
        json={"text": message},
        timeout=15
    )


with open(HISTORY_FILE, "r", encoding="utf-8") as f:
    history = json.load(f)

now = datetime.now(timezone.utc)
week_ago = now - timedelta(days=7)

lines = [
    "🎮 *WEEKLY GAME UPDATE CALENDAR*\n"
]

total_updates = 0

for game, updates in history.items():

    recent_updates = []

    for update in updates:

        raw_date = (
            update.get("article_date")
            or update.get("date_detected")
            or update.get("date")
        )

        if not raw_date:
            continue

        try:

            dt = datetime.fromisoformat(
                raw_date.replace("Z", "+00:00")
            )

            if dt >= week_ago:
                recent_updates.append(dt)

        except Exception:
            continue

    if recent_updates:

        recent_updates.sort(reverse=True)

        total_updates += len(recent_updates)

        lines.append(f"*{game}*")
        lines.append(
            f"• Updates this week: {len(recent_updates)}"
        )

        lines.append(
            f"• Last update: "
            f"{recent_updates[0].strftime('%Y-%m-%d')}"
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

print(message)

send_slack(message)