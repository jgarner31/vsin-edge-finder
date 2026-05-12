"""
expert_scraper.py
-----------------
Scrapes VSIN's "Best Bets Today" page and today's expert articles,
then extracts explicit picks with conviction levels.

Handles three distinct article styles:
  - Makinen (MLB/NBA):  "System Match (PLAY): TEAM"  /  "(FADE): TEAM"
  - Cohen (NBA):        "Bet: DESCRIPTION"  or  standalone bet lines
  - Davis (NHL):        "Bet: DESCRIPTION"  /  "NHL Predictions and Best Bets:"
  - Appelbaum (MLB):    "Top Picks from the MLB Betting Splits" format

Returns a list of pick dicts that feed into the scorer.
"""

import re
import requests
from datetime import date
from bs4 import BeautifulSoup
from config import VSIN_COOKIE

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

BEST_BETS_URL = "https://www.vsin.com/best-bets-today/"

# ----------------------------------------------------------------
# Team name → normalized token mapping
# Used to extract team names from free-form article text
# ----------------------------------------------------------------
_TEAM_TOKENS = {
    # MLB
    "angels": ("angels", "MLB"), "guardians": ("guardians", "MLB"),
    "yankees": ("yankees", "MLB"), "orioles": ("orioles", "MLB"),
    "rays": ("rays", "MLB"), "blue jays": ("blue jays", "MLB"),
    "jays": ("blue jays", "MLB"),
    "diamondbacks": ("diamondbacks", "MLB"), "d-backs": ("diamondbacks", "MLB"),
    "rangers": ("rangers", "MLB"), "mariners": ("mariners", "MLB"),
    "astros": ("astros", "MLB"), "giants": ("giants", "MLB"),
    "dodgers": ("dodgers", "MLB"), "brewers": ("brewers", "MLB"),
    "cubs": ("cubs", "MLB"), "cardinals": ("cardinals", "MLB"),
    "reds": ("reds", "MLB"), "pirates": ("pirates", "MLB"),
    "phillies": ("phillies", "MLB"), "mets": ("mets", "MLB"),
    "braves": ("braves", "MLB"), "marlins": ("marlins", "MLB"),
    "nationals": ("nationals", "MLB"), "padres": ("padres", "MLB"),
    "rockies": ("rockies", "MLB"), "athletics": ("athletics", "MLB"),
    "tigers": ("tigers", "MLB"), "royals": ("royals", "MLB"),
    "white sox": ("white sox", "MLB"), "twins": ("twins", "MLB"),
    "red sox": ("red sox", "MLB"),
    # NBA
    "cavaliers": ("cavaliers", "NBA"), "cavs": ("cavaliers", "NBA"),
    "pistons": ("pistons", "NBA"), "celtics": ("celtics", "NBA"),
    "knicks": ("knicks", "NBA"), "lakers": ("lakers", "NBA"),
    "thunder": ("thunder", "NBA"), "wolves": ("timberwolves", "NBA"),
    "timberwolves": ("timberwolves", "NBA"),
    "spurs": ("spurs", "NBA"), "nuggets": ("nuggets", "NBA"),
    "warriors": ("warriors", "NBA"), "suns": ("suns", "NBA"),
    "clippers": ("clippers", "NBA"), "heat": ("heat", "NBA"),
    "bucks": ("bucks", "NBA"), "hawks": ("hawks", "NBA"),
    "sixers": ("76ers", "NBA"), "76ers": ("76ers", "NBA"),
    "pacers": ("pacers", "NBA"), "nets": ("nets", "NBA"),
    "magic": ("magic", "NBA"), "hornets": ("hornets", "NBA"),
    "wizards": ("wizards", "NBA"), "bulls": ("bulls", "NBA"),
    "grizzlies": ("grizzlies", "NBA"), "jazz": ("jazz", "NBA"),
    "rockets": ("rockets", "NBA"), "pelicans": ("pelicans", "NBA"),
    "mavericks": ("mavericks", "NBA"), "mavs": ("mavericks", "NBA"),
    "blazers": ("trail blazers", "NBA"), "trail blazers": ("trail blazers", "NBA"),
    "kings": ("kings", "NBA"), "raptors": ("raptors", "NBA"),
    # NHL
    "avalanche": ("avalanche", "NHL"), "avs": ("avalanche", "NHL"),
    "wild": ("wild", "NHL"), "stars": ("stars", "NHL"),
    "oilers": ("oilers", "NHL"), "flames": ("flames", "NHL"),
    "canucks": ("canucks", "NHL"), "jets": ("jets", "NHL"),
    "golden knights": ("golden knights", "NHL"), "knights": ("golden knights", "NHL"),
    "sharks": ("sharks", "NHL"), "kings": ("kings", "NHL"),
    "ducks": ("ducks", "NHL"), "coyotes": ("coyotes", "NHL"),
    "blues": ("blues", "NHL"), "blackhawks": ("blackhawks", "NHL"),
    "predators": ("predators", "NHL"), "preds": ("predators", "NHL"),
    "lightning": ("lightning", "NHL"), "bolts": ("lightning", "NHL"),
    "panthers": ("panthers", "NHL"), "bruins": ("bruins", "NHL"),
    "maple leafs": ("maple leafs", "NHL"), "leafs": ("maple leafs", "NHL"),
    "senators": ("senators", "NHL"), "canadiens": ("canadiens", "NHL"),
    "habs": ("canadiens", "NHL"), "sabres": ("sabres", "NHL"),
    "penguins": ("penguins", "NHL"), "flyers": ("flyers", "NHL"),
    "capitals": ("capitals", "NHL"), "rangers": ("rangers", "NHL"),
    "islanders": ("islanders", "NHL"), "devils": ("devils", "NHL"),
    "red wings": ("red wings", "NHL"), "hurricanes": ("hurricanes", "NHL"),
    "canes": ("hurricanes", "NHL"),
}

# Article URL keywords that signal what sport/author it covers
_ARTICLE_SIGNALS = [
    # (url_keyword, sport, author)
    ("makinen",         None,  "Makinen"),
    ("greg-peterson",   "MLB", "Peterson"),
    ("top-picks.*mlb",  "MLB", "Appelbaum"),
    ("mlb-betting-splits", "MLB", "Appelbaum"),
    ("nba.*playoffs",   "NBA", "Cohen"),
    ("nba.*best-bets",  "NBA", "Cohen"),
    ("nba.*predictions","NBA", "Cohen"),
    ("nba.*playoff",    "NBA", "Makinen_NBA"),
    ("nhl.*predictions","NHL", "Davis"),
    ("nhl.*best-bets",  "NHL", "Davis"),
    ("nhl.*picks",      "NHL", "Davis"),
    ("wnba",            "WNBA","Shoemaker"),
]


def _today_slug() -> str:
    """Return today's date as it typically appears in VSIN URLs."""
    today = date.today()
    months = ["january","february","march","april","may","june",
              "july","august","september","october","november","december"]
    return f"{months[today.month - 1]}-{today.day}"


def _fetch_url(url: str) -> str | None:
    """Fetch a URL with VSIN auth cookie. Returns HTML or None."""
    if not VSIN_COOKIE:
        return None
    headers = {**_HEADERS, "Cookie": VSIN_COOKIE}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if not resp.ok:
            return None
        return resp.text
    except requests.exceptions.RequestException:
        return None


def _get_article_links() -> list[dict]:
    """
    Fetch the Best Bets Today index and return today's article links
    with sport/author metadata attached.
    """
    html = _fetch_url(BEST_BETS_URL)
    if not html:
        print("[EXPERT] Could not fetch Best Bets Today page.")
        return []

    soup = BeautifulSoup(html, "html.parser")
    today_slug = _today_slug()
    articles = []
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if today_slug not in href.lower():
            continue
        if href in seen_urls:
            continue
        # Skip prop-only articles
        if any(x in href.lower() for x in ["player-props", "prop-bets", "tennis", "golf"]):
            continue

        seen_urls.add(href)
        sport, author = _classify_article(href)
        if sport or author:
            title = (a_tag.get_text(" ", strip=True) or "").split("\n")[0][:80]
            articles.append({"url": href, "sport": sport, "author": author, "title": title})

    print(f"[EXPERT] Found {len(articles)} relevant articles for today.")
    return articles


def _classify_article(url: str) -> tuple[str | None, str | None]:
    """Return (sport, author) based on URL pattern matching."""
    url_lower = url.lower()
    for pattern, sport, author in _ARTICLE_SIGNALS:
        if re.search(pattern, url_lower):
            return sport, author
    # Fallback: infer sport from URL path
    if "/mlb/" in url_lower:
        return "MLB", "Unknown"
    if "/nba/" in url_lower:
        return "NBA", "Unknown"
    if "/nhl/" in url_lower:
        return "NHL", "Unknown"
    if "/wnba/" in url_lower:
        return "WNBA", "Unknown"
    return None, None


# ----------------------------------------------------------------
# Pick extraction patterns
# ----------------------------------------------------------------

# Makinen-style: "System Match (PLAY): LA ANGELS (+144 at CLE)"
_MAKINEN_PLAY_RE = re.compile(
    r"System\s+Match(?:es)?\s*\(PLAY(?:\s+ALL)?\)\s*:?\s*([^\n(]+)",
    re.IGNORECASE,
)
_MAKINEN_FADE_RE = re.compile(
    r"System\s+Match(?:es)?\s*\(FADE(?:\s+ALL)?\)\s*:?\s*([^\n(]+)",
    re.IGNORECASE,
)
_MAKINEN_TREND_PLAY_RE = re.compile(
    r"Trend\s+Match\s*\(PLAY\)\s*:?\s*([^\n(]+)",
    re.IGNORECASE,
)
_MAKINEN_TREND_FADE_RE = re.compile(
    r"Trend\s+Match\s*\(FADE\)\s*:?\s*([^\n(]+)",
    re.IGNORECASE,
)

# Cohen/Davis-style: "Bet: Cavaliers ML (-155)"
_BET_LINE_RE = re.compile(
    r"Bet\s*:\s*([^\n]+(?:ML|moneyline|spread|\+\d+|\-\d+|over|under)[^\n]*)",
    re.IGNORECASE,
)

# Davis NHL explicit pick block:
# "Colorado Avalanche/Wild Over 6 (-130) – 1 unit"
_DAVIS_PICK_RE = re.compile(
    r"([A-Z][a-zA-Z\s/]+(?:Over|Under|ML|moneyline)[\s\d\.]+\([+\-]\d+\))\s*[–-]\s*([\d\.]+)\s*unit",
    re.IGNORECASE,
)

# Strength ratings underpriced:
# "Today's UNDERPRICED FAVORITE: NY YANKEES -156"
_UNDERPRICED_RE = re.compile(
    r"UNDERPRICED\s+FAVORITE[^:]*:\s*([A-Z][A-Z\s]+[-+]\d+)",
    re.IGNORECASE,
)


def _extract_teams_from_text(text: str, sport_hint: str | None = None) -> list[tuple[str, str]]:
    """
    Find all team names (normalized) in a text chunk.
    Returns list of (normalized_name, sport).
    """
    text_lower = text.lower()
    found = []
    # Sort by length descending to prefer longer matches (e.g., "blue jays" before "jays")
    for token, (norm, sport) in sorted(_TEAM_TOKENS.items(), key=lambda x: -len(x[0])):
        if sport_hint and sport != sport_hint:
            continue
        if re.search(r'\b' + re.escape(token) + r'\b', text_lower):
            found.append((norm, sport))
    return found


def _parse_makinen(text: str, sport: str, author: str) -> list[dict]:
    picks = []

    def _add_picks(matches, conviction, is_fade=False):
        for m in matches:
            raw = m.strip().rstrip(",")
            # May be comma-separated list of teams
            parts = re.split(r",\s*", raw)
            for part in parts:
                teams = _extract_teams_from_text(part, sport)
                for norm, t_sport in teams:
                    picks.append({
                        "team": norm,
                        "sport": t_sport,
                        "author": author,
                        "conviction": conviction,
                        "is_fade": is_fade,       # True = this is a FADE (opponent pick)
                        "raw": part.strip(),
                    })

    _add_picks(_MAKINEN_PLAY_RE.findall(text),       conviction="play")
    _add_picks(_MAKINEN_FADE_RE.findall(text),        conviction="play", is_fade=True)
    _add_picks(_MAKINEN_TREND_PLAY_RE.findall(text),  conviction="slight")
    _add_picks(_MAKINEN_TREND_FADE_RE.findall(text),  conviction="slight", is_fade=True)

    # Underpriced favorites (strength ratings)
    for m in _UNDERPRICED_RE.findall(text):
        teams = _extract_teams_from_text(m, sport)
        for norm, t_sport in teams:
            picks.append({
                "team": norm,
                "sport": t_sport,
                "author": author,
                "conviction": "play",
                "is_fade": False,
                "raw": m.strip(),
            })

    return picks


def _parse_cohen(text: str, sport: str, author: str) -> list[dict]:
    picks = []

    for m in _BET_LINE_RE.findall(text):
        raw = m.strip()
        teams = _extract_teams_from_text(raw, sport)
        for norm, t_sport in teams:
            picks.append({
                "team": norm,
                "sport": t_sport,
                "author": author,
                "conviction": "play",
                "is_fade": False,
                "raw": raw,
            })

    # Also look for parlay lines: "PARLAY: Cavaliers ML ... & Thunder ML ..."
    parlay_re = re.compile(r"PARLAY\s*:\s*([^\n]+)", re.IGNORECASE)
    for m in parlay_re.findall(text):
        teams = _extract_teams_from_text(m, sport)
        for norm, t_sport in teams:
            picks.append({
                "team": norm,
                "sport": t_sport,
                "author": author,
                "conviction": "slight",    # parlays are lower conviction on each leg
                "is_fade": False,
                "raw": m.strip(),
            })

    return picks


def _parse_davis(text: str, sport: str, author: str) -> list[dict]:
    picks = []

    # Davis uses explicit "Bet:" lines
    for m in _BET_LINE_RE.findall(text):
        raw = m.strip()
        teams = _extract_teams_from_text(raw, sport)
        for norm, t_sport in teams:
            picks.append({
                "team": norm,
                "sport": t_sport,
                "author": author,
                "conviction": "play",
                "is_fade": False,
                "raw": raw,
            })

    # Davis also uses "TEAM Over/Under X.X (-odds) – N units" blocks
    for m in _DAVIS_PICK_RE.findall(text):
        desc, units = m
        teams = _extract_teams_from_text(desc, sport)
        try:
            unit_weight = float(units)
        except ValueError:
            unit_weight = 1.0
        conv = "play" if unit_weight >= 1.0 else "slight"
        for norm, t_sport in teams:
            picks.append({
                "team": norm,
                "sport": t_sport,
                "author": author,
                "conviction": conv,
                "is_fade": False,
                "raw": desc.strip(),
            })

    return picks


def _parse_article(html: str, sport: str | None, author: str | None) -> list[dict]:
    """
    Extract picks from a single article's HTML.
    Dispatches to the appropriate parser based on author.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try to get just the article body to avoid nav/sidebar noise
    article_body = (
        soup.find("article") or
        soup.find(class_=re.compile(r"post-content|entry-content|article-body|td-post-content")) or
        soup.find("main")
    )
    text = (article_body or soup).get_text(" ", strip=True)

    if len(text) < 200:
        return []

    if author in ("Makinen", "Makinen_NBA", "Peterson", "Appelbaum", "Unknown"):
        return _parse_makinen(text, sport or "MLB", author)
    elif author in ("Cohen",):
        return _parse_cohen(text, sport or "NBA", author)
    elif author in ("Davis",):
        return _parse_davis(text, sport or "NHL", author)
    else:
        # Generic fallback: try both Makinen and Cohen patterns
        picks = _parse_makinen(text, sport or "MLB", author or "Unknown")
        picks += _parse_cohen(text, sport or "MLB", author or "Unknown")
        return picks


def fetch_expert_picks() -> dict[str, list[dict]]:
    """
    Fetch all today's expert articles and return picks grouped by sport.

    Return structure:
      {
        "MLB": [
          {
            "team": "astros",      # normalized team name
            "sport": "MLB",
            "author": "Makinen",
            "conviction": "play",  # "play" or "slight"
            "is_fade": False,      # True = this team is being FADED
            "raw": "HOUSTON ASTROS (+119 at SEA)",
          }, ...
        ],
        "NBA": [...],
        "NHL": [...],
      }
    """
    articles = _get_article_links()
    picks_by_sport: dict[str, list[dict]] = {"MLB": [], "NBA": [], "NHL": [], "WNBA": []}

    for article in articles:
        url    = article["url"]
        sport  = article["sport"]
        author = article["author"]

        print(f"[EXPERT] Parsing: {author} / {sport} — {url}")
        html = _fetch_url(url)
        if not html:
            print(f"[EXPERT] Could not fetch article: {url}")
            continue

        picks = _parse_article(html, sport, author)
        print(f"[EXPERT]   → {len(picks)} picks extracted.")

        for pick in picks:
            bucket = pick.get("sport") or sport or "MLB"
            if bucket in picks_by_sport:
                picks_by_sport[bucket].append(pick)

    # Deduplicate: if same team/sport/is_fade appears from multiple articles,
    # keep the one with highest conviction
    conv_rank = {"play": 2, "slight": 1}
    for sport_key in picks_by_sport:
        seen: dict[tuple, dict] = {}
        for pick in picks_by_sport[sport_key]:
            key = (pick["team"], pick["sport"], pick["is_fade"])
            existing = seen.get(key)
            if not existing or conv_rank.get(pick["conviction"], 0) > conv_rank.get(existing["conviction"], 0):
                seen[key] = pick
        picks_by_sport[sport_key] = list(seen.values())

    totals = {k: len(v) for k, v in picks_by_sport.items()}
    print(f"[EXPERT] Final picks after dedup: {totals}")
    return picks_by_sport


def get_picks_for_team(expert_picks: list[dict], team_norm: str) -> list[dict]:
    """
    Return all picks that reference a given normalized team name.
    Handles both direct picks and fade picks (where the team is being faded,
    meaning the OPPONENT is the actual pick — caller handles inversion).
    """
    return [p for p in expert_picks if p["team"] == team_norm]
