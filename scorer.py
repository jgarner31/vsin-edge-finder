"""
scorer.py
---------
VSIN Edge Finder — Multi-Signal Scoring Engine

Takes the pre-computed splits data from vsin_scraper.py, expert picks
from expert_scraper.py, and optional Polymarket probabilities, then
produces a 0-10 score with BET / LEAN / PASS classification for every
game across MLB, NBA, and NHL.

Scoring logic (max 10 points per pick):
─────────────────────────────────────────
SIGNAL 1 — Circa Sharp Money (ML)               max 4 pts
  handle% − bets% measures professional action.
  Few tickets but big dollars = sharp bettors loading one side.
  gap ≥ 12 pts  →  +2
  gap ≥ 20 pts  →  +3
  gap ≥ 30 pts  →  +4  (extreme — almost always sharp)

SIGNAL 2 — DK Public Fade                       max 2 pts
  When the public piles onto DraftKings on one side, the other side
  gains edge. We reward picks where the OPPONENT has heavy DK action.
  opponent DK bets ≥ 65%  →  +1
  opponent DK bets ≥ 75%  →  +2

SIGNAL 3 — Expert Consensus (VSIN Pro authors)  max 2 pts
  Makinen (systems), Cohen (NBA), Davis (NHL), Appelbaum (MLB splits)
  1 expert agrees  →  +1
  2+ experts agree →  +2
  Expert fade pick on opponent → treated same as a play on our side

SIGNAL 4 — Polymarket Confirmation               max 2 pts
  Prediction market probability vs implied book probability.
  Requires POLY_MIN_LIQUIDITY to count.
  edge ≥ POLY_EDGE_CONFIRM (+3%)  →  +1
  edge ≥ POLY_EDGE_STRONG  (+6%)  →  +2
  edge ≤ -POLY_EDGE_AGAINST (-3%) →  −1  (market disagrees)

Classification:
  score ≥ BET_THRESHOLD  (7)  →  BET
  score ≥ LEAN_THRESHOLD (4)  →  LEAN
  score <  LEAN_THRESHOLD     →  PASS

Totals scoring uses the same framework applied to over/under gaps.
"""

from config import (
    BET_THRESHOLD,
    LEAN_THRESHOLD,
    SHARP_GAP_MINIMUM,
    DK_FADE_THRESHOLD,
    DK_FADE_STRONG,
    POLYMARKET_ENABLED,
    POLY_MIN_LIQUIDITY,
    POLY_EDGE_CONFIRM,
    POLY_EDGE_STRONG,
    POLY_EDGE_AGAINST,
)
from vsin_scraper import normalize_team


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _label(score: int) -> str:
    """Convert numeric score to human-readable confidence label."""
    if score >= BET_THRESHOLD:
        return "BET"
    if score >= LEAN_THRESHOLD:
        return "LEAN"
    return "PASS"


def _american_to_prob(odds) -> float | None:
    """Convert American moneyline odds to implied win probability (no vig)."""
    if odds is None:
        return None
    if odds < 0:
        return (-odds) / ((-odds) + 100)
    return 100 / (odds + 100)


def _no_vig_probs(home_ml, away_ml):
    """
    Remove the book's vig and return (home_prob, away_prob).
    Returns (None, None) if either line is missing.
    """
    hp = _american_to_prob(home_ml)
    ap = _american_to_prob(away_ml)
    if hp is None or ap is None:
        return None, None
    total = hp + ap
    if total == 0:
        return None, None
    return hp / total, ap / total


def _sharp_points(gap) -> int:
    """Award 0-4 points based on Circa handle/bets gap."""
    if gap is None or gap < SHARP_GAP_MINIMUM:
        return 0
    if gap >= 30:
        return 4
    if gap >= 20:
        return 3
    return 2  # gap >= SHARP_GAP_MINIMUM (12)


def _fade_points(opponent_dk_bets) -> int:
    """Award 0-2 points based on how public the other side is on DraftKings."""
    if opponent_dk_bets is None:
        return 0
    if opponent_dk_bets >= DK_FADE_STRONG:
        return 2
    if opponent_dk_bets >= DK_FADE_THRESHOLD:
        return 1
    return 0


def _expert_points(expert_picks: list, team: str, sport: str, is_fade: bool = False) -> tuple[int, list]:
    """
    Award 0-2 points based on expert consensus.

    A pick counts if:
      - pick['sport'] matches
      - normalize_team(pick['team']) matches normalize_team(team)
      - pick['is_fade'] == is_fade

    Returns (points, list_of_matching_picks).
    """
    norm_team = normalize_team(team)
    matches = []
    for pick in expert_picks:
        if pick.get("sport") != sport:
            continue
        if normalize_team(pick.get("team", "")) != norm_team:
            continue
        if pick.get("is_fade") != is_fade:
            continue
        matches.append(pick)

    if len(matches) >= 2:
        return 2, matches
    if len(matches) == 1:
        return 1, matches
    return 0, []


def _poly_points(our_side_prob, book_prob, liquidity) -> int:
    """
    Award -1 to +2 points based on prediction market edge.
    our_side_prob: Polymarket probability for our pick (0-1)
    book_prob:     No-vig book probability for our pick (0-1)
    liquidity:     Dollar liquidity on the Polymarket contract
    """
    if not POLYMARKET_ENABLED:
        return 0
    if our_side_prob is None or book_prob is None or liquidity is None:
        return 0
    if liquidity < POLY_MIN_LIQUIDITY:
        return 0
    edge = our_side_prob - book_prob
    if edge >= POLY_EDGE_STRONG:
        return 2
    if edge >= POLY_EDGE_CONFIRM:
        return 1
    if edge <= -POLY_EDGE_AGAINST:
        return -1
    return 0


def _build_reasons(sharp_pts, fade_pts, expert_pts, poly_pts,
                   gap, opp_dk, expert_matches, poly_edge, liquidity) -> list[str]:
    """Build a human-readable list of signal explanations."""
    reasons = []

    if sharp_pts > 0:
        reasons.append(
            f"Circa sharp gap +{gap:.0f}pt (handle >> bets) → +{sharp_pts}pt"
        )

    if fade_pts > 0:
        reasons.append(
            f"DK public fade: opponent has {opp_dk:.0f}% of tickets → +{fade_pts}pt"
        )

    if expert_pts > 0:
        authors = ", ".join(set(p.get("author", "?") for p in expert_matches))
        reasons.append(
            f"Expert consensus ({authors}) → +{expert_pts}pt"
        )

    if poly_pts > 0 and poly_edge is not None:
        reasons.append(
            f"Polymarket edge +{poly_edge*100:.1f}% (${liquidity:,.0f} liq) → +{poly_pts}pt"
        )
    elif poly_pts < 0 and poly_edge is not None:
        reasons.append(
            f"Polymarket disagrees {poly_edge*100:.1f}% (${liquidity:,.0f} liq) → {poly_pts}pt"
        )

    return reasons


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def score_moneyline(merged: dict, sport: str, side: str,
                    expert_picks: list | None = None,
                    polymarket_game: dict | None = None) -> dict:
    """
    Score a moneyline pick for a given side ('away' or 'home').

    Parameters
    ----------
    merged        : the 'merged' dict from vsin_scraper.fetch_all_splits()
    sport         : "MLB" | "NBA" | "NHL"
    side          : "away" | "home"
    expert_picks  : list of pick dicts from expert_scraper.fetch_expert_picks()[sport]
    polymarket_game : dict with keys home_prob, away_prob, liquidity (from polymarket.py)

    Returns
    -------
    dict with keys: side, team, score, label, reasons, signal_breakdown
    """
    expert_picks = expert_picks or []

    # ── SIGNAL 1: Circa sharp gap ──────────────────────────────────────────
    gap = merged.get(f"circa_ml_gap_{side}")
    sharp_pts = _sharp_points(gap)

    # ── SIGNAL 2: DK public fade ──────────────────────────────────────────
    opp_side = "home" if side == "away" else "away"
    opp_dk = merged.get(f"dk_ml_bets_{opp_side}")
    fade_pts = _fade_points(opp_dk)

    # ── SIGNAL 3: Expert consensus ────────────────────────────────────────
    team = merged.get(f"{side}_team", "")
    expert_pts, expert_matches = _expert_points(expert_picks, team, sport, is_fade=False)

    # Also check if any expert has a FADE on the opponent
    opp_team = merged.get(f"{opp_side}_team", "")
    fade_expert_pts, fade_expert_matches = _expert_points(expert_picks, opp_team, sport, is_fade=True)
    if fade_expert_pts > expert_pts:
        expert_pts = fade_expert_pts
        expert_matches = fade_expert_matches

    # ── SIGNAL 4: Polymarket confirmation ─────────────────────────────────
    poly_pts = 0
    poly_edge = None
    poly_liq = None

    if polymarket_game:
        poly_liq = polymarket_game.get("liquidity")
        home_ml = merged.get("home_ml")
        away_ml = merged.get("away_ml")
        book_home_prob, book_away_prob = _no_vig_probs(home_ml, away_ml)
        book_prob = book_home_prob if side == "home" else book_away_prob
        our_poly_prob = (
            polymarket_game.get("home_prob") if side == "home"
            else polymarket_game.get("away_prob")
        )
        poly_pts = _poly_points(our_poly_prob, book_prob, poly_liq)
        if our_poly_prob is not None and book_prob is not None:
            poly_edge = our_poly_prob - book_prob

    # ── Total ─────────────────────────────────────────────────────────────
    total = sharp_pts + fade_pts + expert_pts + poly_pts
    total = max(0, min(10, total))   # clamp to [0, 10]

    reasons = _build_reasons(
        sharp_pts, fade_pts, expert_pts, poly_pts,
        gap or 0, opp_dk or 0, expert_matches, poly_edge, poly_liq or 0,
    )

    return {
        "side":   side,
        "team":   team,
        "score":  total,
        "label":  _label(total),
        "reasons": reasons,
        "signal_breakdown": {
            "circa_sharp_pts":  sharp_pts,
            "dk_fade_pts":      fade_pts,
            "expert_pts":       expert_pts,
            "polymarket_pts":   poly_pts,
            "circa_gap":        gap,
            "opp_dk_bets":      opp_dk,
            "expert_matches":   [
                {"team": p.get("team"), "author": p.get("author"),
                 "conviction": p.get("conviction"), "is_fade": p.get("is_fade")}
                for p in expert_matches
            ],
            "poly_edge":        poly_edge,
            "poly_liquidity":   poly_liq,
        }
    }


def score_total(merged: dict, sport: str, side: str,
                expert_picks: list | None = None,
                polymarket_game: dict | None = None) -> dict:
    """
    Score a totals pick for a given side ('over' or 'under').

    Polymarket totals markets are rare; poly_pts will usually be 0 here.

    Returns
    -------
    dict with keys: side, line, score, label, reasons, signal_breakdown
    """
    expert_picks = expert_picks or []
    opp_side = "under" if side == "over" else "over"

    # ── SIGNAL 1: Circa sharp gap ──────────────────────────────────────────
    gap = merged.get(f"circa_tot_gap_{side}")
    sharp_pts = _sharp_points(gap)

    # ── SIGNAL 2: DK public fade ──────────────────────────────────────────
    opp_dk = merged.get(f"dk_tot_bets_{opp_side}")
    fade_pts = _fade_points(opp_dk)

    # ── SIGNAL 3: Expert consensus ─────────────────────────────────────────
    # Expert picks on totals use team="" and side stored in raw text;
    # for now we scan for explicit "over"/"under" mentions in the raw field.
    expert_pts = 0
    expert_matches = []
    for pick in expert_picks:
        if pick.get("sport") != sport:
            continue
        raw = (pick.get("raw") or "").lower()
        if side in raw and not pick.get("is_fade"):
            expert_matches.append(pick)
    if len(expert_matches) >= 2:
        expert_pts = 2
    elif len(expert_matches) == 1:
        expert_pts = 1

    # ── SIGNAL 4: Polymarket (rare for totals) ─────────────────────────────
    poly_pts = 0
    poly_edge = None
    poly_liq = None
    # Polymarket game dicts rarely have over/under probs; skip unless present
    if polymarket_game:
        poly_liq = polymarket_game.get("liquidity")
        our_poly_prob = polymarket_game.get(f"{side}_prob")   # e.g. "over_prob"
        book_prob = 0.5   # totals are priced close to -110 on both sides ≈ 52.4%
        poly_pts = _poly_points(our_poly_prob, book_prob, poly_liq)
        if our_poly_prob is not None:
            poly_edge = our_poly_prob - book_prob

    # ── Total ─────────────────────────────────────────────────────────────
    total = sharp_pts + fade_pts + expert_pts + poly_pts
    total = max(0, min(10, total))

    reasons = _build_reasons(
        sharp_pts, fade_pts, expert_pts, poly_pts,
        gap or 0, opp_dk or 0, expert_matches, poly_edge, poly_liq or 0,
    )

    return {
        "side":  side,
        "line":  merged.get("total_line"),
        "score": total,
        "label": _label(total),
        "reasons": reasons,
        "signal_breakdown": {
            "circa_sharp_pts":  sharp_pts,
            "dk_fade_pts":      fade_pts,
            "expert_pts":       expert_pts,
            "polymarket_pts":   poly_pts,
            "circa_gap":        gap,
            "opp_dk_bets":      opp_dk,
            "expert_matches":   [
                {"team": p.get("team"), "author": p.get("author"),
                 "conviction": p.get("conviction")}
                for p in expert_matches
            ],
            "poly_edge":        poly_edge,
            "poly_liquidity":   poly_liq,
        }
    }


def score_game(game_entry: dict, sport: str,
               expert_picks: list | None = None,
               polymarket_game: dict | None = None) -> dict:
    """
    Score all markets for a single game entry from fetch_all_splits().

    Parameters
    ----------
    game_entry     : one value from splits_by_sport[sport]  → {circa, draftkings, merged}
    sport          : "MLB" | "NBA" | "NHL"
    expert_picks   : list of pick dicts for this sport
    polymarket_game: dict from polymarket.py for this game

    Returns
    -------
    {
      "game":       "<Away> @ <Home>",
      "away_team":  str,
      "home_team":  str,
      "sport":      str,
      "away_ml":    score_moneyline result for away side,
      "home_ml":    score_moneyline result for home side,
      "over":       score_total result for over,
      "under":      score_total result for under,
      "top_pick":   best single pick (highest score across all 4 markets),
      "has_signal": bool  (True if top_pick.score >= LEAN_THRESHOLD),
    }
    """
    merged = game_entry.get("merged", {})
    away = merged.get("away_team", "")
    home = merged.get("home_team", "")

    away_ml  = score_moneyline(merged, sport, "away", expert_picks, polymarket_game)
    home_ml  = score_moneyline(merged, sport, "home", expert_picks, polymarket_game)
    over     = score_total(merged, sport, "over",  expert_picks, polymarket_game)
    under    = score_total(merged, sport, "under", expert_picks, polymarket_game)

    # Pick the single highest-scoring market for the "top pick" card
    candidates = [away_ml, home_ml, over, under]
    top = max(candidates, key=lambda c: c["score"])

    return {
        "game":      f"{away} @ {home}",
        "away_team": away,
        "home_team": home,
        "sport":     sport,
        "away_ml":   away_ml,
        "home_ml":   home_ml,
        "over":      over,
        "under":     under,
        "top_pick":  top,
        "has_signal": top["score"] >= LEAN_THRESHOLD,
    }


def score_all_games(splits_by_sport: dict,
                    expert_picks_by_sport: dict | None = None,
                    polymarket_data: dict | None = None) -> dict:
    """
    Score every game across all sports in splits_by_sport.

    Parameters
    ----------
    splits_by_sport       : output of vsin_scraper.fetch_all_splits()
    expert_picks_by_sport : output of expert_scraper.fetch_expert_picks()
                            keyed by sport  e.g. {"MLB": [...], "NBA": [...]}
    polymarket_data       : dict keyed by normalized game_key → poly game dict
                            (optional — pass None to skip Polymarket scoring)

    Returns
    -------
    {
      sport: [scored_game, ...],   # sorted best-score-first
      ...
    }
    """
    expert_picks_by_sport = expert_picks_by_sport or {}
    polymarket_data       = polymarket_data or {}
    result = {}

    for sport, games in splits_by_sport.items():
        expert_picks = expert_picks_by_sport.get(sport, [])
        scored = []

        for game_key, game_entry in games.items():
            poly_game = polymarket_data.get(game_key)
            scored_game = score_game(game_entry, sport, expert_picks, poly_game)
            scored_game["game_key"] = game_key
            scored.append(scored_game)

        # Sort: BETs first, then LEANs, then by score desc
        scored.sort(key=lambda g: (
            0 if g["top_pick"]["label"] == "BET"  else
            1 if g["top_pick"]["label"] == "LEAN" else 2,
            -(g["top_pick"]["score"]),
            g["game"],
        ))

        result[sport] = scored
        signal_count = sum(1 for g in scored if g["has_signal"])
        print(f"[SCORER] {sport}: {len(scored)} games scored, "
              f"{signal_count} with LEAN/BET signal.")

    return result
