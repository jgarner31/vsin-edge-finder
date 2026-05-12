"""
main.py
-------
VSIN Edge Finder — Main Polling Loop

Run this locally:
    python main.py

On Railway, Railway runs it automatically via the Procfile.

What happens every POLL_INTERVAL_SECONDS:
  1. Fetch Circa + DraftKings splits for MLB / NBA / NHL from VSIN Pro
  2. Scrape today's VSIN expert pick articles (Makinen / Cohen / Davis)
  3. Fetch Polymarket moneyline probabilities for confirmation
  4. Score every game with scorer.score_all_games()
  5. Update the Flask dashboard state
  6. Print a terminal summary
  7. Send Discord alert if BET signals exist and picks have changed
"""

import os
import sys
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (
    ACTIVE_SPORTS,
    POLL_INTERVAL_SECONDS,
    ACTIVE_HOURS_START,
    ACTIVE_HOURS_END,
    LEAN_THRESHOLD,
    BET_THRESHOLD,
)
from vsin_scraper      import fetch_all_splits
from expert_scraper    import fetch_expert_picks
from polymarket        import fetch_polymarket_for_splits
from scorer            import score_all_games
from discord_alert     import send_top_plays_summary, send_startup_message
from web_app           import run_web_server, dashboard_state, manual_refresh_event

CENTRAL = ZoneInfo("America/Chicago")

# Track last Discord alert signature so we don't spam on every poll
_last_discord_signature: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clear():
    if os.name == "nt":
        os.system("cls")
    elif os.environ.get("TERM"):
        os.system("clear")


def _is_active_hours() -> bool:
    now_ct = datetime.now(CENTRAL)
    return ACTIVE_HOURS_START <= now_ct.hour < ACTIVE_HOURS_END


def _sleep_until_active():
    """
    Wait until active hours, waking early if a manual refresh fires.
    Returns True if interrupted by a manual refresh.
    """
    from datetime import timedelta
    while True:
        now_ct     = datetime.now(CENTRAL)
        next_start = now_ct.replace(hour=ACTIVE_HOURS_START, minute=0, second=0, microsecond=0)
        if now_ct >= next_start:
            next_start += timedelta(days=1)
        wait_sec = max(1, (next_start - now_ct).total_seconds())
        print(
            f"  💤  Outside active hours ({ACTIVE_HOURS_START}AM–"
            f"{ACTIVE_HOURS_END % 12 or 12}PM CT). "
            f"Next poll at {next_start.strftime('%I:%M %p CT')}."
        )
        if manual_refresh_event.wait(timeout=min(wait_sec, 60)):
            manual_refresh_event.clear()
            return True
        if _is_active_hours():
            return False


def _wait_for_next_poll() -> bool:
    """
    Wait POLL_INTERVAL_SECONDS, waking early if a manual refresh fires.
    Returns True if interrupted by a manual refresh.
    """
    if manual_refresh_event.wait(timeout=POLL_INTERVAL_SECONDS):
        manual_refresh_event.clear()
        return True
    return False


def _top_picks_signature(scored_by_sport: dict) -> str:
    """Fingerprint the top picks so we know if they've meaningfully changed."""
    parts = []
    for sport, games in sorted(scored_by_sport.items()):
        for g in games:
            top = g.get("top_pick", {})
            if top.get("label") in ("BET", "LEAN"):
                parts.append(f"{g['game_key']}:{top.get('side', '')}:{top['score']}")
    return "|".join(parts)


def _print_dashboard(scored_by_sport: dict, poll_count: int):
    """Print a compact terminal summary."""
    _clear()
    now = datetime.now(CENTRAL).strftime("%Y-%m-%d %I:%M %p CT")
    print("=" * 70)
    print(f"  🎯  VSIN EDGE FINDER   |   Poll #{poll_count}   |   {now}")
    print("=" * 70)

    any_signal = False
    for sport in ACTIVE_SPORTS:
        games = scored_by_sport.get(sport, [])
        if not games:
            continue
        signals = [g for g in games if g["has_signal"]]
        print(f"\n  {sport} — {len(games)} games, {len(signals)} with signal")
        for g in games:
            top = g.get("top_pick", {})
            if not g["has_signal"]:
                continue
            any_signal = True
            label = top.get("label", "PASS")
            score = top.get("score", 0)
            team  = top.get("team", "?")
            side  = top.get("side", "?")
            print(
                f"    [{label:4s} {score}/10]  {g['game']:<35}  "
                f"→ {team} ({side})"
            )

    if not any_signal:
        print("\n  ✅  No LEAN or BET signals this poll.")

    print(f"\n  Next check in {POLL_INTERVAL_SECONDS // 60} min  |  Ctrl+C to stop")
    print("=" * 70)


# ─────────────────────────────────────────────────────────────────────────────
# Main poll cycle
# ─────────────────────────────────────────────────────────────────────────────

def _run_poll_cycle(poll_count: int):
    """
    One complete fetch-score-alert cycle.
    All errors are caught so the loop never crashes.
    """
    global _last_discord_signature

    manual_refresh = dashboard_state.get("refresh_status") == "queued"
    dashboard_state["refresh_status"]  = "running"
    dashboard_state["refresh_message"] = (
        "Manual refresh in progress..."
        if manual_refresh else
        "Scheduled poll in progress..."
    )

    # ── 1. VSIN splits ───────────────────────────────────────────────────────
    try:
        splits_by_sport = fetch_all_splits(ACTIVE_SPORTS)
    except Exception as e:
        print(f"[ERROR] fetch_all_splits failed: {e}")
        dashboard_state["refresh_status"]  = "error"
        dashboard_state["refresh_message"] = f"VSIN fetch failed: {e}"
        return

    # ── 2. Expert picks ──────────────────────────────────────────────────────
    expert_by_sport: dict = {}
    try:
        expert_by_sport = fetch_expert_picks()
        for sport, picks in expert_by_sport.items():
            print(f"[EXPERT] {sport}: {len(picks)} pick(s) parsed.")
    except Exception as e:
        print(f"[WARN] expert_scraper failed: {e}")

    # ── 3. Polymarket ────────────────────────────────────────────────────────
    poly_data: dict = {}
    try:
        poly_data = fetch_polymarket_for_splits(splits_by_sport)
    except Exception as e:
        print(f"[WARN] polymarket fetch failed: {e}")

    # ── 4. Score everything ──────────────────────────────────────────────────
    try:
        scored_by_sport = score_all_games(
            splits_by_sport,
            expert_picks_by_sport=expert_by_sport,
            polymarket_data=poly_data,
        )
    except Exception as e:
        print(f"[ERROR] scorer failed: {e}")
        dashboard_state["refresh_status"]  = "error"
        dashboard_state["refresh_message"] = f"Scoring failed: {e}"
        return

    # Attach the raw merged dict to each scored game for the web dashboard
    # (needed to render ML lines in pick cards)
    for sport, games in scored_by_sport.items():
        for g in games:
            gk = g.get("game_key", "")
            raw_entry = splits_by_sport.get(sport, {}).get(gk, {})
            g["_merged"] = raw_entry.get("merged", {})

    # ── 5. Update dashboard state ────────────────────────────────────────────
    dashboard_state["scored_by_sport"] = scored_by_sport
    dashboard_state["expert_by_sport"] = expert_by_sport
    dashboard_state["splits_by_sport"] = splits_by_sport
    dashboard_state["last_updated"]    = datetime.now(CENTRAL)
    dashboard_state["poll_count"]      = poll_count
    dashboard_state["refresh_status"]  = "done"
    dashboard_state["refresh_message"] = (
        "Manual refresh complete — latest VSIN splits are on the board."
        if manual_refresh else
        "Scheduled poll completed successfully."
    )

    # ── 6. Terminal dashboard ────────────────────────────────────────────────
    _print_dashboard(scored_by_sport, poll_count)

    # ── 7. Discord alerts ────────────────────────────────────────────────────
    sig = _top_picks_signature(scored_by_sport)
    if sig and sig != _last_discord_signature:
        send_top_plays_summary(scored_by_sport, poll_count)
        _last_discord_signature = sig


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    active_end_label = f"{ACTIVE_HOURS_END % 12 or 12}PM"
    print("🎯 VSIN Edge Finder starting up...")
    print(f"   Sports: {', '.join(ACTIVE_SPORTS)}")
    print(f"   Active hours: {ACTIVE_HOURS_START}AM – {active_end_label} Central")
    print(f"   Poll interval: every {POLL_INTERVAL_SECONDS // 60} minutes")
    print()

    # Start the web dashboard in a background thread
    # daemon=True means it shuts down automatically when the main loop exits
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    print(f"   🌐 Web dashboard running on port {os.environ.get('PORT', 8080)}")

    send_startup_message()

    poll_count = 0

    while True:
        # Sleep outside active hours (skip in first poll so startup is immediate)
        if poll_count > 0 and not _is_active_hours():
            manual_early = _sleep_until_active()
            if not manual_early:
                continue

        poll_count += 1
        _run_poll_cycle(poll_count)

        try:
            _wait_for_next_poll()
        except KeyboardInterrupt:
            print("\n\n  Tracker stopped. Good luck out there. 🎯\n")
            sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Tracker stopped.\n")
        sys.exit(0)
