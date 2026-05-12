"""
config.py
---------
VSIN Edge Finder — Configuration

All secrets come from environment variables (set in Railway).
Edit the non-secret defaults here freely.
"""

import os

# ----------------------------------------------------------------
# VSIN PRO COOKIES  (required — everything depends on these)
#
# VSIN_COOKIE     → data.vsin.com splits API cookie
#   How to get it:
#     1. Log into vsin.com in Chrome
#     2. Open DevTools → Network tab → click any splits page
#     3. Find the request to data.vsin.com → Request Headers → Cookie
#     4. Copy the full cookie string
#
# VSIN_WWW_COOKIE → www.vsin.com subscriber article cookie
#   How to get it:
#     1. Log into www.vsin.com in Chrome
#     2. Open DevTools → Network tab → click any paywalled article
#     3. Find the request for that page → Request Headers → Cookie
#     4. Copy the full cookie string (includes wordpress_logged_in_... etc.)
#
#   Set both as separate environment variables in Railway.
# ----------------------------------------------------------------
VSIN_COOKIE     = os.environ.get("VSIN_COOKIE",     "").strip()
VSIN_WWW_COOKIE = os.environ.get("VSIN_WWW_COOKIE", "").strip()

# ----------------------------------------------------------------
# DISCORD WEBHOOK  (optional but highly recommended)
# Channel Settings → Integrations → Webhooks → Copy Webhook URL
# ----------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# ----------------------------------------------------------------
# SPORTS TO TRACK
# Remove any sport you don't want. Options: "MLB", "NBA", "NHL", "WNBA"
# ----------------------------------------------------------------
ACTIVE_SPORTS = ["MLB", "NBA", "NHL"]

# ----------------------------------------------------------------
# SCORING THRESHOLDS
# ----------------------------------------------------------------
BET_THRESHOLD  = 7   # score >= this → BET
LEAN_THRESHOLD = 4   # score >= this → LEAN  (below = PASS)

# Minimum Circa handle/bets gap to register as a sharp signal
SHARP_GAP_MINIMUM = 12   # points (e.g. 60% handle vs 48% bets = 12pt gap)

# Minimum DK public bets % on the opposing side to award fade bonus
DK_FADE_THRESHOLD     = 65   # 65%+ public bets on opponent → +1 pt
DK_FADE_STRONG        = 75   # 75%+ public bets on opponent → +2 pts

# Minimum confidence to fire a Discord alert
DISCORD_MIN_SCORE = 7   # only BETs get Discorded

# ----------------------------------------------------------------
# POLLING SCHEDULE
# ----------------------------------------------------------------
POLL_INTERVAL_SECONDS = 3 * 60 * 60   # 3 hours during active window
ACTIVE_HOURS_START    = 9              # 9 AM Central
ACTIVE_HOURS_END      = 23            # 11 PM Central (covers late West Coast)

# ----------------------------------------------------------------
# EXPERT ARTICLE SETTINGS
# ----------------------------------------------------------------
# How many articles back to look on Best Bets Today
MAX_ARTICLES_PER_SPORT = 4

# Minimum word count for an article to be worth parsing
MIN_ARTICLE_LENGTH = 200

# ----------------------------------------------------------------
# POLYMARKET
# ----------------------------------------------------------------
POLYMARKET_ENABLED  = True
# Minimum liquidity for Polymarket signal to count
POLY_MIN_LIQUIDITY  = 1_000
# Edge thresholds (Polymarket prob vs implied book prob)
POLY_EDGE_CONFIRM   = 0.03   # +3% → adds to score
POLY_EDGE_STRONG    = 0.06   # +6% → bigger bonus
POLY_EDGE_AGAINST   = 0.03   # -3% → subtracts from score

# ----------------------------------------------------------------
# PORT  (Railway sets this automatically)
# ----------------------------------------------------------------
PORT = int(os.environ.get("PORT", 8080))
