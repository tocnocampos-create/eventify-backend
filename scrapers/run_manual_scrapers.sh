#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"
source venv/bin/activate

export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:MbowHygexBYnHROAJguYAaccBeNIrvwz@shuttle.proxy.rlwy.net:17408/railway}"

echo ""
echo "████████████████████████████████████████████████████████████"
echo "  CINÉPOLIS SCRAPER"
echo "████████████████████████████████████████████████████████████"
echo ""
python scrapers/cinepolis_scraper.py || echo "[!] cinepolis_scraper.py exited with error $?"

echo ""
echo "████████████████████████████████████████████████████████████"
echo "  THELONIOUS SCRAPER"
echo "████████████████████████████████████████████████████████████"
echo ""
python scrapers/thelonious_scraper.py || echo "[!] thelonious_scraper.py exited with error $?"

echo ""
echo "████████████████████████████████████████████████████████████"
echo "  DONE"
echo "████████████████████████████████████████████████████████████"
echo ""
