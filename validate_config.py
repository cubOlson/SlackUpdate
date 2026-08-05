"""
Validate games.yaml against game_keywords.yaml before the bot ever runs.

The most common silent failure in this project is adding a game to
games.yaml but forgetting its keyword rules: an rss/scrape/genshin game
with no `high` keywords can never satisfy the is_update check in
check_updates.py, so it tracks forever without ever logging an update.
This script catches that (and other config mistakes) up front.

Usage:
    python validate_config.py            # report; exit 1 if any errors
    python validate_config.py --strict   # also fail on warnings

Exit codes:
    0  config is valid (warnings may still be printed)
    1  one or more errors (or warnings, with --strict)
"""

import sys
import argparse

import yaml

GAMES_FILE = "games.yaml"
KEYWORDS_FILE = "game_keywords.yaml"

# Every mode check_updates.py knows how to fingerprint.
KNOWN_MODES = {"rss", "scrape", "build", "minecraft", "genshin", "appstore"}

# These modes read an authoritative version/patch feed, so a changed entry is
# itself the update signal — they don't need keyword rules to log.
AUTHORITATIVE_MODES = {"build", "minecraft", "appstore"}


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def game_mode(game):
    return game.get("mode", "scrape")


def has_keywords(keyword_rules, name):
    entry = keyword_rules.get(name) or {}
    return bool(entry.get("high"))


def validate(games_cfg, keyword_rules):
    """Return (errors, warnings) as lists of human-readable strings."""
    errors = []
    warnings = []

    games = (games_cfg or {}).get("games")
    if not games:
        errors.append(f"{GAMES_FILE}: no 'games' list found")
        return errors, warnings

    seen_names = set()
    referenced = set()

    for i, game in enumerate(games):
        # A stable label even when 'name' is the thing that's missing.
        label = game.get("name") or f"<game #{i + 1}>"

        # --- name --------------------------------------------------------
        name = game.get("name")
        if not name or not str(name).strip():
            errors.append(f"{label}: missing or empty 'name'")
            continue
        if name in seen_names:
            errors.append(f"{name}: duplicate 'name' (appears more than once)")
        seen_names.add(name)

        # --- source url --------------------------------------------------
        if not game.get("url") and not game.get("urls"):
            errors.append(f"{name}: has neither 'url' nor 'urls'")
        if game.get("urls") and not isinstance(game["urls"], list):
            errors.append(f"{name}: 'urls' must be a list")

        # news_url is what the Slack alert links to; it falls back to the
        # first source url, so a missing one is a warning, not an error.
        if not game.get("news_url"):
            warnings.append(
                f"{name}: no 'news_url' — Slack link will fall back to the "
                f"scrape source url"
            )

        # --- mode --------------------------------------------------------
        mode = game_mode(game)
        if mode not in KNOWN_MODES:
            errors.append(
                f"{name}: unknown mode '{mode}' "
                f"(expected one of {sorted(KNOWN_MODES)})"
            )

        # --- keyword rules ----------------------------------------------
        # Non-authoritative modes go through detect_keywords() to decide
        # is_update, so without a 'high' list they can never log.
        if mode not in AUTHORITATIVE_MODES:
            referenced.add(name)
            if not has_keywords(keyword_rules, name):
                errors.append(
                    f"{name}: mode '{mode}' needs keyword rules, but "
                    f"{KEYWORDS_FILE} has no non-empty 'high' list for it "
                    f"(this game can never log an update)"
                )

    # --- orphaned keyword entries ---------------------------------------
    # Keyword blocks that don't match any game are harmless but usually a
    # rename typo — worth flagging.
    for kw_name in (keyword_rules or {}):
        if kw_name not in seen_names:
            warnings.append(
                f"{KEYWORDS_FILE}: '{kw_name}' has keyword rules but no "
                f"matching game in {GAMES_FILE} (renamed or removed?)"
            )

    return errors, warnings


def main():
    ap = argparse.ArgumentParser(description="Validate the bot's game config.")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    games_cfg = load_yaml(GAMES_FILE)
    keyword_rules = load_yaml(KEYWORDS_FILE) or {}

    errors, warnings = validate(games_cfg, keyword_rules)

    game_count = len((games_cfg or {}).get("games") or [])
    print(f"Validating {game_count} games in {GAMES_FILE}\n")

    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")

    print()
    if errors:
        print(f"❌ {len(errors)} error(s), {len(warnings)} warning(s)")
        sys.exit(1)
    if warnings and args.strict:
        print(f"❌ {len(warnings)} warning(s) (--strict)")
        sys.exit(1)
    print(f"✅ config valid — {len(warnings)} warning(s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
