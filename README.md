# RWC HomeScout

Private home-search dashboard: the entire Peninsula (Daly City → Palo Alto, every city its own pocket) + East Bay (Fremont, Union City). Targets: Woodside Plaza, Selby-Atherwood, Menlo Park, West Menlo.

- **Dashboard**: GitHub Pages serves `docs/` → https://tintinsharks.github.io/homescout/
- **Targets**: Woodside Plaza + Selby-Atherwood (unincorporated San Mateo County island between RWC and Atherton; boundary = convex hull of classified homes, city limits from county GIS `City_Limits` layer)
- **Data**: `fetch_data.py` pulls active + 365-day sold single-family listings (1800+ sqft)
  from Redfin's CSV download endpoint, classifies each home into official City of
  Redwood City neighborhood polygons (`docs/neighborhoods.geojson`), and tracks price
  history between runs in `history.json`.
- **Other cities**: `fetch_cities.py` (runs in .venv) pulls 18 cities (rest of Peninsula + East Bay) from Realtor.com via HomeHarvest and merges into data.json with global de-dupe — Redfin region IDs are unavailable for most cities and its 350-row cap truncates their volume.
- **Refresh**: `update.sh` runs via cron every 4 hours on the Mac, commits `docs/data.json`,
  and pushes — Pages redeploys automatically.

```
15 */4 * * * /Users/nprabhak/HomeScout/update.sh
```

Manual refresh: `./update.sh` (or `python3 fetch_data.py` without pushing).
