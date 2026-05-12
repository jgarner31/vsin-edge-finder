#!/bin/bash
# Push the VSIN Edge Finder to GitHub so Railway can auto-deploy it.
#
# How to use:
#   1. Create a new GitHub repo called "vsin-edge-finder" at github.com/jgarner31
#   2. Open Terminal and run:
#        cd ~/betting/vsin-edge-finder && bash push_to_github.sh
#   3. In Railway: New Project → Deploy from GitHub → pick vsin-edge-finder
#   4. Set environment variables in Railway:
#        VSIN_COOKIE        — your VSIN Pro session cookie
#        DISCORD_WEBHOOK_URL — your Discord channel webhook (optional)

set -e
cd "$(dirname "$0")"

echo "==> Setting up git in ~/betting/vsin-edge-finder ..."
git init
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/jgarner31/vsin-edge-finder.git

echo "==> Staging all files ..."
git add .
git status

echo "==> Committing ..."
git commit -m "Initialize VSIN Edge Finder — multi-sport Circa-first tracker"

echo "==> Pushing to GitHub (main branch) ..."
git branch -M main
git push -u origin main --force

echo ""
echo "✅ Done! Railway will auto-deploy from GitHub in ~1 minute."
echo ""
echo "Remember to set these environment variables in Railway:"
echo "  VSIN_COOKIE          — your VSIN Pro session cookie"
echo "  DISCORD_WEBHOOK_URL  — Discord webhook URL (optional)"
