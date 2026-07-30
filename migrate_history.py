"""
One-shot (re-runnable) cleanup of update_history.json.

Applies the same hygiene rules check_updates.py now uses on write:
  - normalize every record to the {date_detected, article_date, titles, url}
    schema (legacy {date, detected} records are converted)
  - clean titles (strip embedded timestamps / "Game Updates" labels)
  - drop junk titles (page chrome, empty)
  - normalize article_date to ISO-8601 UTC
  - dedup per game by article-day (or title when undated)
  - sort chronologically

Writes a timestamped backup next to the file before overwriting. Safe to
run multiple times (idempotent).

Usage:
    python migrate_history.py            # migrate in place (+ backup)
    python migrate_history.py --dry-run  # report changes, write nothing
"""

import sys
import json
import shutil
import argparse
from datetime import datetime, timezone

import check_updates as c

HISTORY_FILE = "update_history.json"


def sort_dt(rec):
    """Best datetime for chronological ordering; oldest-first."""
    return (
        c.to_iso(rec.get("article_date"))
        or c.to_iso(rec.get("date_detected"))
        or ""
    )


def migrate(history: dict):
    out = {}
    stats = {"in": 0, "out": 0, "dropped_junk": 0, "deduped": 0}

    for game, records in history.items():
        cleaned = []
        seen = set()

        for r in records:
            stats["in"] += 1

            # legacy {date, detected} -> {date_detected}
            detected_at = r.get("date_detected") or r.get("date")

            titles = [
                c.clean_title(t)
                for t in (r.get("titles") or [])
                if not c.is_junk_title(c.clean_title(t))
            ]

            if not titles:
                stats["dropped_junk"] += 1
                continue

            rec = {
                "date_detected": c.to_iso(detected_at) or detected_at,
                "article_date": c.to_iso(r.get("article_date")),
                "titles": titles[:3],
                "url": r.get("url"),
            }

            key = c.record_key(rec)
            if key in seen:
                stats["deduped"] += 1
                continue

            seen.add(key)
            cleaned.append(rec)

        cleaned.sort(key=sort_dt)
        out[game] = cleaned
        stats["out"] += len(cleaned)

    return out, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)

    out, stats = migrate(history)

    print(
        f"records: {stats['in']} in -> {stats['out']} out  "
        f"(dropped {stats['dropped_junk']} junk, "
        f"{stats['deduped']} duplicates)"
    )

    if args.dry_run:
        print("dry run — no files written")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = f"{HISTORY_FILE}.{stamp}.bak"
    shutil.copyfile(HISTORY_FILE, backup)
    print(f"backup written: {backup}")

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"migrated {HISTORY_FILE}")


if __name__ == "__main__":
    main()
