"""
web_app.py
----------
VSIN Edge Finder — Flask Dashboard

Self-refreshing multi-sport dashboard for MLB, NBA, and NHL.
Auto-refreshes every 3 minutes in the browser. Has a manual
"Refresh Now" button that triggers an immediate poll.

The main polling loop (main.py) writes to dashboard_state.
This Flask app reads from it to build the page on every request.
Think of dashboard_state as a shared whiteboard between two workers.
"""

import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, redirect, url_for, request

from config import ACTIVE_HOURS_START, ACTIVE_HOURS_END
import bet_tracker

app = Flask(__name__)
CENTRAL = ZoneInfo("America/Chicago")

manual_refresh_event = threading.Event()
MANUAL_REFRESH_COOLDOWN_SECONDS = 60

# ─────────────────────────────────────────────────────────────────────────────
# Shared state — main.py writes here; Flask reads from it
# ─────────────────────────────────────────────────────────────────────────────
dashboard_state = {
    "scored_by_sport":    {},      # {sport: [scored_game, ...]}
    "expert_by_sport":    {},      # {sport: [pick_dicts]}
    "splits_by_sport":    {},      # raw splits for reference table
    "last_updated":       None,    # datetime of last successful poll
    "poll_count":         0,
    "refresh_status":     "idle",
    "refresh_message":    "",
    "last_refresh_request": None,
}


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_ml(odds) -> str:
    if odds is None:
        return "—"
    return f"+{odds}" if odds > 0 else str(odds)


def _fmt_pct(val) -> str:
    if val is None:
        return "—"
    return f"{int(val)}%"


def _to_ct(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(CENTRAL)
    return dt.astimezone(CENTRAL)


def _label_color(label: str) -> str:
    return {"BET": "#32CD32", "LEAN": "#FFD700"}.get(label, "#555")


def _label_bg(label: str) -> str:
    return {"BET": "#143d1f", "LEAN": "#3d3514"}.get(label, "#222")


def _border_color(label: str) -> str:
    return {"BET": "#2d7d46", "LEAN": "#7c6721"}.get(label, "#333")


def _badge(label: str) -> str:
    color  = _label_color(label)
    bg     = _label_bg(label)
    return (
        f'<span style="background:{bg};color:{color};padding:4px 10px;'
        f'border-radius:999px;font-size:0.78em;font-weight:bold;">{label}</span>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Page sections
# ─────────────────────────────────────────────────────────────────────────────

def _pick_card_html(game: dict, market_key: str) -> str:
    """Render one pick as a compact card."""
    pick   = game[market_key]
    label  = pick["label"]
    score  = pick["score"]
    sport  = game["sport"]
    sport_emoji = {"MLB": "⚾", "NBA": "🏀", "NHL": "🏒"}.get(sport, "🏟️")
    mkt_labels = {
        "away_ml": "Away ML",
        "home_ml": "Home ML",
        "over":    "Over",
        "under":   "Under",
    }

    team = pick.get("team", "")
    side = pick.get("side", "").upper()
    line_str = ""
    if market_key in ("away_ml", "home_ml"):
        ml_val = game.get("away_ml" if side == "AWAY" else "home_ml", {}).get("signal_breakdown", {})
        raw_ml = (
            game.get("splits_entry", {}).get("merged", {}).get(f"{pick.get('side')}_ml")
        )
        # Try merged dict from splits
        merged = game.get("_merged", {})
        raw_ml = merged.get(f"{pick.get('side')}_ml")
        if raw_ml is not None:
            line_str = f" <span style='color:#aaa;font-size:0.85em;'>({_fmt_ml(raw_ml)})</span>"
    else:
        ln = pick.get("line")
        if ln is not None:
            line_str = f" <span style='color:#aaa;font-size:0.85em;'>({ln})</span>"

    reasons_html = ""
    if pick.get("reasons"):
        items = "".join(f'<li style="margin-bottom:3px;">{r}</li>' for r in pick["reasons"])
        reasons_html = f'<ul style="margin:8px 0 0 0;padding-left:18px;font-size:0.85em;color:#bbb;line-height:1.5;">{items}</ul>'

    # Pre-fill values for the Log Bet form
    raw_ml = game.get("_merged", {}).get(f"{pick.get('side')}_ml") if market_key in ("away_ml", "home_ml") else None
    alert_odds_val = raw_ml if raw_ml is not None else ""

    return f"""
    <div style="background:#171717;border:1px solid {_border_color(label)};
                border-radius:12px;padding:16px;margin-bottom:12px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">
            <div>
                <div style="font-size:1.05em;font-weight:bold;">
                    {sport_emoji} {game['game']}
                </div>
                <div style="color:#888;font-size:0.85em;margin-top:3px;">
                    {sport} &nbsp;·&nbsp; {mkt_labels.get(market_key, market_key)}
                    &nbsp;·&nbsp; <strong style="color:#e0e0e0;">{team}</strong>{line_str}
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
                {_badge(label)}
                <span style="color:{_label_color(label)};font-weight:bold;font-size:1.1em;">{score}/10</span>
            </div>
        </div>
        {reasons_html}
        <form method="post" action="/bets/log" style="margin-top:12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
            <input type="hidden" name="sport"        value="{sport}">
            <input type="hidden" name="game"         value="{game['game']}">
            <input type="hidden" name="side"         value="{pick.get('side', '')}">
            <input type="hidden" name="team"         value="{team}">
            <input type="hidden" name="market"       value="{market_key}">
            <input type="hidden" name="signal_score" value="{score}">
            <input type="hidden" name="alert_odds"   value="{alert_odds_val}">
            <input type="number" name="bet_odds"  placeholder="Odds (e.g. +158)"
                   style="width:130px;background:#222;color:#e0e0e0;border:1px solid #444;
                          border-radius:6px;padding:6px 10px;font-size:0.88em;">
            <input type="number" name="units" value="1" min="0.1" max="10" step="0.5"
                   style="width:80px;background:#222;color:#e0e0e0;border:1px solid #444;
                          border-radius:6px;padding:6px 10px;font-size:0.88em;">
            <span style="color:#777;font-size:0.82em;">units</span>
            <button type="submit"
                    style="background:#2d7d46;color:white;border:none;border-radius:6px;
                           padding:6px 14px;font-size:0.85em;cursor:pointer;font-weight:bold;">
                📝 Log Bet
            </button>
        </form>
    </div>"""


def _top_picks_section(scored_by_sport: dict) -> str:
    all_picks = []
    for sport, games in scored_by_sport.items():
        for g in games:
            top = g.get("top_pick", {})
            if top.get("label") in ("BET", "LEAN"):
                # Find which key holds this top pick
                for mk in ("away_ml", "home_ml", "over", "under"):
                    if g.get(mk) is top:
                        all_picks.append((g, mk, top["score"], top["label"]))
                        break

    if not all_picks:
        return '<p style="color:#888;text-align:center;padding:20px;">No BET or LEAN signals on the board yet.</p>'

    all_picks.sort(key=lambda x: (0 if x[3] == "BET" else 1, -x[2]))
    return "".join(_pick_card_html(g, mk) for g, mk, _, _ in all_picks)


def _splits_table_html(games_dict: dict, sport: str) -> str:
    """Compact Circa + DK splits reference table for one sport."""
    if not games_dict:
        return f'<p style="color:#888;padding:10px;">No {sport} splits available.</p>'

    rows = []
    for game_key, entry in games_dict.items():
        merged = entry.get("merged", {})
        away   = merged.get("away_team", "?")
        home   = merged.get("home_team", "?")

        # Circa ML
        c_a_h = _fmt_pct(entry.get("circa", {}).get("moneyline", {}).get("away", {}).get("handle_pct"))
        c_a_b = _fmt_pct(entry.get("circa", {}).get("moneyline", {}).get("away", {}).get("bets_pct"))
        c_h_h = _fmt_pct(entry.get("circa", {}).get("moneyline", {}).get("home", {}).get("handle_pct"))
        c_h_b = _fmt_pct(entry.get("circa", {}).get("moneyline", {}).get("home", {}).get("bets_pct"))

        # DK ML
        d_a_b = _fmt_pct(merged.get("dk_ml_bets_away"))
        d_h_b = _fmt_pct(merged.get("dk_ml_bets_home"))

        # Gaps
        gap_a = merged.get("circa_ml_gap_away")
        gap_h = merged.get("circa_ml_gap_home")
        gap_a_str = f'<span style="color:#32CD32;">+{gap_a:.0f}</span>' if gap_a and gap_a >= 12 else (f"{gap_a:.0f}" if gap_a else "—")
        gap_h_str = f'<span style="color:#32CD32;">+{gap_h:.0f}</span>' if gap_h and gap_h >= 12 else (f"{gap_h:.0f}" if gap_h else "—")

        # Lines
        away_ml = _fmt_ml(merged.get("away_ml"))
        home_ml = _fmt_ml(merged.get("home_ml"))
        total   = merged.get("total_line")
        total_str = f"{total}" if total else "—"

        rows.append(f"""
        <tr style="border-bottom:1px solid #222;">
            <td style="padding:8px 12px;font-weight:bold;">{away} @ {home}</td>
            <td style="padding:8px 12px;">{away_ml} / {home_ml}</td>
            <td style="padding:8px 12px;">{c_a_h}/{c_a_b} &nbsp;|&nbsp; {c_h_h}/{c_h_b}</td>
            <td style="padding:8px 12px;">{gap_a_str} / {gap_h_str}</td>
            <td style="padding:8px 12px;">{d_a_b} / {d_h_b}</td>
            <td style="padding:8px 12px;">{total_str}</td>
        </tr>""")

    return f"""
    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:0.88em;">
        <thead>
            <tr style="background:#1a1a1a;color:#888;font-size:0.82em;text-transform:uppercase;letter-spacing:0.5px;">
                <th style="padding:8px 12px;text-align:left;">Game</th>
                <th style="padding:8px 12px;text-align:left;">ML Away / Home</th>
                <th style="padding:8px 12px;text-align:left;">Circa Hnd% / Bets%</th>
                <th style="padding:8px 12px;text-align:left;">Gap Away / Home</th>
                <th style="padding:8px 12px;text-align:left;">DK Bets%</th>
                <th style="padding:8px 12px;text-align:left;">Total</th>
            </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
    </table>
    </div>"""


def _expert_picks_html(expert_by_sport: dict) -> str:
    all_picks = []
    for sport, picks in expert_by_sport.items():
        for p in picks:
            all_picks.append((sport, p))

    if not all_picks:
        return '<p style="color:#888;text-align:center;padding:16px;">No expert picks parsed yet.</p>'

    rows = []
    for sport, p in all_picks:
        sport_emoji = {"MLB": "⚾", "NBA": "🏀", "NHL": "🏒"}.get(sport, "")
        conviction  = (p.get("conviction") or "").upper()
        fade_badge  = ' <span style="color:#FF6B6B;font-size:0.78em;">[FADE]</span>' if p.get("is_fade") else ""
        rows.append(
            f'<div style="border-bottom:1px solid #222;padding:8px 0;">'
            f'<span style="color:#888;font-size:0.85em;">{sport_emoji} {sport}</span>'
            f'&nbsp;&nbsp;<strong>{p.get("team", "?")}</strong>{fade_badge}'
            f'&nbsp;&nbsp;<span style="color:#aaa;font-size:0.85em;">{p.get("author", "?")}</span>'
            f'&nbsp;&nbsp;<span style="color:#FFD700;font-size:0.82em;">{conviction}</span>'
            f'</div>'
        )

    return f'<div style="font-size:0.9em;">{"".join(rows)}</div>'


def _bet_tracker_summary_html() -> str:
    """Compact stats bar + pending bets for the main dashboard."""
    try:
        stats   = bet_tracker.get_stats()
        pending = bet_tracker.get_pending_bets()
    except Exception:
        return '<p style="color:#888;font-size:0.85em;">Bet tracker initializing...</p>'

    # ── Stats bar ─────────────────────────────────────────────────────────
    pnl_color = "#32CD32" if stats["total_units"] >= 0 else "#FF6B6B"
    clv_color = "#32CD32" if (stats.get("avg_clv") or 0) >= 0 else "#FF6B6B"

    stats_html = f"""
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;">
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 16px;min-width:100px;text-align:center;">
            <div style="font-size:1.3em;font-weight:bold;">{stats['total_bets']}</div>
            <div style="color:#777;font-size:0.78em;">Total Bets</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 16px;min-width:100px;text-align:center;">
            <div style="font-size:1.3em;font-weight:bold;">{bet_tracker.fmt_pct(stats['win_rate'])}</div>
            <div style="color:#777;font-size:0.78em;">Win Rate</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 16px;min-width:100px;text-align:center;">
            <div style="font-size:1.3em;font-weight:bold;color:{pnl_color};">{bet_tracker.fmt_units(stats['total_units'])}</div>
            <div style="color:#777;font-size:0.78em;">P&amp;L (units)</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 16px;min-width:100px;text-align:center;">
            <div style="font-size:1.3em;font-weight:bold;">{bet_tracker.fmt_pct(stats['roi_pct'])}</div>
            <div style="color:#777;font-size:0.78em;">ROI</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 16px;min-width:100px;text-align:center;">
            <div style="font-size:1.3em;font-weight:bold;color:{clv_color};">{bet_tracker.fmt_clv(stats['avg_clv'])}</div>
            <div style="color:#777;font-size:0.78em;">Avg CLV</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 16px;min-width:100px;text-align:center;">
            <div style="font-size:1.3em;font-weight:bold;">{stats['pending']}</div>
            <div style="color:#777;font-size:0.78em;">Pending</div>
        </div>
    </div>"""

    if not pending:
        pending_html = '<p style="color:#777;font-size:0.85em;">No pending bets. Log a bet from a pick card above.</p>'
    else:
        rows = []
        for b in pending:
            rows.append(f"""
            <div style="background:#1a1a1a;border-radius:8px;padding:12px 14px;margin-bottom:8px;
                        display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <div>
                    <div style="font-weight:bold;font-size:0.95em;">{b['game']}</div>
                    <div style="color:#888;font-size:0.82em;margin-top:2px;">
                        {b['sport']} · {b['market']} · {b['team']} ·
                        {bet_tracker.fmt_odds(b['bet_odds'])} · {b['units']}u · Score {b['signal_score']}/10
                    </div>
                </div>
                <form method="post" action="/bets/update"
                      style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
                    <input type="hidden" name="bet_id" value="{b['id']}">
                    <input type="number" name="closing_line" placeholder="Closing odds"
                           style="width:130px;background:#222;color:#e0e0e0;border:1px solid #444;
                                  border-radius:6px;padding:5px 8px;font-size:0.85em;">
                    <select name="result"
                            style="background:#222;color:#e0e0e0;border:1px solid #444;
                                   border-radius:6px;padding:5px 8px;font-size:0.85em;">
                        <option value="win">Win ✅</option>
                        <option value="loss">Loss ❌</option>
                        <option value="push">Push ➖</option>
                        <option value="no_bet">No Bet 🚫</option>
                    </select>
                    <button type="submit"
                            style="background:#1E90FF;color:white;border:none;border-radius:6px;
                                   padding:5px 12px;font-size:0.85em;cursor:pointer;font-weight:bold;">
                        Update
                    </button>
                </form>
            </div>""")
        pending_html = "".join(rows)

    return stats_html + pending_html


def _build_page() -> str:
    state = dashboard_state
    now   = datetime.now(CENTRAL).strftime("%Y-%m-%d %I:%M:%S %p CT")

    last_updated = _to_ct(state["last_updated"])
    last_upd_str = last_updated.strftime("%Y-%m-%d %I:%M:%S %p CT") if last_updated else None
    last_req = _to_ct(state.get("last_refresh_request"))
    last_req_str = last_req.strftime("%Y-%m-%d %I:%M:%S %p CT") if last_req else "Never"

    scored_by_sport = state.get("scored_by_sport") or {}
    expert_by_sport = state.get("expert_by_sport") or {}
    splits_by_sport = state.get("splits_by_sport") or {}

    refresh_status  = state.get("refresh_status", "idle")
    refresh_message = state.get("refresh_message", "")
    refresh_color = {
        "idle": "#888", "queued": "#FFD700", "running": "#87ceeb",
        "done": "#32CD32", "cooldown": "#FFA500", "error": "#FF6B6B",
    }.get(refresh_status, "#888")

    if last_upd_str:
        status_html = (
            f'<span style="color:#32CD32;">●</span> Last updated: {last_upd_str} '
            f'&nbsp;|&nbsp; Poll #{state["poll_count"]}'
        )
    else:
        status_html = '<span style="color:#FFA500;">●</span> Waiting for first data poll...'

    top_picks_html = _top_picks_section(scored_by_sport)

    # Build per-sport splits tabs
    sport_sections = ""
    for sport in ("MLB", "NBA", "NHL"):
        games_dict = splits_by_sport.get(sport, {})
        emoji = {"MLB": "⚾", "NBA": "🏀", "NHL": "🏒"}.get(sport, "")
        count = len(games_dict)
        sport_sections += f"""
        <h2>{emoji} {sport} — {count} game{"s" if count != 1 else ""}</h2>
        {_splits_table_html(games_dict, sport)}
        """

    expert_html  = _expert_picks_html(expert_by_sport)
    tracker_html = _bet_tracker_summary_html()

    active_end_label = f"{ACTIVE_HOURS_END % 12 or 12}PM"
    active_label = f"{ACTIVE_HOURS_START}AM – {active_end_label} CT"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="180">
    <title>🎯 VSIN Edge Finder</title>
    <style>
        * {{ box-sizing:border-box; }}
        body {{ margin:0;padding:0;background:#121212;color:#e0e0e0;
               font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
        h1   {{ margin:0;font-size:1.4em; }}
        h2   {{ font-size:1.05em;color:#aaa;margin:28px 0 10px;text-transform:uppercase;
               letter-spacing:1px;border-bottom:1px solid #2a2a2a;padding-bottom:6px; }}
        a    {{ color:#87ceeb; }}
        button:hover {{ opacity:0.85; }}
    </style>
</head>
<body>
<div style="max-width:960px;margin:0 auto;padding:20px;">

    <!-- Header -->
    <div style="background:#1a1a1a;border-radius:8px;padding:16px 20px;margin-bottom:18px;
                display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
        <h1>🎯 VSIN Edge Finder</h1>
        <div style="font-size:0.82em;color:#666;">
            Page auto-refreshes every 3 min &nbsp;|&nbsp; {now}
        </div>
    </div>

    <!-- Status bar -->
    <div style="background:#1a1a1a;border-radius:6px;padding:10px 16px;margin-bottom:14px;
                font-size:0.88em;color:#ccc;">
        {status_html} &nbsp;|&nbsp; Active hours: {active_label}
    </div>

    <!-- Manual Refresh -->
    <div style="background:#1a1a1a;border-radius:6px;padding:14px 16px;margin-bottom:22px;
                display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
        <div>
            <div style="font-weight:bold;font-size:0.95em;">Need fresh splits?</div>
            <div style="font-size:0.85em;color:{refresh_color};margin-top:3px;">
                {refresh_message or "Click Refresh Now to pull latest VSIN splits immediately."}
            </div>
            <div style="font-size:0.78em;color:#555;margin-top:4px;">Last request: {last_req_str}</div>
        </div>
        <form method="post" action="/refresh" style="margin:0;">
            <button type="submit"
                    style="background:#1E90FF;color:white;border:none;border-radius:6px;
                           padding:10px 18px;font-weight:bold;cursor:pointer;font-size:0.9em;">
                🔄 Refresh Now
            </button>
        </form>
    </div>

    <!-- Top Picks -->
    <h2>🎯 Top Plays — BET &amp; LEAN signals</h2>
    {top_picks_html}

    <!-- Bet Tracker Summary -->
    <h2>📊 Bet Tracker &nbsp;<a href="/bets" style="font-size:0.8em;font-weight:normal;color:#87ceeb;">View full log →</a></h2>
    {tracker_html}

    <!-- Expert Picks -->
    <h2>📰 VSIN Expert Picks (today's articles)</h2>
    <div style="background:#171717;border:1px solid #2a2a2a;border-radius:10px;padding:14px;">
        {expert_html}
    </div>

    <!-- Per-sport splits reference -->
    <div style="margin-top:8px;">
        {sport_sections}
    </div>

    <!-- Footer -->
    <div style="margin-top:32px;font-size:0.78em;color:#444;text-align:center;">
        VSIN Edge Finder &nbsp;·&nbsp; Circa sharp splits + DK public fade + Expert consensus + Polymarket<br>
        Signals: Circa handle% − bets% gap (sharp $) · DK bets% fade · VSIN Pro expert picks · Polymarket edge
    </div>

</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return _build_page()


@app.route("/health")
def health():
    return {
        "status":     "ok",
        "poll_count": dashboard_state["poll_count"],
    }, 200


@app.route("/refresh", methods=["POST"])
def refresh():
    now = datetime.now(CENTRAL)
    last = _to_ct(dashboard_state.get("last_refresh_request"))
    if last:
        elapsed = (now - last).total_seconds()
        if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
            wait = int(MANUAL_REFRESH_COOLDOWN_SECONDS - elapsed)
            dashboard_state["refresh_status"]  = "cooldown"
            dashboard_state["refresh_message"] = (
                f"Cooling down. Try again in {wait}s."
            )
            return redirect(url_for("index"))

    dashboard_state["last_refresh_request"] = now
    dashboard_state["refresh_status"]  = "queued"
    dashboard_state["refresh_message"] = "Manual refresh queued. Pulling latest VSIN splits now..."
    manual_refresh_event.set()
    return redirect(url_for("index"))


@app.route("/bets/log", methods=["POST"])
def bets_log():
    """Log a new bet from a pick card form."""
    f = request.form
    try:
        bet_odds = int(f.get("bet_odds") or f.get("alert_odds") or 0) or None
    except (ValueError, TypeError):
        bet_odds = None
    try:
        units = float(f.get("units") or 1.0)
    except (ValueError, TypeError):
        units = 1.0
    try:
        alert_odds = int(f.get("alert_odds") or 0) or None
    except (ValueError, TypeError):
        alert_odds = None

    bet_tracker.log_bet(
        sport        = f.get("sport", ""),
        game         = f.get("game", ""),
        side         = f.get("side", ""),
        team         = f.get("team", ""),
        market       = f.get("market", ""),
        signal_score = int(f.get("signal_score") or 0),
        alert_odds   = alert_odds,
        bet_odds     = bet_odds,
        units        = units,
        notes        = f.get("notes", ""),
    )
    return redirect(url_for("index"))


@app.route("/bets/update", methods=["POST"])
def bets_update():
    """Update a pending bet with result and optional closing line."""
    f = request.form
    try:
        bet_id = int(f.get("bet_id", 0))
    except (ValueError, TypeError):
        return redirect(url_for("bets_log_page"))

    result = f.get("result", "pending")
    try:
        closing_line = int(f.get("closing_line") or 0) or None
    except (ValueError, TypeError):
        closing_line = None

    bet_tracker.update_result(bet_id, result, closing_line, f.get("notes"))
    return redirect(url_for("bets_log_page"))


@app.route("/bets/delete", methods=["POST"])
def bets_delete():
    """Delete a bet (logged by mistake)."""
    try:
        bet_id = int(request.form.get("bet_id", 0))
        bet_tracker.delete_bet(bet_id)
    except (ValueError, TypeError):
        pass
    return redirect(url_for("bets_log_page"))


@app.route("/bets")
def bets_log_page():
    """Full bet log page with all historical bets."""
    try:
        bets  = bet_tracker.get_all_bets()
        stats = bet_tracker.get_stats()
    except Exception as e:
        return f"<p style='color:red;font-family:monospace;padding:20px;'>Tracker error: {e}</p>"

    pnl_color = "#32CD32" if stats["total_units"] >= 0 else "#FF6B6B"
    clv_color = "#32CD32" if (stats.get("avg_clv") or 0) >= 0 else "#FF6B6B"
    now = datetime.now(CENTRAL).strftime("%Y-%m-%d %I:%M %p CT")

    result_colors = {
        "win": "#32CD32", "loss": "#FF6B6B", "push": "#aaa",
        "no_bet": "#666", "pending": "#FFD700",
    }
    result_labels = {
        "win": "Win ✅", "loss": "Loss ❌", "push": "Push ➖",
        "no_bet": "No Bet 🚫", "pending": "Pending ⏳",
    }

    rows = ""
    for b in bets:
        rc = result_colors.get(b["result"], "#aaa")
        rl = result_labels.get(b["result"], b["result"])
        logged = b["logged_at"][:16].replace("T", " ") if b["logged_at"] else "—"
        rows += f"""
        <tr style="border-bottom:1px solid #1e1e1e;">
            <td style="padding:8px 12px;color:#777;font-size:0.82em;">{logged}</td>
            <td style="padding:8px 12px;">{b['sport']}</td>
            <td style="padding:8px 12px;font-weight:bold;">{b['game']}</td>
            <td style="padding:8px 12px;">{b['team']} ({b['side']})</td>
            <td style="padding:8px 12px;">{b['market']}</td>
            <td style="padding:8px 12px;">{bet_tracker.fmt_odds(b['bet_odds'])}</td>
            <td style="padding:8px 12px;">{b['units']}u</td>
            <td style="padding:8px 12px;font-weight:bold;color:{rc};">{rl}</td>
            <td style="padding:8px 12px;">{bet_tracker.fmt_odds(b['closing_line'])}</td>
            <td style="padding:8px 12px;color:{clv_color};">{bet_tracker.fmt_clv(b['clv_cents'])}</td>
            <td style="padding:8px 12px;color:{pnl_color};">{bet_tracker.fmt_units(b['pnl_units'])}</td>
            <td style="padding:8px 12px;">{b['signal_score']}/10</td>
            <td style="padding:8px 12px;">
                <form method="post" action="/bets/delete" style="margin:0;">
                    <input type="hidden" name="bet_id" value="{b['id']}">
                    <button type="submit"
                            style="background:none;color:#555;border:1px solid #333;
                                   border-radius:4px;padding:2px 8px;cursor:pointer;font-size:0.8em;"
                            onclick="return confirm('Delete this bet?')">✕</button>
                </form>
            </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📊 Bet Log — VSIN Edge Finder</title>
    <style>
        body {{ margin:0;padding:0;background:#121212;color:#e0e0e0;
               font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
        h1   {{ margin:0;font-size:1.3em; }}
        th   {{ background:#1a1a1a;color:#777;text-align:left;padding:8px 12px;
               font-size:0.8em;text-transform:uppercase;letter-spacing:0.5px; }}
        tr:hover {{ background:#1a1a1a; }}
    </style>
</head>
<body>
<div style="max-width:1100px;margin:0 auto;padding:20px;">

    <div style="display:flex;justify-content:space-between;align-items:center;
                flex-wrap:wrap;gap:10px;margin-bottom:20px;">
        <h1>📊 Bet Log</h1>
        <div style="display:flex;gap:12px;align-items:center;">
            <a href="/" style="color:#87ceeb;font-size:0.9em;">← Dashboard</a>
            <span style="color:#555;font-size:0.82em;">{now}</span>
        </div>
    </div>

    <!-- Stats bar -->
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:24px;">
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 18px;text-align:center;">
            <div style="font-size:1.4em;font-weight:bold;">{stats['wins']}-{stats['losses']}-{stats['pushes']}</div>
            <div style="color:#777;font-size:0.78em;">W-L-P</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 18px;text-align:center;">
            <div style="font-size:1.4em;font-weight:bold;">{bet_tracker.fmt_pct(stats['win_rate'])}</div>
            <div style="color:#777;font-size:0.78em;">Win Rate</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 18px;text-align:center;">
            <div style="font-size:1.4em;font-weight:bold;color:{pnl_color};">{bet_tracker.fmt_units(stats['total_units'])}</div>
            <div style="color:#777;font-size:0.78em;">Net P&amp;L</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 18px;text-align:center;">
            <div style="font-size:1.4em;font-weight:bold;">{bet_tracker.fmt_pct(stats['roi_pct'])}</div>
            <div style="color:#777;font-size:0.78em;">ROI</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 18px;text-align:center;">
            <div style="font-size:1.4em;font-weight:bold;color:{clv_color};">{bet_tracker.fmt_clv(stats['avg_clv'])}</div>
            <div style="color:#777;font-size:0.78em;">Avg CLV</div>
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:10px 18px;text-align:center;">
            <div style="font-size:1.4em;font-weight:bold;">
                <span style="color:#32CD32;">{stats['clv_positive']}</span> /
                <span style="color:#FF6B6B;">{stats['clv_negative']}</span>
            </div>
            <div style="color:#777;font-size:0.78em;">CLV +/-</div>
        </div>
    </div>

    <!-- Bet table -->
    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:0.88em;">
        <thead>
            <tr>
                <th>Date</th><th>Sport</th><th>Game</th><th>Pick</th>
                <th>Market</th><th>Odds</th><th>Units</th><th>Result</th>
                <th>Close</th><th>CLV</th><th>P&amp;L</th><th>Score</th><th></th>
            </tr>
        </thead>
        <tbody>
            {rows if rows else '<tr><td colspan="13" style="padding:20px;color:#666;text-align:center;">No bets logged yet. Use the Log Bet button on any pick card.</td></tr>'}
        </tbody>
    </table>
    </div>

</div>
</body>
</html>"""


@app.route("/debug/expert")
def debug_expert():
    """
    Diagnostic endpoint — shows exactly what the expert scraper sees.
    Visit /debug/expert in your browser to troubleshoot article parsing.
    """
    import requests
    from expert_scraper import (
        _fetch_url, _get_article_links, _parse_article, _today_slug,
        BEST_BETS_URL, _HEADERS, VSIN_COOKIE,
    )
    from bs4 import BeautifulSoup

    lines = []
    lines.append("<h2>Expert Scraper Debug</h2>")
    lines.append(f"<p><b>Today slug:</b> <code>{_today_slug()}</code></p>")
    lines.append(f"<p><b>Best Bets URL:</b> <code>{BEST_BETS_URL}</code></p>")
    lines.append(f"<p><b>VSIN_COOKIE set:</b> {'Yes (' + str(len(VSIN_COOKIE)) + ' chars)' if VSIN_COOKIE else '<span style=color:red>No</span>'}</p>")

    # ── Step 1: can we reach the Best Bets Today page? ───────────────────────
    lines.append("<hr><h3>Step 1: Fetch Best Bets Today index</h3>")
    html = _fetch_url(BEST_BETS_URL)
    if not html:
        lines.append("<p style='color:red'>❌ Could not fetch Best Bets Today page at all.</p>")
    else:
        lines.append(f"<p style='color:green'>✅ Page fetched ({len(html):,} chars)</p>")

        # ── Step 2: show all links on the page that contain today's slug ────
        today_slug = _today_slug()
        soup = BeautifulSoup(html, "html.parser")
        all_links = [a["href"] for a in soup.find_all("a", href=True)]
        slug_links = [h for h in all_links if today_slug in h.lower()]

        lines.append(f"<p><b>Total links on page:</b> {len(all_links)}</p>")
        lines.append(f"<p><b>Links containing '{today_slug}':</b> {len(slug_links)}</p>")

        if slug_links:
            lines.append("<ul>")
            for lnk in slug_links[:30]:
                lines.append(f"<li><code>{lnk}</code></li>")
            lines.append("</ul>")
        else:
            lines.append(f"<p style='color:orange'>⚠️ No links found with today's slug '<b>{today_slug}</b>'. "
                         "VSIN may not have published articles yet, or the date format differs.</p>")

            # Show a sample of all links so we can see what slugs ARE present
            lines.append("<p><b>Sample of all article-like links (first 20):</b></p><ul>")
            article_links = [h for h in all_links if "vsin.com/" in h and len(h) > 40][:20]
            for lnk in article_links:
                lines.append(f"<li><code>{lnk}</code></li>")
            lines.append("</ul>")

    # ── Step 3: matched articles (from the real function) ───────────────────
    lines.append("<hr><h3>Step 2: Matched articles</h3>")
    articles = _get_article_links()
    if not articles:
        lines.append("<p style='color:orange'>⚠️ No articles matched the sport/author filters.</p>")
    else:
        lines.append(f"<p style='color:green'>✅ {len(articles)} article(s) matched</p><ul>")
        for a in articles:
            lines.append(f"<li><b>{a['author']}</b> / {a['sport']} — <code>{a['url']}</code></li>")
        lines.append("</ul>")

    # ── Step 4: fetch each article and show a text preview + picks ──────────
    lines.append("<hr><h3>Step 3: Article content + picks</h3>")
    for article in articles:
        url    = article["url"]
        sport  = article["sport"]
        author = article["author"]
        lines.append(f"<h4>{author} / {sport}</h4>")
        lines.append(f"<p><code>{url}</code></p>")

        art_html = _fetch_url(url)
        if not art_html:
            lines.append("<p style='color:red'>❌ Could not fetch article HTML.</p>")
            continue

        from bs4 import BeautifulSoup as BS
        import re
        soup2 = BS(art_html, "html.parser")
        body = (
            soup2.find("article") or
            soup2.find(class_=re.compile(r"post-content|entry-content|article-body|td-post-content")) or
            soup2.find("main")
        )
        text = (body or soup2).get_text(" ", strip=True)
        lines.append(f"<p><b>Text length:</b> {len(text)} chars</p>")
        # Show first 800 chars of article text
        preview = text[:800].replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"<pre style='background:#1a1a2e;color:#ccc;padding:10px;white-space:pre-wrap;font-size:0.8em'>{preview}...</pre>")

        picks = _parse_article(art_html, sport, author)
        if picks:
            lines.append(f"<p style='color:green'>✅ <b>{len(picks)} pick(s) extracted:</b></p><ul>")
            for p in picks:
                lines.append(f"<li>{p['author']} → <b>{p['team']}</b> ({p['sport']}) "
                              f"conviction={p['conviction']} fade={p['is_fade']} "
                              f"| raw: <i>{p['raw'][:80]}</i></li>")
            lines.append("</ul>")
        else:
            lines.append("<p style='color:orange'>⚠️ No picks extracted from this article.</p>")

    html_out = f"""<!DOCTYPE html>
<html><head><title>Expert Debug</title>
<style>body{{background:#0d0d1a;color:#ddd;font-family:monospace;padding:20px}}
h2,h3,h4{{color:#4fc3f7}} code{{background:#1a1a2e;padding:2px 6px;border-radius:3px}}
hr{{border-color:#333}}</style></head>
<body>{''.join(lines)}</body></html>"""
    return html_out


def run_web_server():
    """
    Start the Flask web server in a background thread.
    Railway provides the PORT env variable; we read it here.
    host='0.0.0.0' is required so Railway can route traffic to the app.
    """
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
