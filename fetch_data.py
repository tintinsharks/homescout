#!/usr/bin/env python3
"""HomeScout data fetcher.

Pulls active + sold (365d) single-family listings for Redwood City from
Redfin's CSV download endpoint, classifies each home into an official
city neighborhood polygon, tracks price history between runs, and writes
docs/data.json for the GitHub Pages dashboard.

Run every 4 hours via cron (see update.sh). Stdlib only.
"""

import csv
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
HISTORY_FILE = ROOT / "history.json"
GEOJSON_FILE = DOCS / "neighborhoods.geojson"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE = "https://www.redfin.com/stingray/api/gis-csv"
REGION = "region_id=15525&region_type=6"  # Redwood City
COMMON = "al=1&uipt=1&v=8&sf=1,2,3,5,6,7&status=9&num_homes=350"
MIN_SQFT = 1800  # small buffer below the 2000 hard filter (UI default is 2000)

POCKET_NAMES = {
    "FARMHILL": "Farm Hills",
    "WOODSIDE PLAZA": "Woodside Plaza",
    "MT. CARMEL": "Mount Carmel",
    "EAGLE HILL": "Eagle Hill",
    "EDGEWOOD PARK": "Edgewood Park",
    "CANYON": "Canyon",
    "CENTRAL": "Central",
    "CENTENNIAL": "Centennial",
    "STAMBAUGH - HELLER": "Stambaugh-Heller",
    "REDWOOD OAKS": "Redwood Oaks",
    "PALM": "Palm Park",
    "FRIENDLY ACRES": "Friendly Acres",
    "REDWOOD VILLAGE": "Redwood Village",
    "ROOSEVELT": "Roosevelt",
    "DOWNTOWN": "Downtown",
    "BAIR ISLAND": "Bair Island",
    "REDWOOD SHORES": "Redwood Shores",
}
TARGET_POCKETS = ["Woodside Plaza", "Selby-Atherwood (county)"]
CITY_LIMITS_FILE = ROOT / "city_limits.geojson"


def fetch_csv(params: str):
    url = f"{BASE}?{COMMON}&{REGION}&{params}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            if not text.startswith("SALE TYPE"):
                raise ValueError(f"unexpected response: {text[:120]}")
            reader = csv.DictReader(io.StringIO(text))
            return [r for r in reader if r.get("SALE TYPE") in ("MLS Listing", "PAST SALE")]
        except Exception as e:
            print(f"  fetch attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"all fetch attempts failed for: {params}")


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_geom(lon: float, lat: float, geom: dict) -> bool:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        # even-odd across all rings handles holes
        crossings = sum(point_in_ring(lon, lat, ring) for ring in poly)
        if crossings % 2 == 1:
            return True
    return False


def load_neighborhoods():
    gj = json.loads(GEOJSON_FILE.read_text())
    out = []
    for f in gj["features"]:
        raw = f["properties"].get("NB_Area") or f["properties"].get("name", "")
        name = POCKET_NAMES.get(raw)
        if not name:
            continue
        geom = f["geometry"]
        # bounding box for a cheap pre-check
        pts = []
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for poly in polys:
            for ring in poly:
                pts.extend(ring)
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        out.append((name, geom, (min(lons), min(lats), max(lons), max(lats))))
    return out


def load_city_limits():
    gj = json.loads(CITY_LIMITS_FILE.read_text())
    return [(f["properties"]["NAME"], f["geometry"]) for f in gj["features"]]


def classify(lon, lat, zipcode, hoods, limits):
    if lon is not None and lat is not None:
        for name, geom, (x0, y0, x1, y1) in hoods:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and point_in_geom(lon, lat, geom):
                return name
        # not in any city neighborhood — check if it's outside city limits entirely
        in_rwc = any(n == "REDWOOD CITY" and point_in_geom(lon, lat, g) for n, g in limits)
        if not in_rwc:
            # unincorporated San Mateo County pockets
            if zipcode in ("94061", "94027") and lon > -122.245:
                return "Selby-Atherwood (county)"
            if zipcode == "94062":
                return "Emerald Hills (county)"
            return "Other county"
    if zipcode == "94062":
        return "Emerald Hills (county)"
    if zipcode == "94065":
        return "Redwood Shores"
    if zipcode == "94063":
        return "East RWC"
    return "Other RWC"


def to_int(s):
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def to_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_sold_date(s):
    try:
        return datetime.strptime(s, "%B-%d-%Y").date().isoformat()
    except (ValueError, TypeError):
        return None


def norm_row(r: dict, hoods, limits):
    price = to_int(r.get("PRICE"))
    sqft = to_int(r.get("SQUARE FEET"))
    if not price or not sqft:
        return None
    lat = to_float(r.get("LATITUDE"))
    lon = to_float(r.get("LONGITUDE"))
    zipcode = (r.get("ZIP OR POSTAL CODE") or "").strip()[:5]
    pocket = classify(lon, lat, zipcode, hoods, limits)
    url_key = next((k for k in r if k.startswith("URL")), None)
    return {
        "mls": (r.get("MLS#") or "").strip(),
        "address": (r.get("ADDRESS") or "").strip(),
        "city": (r.get("CITY") or "").strip(),
        "zip": zipcode,
        "price": price,
        "beds": to_float(r.get("BEDS")),
        "baths": to_float(r.get("BATHS")),
        "sqft": sqft,
        "lot": to_int(r.get("LOT SIZE")),
        "year": to_int(r.get("YEAR BUILT")),
        "dom": to_int(r.get("DAYS ON MARKET")),
        "ppsf": round(price / sqft),
        "status": (r.get("STATUS") or "").strip(),
        "sold_date": parse_sold_date(r.get("SOLD DATE")),
        "open_house": (r.get("NEXT OPEN HOUSE START TIME") or "").strip(),
        "url": (r.get(url_key) or "").strip() if url_key else "",
        "lat": lat,
        "lon": lon,
        "pocket": pocket,
        "target": pocket in TARGET_POCKETS,
    }


def dedupe(rows):
    seen = {}
    for row in rows:
        key = row["mls"] or (row["address"], row["zip"])
        seen[key] = row
    return list(seen.values())


def convex_hull(points):
    """Andrew's monotone chain; points = [(lon, lat)]. For the map overlay."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def main():
    hoods = load_neighborhoods()
    limits = load_city_limits()
    today = datetime.now(timezone.utc).date()

    print(f"[{datetime.now(timezone.utc).isoformat()}] fetching active listings...")
    active_rows = fetch_csv(f"min_sqft={MIN_SQFT}")
    print(f"  {len(active_rows)} active rows")

    print("fetching sold (365d), split by price band to dodge the 350-row cap...")
    sold_rows = []
    for band in ("max_price=2000000", "min_price=2000001&max_price=2500000",
                 "min_price=2500001&max_price=3200000", "min_price=3200001"):
        rows = fetch_csv(f"min_sqft={MIN_SQFT}&sold_within_days=365&{band}")
        print(f"  {band}: {len(rows)} rows")
        if len(rows) >= 350:
            print(f"  WARNING: {band} hit the 350-row cap; results may be truncated", file=sys.stderr)
        sold_rows.extend(rows)
        time.sleep(3)

    active = dedupe([x for x in (norm_row(r, hoods, limits) for r in active_rows) if x and not x["sold_date"]])
    sold = dedupe([x for x in (norm_row(r, hoods, limits) for r in sold_rows) if x and x["sold_date"]])
    sold.sort(key=lambda x: x["sold_date"], reverse=True)
    active.sort(key=lambda x: (x["dom"] if x["dom"] is not None else 999))

    # --- price history tracking between runs ---
    history = {}
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())
    now_iso = today.isoformat()
    active_ids = set()
    for home in active:
        key = home["mls"] or home["address"]
        active_ids.add(key)
        entry = history.get(key)
        if entry is None:
            # estimate original list date from days-on-market on first sight
            dom = home["dom"] or 0
            entry = {"first_seen": (today - timedelta(days=dom)).isoformat(), "prices": []}
            history[key] = entry
        if not entry["prices"] or entry["prices"][-1][1] != home["price"]:
            entry["prices"].append([now_iso, home["price"]])
        home["first_seen"] = entry["first_seen"]
        home["price_history"] = entry["prices"]
    # drop listings gone for a while to keep the file tidy
    for key in [k for k, v in history.items() if k not in active_ids
                and v["prices"] and v["prices"][-1][0] < (today - timedelta(days=120)).isoformat()]:
        del history[key]
    HISTORY_FILE.write_text(json.dumps(history, indent=1))

    # map overlay for pockets that have no official polygon (e.g. county islands)
    selby_pts = [(h["lon"], h["lat"]) for h in active + sold
                 if h["pocket"] == "Selby-Atherwood (county)" and h["lon"]]
    extra_polys = {}
    if len(selby_pts) >= 3:
        extra_polys["Selby-Atherwood (county)"] = convex_hull(selby_pts)

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_pockets": TARGET_POCKETS,
        "extra_polys": extra_polys,
        "budget": 3300000,
        "min_sqft_default": 2000,
        "active": active,
        "sold": sold,
    }
    (DOCS / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    n_new = sum(1 for h in active if h.get("dom") is not None and h["dom"] <= 4)
    print(f"wrote docs/data.json: {len(active)} active ({n_new} new), {len(sold)} sold, "
          f"{sum(1 for h in active if h['target'])} active in target pockets")


if __name__ == "__main__":
    main()
