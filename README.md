# RWC HomeScout

Private home-search dashboard for Redwood City (Farm Hills & Woodside Plaza focus).

- **Dashboard**: GitHub Pages serves `docs/` → https://tintinsharks.github.io/homescout/
- **Data**: `fetch_data.py` pulls active + 365-day sold single-family listings (1800+ sqft)
  from Redfin's CSV download endpoint, classifies each home into official City of
  Redwood City neighborhood polygons (`docs/neighborhoods.geojson`), and tracks price
  history between runs in `history.json`.
- **Refresh**: `update.sh` runs via cron every 4 hours on the Mac, commits `docs/data.json`,
  and pushes — Pages redeploys automatically.

```
15 */4 * * * /Users/nprabhak/HomeScout/update.sh
```

Manual refresh: `./update.sh` (or `python3 fetch_data.py` without pushing).
