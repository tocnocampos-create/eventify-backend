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
echo "  TICKETMASTER SCRAPER"
echo "████████████████████████████████████████████████████████████"
echo ""
python scrapers/ticketmaster_scraper.py || echo "[!] ticketmaster_scraper.py exited with error $?"

echo ""
echo "████████████████████████████████████████████████████████████"
echo "  FEVER SCRAPER"
echo "████████████████████████████████████████████████████████████"
echo ""
python scrapers/fever_scraper.py || echo "[!] fever_scraper.py exited with error $?"

echo ""
echo "████████████████████████████████████████████████████████████"
echo "  PASSLINE SCRAPER"
echo "████████████████████████████████████████████████████████████"
echo ""
python scrapers/passline_scraper.py || echo "[!] passline_scraper.py exited with error $?"

echo ""
echo "████████████████████████████████████████████████████████████"
echo "  DONE"
echo "████████████████████████████████████████████████████████████"
echo ""
