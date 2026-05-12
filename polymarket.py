"""
polymarket.py
-------------
VSIN Edge Finder — Polymarket Confirmation Layer

Fetches prediction market probabilities from Polymarket's public API
and matches them to games from the VSIN splits data by team name.

Polymarket is a secondary confirmation signal only. A strong Polymarket
edge (+6%) adds 2 points to our score; a disagreeing market (-3%) takes
1 point off. We skip any market with less than POLY_MIN_LIQUIDITY.

Keyed by normalized game_key: e.g. "astros_mariners"
"""

import json
import requests
from vsin_scraper import normalize_team
from config import POLY_MIN_LIQUIDITY

BASE_URL = "https://gamma-api.polymarket.com"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers  (adapted from mlb/ polymarket.py)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json_list(raw):
    if raw in (None, "", []):
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []


def _parse_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price > 1:
        price /= 100.0
    return price


def _extract_team_outcomes(market):
    outcomes = _parse_json_list(market.get("outcomes"))
    prices   = _parse_json_list(market.get("outcomePrices"))
    if len(outcomes) != len(prices) or not outcomes:
        return None
    paired = []
    for outcome, price in zip(outcomes, prices):
        text = str(outcome).strip()
        if text.lower() in {"yes", "no"}:
            continue
        prob = _parse_price(price)
        if prob is None:
            continue
        paired.append({"label": text, "norm": normalize_team(text), "prob": prob})
    return paired if len(paired) >= 2 else None


def _is_moneyline_market(market):
    smt = market.get("sportsMarketType")
    q   = (market.get("question") or "").lower()
    return smt in {"moneyline", "child_moneyline"} or "moneyline" in q


def _title_matches_game(title, away_team, home_team):
    t  = (title or "").lower()
    an = normalize_team(away_team)
    hn = normalize_team(home_team)
    if not an or not hn:
        return False
    return (an in t and hn in t) or (
        (away_team or "").lower() in t and (home_team or "").lower() in t
    )


def _match_to_game(event, market, away_team, home_team):
    """
    Try to match one Polymarket event/market to a given game.
    Returns a game dict or None.
    """
    outcomes = _extract_team_outcomes(market)
    if not outcomes:
        return None

    title = " ".join(filter(None, [
        event.get("title"), event.get("subtitle"), market.get("question"),
    ]))
    if not _title_matches_game(title, away_team, home_team):
        return None

    away_n = normalize_team(away_team)
    home_n = normalize_team(home_team)

    home_prob = away_prob = None
    for o in outcomes:
        on = o["norm"]
        if on == away_n or (away_n and on and away_n in on):
            away_prob = o["prob"]
        elif on == home_n or (home_n and on and home_n in on):
            home_prob = o["prob"]

    # Positional fallback if name matching failed
    if (home_prob is None or away_prob is None) and len(outcomes) >= 2:
        away_prob = outcomes[0]["prob"]
        home_prob = outcomes[1]["prob"]

    if home_prob is None or away_prob is None:
        return None

    liq = market.get("liquidityNum") or market.get("liquidity")
    try:
        liq = float(liq or 0)
    except (TypeError, ValueError):
        liq = 0

    return {
        "event_title": event.get("title"),
        "question":    market.get("question"),
        "home_prob":   home_prob,
        "away_prob":   away_prob,
        "liquidity":   liq,
        "volume":      market.get("volumeNum") or market.get("volume"),
        "market_id":   market.get("id"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_polymarket_for_splits(splits_by_sport: dict) -> dict:
    """
    Query Polymarket for each game in splits_by_sport and return a dict
    keyed by normalized game_key (e.g. "astros_mariners").

    Only includes games where liquidity >= POLY_MIN_LIQUIDITY.

    Parameters
    ----------
    splits_by_sport : output of vsin_scraper.fetch_all_splits()

    Returns
    -------
    {
      "astros_mariners": {
        "event_title": str,
        "question": str,
        "home_prob": float,
        "away_prob": float,
        "liquidity": float,
        "volume": float,
        "market_id": str,
      },
      ...
    }
    """
    results: dict = {}

    for sport, games in splits_by_sport.items():
        for game_key, game_entry in games.items():
            merged     = game_entry.get("merged", {})
            away_team  = merged.get("away_team", "")
            home_team  = merged.get("home_team", "")
            if not away_team or not home_team:
                continue

            queries = [
                f"{away_team} {home_team}",
                f"{normalize_team(away_team)} {normalize_team(home_team)}",
            ]

            best    = None
            best_liq = -1

            for query in queries:
                try:
                    resp = requests.get(
                        f"{BASE_URL}/public-search",
                        params={"q": query, "limit_per_type": 10},
                        timeout=20,
                    )
                    resp.raise_for_status()
                except requests.exceptions.RequestException as e:
                    print(f"[POLYMARKET] Search failed for {away_team} @ {home_team}: {e}")
                    continue

                payload = resp.json()
                for event in (payload.get("events") or []):
                    for market in (event.get("markets") or []):
                        if not _is_moneyline_market(market):
                            continue
                        match = _match_to_game(event, market, away_team, home_team)
                        if match and match["liquidity"] > best_liq:
                            best     = match
                            best_liq = match["liquidity"]

                if best:
                    break   # found a good match on first query

            if best and best["liquidity"] >= POLY_MIN_LIQUIDITY:
                results[game_key] = best
                print(
                    f"[POLYMARKET] {sport} | {away_team} @ {home_team} → "
                    f"H {best['home_prob']:.2f} / A {best['away_prob']:.2f} "
                    f"(${best['liquidity']:,.0f} liq)"
                )
            else:
                print(f"[POLYMARKET] No match for {away_team} @ {home_team}")

    print(f"[POLYMARKET] Matched {len(results)} games total.")
    return results
