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

# A cadence/prediction is only meaningful with at least this many intervals
# (this many + 1 recorded updates). One gap between two updates is not a rhythm.
MIN_INTERVALS = 2

# Floor for the "quiet/broken" check: even a fast-cadence game shouldn't be
# flagged before this many days of silence (avoids noise on bursty games).
STALE_MIN_DAYS = 21

# A quiet game is only "newly quiet" (worth showing in full) if it crossed the
# threshold within this many days — roughly the weekly report interval. Games
# quiet longer than that were already reported before, so we collapse them into
# a single summary line instead of relisting them every week.
NEWLY_QUIET_WINDOW = 7


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
    n_intervals = len(intervals)

    median_interval = statistics.median(intervals) if intervals else None
    mean_interval = statistics.fmean(intervals) if intervals else None

    # Regularity: coefficient of variation of the intervals. Lower = steadier
    # cadence = more trustworthy prediction.
    if n_intervals >= 2 and mean_interval:
        cv = statistics.pstdev(intervals) / mean_interval
    else:
        cv = None

    # Only predict when we've seen enough gaps for a cadence to mean anything.
    # A single interval (two updates) is not a rhythm.
    predicted_next = None
    overdue_by = None
    if median_interval and n_intervals >= MIN_INTERVALS:
        predicted_next = last + timedelta(days=median_interval)
        overdue_by = (now - predicted_next).days  # >0 means overdue

    return {
        "name": name,
        "count": len(dates),
        "n_intervals": n_intervals,
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


def stale_threshold(s):
    """Days of silence before a game counts as quiet/broken. Cadence-aware
    (3x its usual gap) with a floor so bursty games don't trip it, and a fixed
    fallback when there isn't a trustworthy cadence yet."""
    cadence = s["median_interval"]
    if cadence and s["n_intervals"] >= MIN_INTERVALS:
        return max(STALE_MIN_DAYS, 3 * cadence)
    return 60


def is_stale(s):
    # Silent far longer than its own cadence usually means a dormant game OR a
    # broken source/scraper — either way, worth checking.
    return s["days_since_last"] > stale_threshold(s)


def updates_since(records, since):
    """Summarize a game's updates on/after `since`: (count, latest_title,
    latest_dt). Dedups by title, like the rest of the report."""
    seen = set()
    items = []
    for rec in records:
        dt = record_date(rec)
        if dt is None or dt < since:
            continue
        title = (rec.get("titles") or ["(no title)"])[0]
        if title in seen:
            continue
        seen.add(title)
        items.append((dt, title))

    if not items:
        return 0, None, None
    items.sort(reverse=True)
    return len(items), items[0][1], items[0][0]


def build_report(stats, now, window_days, top):
    lines = ["📊 *GAME UPDATE ANALYSIS*",
             f"_As of {now.strftime('%Y-%m-%d')} · {len(stats)} games tracked_",
             ""]

    # --- This week's updates (the old weekly-calendar digest) -----------
    this_week = [s for s in stats if s.get("week_count")]
    this_week.sort(key=lambda s: s["week_latest_dt"], reverse=True)

    lines.append("🎮 *Updated this week*")
    if this_week:
        for s in this_week:
            title = (s["week_latest_title"] or "")[:70]
            lines.append(
                f"• *{s['name']}* — {s['week_count']} update(s) · "
                f"latest: {title} ({fmt_date(s['week_latest_dt'])})"
            )
    else:
        lines.append("_No updates logged in the last 7 days._")
    lines.append("")

    # --- Leaderboard: most updated within the window --------------------
    ranked = sorted(stats, key=lambda s: s["window_count"], reverse=True)
    ranked = [s for s in ranked if s["window_count"] > 0][:top]

    lines.append(f"🏆 *Most updated (last {window_days} days)*")
    if ranked:
        for i, s in enumerate(ranked, 1):
            cadence = (
                f"~{s['median_interval']}d"
                if s["median_interval"] and s["n_intervals"] >= MIN_INTERVALS
                else "n/a"
            )
            lines.append(
                f"{i}. *{s['name']}* — {s['window_count']} updates "
                f"(every {cadence})"
            )
    else:
        lines.append("_No updates recorded in this window._")
    lines.append("")

    stale = [s for s in stats if is_stale(s)]
    fresh = [s for s in stats if not is_stale(s)]

    # --- Predictions: which games are about to update -------------------
    # Forward-looking: show upcoming updates soonest-first (the "updating
    # this week" view), then fill any remaining room with games just past
    # their ETA ("due now"). Genuinely-stale/broken games are excluded above.
    predictable = [s for s in fresh if s["predicted_next"] is not None]

    upcoming = sorted(
        (s for s in predictable if (s["predicted_next"] - now).days >= 0),
        key=lambda s: s["predicted_next"],
    )
    due_now = sorted(
        (s for s in predictable if (s["predicted_next"] - now).days < 0),
        key=lambda s: s["predicted_next"],
        reverse=True,  # least-overdue (closest to now) first
    )

    lines.append("🔮 *Predicted next updates (soonest first)*")
    picks = (upcoming + due_now)[:top]
    for s in picks:
        conf = confidence_label(s["cv"], s["n_intervals"])
        due = s["predicted_next"]
        days = (due - now).days
        if days < 0:
            when = "due now"
        elif days == 0:
            when = "today"
        else:
            when = f"in {days}d"
        # Flag the ones landing within the coming week.
        this_week = " 🗓️ this week" if 0 <= days <= 7 else ""
        lines.append(
            f"• *{s['name']}* → {fmt_date(due)} ({when}){this_week} "
            f"· cadence ~{s['median_interval']}d · confidence {conf}"
        )
    if not picks:
        lines.append("_Not enough history to predict yet._")
    lines.append("")

    # --- Health: quiet games / possible source breakage -----------------
    # Show games that went quiet *recently* in full — those are the actionable
    # ones (a source that just broke). Games that have been quiet a while were
    # already reported, so collapse them into one line rather than relisting
    # the same dead games every week.
    if stale:
        stale.sort(key=lambda s: s["days_since_last"], reverse=True)

        newly = [
            s for s in stale
            if s["days_since_last"] <= stale_threshold(s) + NEWLY_QUIET_WINDOW
        ]
        ongoing = [s for s in stale if s not in newly]

        lines.append("⚠️ *Quiet — check source/scraper*")

        for s in newly[:top]:
            lines.append(
                f"• *{s['name']}* — last update {fmt_date(s['last'])} "
                f"({s['days_since_last']}d ago) · newly quiet"
            )

        if ongoing:
            preview = ", ".join(s["name"] for s in ongoing[:6])
            more = f" +{len(ongoing) - 6} more" if len(ongoing) > 6 else ""
            lines.append(
                f"_{len(ongoing)} still quiet (unchanged): {preview}{more}_"
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
    week_start = now - timedelta(days=7)

    stats = []
    for name, records in history.items():
        s = analyze_game(name, records, now)
        if s is None:
            continue
        # updates within the leaderboard window
        s["window_count"] = sum(
            1 for dt in update_dates(records) if dt >= window_start
        )
        # updates in the last 7 days (for the "this week" digest)
        wc, wt, wdt = updates_since(records, week_start)
        s["week_count"] = wc
        s["week_latest_title"] = wt
        s["week_latest_dt"] = wdt
        stats.append(s)

    report = build_report(stats, now, args.days, args.top)
    print(report)

    if args.slack:
        send_slack(report)


if __name__ == "__main__":
    main()
