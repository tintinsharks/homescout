#!/bin/zsh
# HomeScout 4-hourly refresh: fetch data, commit, push (GitHub Pages redeploys).
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:$PATH"
cd /Users/nprabhak/HomeScout || exit 1

echo "----- $(date '+%Y-%m-%d %H:%M:%S') -----" >> logs/fetch.log
/usr/bin/python3 fetch_data.py >> logs/fetch.log 2>&1 || { echo "FETCH FAILED" >> logs/fetch.log; exit 1; }
./.venv/bin/python enrich_data.py >> logs/fetch.log 2>&1 || echo "ENRICH FAILED (non-fatal)" >> logs/fetch.log
./.venv/bin/python fetch_cities.py >> logs/fetch.log 2>&1 || echo "CITIES FAILED (non-fatal)" >> logs/fetch.log
/usr/bin/python3 hazard_tag.py >> logs/fetch.log 2>&1 || echo "HAZARD FAILED (non-fatal)" >> logs/fetch.log
# market temperature + seasonality: heavier (5y pull), refresh at most once/day
if [ ! -f docs/market.json ] || [ -z "$(find docs/market.json -mtime -1 2>/dev/null)" ]; then
  ./.venv/bin/python market_trends.py >> logs/fetch.log 2>&1 || echo "TRENDS FAILED (non-fatal)" >> logs/fetch.log
fi

# stamp docs/index.html with a content-hash build id (ignoring the stamp line
# itself) so the page auto-reloads clients ONLY when its code actually changes,
# not on data-only refreshes.
NEWHASH=$(sed 's/<meta name="build" content="[^"]*"/<meta name="build" content="X"/' docs/index.html | md5 -q 2>/dev/null || sed 's/<meta name="build" content="[^"]*"/<meta name="build" content="X"/' docs/index.html | md5sum | cut -d' ' -f1)
CURSTAMP=$(sed -n 's/.*<meta name="build" content="\([^"]*\)".*/\1/p' docs/index.html | head -1)
if [ "$NEWHASH" != "$CURSTAMP" ]; then
  sed -i '' "s/<meta name=\"build\" content=\"[^\"]*\"/<meta name=\"build\" content=\"$NEWHASH\"/" docs/index.html
  echo "stamped build $NEWHASH" >> logs/fetch.log
fi

git add -A
if git diff --cached --quiet; then
  echo "no changes" >> logs/fetch.log
  exit 0
fi
git commit -q -m "data refresh $(date '+%Y-%m-%d %H:%M')"
git push -q origin main >> logs/fetch.log 2>&1
