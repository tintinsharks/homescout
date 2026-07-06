#!/usr/bin/env python3
"""Tag active listings in docs/data.json with fire / flood hazard zones.

Reads docs/hazards.geojson (see hazards_download.py) and stamps each active:
  fire_zone:  "Very High" | "High" | absent
  flood_zone: "AE" | "AH" | "AO" | "A" | "VE" | "X-0.2%" | absent

Runs in update.sh after fetch_cities so the merged active set is tagged.
Stdlib only; point-in-polygon reused from fetch_data.
"""

import json

import fetch_data as fd

HAZ_FILE = fd.DOCS / "hazards.geojson"
DATA_FILE = fd.DOCS / "data.json"
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

    slim(data)   # drop bytes the browser never reads → faster/more reliable Pages builds
    DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))
    print(f"hazard-tagged {len(data['active'])} actives: "
          f"{n_fire} in fire zones, {n_flood} in flood zones")


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
