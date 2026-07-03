#!/usr/bin/env python3
"""Download official hazard-zone polygons into docs/hazards.geojson.

Sources:
  - San Mateo County GIS "Wildfire_Severity" (CalFire FHSZ classes, SRA+LRA)
  - Alameda County LRA Fire Hazard Severity Zones (for Fremont / Union City)
  - FEMA NFHL flood hazard zones (SFHA A/AE/AH/AO/VE + 0.2% annual chance)

Run occasionally (zones change on multi-year cycles), not on the 4-hour cron.
hazard_tag.py uses the output to tag every active listing. Stdlib only.
"""

import json
import sys
import urllib.parse
import urllib.request

OUT = "docs/hazards.geojson"
PENINSULA_BOX = "-122.55,37.35,-122.10,37.75"
EASTBAY_BOX = "-122.15,37.45,-121.85,37.65"

SM_FIRE = ("https://services.arcgis.com/yq3FgOI44hYHAFVZ/arcgis/rest/services/"
           "Wildfire_Severity/FeatureServer/0/query")
AL_FIRE = ("https://services7.arcgis.com/T3LbxamSmhpjBppB/arcgis/rest/services/"
           "Alameda_County_Local_Responsibility_Area_Fire_Hazard_Severity_Zone_/"
           "FeatureServer/0/query")
FEMA = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"


def fetch(url, params):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": "homescout/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def paged_geojson(url, base_params):
    feats, offset = [], 0
    while True:
        params = dict(base_params, resultOffset=offset, f="geojson")
        d = fetch(url, params)
        batch = d.get("features", [])
        feats.extend(batch)
        if len(batch) < 1000:
            return feats
        offset += len(batch)


def rnd(geom, nd=5):
    def rr(ring):
        return [[round(x, nd), round(y, nd)] for x, y in ring]
    if geom["type"] == "Polygon":
        geom["coordinates"] = [rr(r) for r in geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        geom["coordinates"] = [[rr(r) for r in p] for p in geom["coordinates"]]
    return geom


def main():
    out = []

    print("San Mateo County fire zones...")
    feats = paged_geojson(SM_FIRE, {
        "where": "HAZ_CLASS IN ('High','Very High')",
        "outFields": "HAZ_CLASS,SRA", "outSR": 4326,
        "maxAllowableOffset": 0.0001, "resultRecordCount": 1000})
    for f in feats:
        out.append({"type": "Feature",
                    "properties": {"kind": "fire", "cls": f["properties"]["HAZ_CLASS"]},
                    "geometry": rnd(f["geometry"])})
    print(f"  {len(feats)} polygons")

    print("Alameda County fire zones (East Bay box)...")
    feats = paged_geojson(AL_FIRE, {
        "where": "FHSZ_Descr IN ('High','Very High')",
        "geometry": EASTBAY_BOX, "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FHSZ_Descr", "outSR": 4326,
        "maxAllowableOffset": 0.0001, "resultRecordCount": 1000})
    for f in feats:
        out.append({"type": "Feature",
                    "properties": {"kind": "fire", "cls": f["properties"]["FHSZ_Descr"]},
                    "geometry": rnd(f["geometry"])})
    print(f"  {len(feats)} polygons")

    for name, box in (("Peninsula", PENINSULA_BOX), ("East Bay", EASTBAY_BOX)):
        print(f"FEMA flood zones ({name} box)...")
        feats = paged_geojson(FEMA, {
            "where": ("FLD_ZONE IN ('A','AE','AH','AO','VE') "
                      "OR ZONE_SUBTY LIKE '0.2 PCT%'"),
            "geometry": box, "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY", "outSR": 4326,
            "maxAllowableOffset": 0.0002, "resultRecordCount": 1000})
        for f in feats:
            p = f["properties"]
            cls = p["FLD_ZONE"] if p["FLD_ZONE"] != "X" else "X-0.2%"
            out.append({"type": "Feature",
                        "properties": {"kind": "flood", "cls": cls},
                        "geometry": rnd(f["geometry"])})
        print(f"  {len(feats)} polygons")

    gj = {"type": "FeatureCollection", "features": out}
    with open(OUT, "w") as fh:
        json.dump(gj, fh, separators=(",", ":"))
    import os
    print(f"wrote {OUT}: {len(out)} features, {os.path.getsize(OUT)//1024} KB")


if __name__ == "__main__":
    sys.exit(main())
