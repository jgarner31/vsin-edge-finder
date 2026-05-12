"""
bet_tracker.py
--------------
VSIN Edge Finder — Bet Tracker + CLV Logger

Stores every bet you place in a SQLite database so you can measure:

  Win Rate  — what % of your picks win
  ROI       — profit per unit risked over time
  CLV       — Closing Line Value: did you beat the closing price?
              Positive CLV means you consistently get sharp prices.
              It's the gold standard for measuring if a system works.

Storage
-------
On Railway:  /data/bets.db   (add a Volume in Railway → Storage)
Locally:     ./bets.db       (auto-fallback if /data doesn't exist)

Table: bets
-----------
id            INTEGER  PRIMARY KEY
logged_at     TEXT     ISO timestamp when you logged the bet
sport         TEXT     MLB / NBA / NHL
game          TEXT     "Giants @ Dodgers"
side          TEXT     "away" | "home" | "over" | "under"
team          TEXT     "Giants"
market        TEXT     "away_ml" | "home_ml" | "over" | "under"
signal_score  INTEGER  scorer output (0-10)
alert_odds    INTEGER  American odds at alert time
bet_odds      INTEGER  American odds you actually got (may differ slightly)
units         REAL     size of bet in units (e.g. 1.0, 2.0)
result        TEXT     "pending" | "win" | "loss" | "push" | "no_bet"
closing_line  INTEGER  closing American odds (enter after game)
pnl_units     REAL     profit/loss in units (computed on result entry)
clv_cents     REAL     CLV in cents (bet_odds edge vs closing_line)
notes         TEXT     any free-text notes
"""

import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

# Use /data/ on Railway (persistent volume), fall back to local directory
_DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_PATH   = os.path.join(_DATA_DIR, "bets.db")


# ─────────────────────────────────────────────────────────────────────────────
# Database setup
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    """Create the bets table if it doesn't exist. Safe to call on every startup."""
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at    TEXT    NOT NULL,
            sport        TEXT    NOT NULL,
            game         TEXT    NOT NULL,
            side         TEXT    NOT NULL,
            team         TEXT    NOT NULL DEFAULT '',
            market       TEXT    NOT NULL,
            signal_score INTEGER NOT NULL DEFAULT 0,
            alert_odds   INTEGER,
            bet_odds     INTEGER,
            units        REAL    NOT NULL DEFAULT 1.0,
            result       TEXT    NOT NULL DEFAULT 'pending',
            closing_line INTEGER,
            pnl_units    REAL,
            clv_cents    REAL,
            notes        TEXT    DEFAULT ''
        )
    """)
    con.commit()
    con.close()


def _get_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row   # rows behave like dicts
    return con


# ─────────────────────────────────────────────────────────────────────────────
# Write operations
# ─────────────────────────────────────────────────────────────────────────────

def log_bet(sport: str, game: str, side: str, team: str, market: str,
            signal_score: int, alert_odds: int | None,
            bet_odds: int | None = None,
            units: float = 1.0,
            notes: str = "") -> int:
    """
    Insert a new pending bet. Returns the new bet's id.

    Parameters
    ----------
    sport         : "MLB" | "NBA" | "NHL"
    game          : "Giants @ Dodgers"
    side          : "away" | "home" | "over" | "under"
    team          : "Giants"
    market        : "away_ml" | "home_ml" | "over" | "under"
    signal_score  : the 0-10 score from scorer.py at alert time
    alert_odds    : American ML odds shown in the alert
    bet_odds      : odds you actually got (leave None if same as alert)
    units         : how many units you're betting (default 1.0)
    notes         : optional free text
    """
    now = datetime.now(CENTRAL).isoformat()
    con = _get_conn()
    cur = con.execute(
        """INSERT INTO bets
           (logged_at, sport, game, side, team, market, signal_score,
            alert_odds, bet_odds, units, result, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)""",
        (now, sport, game, side, team, market, signal_score,
         alert_odds, bet_odds if bet_odds is not None else alert_odds,
         units, notes),
    )
    bet_id = cur.lastrowid
    con.commit()
    con.close()
    return bet_id


def update_result(bet_id: int, result: str,
                  closing_line: int | None = None,
                  notes: str | None = None):
    """
    Record the outcome of a bet and compute P&L + CLV.

    result       : "win" | "loss" | "push" | "no_bet"
    closing_line : American odds at game start (for CLV calculation)
    notes        : optional update to notes field
    """
    con = _get_conn()
    row = con.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
    if not row:
        con.close()
        return

    units    = row["units"]
    bet_odds = row["bet_odds"]

    # ── P&L calculation ───────────────────────────────────────────────────
    pnl = None
    if result == "win":
        if bet_odds is not None:
            if bet_odds > 0:
                pnl = units * (bet_odds / 100)
            else:
                pnl = units * (100 / abs(bet_odds))
    elif result == "loss":
        pnl = -units
    elif result in ("push", "no_bet"):
        pnl = 0.0

    # ── CLV calculation ───────────────────────────────────────────────────
    # CLV in cents = difference between your odds and closing odds,
    # expressed as implied probability difference × 100.
    clv = None
    if closing_line is not None and bet_odds is not None:
        our_prob   = _american_to_prob(bet_odds)
        close_prob = _american_to_prob(closing_line)
        if our_prob is not None and close_prob is not None:
            # Positive CLV = you got a BETTER price than closing line
            # (lower implied probability = better price for the bettor)
            clv = round((close_prob - our_prob) * 100, 2)

    update_notes = notes if notes is not None else row["notes"]

    con.execute(
        """UPDATE bets
           SET result=?, closing_line=?, pnl_units=?, clv_cents=?, notes=?
           WHERE id=?""",
        (result, closing_line, pnl, clv, update_notes, bet_id),
    )
    con.commit()
    con.close()


def delete_bet(bet_id: int):
    """Remove a bet (e.g. logged by mistake)."""
    con = _get_conn()
    con.execute("DELETE FROM bets WHERE id=?", (bet_id,))
    con.commit()
    con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Read operations
# ─────────────────────────────────────────────────────────────────────────────

def get_all_bets(limit: int = 200) -> list[dict]:
    """Return all bets newest-first as a list of dicts."""
    con = _get_conn()
    rows = con.execute(
        "SELECT * FROM bets ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_pending_bets() -> list[dict]:
    """Return bets with result='pending' for the update form."""
    con = _get_conn()
    rows = con.execute(
        "SELECT * FROM bets WHERE result='pending' ORDER BY id DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """
    Compute summary stats across all settled bets (win/loss/push).

    Returns
    -------
    {
      total_bets    : int,
      settled       : int,
      pending       : int,
      wins          : int,
      losses        : int,
      pushes        : int,
      win_rate      : float | None   (wins / (wins + losses))
      total_units   : float          (net P&L in units)
      roi_pct       : float | None   (total_units / total_risked * 100)
      avg_clv       : float | None   (average CLV in cents across bets with closing lines)
      clv_positive  : int            (# bets with positive CLV)
      clv_negative  : int            (# bets with negative CLV)
      by_sport      : {sport: {wins, losses, pnl_units}}
      by_score      : {score: {wins, losses}}   (performance by signal score)
    }
    """
    con = _get_conn()
    rows = [dict(r) for r in con.execute("SELECT * FROM bets").fetchall()]
    con.close()

    settled = [r for r in rows if r["result"] in ("win", "loss", "push")]
    wins    = [r for r in settled if r["result"] == "win"]
    losses  = [r for r in settled if r["result"] == "loss"]
    pushes  = [r for r in settled if r["result"] == "push"]
    pending = [r for r in rows    if r["result"] == "pending"]

    total_units  = sum(r["pnl_units"] or 0 for r in settled)
    total_risked = sum(r["units"] or 1  for r in settled if r["result"] != "push")

    win_rate = len(wins) / (len(wins) + len(losses)) if (wins or losses) else None
    roi_pct  = (total_units / total_risked * 100) if total_risked else None

    clv_vals     = [r["clv_cents"] for r in rows if r["clv_cents"] is not None]
    avg_clv      = sum(clv_vals) / len(clv_vals) if clv_vals else None
    clv_positive = sum(1 for c in clv_vals if c > 0)
    clv_negative = sum(1 for c in clv_vals if c < 0)

    # By sport
    by_sport: dict = {}
    for r in settled:
        s = r["sport"]
        if s not in by_sport:
            by_sport[s] = {"wins": 0, "losses": 0, "pushes": 0, "pnl_units": 0.0}
        by_sport[s][r["result"] + "s"] = by_sport[s].get(r["result"] + "s", 0) + 1
        by_sport[s]["pnl_units"] += r["pnl_units"] or 0

    # By signal score bucket
    by_score: dict = {}
    for r in settled:
        sc = str(r["signal_score"])
        if sc not in by_score:
            by_score[sc] = {"wins": 0, "losses": 0, "pushes": 0}
        by_score[sc][r["result"] + "s"] = by_score[sc].get(r["result"] + "s", 0) + 1

    return {
        "total_bets":   len(rows),
        "settled":      len(settled),
        "pending":      len(pending),
        "wins":         len(wins),
        "losses":       len(losses),
        "pushes":       len(pushes),
        "win_rate":     win_rate,
        "total_units":  round(total_units, 2),
        "roi_pct":      round(roi_pct, 1) if roi_pct is not None else None,
        "avg_clv":      round(avg_clv, 2) if avg_clv is not None else None,
        "clv_positive": clv_positive,
        "clv_negative": clv_negative,
        "by_sport":     by_sport,
        "by_score":     by_score,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _american_to_prob(odds: int) -> float | None:
    """Convert American odds to implied win probability."""
    if odds is None:
        return None
    if odds < 0:
        return (-odds) / ((-odds) + 100)
    return 100 / (odds + 100)


def fmt_odds(odds) -> str:
    if odds is None:
        return "—"
    return f"+{odds}" if odds > 0 else str(odds)


def fmt_units(val) -> str:
    if val is None:
        return "—"
    return f"+{val:.2f}u" if val >= 0 else f"{val:.2f}u"


def fmt_clv(val) -> str:
    if val is None:
        return "—"
    return f"+{val:.1f}¢" if val >= 0 else f"{val:.1f}¢"


def fmt_pct(val) -> str:
    if val is None:
        return "—"
    return f"{val:.1f}%"
