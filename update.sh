#!/bin/zsh
# HomeScout 4-hourly refresh: fetch data, commit, push (GitHub Pages redeploys).
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:$PATH"
cd /Users/nprabhak/HomeScout || exit 1

echo "----- $(date '+%Y-%m-%d %H:%M:%S') -----" >> logs/fetch.log
/usr/bin/python3 fetch_data.py >> logs/fetch.log 2>&1 || { echo "FETCH FAILED" >> logs/fetch.log; exit 1; }

git add -A
if git diff --cached --quiet; then
  echo "no changes" >> logs/fetch.log
  exit 0
fi
git commit -q -m "data refresh $(date '+%Y-%m-%d %H:%M')"
git push -q origin main >> logs/fetch.log 2>&1
