"""
discord_alert.py
----------------
VSIN Edge Finder — Discord Alerts

Sends BET and LEAN alerts to a Discord webhook.
Only fires when score >= DISCORD_MIN_SCORE (default: BET threshold = 7).

One startup message + one curated top-plays summary per meaningful change.
"""

import json
import requests
from config import DISCORD_WEBHOOK_URL, DISCORD_MIN_SCORE

# Label → Discord embed sidebar color (decimal int)
_COLORS = {
    "BET":  0x32CD32,   # green
    "LEAN": 0xFFD700,   # gold
    "PASS": 0x555555,   # grey (shouldn't get here)
}

_EMOJIS = {
    "BET":  "🎯",
    "LEAN": "👀",
    "MLB":  "⚾",
    "NBA":  "🏀",
    "NHL":  "🏒",
    "WNBA": "🏀",
}

_MARKET_LABELS = {
    "away_ml": "Away Moneyline",
    "home_ml": "Home Moneyline",
    "over":    "Over",
    "under":   "Under",
}


def _webhook_ready() -> bool:
    return bool(DISCORD_WEBHOOK_URL and DISCORD_WEBHOOK_URL != "YOUR_DISCORD_WEBHOOK_URL_HERE")


def _post(payload: dict):
    if not _webhook_ready():
        return
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            print(f"[DISCORD] Unexpected status {resp.status_code}: {resp.text[:120]}")
    except requests.exceptions.RequestException as e:
        print(f"[DISCORD] Request failed: {e}")


def _fmt_ml(odds) -> str:
    if odds is None:
        return "—"
    return f"+{odds}" if odds > 0 else str(odds)


def _build_pick_embed(game: dict, market_key: str) -> dict:
    """
    Build a Discord embed for one scored pick (away_ml / home_ml / over / under).

    game       — scored game dict from scorer.score_game()
    market_key — "away_ml" | "home_ml" | "over" | "under"
    """
    pick   = game[market_key]
    sport  = game["sport"]
    label  = pick["label"]
    score  = pick["score"]
    sport_emoji = _EMOJIS.get(sport, "🏟️")
    label_emoji = _EMOJIS.get(label, "⚠️")
    mkt_label   = _MARKET_LABELS.get(market_key, market_key)

    team = pick.get("team") or pick.get("side", "").upper()
    line = pick.get("line")   # totals line if available

    desc_parts = [f"**{label_emoji} {label}** — {mkt_label}"]
    if market_key in ("away_ml", "home_ml"):
        ml = game["merged"].get(f"{pick['side']}_ml") if "merged" in game else None
        if ml is not None:
            desc_parts.append(f"**Line:** {_fmt_ml(ml)}")
    else:
        if line is not None:
            desc_parts.append(f"**Line:** {line}")

    desc_parts.append(f"**Score:** {score}/10")
    desc_parts.append("")

    if pick.get("reasons"):
        desc_parts.append("**Signals:**")
        for r in pick["reasons"]:
            desc_parts.append(f"• {r}")

    # Raw splits summary
    sb = pick.get("signal_breakdown", {})
    circa_gap = sb.get("circa_gap")
    opp_dk    = sb.get("opp_dk_bets")
    if circa_gap is not None:
        desc_parts.append(f"\nCirca gap: **+{circa_gap:.0f}pt** (handle vs tickets)")
    if opp_dk is not None:
        desc_parts.append(f"Opponent DK tickets: **{opp_dk:.0f}%**")

    return {
        "title":       f"{sport_emoji} {game['game']}",
        "description": "\n".join(desc_parts),
        "color":       _COLORS.get(label, 0x555555),
        "footer":      {"text": f"VSIN Edge Finder • {sport} • Score {score}/10"},
    }


def send_top_plays_summary(scored_games_by_sport: dict, poll_count: int):
    """
    Send a single Discord message summarising all BET/LEAN picks across sports.
    Called after every poll where signals exist.

    scored_games_by_sport — output of scorer.score_all_games()
    """
    if not _webhook_ready():
        print("[DISCORD] Webhook not configured — skipping alert.")
        return

    all_picks = []
    for sport, games in scored_games_by_sport.items():
        for g in games:
            top = g.get("top_pick", {})
            if top.get("score", 0) >= DISCORD_MIN_SCORE:
                all_picks.append((g, top))

    if not all_picks:
        return

    # Sort: BETs first, then by score desc
    all_picks.sort(key=lambda x: (0 if x[1]["label"] == "BET" else 1, -x[1]["score"]))

    embeds = []
    for game, top in all_picks[:5]:   # Discord max 10 embeds; keep top 5
        # Find which market key this top pick came from
        market_key = next(
            (k for k in ("away_ml", "home_ml", "over", "under")
             if game.get(k) is top),
            "away_ml",
        )
        embeds.append(_build_pick_embed(game, market_key))

    # Summary title embed
    bet_count  = sum(1 for _, t in all_picks if t["label"] == "BET")
    lean_count = sum(1 for _, t in all_picks if t["label"] == "LEAN")
    summary = {
        "title":       "🎯 VSIN Edge Finder — Top Plays",
        "description": f"Poll #{poll_count} complete\n"
                       f"**{bet_count} BET** | **{lean_count} LEAN** signals across all sports",
        "color":       0x1E90FF,
        "footer":      {"text": "VSIN Pro • Circa sharp splits + DK fade + Expert consensus"},
    }

    payload = {"embeds": [summary] + embeds}
    _post(payload)
    print(f"[DISCORD] Sent top plays summary ({len(embeds)} picks).")


def send_startup_message():
    """Fires when the tracker starts up on Railway."""
    if not _webhook_ready():
        return
    payload = {
        "content": (
            "🎯 **VSIN Edge Finder is now running.**\n"
            "Monitoring MLB ⚾ · NBA 🏀 · NHL 🏒 for Circa sharp action. "
            "Alerts will appear here."
        )
    }
    _post(payload)
