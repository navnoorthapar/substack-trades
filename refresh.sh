#!/usr/bin/env bash
# Run the full pipeline locally and push updated data + site to GitHub.
# Usage: ./refresh.sh
set -e

cd "$(dirname "$0")"

echo "=== Fetching posts from Substack ==="
python fetch_all_posts.py

echo ""
echo "=== Extracting trades ==="
python extract_trades.py

echo ""
echo "=== Filtering & deduplicating ==="
python filter_trades.py

echo ""
echo "=== Building site ==="
python build_site.py

echo ""
echo "=== Committing and pushing ==="
git add trades_extracted.json docs/index.html
if git diff --staged --quiet; then
  echo "No changes since last run."
else
  TRADE_COUNT=$(python -c "import json; print(len(json.load(open('trades_extracted.json'))))")
  git commit -m "update: ${TRADE_COUNT} trades ($(date -u '+%Y-%m-%d'))"
  git push origin main
  echo ""
  echo "Done. Site will be live in ~1 minute at:"
  echo "https://navnoorthapar.github.io/substack-trades/"
fi
