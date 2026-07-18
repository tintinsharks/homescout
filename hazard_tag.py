#!/usr/bin/env python3
"""Tag active listings in docs/data.json with fire / flood hazard zones.

Reads docs/hazards.geojson (see hazards_download.py) and stamps each active:
  fire_zone:  "Very High" | "High" | absent
  flood_zone: "AE" | "AH" | "AO" | "A" | "VE" | "X-0.2%" | absent

Runs in update.sh after fetch_cities so the merged active set is tagged.
Stdlib only; point-in-polygon reused from fetch_data.
"""

import json
from datetime import datetime, timedelta, timezone

import fetch_data as fd

HAZ_FILE = fd.DOCS / "hazards.geojson"
DATA_FILE = fd.DOCS / "data.json"
HISTORY_FILE = fd.ROOT / "history.json"
FIRE_RANK = {"Very High": 2, "High": 1}
FLOOD_RANK = {"VE": 6, "AE": 5, "AH": 4, "AO": 3, "A": 2, "X-0.2%": 1}


def bbox_of(geom):
    pts = []
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        for ring in poly:
            pts.extend(ring)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def main():
    gj = json.loads(HAZ_FILE.read_text())
    zones = [(f["properties"]["kind"], f["properties"]["cls"],
              f["geometry"], bbox_of(f["geometry"]))
             for f in gj["features"]]
    data = json.loads(DATA_FILE.read_text())

    n_fire = n_flood = 0
    for h in data["active"]:
        h.pop("fire_zone", None)
        h.pop("flood_zone", None)
        if not h.get("lat"):
            continue
        lon, lat = h["lon"], h["lat"]
        fire = flood = None
        for kind, cls, geom, (x0, y0, x1, y1) in zones:
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            if kind == "fire" and FIRE_RANK.get(cls, 0) <= FIRE_RANK.get(fire, 0):
                continue
            if kind == "flood" and FLOOD_RANK.get(cls, 0) <= FLOOD_RANK.get(flood, 0):
                continue
            if fd.point_in_geom(lon, lat, geom):
                if kind == "fire":
                    fire = cls
                else:
                    flood = cls
        if fire:
            h["fire_zone"] = fire
            n_fire += 1
        if flood:
            h["flood_zone"] = flood
            n_flood += 1

    track_history(data)   # first-seen + price cuts across ALL cities (was RWC-only)
    slim(data)   # drop bytes the browser never reads → faster/more reliable Pages builds
    DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))
    print(f"hazard-tagged {len(data['active'])} actives: "
          f"{n_fire} in fire zones, {n_flood} in flood zones")


def track_history(data):
    """Between-run first-seen + price history for EVERY active listing (all cities),
    so 🆕 just-listed and ▼ price-cut work everywhere, not just the Redfin core."""
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    try:
        hist = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
    except Exception:
        hist = {}
    seen, n_cut, n_new = set(), 0, 0
    week_ago = (today - timedelta(days=7)).isoformat()
    for h in data["active"]:
        key = h.get("mls") or h.get("address")   # same scheme as before → continuity
        if not key:
            continue
        seen.add(key)
        e = hist.get(key)
        if e is None:
            dom = h.get("dom") or 0
            e = {"first_seen": (today - timedelta(days=min(dom, 120))).isoformat(), "prices": []}
            hist[key] = e
        if not e["prices"] or e["prices"][-1][1] != h["price"]:
            e["prices"].append([today_iso, h["price"]])
        h["first_seen"] = e["first_seen"]
        h["price_history"] = e["prices"]
        if max(p[1] for p in e["prices"]) > h["price"]:
            n_cut += 1
        if e["first_seen"] >= week_ago:
            n_new += 1
    cutoff = (today - timedelta(days=120)).isoformat()
    for k in [k for k, v in hist.items()
              if k not in seen and v.get("prices") and v["prices"][-1][0] < cutoff]:
        del hist[k]
    HISTORY_FILE.write_text(json.dumps(hist, separators=(",", ":")))
    print(f"history: {n_cut} price cuts, {n_new} first-seen ≤7d, {len(hist)} tracked")


# fields the frontend never reads off a SOLD row (kept on actives)
SOLD_DROP = ("type", "mls", "city", "zip", "open_house", "status")


def slim(data):
    for h in data["sold"]:
        for k in SOLD_DROP:
            h.pop(k, None)
        # coords to ~11m precision; ppsf/price already ints
        if h.get("lat") is not None:
            h["lat"] = round(h["lat"], 4)
            h["lon"] = round(h["lon"], 4)
    for h in data["active"]:
        if h.get("remarks"):
            h["remarks"] = h["remarks"][:280]
        if h.get("comps"):
            h["comps"] = h["comps"][:4]


if __name__ == "__main__":
    main()
