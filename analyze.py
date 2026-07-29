"""
Analyze update_history.json to answer three questions:

  1. When was each game last updated?
  2. How often does each game update (cadence), and when is the next
     update likely? -> prediction
  3. Which games are updated the most? -> leaderboard

Usage:
    python analyze.py                 # print report to console
    python analyze.py --slack         # also post the report to Slack
    python analyze.py --days 30       # leaderboard window (default 90)
    python analyze.py --top 15        # leaderboard size (default 10)
"""

import os
import re
import sys
import json
import argparse
import statistics
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

HISTORY_FILE = "update_history.json"

# "Jun 23, 2026" or "June 23, 2026" embedded anywhere in a string
_MONTH_DAY_RE = re.compile(r"[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}")


def parse_date(raw):
    """Parse the many date formats found in the history file.

    Handles ISO 8601 (with Z), RFC-822 RSS dates, bare "Month Day, Year",
    and messy strings that merely *contain* a "Month Day, Year". Always
    returns a timezone-aware UTC datetime, or None.
    """
    if not raw:
        return None

    raw = raw.strip()

    # ISO 8601, e.g. 2026-07-28T13:00:00.000Z
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return _as_utc(dt)
    except ValueError:
        pass

    # RFC-822 RSS, e.g. Tue, 28 Jul 2026 15:47:52 +0000
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return _as_utc(dt)
    except (TypeError, ValueError):
        pass

    # A "Month Day, Year" somewhere in the string (also covers messy blobs)
    m = _MONTH_DAY_RE.search(raw)
    if m:
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(m.group(0), fmt).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

    return None


def _as_utc(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def record_date(rec):
    """Best available date for a record: real article date first, then the
    time we detected it, then the legacy 'date' field."""
    return (
        parse_date(rec.get("article_date"))
        or parse_date(rec.get("date_detected"))
        or parse_date(rec.get("date"))
    )


def update_dates(records):
    """Return a sorted list of unique update datetimes for one game.

    Collapses records that fall on the same calendar day so re-runs and
    duplicate captures don't count as separate updates.
    """
    seen_days = {}
    for rec in records:
        dt = record_date(rec)
        if dt is None:
            continue
        seen_days.setdefault(dt.date(), dt)

    return sorted(seen_days.values())


def analyze_game(name, records, now):
    dates = update_dates(records)
    if not dates:
        return None

    last = dates[-1]
    days_since_last = (now - last).days

    intervals = [
        (b - a).days
        for a, b in zip(dates, dates[1:])
        if (b - a).days > 0
    ]

    median_interval = statistics.median(intervals) if intervals else None
    mean_interval = statistics.fmean(intervals) if intervals else None

    # Regularity: coefficient of variation of the intervals. Lower = steadier
    # cadence = more trustworthy prediction.
    if len(intervals) >= 2 and mean_interval:
        cv = statistics.pstdev(intervals) / mean_interval
    else:
        cv = None

    predicted_next = None
    overdue_by = None
    if median_interval:
        predicted_next = last + timedelta(days=median_interval)
        overdue_by = (now - predicted_next).days  # >0 means overdue

    return {
        "name": name,
        "count": len(dates),
        "first": dates[0],
        "last": last,
        "days_since_last": days_since_last,
        "median_interval": median_interval,
        "mean_interval": mean_interval,
        "cv": cv,
        "predicted_next": predicted_next,
        "overdue_by": overdue_by,
    }


def confidence_label(cv, n_intervals):
    if cv is None or n_intervals < 3:
        return "low"
    if cv < 0.4:
        return "high"
    if cv < 0.8:
        return "medium"
    return "low"


def fmt_date(dt):
    return dt.strftime("%Y-%m-%d") if dt else "—"


def build_report(stats, now, window_days, top):
    lines = ["📊 *GAME UPDATE ANALYSIS*",
             f"_As of {now.strftime('%Y-%m-%d')} · {len(stats)} games tracked_",
             ""]

    # --- Leaderboard: most updated within the window --------------------
    ranked = sorted(stats, key=lambda s: s["window_count"], reverse=True)
    ranked = [s for s in ranked if s["window_count"] > 0][:top]

    lines.append(f"🏆 *Most updated (last {window_days} days)*")
    if ranked:
        for i, s in enumerate(ranked, 1):
            cadence = (
                f"~{s['median_interval']}d" if s["median_interval"] else "n/a"
            )
            lines.append(
                f"{i}. *{s['name']}* — {s['window_count']} updates "
                f"(every {cadence})"
            )
    else:
        lines.append("_No updates recorded in this window._")
    lines.append("")

    # A game is "stale" when it's silent for far longer than its own cadence
    # (or >60 days with no cadence). That usually means a genuinely dormant
    # game OR a broken source/scraper — either way, worth checking.
    def is_stale(s):
        cadence = s["median_interval"]
        if cadence:
            return s["days_since_last"] > max(60, 3 * cadence)
        return s["days_since_last"] > 60

    stale = [s for s in stats if is_stale(s)]
    fresh = [s for s in stats if not is_stale(s)]

    # --- Predictions: due soon / recently overdue (stale ones excluded) --
    predictable = [s for s in fresh if s["predicted_next"] is not None]
    predictable.sort(key=lambda s: s["predicted_next"])

    lines.append("🔮 *Predicted next updates (soonest first)*")
    shown = 0
    for s in predictable:
        conf = confidence_label(s["cv"], s["count"] - 1)
        due = s["predicted_next"]
        if s["overdue_by"] is not None and s["overdue_by"] > 0:
            when = f"overdue by {s['overdue_by']}d"
        else:
            days = (due - now).days
            when = f"in {days}d" if days >= 0 else fmt_date(due)
        lines.append(
            f"• *{s['name']}* → {fmt_date(due)} ({when}) "
            f"· cadence ~{s['median_interval']}d · confidence {conf}"
        )
        shown += 1
        if shown >= top:
            break
    if shown == 0:
        lines.append("_Not enough history to predict yet._")
    lines.append("")

    # --- Health: quiet games / possible source breakage -----------------
    if stale:
        stale.sort(key=lambda s: s["days_since_last"], reverse=True)
        lines.append("⚠️ *Quiet — check source/scraper*")
        for s in stale[:top]:
            lines.append(
                f"• *{s['name']}* — last update {fmt_date(s['last'])} "
                f"({s['days_since_last']}d ago)"
            )
        lines.append("")

    return "\n".join(lines)


def send_slack(message):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        raise RuntimeError("Missing SLACK_WEBHOOK_URL env var")
    r = requests.post(url, json={"text": message}, timeout=15)
    r.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Analyze game update history.")
    ap.add_argument("--slack", action="store_true", help="post report to Slack")
    ap.add_argument("--days", type=int, default=90, help="leaderboard window")
    ap.add_argument("--top", type=int, default=10, help="rows per section")
    args = ap.parse_args()

    # Windows consoles default to cp1252 and choke on emoji; force UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=args.days)

    stats = []
    for name, records in history.items():
        s = analyze_game(name, records, now)
        if s is None:
            continue
        # updates within the leaderboard window
        s["window_count"] = sum(
            1 for dt in update_dates(records) if dt >= window_start
        )
        stats.append(s)

    report = build_report(stats, now, args.days, args.top)
    print(report)

    if args.slack:
        send_slack(report)


if __name__ == "__main__":
    main()
