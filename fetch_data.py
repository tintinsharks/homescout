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
import statistics as st
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
COMMON = "al=1&uipt=1&v=8&sf=1,2,3,5,6,7&status=9&num_homes=350"
MIN_SQFT = 1800  # small buffer below the 2000 hard filter (UI default is 2000)

ATHERTON_POLY = ("poly=-122.24+37.435,-122.17+37.435,-122.17+37.472,"
                 "-122.24+37.472,-122.24+37.435")
# (region query, sold price bands sized to keep each response under the 350-row cap)
REGIONS = [
    ("Redwood City", "region_id=15525&region_type=6",
     ["max_price=1300000", "min_price=1300001&max_price=1600000",
      "min_price=1600001&max_price=1850000", "min_price=1850001&max_price=2100000",
      "min_price=2100001&max_price=2400000", "min_price=2400001&max_price=2750000",
      "min_price=2750001&max_price=3200000", "min_price=3200001&max_price=4000000",
      "min_price=4000001"]),
    ("Menlo Park", "region_id=11961&region_type=6",
     ["max_price=2200000", "min_price=2200001&max_price=2900000",
      "min_price=2900001&max_price=3800000", "min_price=3800001&max_price=5200000",
      "min_price=5200001"]),
    ("Atherton", ATHERTON_POLY,
     ["max_price=6500000", "min_price=6500001"]),
]
# poly/box queries can leak neighbors we don't cover
EXCLUDE_CITIES = {"PALO ALTO", "EAST PALO ALTO", "STANFORD", "LOS ALTOS",
                  "SAN CARLOS", "MOUNTAIN VIEW", "SUNNYVALE", "PORTOLA VALLEY"}

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
TARGET_POCKETS = ["Woodside Plaza", "Selby-Atherwood (county)",
                  "Menlo Park", "West Menlo (county)"]
CITY_LIMITS_FILE = ROOT / "city_limits.geojson"


def fetch_csv(region: str, params: str):
    url = f"{BASE}?{COMMON}&{region}&{params}"
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


# cities covered whole-city via the HomeHarvest module (fetch_cities.py) —
# recognized here by name so market_trends buckets their rows correctly
CITY_POCKETS = {
    "FREMONT": "Fremont", "UNION CITY": "Union City",
    "SAN CARLOS": "San Carlos", "BELMONT": "Belmont", "SAN MATEO": "San Mateo",
    "FOSTER CITY": "Foster City", "BURLINGAME": "Burlingame",
    "HILLSBOROUGH": "Hillsborough", "MILLBRAE": "Millbrae",
    "SAN BRUNO": "San Bruno", "SOUTH SAN FRANCISCO": "South San Francisco",
    "DALY CITY": "Daly City", "PACIFICA": "Pacifica",
    "HALF MOON BAY": "Half Moon Bay", "WOODSIDE": "Woodside",
    "PORTOLA VALLEY": "Portola Valley", "PALO ALTO": "Palo Alto",
    "EAST PALO ALTO": "East Palo Alto",
}


def classify(lon, lat, zipcode, city, hoods, limits):
    cu = city.upper()
    if cu in CITY_POCKETS and cu not in ("WOODSIDE",):
        # Woodside still goes through the polygon path (RWC-border spillover)
        return CITY_POCKETS[cu]
    if lon is not None and lat is not None:
        for name, geom, (x0, y0, x1, y1) in hoods:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and point_in_geom(lon, lat, geom):
                return name
        # not in an RWC neighborhood — which city limits (if any) contain it?
        in_city = next((n for n, g in limits if point_in_geom(lon, lat, g)), None)
        if in_city == "ATHERTON":
            return "Atherton"
        if in_city == "MENLO PARK":
            return "Menlo Park"
        if in_city is None:
            # unincorporated San Mateo County pockets (or towns we don't map)
            if city.upper() == "WOODSIDE":
                return "Woodside"
            if zipcode in ("94061", "94027") and lon > -122.245:
                return "Selby-Atherwood (county)"
            if zipcode == "94062":
                return "Emerald Hills (county)"
            if zipcode == "94025":
                return "West Menlo (county)"
            return "Other county"
    if zipcode == "94062":
        return "Emerald Hills (county)"
    if zipcode == "94065":
        return "Redwood Shores"
    if zipcode == "94063":
        return "East RWC"
    if zipcode in ("94025", "94026"):
        return "Menlo Park"
    if zipcode == "94027":
        return "Atherton"
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
    sqft = to_int(r.get("SQUARE FEET"))   # may be missing on fresh listings
    if not price:
        return None
    city = (r.get("CITY") or "").strip()
    if city.upper() in EXCLUDE_CITIES:
        return None
    lat = to_float(r.get("LATITUDE"))
    lon = to_float(r.get("LONGITUDE"))
    zipcode = (r.get("ZIP OR POSTAL CODE") or "").strip()[:5]
    pocket = classify(lon, lat, zipcode, city, hoods, limits)
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
        "ppsf": round(price / sqft) if sqft else None,
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


def haversine_m(lat1, lon1, lat2, lon2):
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def gem_score(home, sold, today):
    """Comp-based expected price for an active listing.

    Comps: sold ≤15 months ago, sqft within 0.75x–1.35x, same pocket or ≤1.2 km,
    and plausible $/sqft (guards against bad-data comps). Expected $/sqft is the
    MEDIAN of the top-12 comps' $/sqft — robust to a single mis-keyed outlier and
    to the bimodal pricing you get across school-district lines. gem_pct is capped
    at ±35% since anything larger is almost always a data artifact, not a deal.
    """
    if not home["lat"] or not home.get("sqft"):
        return
    cutoff = (today - timedelta(days=456)).isoformat()
    cands = []
    for s in sold:
        if not s["sold_date"] or s["sold_date"] < cutoff or not s["lat"]:
            continue
        if not (200 <= s["ppsf"] <= 3000):          # drop bad-data comps
            continue
        if not (0.75 * home["sqft"] <= s["sqft"] <= 1.35 * home["sqft"]):
            continue
        dist = haversine_m(home["lat"], home["lon"], s["lat"], s["lon"])
        same = s["pocket"] == home["pocket"]
        if not same and dist > 1200:
            continue
        months = max(0.0, (today - datetime.strptime(s["sold_date"], "%Y-%m-%d").date()).days / 30.4)
        w = (1.0 / (1 + months / 6)) \
            * (1.0 / (1 + abs(s["sqft"] - home["sqft"]) / (0.25 * home["sqft"]))) \
            * (1.0 / (1 + dist / 800)) \
            * (1.6 if same else 1.0)
        cands.append((w, dist, s))
    cands.sort(key=lambda x: -x[0])
    top = cands[:12]
    if len(top) < 4:
        return
    exp_ppsf = st.median([s["ppsf"] for _, _, s in top])
    expected = int(exp_ppsf * home["sqft"])
    home["expected_price"] = expected
    home["gem_pct"] = max(-35.0, min(35.0, round((expected - home["price"]) / expected * 100, 1)))
    home["n_comps"] = len(top)
    home["comps"] = [{
        "address": s["address"], "sold_date": s["sold_date"], "price": s["price"],
        "sqft": s["sqft"], "ppsf": s["ppsf"], "dist_m": int(d), "pocket": s["pocket"],
        "url": s["url"],
    } for _, d, s in top[:5]]


def send_alerts(active, prev_active):
    """POST new-listing / price-cut alerts for target pockets to a Discord webhook.

    Enable by creating ~/.homescout_alerts.json: {"discord_webhook_url": "https://..."}
    """
    cfg_path = Path.home() / ".homescout_alerts.json"
    if not cfg_path.exists():
        return
    try:
        url = json.loads(cfg_path.read_text()).get("discord_webhook_url")
    except Exception:
        return
    if not url:
        return
    prev = {h["mls"]: h for h in prev_active}
    lines = []
    for h in active:
        high_gem = (h.get("gem_pct") or 0) >= 10 and h["price"] <= 3300000
        if (not h["target"] and not high_gem) or h["sqft"] < 2000:
            continue
        old = prev.get(h["mls"])
        gem = f" · gem {h['gem_pct']:+.0f}%" if h.get("gem_pct") is not None else ""
        if old is None:
            icon = "🆕" if h["target"] else "💎"
            lines.append(f"{icon} **{h['address']}** ({h['pocket']}) — ${h['price']:,} · "
                         f"{h['sqft']:,} sqft · ${h['ppsf']}/sqft{gem}\n{h['url']}")
        elif h["price"] < old["price"]:
            lines.append(f"✂️ **{h['address']}** cut ${old['price'] - h['price']:,} → ${h['price']:,}{gem}\n{h['url']}")
    if not lines:
        return
    body = json.dumps({"content": "🏡 **HomeScout**\n" + "\n\n".join(lines[:8])}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
        print(f"sent {len(lines)} alert(s) to Discord")
    except Exception as e:
        print(f"alert send failed: {e}", file=sys.stderr)


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

    active_rows, sold_rows = [], []
    for name, region, bands in REGIONS:
        print(f"[{datetime.now(timezone.utc).isoformat()}] {name}: fetching active listings...")
        rows = fetch_csv(region, f"min_sqft={MIN_SQFT}")
        print(f"  {len(rows)} active rows")
        if len(rows) >= 350:
            print(f"  WARNING: {name} actives hit the 350-row cap", file=sys.stderr)
        active_rows.extend(rows)
        time.sleep(2)
        print(f"{name}: fetching sold (730d) by price band...")
        for band in bands:
            rows = fetch_csv(region, f"sold_within_days=730&min_sqft={MIN_SQFT}&{band}")
            print(f"  {band}: {len(rows)} rows")
            if len(rows) >= 350:
                print(f"  WARNING: {name} {band} hit the 350-row cap; results may be truncated", file=sys.stderr)
            sold_rows.extend(rows)
            time.sleep(3)

    active = dedupe([x for x in (norm_row(r, hoods, limits) for r in active_rows) if x and not x["sold_date"]])
    sold = dedupe([x for x in (norm_row(r, hoods, limits) for r in sold_rows) if x and x["sold_date"] and x["sqft"]])
    sold.sort(key=lambda x: x["sold_date"], reverse=True)
    active.sort(key=lambda x: (x["dom"] if x["dom"] is not None else 999))

    for home in active:
        gem_score(home, sold, today)

    # previous snapshot for alert diffing (before we overwrite data.json)
    prev_active = []
    prev_path = DOCS / "data.json"
    if prev_path.exists():
        try:
            prev_active = json.loads(prev_path.read_text()).get("active", [])
        except Exception:
            pass

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
    extra_polys = {}
    for pocket in ("Selby-Atherwood (county)", "West Menlo (county)"):
        pts = [(h["lon"], h["lat"]) for h in active + sold
               if h["pocket"] == pocket and h["lon"]]
        if len(pts) >= 3:
            extra_polys[pocket] = convex_hull(pts)

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
    send_alerts(active, prev_active)
    n_new = sum(1 for h in active if h.get("dom") is not None and h["dom"] <= 4)
    print(f"wrote docs/data.json: {len(active)} active ({n_new} new), {len(sold)} sold, "
          f"{sum(1 for h in active if h['target'])} active in target pockets")


if __name__ == "__main__":
    main()
