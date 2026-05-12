"""
vsin_scraper.py
---------------
Unified VSIN Pro splits scraper for MLB, NBA, and NHL.

Fetches Circa Sports + DraftKings handle/bet percentages for every
game on today's board. Returns a sport-keyed dict of game objects
that feed directly into the scoring model.

Circa is the sharp-money reference (real Nevada book, limits pros).
DraftKings is the public-money reference (mass market, takes everyone).
The gap between Circa handle% and Circa bets% on a given side is the
primary sharp signal — big money on few tickets means sharp action.
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from config import VSIN_COOKIE

# -------------------------------------------------------------------
# VSIN page URLs for each sport + book combo
# The ?source= param tells the page which book's splits to show.
# -------------------------------------------------------------------
BASE_URL = "https://data.vsin.com/betting-splits/"

SPORT_PARAMS = {
    "MLB":  {"leagues": ["MLB"]},
    "NBA":  {"leagues": ["NBA"]},
    "NHL":  {"leagues": ["NHL"]},
    "WNBA": {"leagues": ["WNBA"]},
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.vsin.com/",
}

# City prefixes to strip when normalizing team names for matching
_CITY_PREFIXES = [
    "arizona", "atlanta", "baltimore", "boston", "brooklyn", "buffalo",
    "calgary", "carolina", "charlotte", "chicago", "cincinnati",
    "cleveland", "colorado", "columbus", "dallas", "denver", "detroit",
    "edmonton", "florida", "golden state", "houston", "indiana",
    "kansas city", "las vegas", "la", "los angeles", "memphis",
    "miami", "milwaukee", "minnesota", "nashville", "new england",
    "new jersey", "new orleans", "new york", "new york city",
    "oklahoma city", "orlando", "ottawa", "philadelphia", "phoenix",
    "pittsburgh", "portland", "sacramento", "san antonio", "san diego",
    "san francisco", "san jose", "seattle", "st. louis", "tampa bay",
    "texas", "toronto", "utah", "vancouver", "vegas", "washington",
    "winnipeg",
]


def normalize_team(name: str) -> str:
    """Strip city prefix, lowercase, strip whitespace."""
    n = (name or "").lower().strip()
    for city in sorted(_CITY_PREFIXES, key=len, reverse=True):
        if n.startswith(city + " "):
            return n[len(city):].strip()
    return n


def _pct(raw: str):
    if not raw or raw.strip() in ("?", "—", ""):
        return None
    m = re.search(r"\d+", raw)
    return int(m.group()) if m else None


def _ml(raw: str):
    if not raw or raw.strip() in ("?", "—", ""):
        return None
    cleaned = re.sub(r"[▲▼↺\s]", "", raw)
    try:
        return int(cleaned.replace(",", "").replace("+", ""))
    except ValueError:
        return None


def _line(raw: str):
    if not raw or raw.strip() in ("?", "—", ""):
        return None
    cleaned = re.sub(r"[▲▼↺\s]", "", raw)
    try:
        return float(cleaned.replace(",", ""))
    except ValueError:
        return None


def _fetch_splits_page(source: str) -> str | None:
    """
    Fetch the VSIN betting splits page for a given source (CIRCA or DK).
    Returns raw HTML or None on failure.
    """
    if not VSIN_COOKIE:
        print("[VSIN] VSIN_COOKIE not set — splits disabled.")
        return None

    headers = {**_HEADERS, "Cookie": VSIN_COOKIE}
    params = {"source": source}

    try:
        resp = requests.get(BASE_URL, params=params, headers=headers, timeout=20)
    except requests.exceptions.RequestException as e:
        print(f"[VSIN] Network error fetching {source}: {e}")
        return None

    if resp.status_code in (401, 403):
        print(f"[VSIN] Auth failed for {source} (HTTP {resp.status_code}). Check VSIN_COOKIE.")
        return None
    if "login" in resp.url.lower() or resp.status_code in (301, 302):
        print(f"[VSIN] Redirected to login for {source}. Cookie may be expired.")
        return None
    if not resp.ok:
        print(f"[VSIN] Unexpected status {resp.status_code} for {source}.")
        return None

    return resp.text


def _parse_splits_page(html: str, sport_filter: str | None = None) -> dict:
    """
    Parse the full splits page HTML into a structured dict keyed by
    normalized "{away_norm}_{home_norm}".

    Each value is a game dict with:
      sport, away_team, home_team,
      moneyline: {away: {line, handle_pct, bets_pct}, home: {...}}
      spread:    {away: {line, handle_pct, bets_pct}, home: {...}}
      total:     {line, over: {handle_pct, bets_pct}, under: {...}}
    """
    soup = BeautifulSoup(html, "html.parser")
    games = {}

    # Each sport section has a header row like "NBA - MONDAY, MAY 11"
    # followed by pairs of <tr> for away/home teams.
    current_sport = None

    for table in soup.find_all("table"):
        thead = table.find("thead")
        if not thead:
            continue

        header_text = thead.get_text(" ", strip=True).upper()

        # Identify sport from header
        for sport in ("MLB", "NBA", "NHL", "WNBA", "NFL", "CFB"):
            if sport in header_text:
                current_sport = sport
                break

        if sport_filter and current_sport != sport_filter:
            continue

        tbody = table.find("tbody")
        if not tbody:
            continue

        rows = tbody.find_all("tr")
        # Process pairs of rows (away, home)
        i = 0
        while i < len(rows) - 1:
            a_cells = [td.get_text(" ", strip=True) for td in rows[i].find_all("td")]
            h_cells = [td.get_text(" ", strip=True) for td in rows[i + 1].find_all("td")]

            # Need at least 9 columns: indicator, team, spread, handle, bets,
            # total, handle, bets, ml, handle, bets
            if len(a_cells) < 9 or len(h_cells) < 9:
                i += 2
                continue

            away_team = a_cells[1].strip()
            home_team = h_cells[1].strip()

            if not away_team or not home_team:
                i += 2
                continue

            key = f"{normalize_team(away_team)}_{normalize_team(home_team)}"

            games[key] = {
                "sport":     current_sport,
                "away_team": away_team,
                "home_team": home_team,
                "spread": {
                    "away": {
                        "line":       _line(a_cells[2]),
                        "handle_pct": _pct(a_cells[3]),
                        "bets_pct":   _pct(a_cells[4]),
                    },
                    "home": {
                        "line":       _line(h_cells[2]),
                        "handle_pct": _pct(h_cells[3]),
                        "bets_pct":   _pct(h_cells[4]),
                    },
                },
                "total": {
                    "line": _line(a_cells[5]),
                    "over": {
                        "handle_pct": _pct(a_cells[6]),
                        "bets_pct":   _pct(a_cells[7]),
                    },
                    "under": {
                        "handle_pct": _pct(h_cells[6]),
                        "bets_pct":   _pct(h_cells[7]),
                    },
                },
                "moneyline": {
                    "away": {
                        "line":       _ml(a_cells[8])  if len(a_cells) > 8  else None,
                        "handle_pct": _pct(a_cells[9]) if len(a_cells) > 9  else None,
                        "bets_pct":   _pct(a_cells[10]) if len(a_cells) > 10 else None,
                    },
                    "home": {
                        "line":       _ml(h_cells[8])  if len(h_cells) > 8  else None,
                        "handle_pct": _pct(h_cells[9]) if len(h_cells) > 9  else None,
                        "bets_pct":   _pct(h_cells[10]) if len(h_cells) > 10 else None,
                    },
                },
            }
            i += 2

    return games


def fetch_all_splits(sports: list[str] | None = None) -> dict:
    """
    Fetch Circa + DraftKings splits for all requested sports.

    Returns a dict keyed by sport, then by normalized game key:
      {
        "MLB": {
          "astros_mariners": {
            "circa":       { sport, away_team, home_team, moneyline, spread, total },
            "draftkings":  { ... same structure ... },
            "merged": {
              # convenience fields used by scorer
              "away_team": ...,
              "home_team": ...,
              "sport": ...,
              "circa_ml_gap_away": int,   # circa handle - bets for away ML
              "circa_ml_gap_home": int,
              "dk_ml_bets_away": int,
              "dk_ml_bets_home": int,
              "circa_total_over_gap": int,
              "dk_total_over_bets": int,
            }
          }, ...
        },
        "NBA": { ... },
        "NHL": { ... },
      }
    """
    if not sports:
        from config import ACTIVE_SPORTS
        sports = ACTIVE_SPORTS

    print("[VSIN] Fetching Circa splits...")
    circa_html = _fetch_splits_page("CIRCA")
    print("[VSIN] Fetching DraftKings splits...")
    dk_html = _fetch_splits_page("DK")

    circa_all = _parse_splits_page(circa_html) if circa_html else {}
    dk_all    = _parse_splits_page(dk_html)    if dk_html    else {}

    print(f"[VSIN] Parsed {len(circa_all)} Circa games, {len(dk_all)} DK games.")

    # Merge by sport
    result: dict[str, dict] = {sport: {} for sport in sports}

    all_keys = set(circa_all.keys()) | set(dk_all.keys())
    for key in all_keys:
        circa_game = circa_all.get(key)
        dk_game    = dk_all.get(key)
        sport = (circa_game or dk_game or {}).get("sport")
        if sport not in sports:
            continue

        base = circa_game or dk_game
        away = base["away_team"]
        home = base["home_team"]

        # Pre-compute the primary scoring signals for convenience
        c_ml = (circa_game or {}).get("moneyline", {})
        d_ml = (dk_game    or {}).get("moneyline", {})
        c_tot = (circa_game or {}).get("total", {})
        d_tot = (dk_game    or {}).get("total", {})

        def gap(side_dict):
            h = (side_dict or {}).get("handle_pct")
            b = (side_dict or {}).get("bets_pct")
            return (h - b) if (h is not None and b is not None) else None

        merged = {
            "away_team": away,
            "home_team": home,
            "sport": sport,
            # Sharp signal: positive = sharp money on this side
            "circa_ml_gap_away":  gap(c_ml.get("away")),
            "circa_ml_gap_home":  gap(c_ml.get("home")),
            "circa_tot_gap_over": gap(c_tot.get("over")),
            "circa_tot_gap_under": gap(c_tot.get("under")),
            # Public signal: high DK bets% = public side (worth fading)
            "dk_ml_bets_away":    (d_ml.get("away") or {}).get("bets_pct"),
            "dk_ml_bets_home":    (d_ml.get("home") or {}).get("bets_pct"),
            "dk_tot_bets_over":   (d_tot.get("over") or {}).get("bets_pct"),
            "dk_tot_bets_under":  (d_tot.get("under") or {}).get("bets_pct"),
            # Raw lines for display
            "away_ml": (c_ml.get("away") or d_ml.get("away") or {}).get("line"),
            "home_ml": (c_ml.get("home") or d_ml.get("home") or {}).get("line"),
            "total_line": (c_tot or d_tot or {}).get("line"),
        }

        result[sport][key] = {
            "circa":      circa_game,
            "draftkings": dk_game,
            "merged":     merged,
        }

    for sport in sports:
        print(f"[VSIN] {sport}: {len(result[sport])} games with splits data.")

    return result


def find_game(splits_by_sport: dict, sport: str, away: str, home: str) -> dict | None:
    """
    Fuzzy-match a game from the splits dict by team names.
    Returns the full game entry (circa, draftkings, merged) or None.
    """
    games = splits_by_sport.get(sport, {})
    away_n = normalize_team(away)
    home_n = normalize_team(home)
    key = f"{away_n}_{home_n}"

    if key in games:
        return games[key]

    # Fuzzy fallback
    for k, entry in games.items():
        m = entry["merged"]
        va = normalize_team(m["away_team"])
        vh = normalize_team(m["home_team"])
        if (away_n in va or va in away_n) and (home_n in vh or vh in home_n):
            return entry

    return None
