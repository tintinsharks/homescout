#!/usr/bin/env python3
"""HomeScout per-city module (runs in .venv — needs homeharvest).

The core pipeline (fetch_data.py) covers Redwood City / Menlo Park / Atherton
via Redfin. Every other city — the rest of the Peninsula plus the East Bay —
comes from Realtor.com via HomeHarvest: queried per city (no neighbor leakage,
no 350-row cap) and merged into docs/data.json after fetch_data + enrich run.

Each city becomes its own pocket. Actives arrive pre-enriched (AVM / remarks /
schools from the same call); solds carry sale-to-list + DOM; actives are then
gem-scored against the full combined comp pool.
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone

import pandas as pd
from homeharvest import scrape_property

import fetch_data as fd
from enrich_data import OPP_KEYWORDS

DATA_FILE = fd.DOCS / "data.json"
# HomeHarvest's scrape_property has no read timeout, so a single unresponsive
# Realtor.com socket can hang forever and freeze the whole refresh (it once
# stalled ~7h, blocking every scheduled run behind it). Run each call in a
# daemon thread and abandon it past this many seconds — the caller's except
# block then skips that city and continues. Normal calls finish in seconds.
SCRAPE_TIMEOUT = 180


def scrape_property_timeout(**kwargs):
    """scrape_property with a hard wall-clock timeout. Raises TimeoutError if the
    call outlives SCRAPE_TIMEOUT; the stuck daemon thread is abandoned (dies on
    process exit) so it can never block the pipeline or a clean shutdown."""
    box = {}
    def run():
        try:
            box["df"] = scrape_property(**kwargs)
        except Exception as e:      # noqa: BLE001 — re-raised in caller thread
            box["err"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(SCRAPE_TIMEOUT)
    if t.is_alive():
        raise TimeoutError(f"scrape_property exceeded {SCRAPE_TIMEOUT}s")
    if "err" in box:
        raise box["err"]
    return box.get("df")
# (location, pocket name or None to classify by polygon, also fetch solds?)
# The core three cities are covered by Redfin for solds, but their ACTIVES are
# pulled here too: Redfin's CSV endpoint omits some MLS listings by rule, so
# the active set is the union of both feeds (global de-dupe, Redfin rows win).
CITIES = [
    ("Redwood City, CA", None, False), ("Menlo Park, CA", None, False),
    ("Atherton, CA", None, False),
    # East Bay
    ("Fremont, CA", "Fremont", True), ("Union City, CA", "Union City", True),
    # Peninsula
    ("San Carlos, CA", "San Carlos", True), ("Belmont, CA", "Belmont", True),
    ("San Mateo, CA", "San Mateo", True), ("Foster City, CA", "Foster City", True),
    ("Burlingame, CA", "Burlingame", True), ("Hillsborough, CA", "Hillsborough", True),
    ("Millbrae, CA", "Millbrae", True), ("San Bruno, CA", "San Bruno", True),
    ("South San Francisco, CA", "South San Francisco", True),
    ("Daly City, CA", "Daly City", True), ("Pacifica, CA", "Pacifica", True),
    ("Half Moon Bay, CA", "Half Moon Bay", True), ("Woodside, CA", "Woodside", True),
    ("Portola Valley, CA", "Portola Valley", True), ("Palo Alto, CA", "Palo Alto", True),
    ("East Palo Alto, CA", "East Palo Alto", True),
]
SOLD_DAYS = 730


def i(v):
    return fd.to_int(v)


def f(v):
    try:
        return None if pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return None


def iso(v):
    return str(v)[:10] if v is not None and not pd.isna(v) else None


def s(v):
    try:
        return "" if v is None or pd.isna(v) else str(v)
    except (TypeError, ValueError):
        return str(v)


def row_from(r, pocket, hoods=None, limits=None):
    price = i(r.get("list_price")) or i(r.get("sold_price"))
    sqft = i(r.get("sqft"))          # may be missing on fresh listings — keep the row
    if not price:
        return None
    lat, lon = f(r.get("latitude")), f(r.get("longitude"))
    city = s(r.get("city")).strip()
    if pocket is None:
        zc = s(r.get("zip_code"))[:5]
        pocket = fd.classify(lon, lat, zc, city, hoods or [], limits or [])
    fb, hb = i(r.get("full_baths")) or 0, i(r.get("half_baths")) or 0
    return {
        "type": "home",
        "mls": s(r.get("mls_id")).strip(),
        "address": s(r.get("full_street_line")).strip(),
        "city": s(r.get("city")).strip(),
        "zip": s(r.get("zip_code"))[:5],
        "price": price,
        "beds": f(r.get("beds")),
        "baths": (fb + 0.5 * hb) or None,
        "sqft": sqft,
        "lot": i(r.get("lot_sqft")),
        "year": i(r.get("year_built")),
        "dom": i(r.get("days_on_mls")),
        "ppsf": round(price / sqft) if sqft else None,
        "status": s(r.get("status")).strip().title(),
        "sold_date": None,
        "open_house": "",
        "url": s(r.get("property_url")),
        "photo": s(r.get("primary_photo")),
        "lat": lat,
        "lon": lon,
        "pocket": pocket,
        "target": pocket in fd.TARGET_POCKETS,
    }


def enrich_active(h, r):
    est = i(r.get("estimated_value"))
    if est:
        h["est_value"] = est
        h["vs_est_pct"] = round((est - h["price"]) / est * 100, 1)
    h["assessed"] = i(r.get("assessed_value"))
    h["tax"] = i(r.get("tax"))
    h["hoa"] = i(r.get("hoa_fee"))
    h["last_sold"] = iso(r.get("last_sold_date"))
    h["last_sold_price"] = i(r.get("last_sold_price"))
    text = "" if pd.isna(r.get("text")) else str(r.get("text"))
    h["remarks"] = text[:500]
    low = text.lower()
    h["opp_flags"] = sorted({k.upper().replace(" ", "-") for k in OPP_KEYWORDS if k in low})
    sch = r.get("nearby_schools")
    if isinstance(sch, str):
        h["schools"] = sch[:200]
    elif isinstance(sch, list):
        h["schools"] = ", ".join(map(str, sch[:4]))


def main():
    data = json.loads(DATA_FILE.read_text())
    today = datetime.now(timezone.utc).date()

    hoods = fd.load_neighborhoods()
    limits = fd.load_city_limits()
    eb_active, eb_sold = [], []
    for loc, pocket, want_solds in CITIES:
        for lt in ("for_sale", "pending"):
            for ptype, typ in (("single_family", "home"), ("land", "lot")):
                try:
                    df = scrape_property_timeout(location=loc, listing_type=lt,
                                                 property_type=[ptype])
                except Exception as e:
                    print(f"{loc} {lt} {ptype} failed: {e}", file=sys.stderr)
                    continue
                for _, r in df.iterrows():
                    h = row_from(r, pocket, hoods, limits)
                    if not h:      # actives: keep every size, even missing sqft
                        continue
                    h["type"] = typ
                    enrich_active(h, r)
                    eb_active.append(h)
                print(f"{loc} {lt} {ptype}: {len(df)} rows")
        if not want_solds:     # core cities: Redfin already provides solds
            continue
        try:
            df = scrape_property_timeout(location=loc, listing_type="sold",
                                         property_type=["single_family"], past_days=SOLD_DAYS)
        except Exception as e:
            print(f"{loc} sold failed: {e}", file=sys.stderr)
            continue
        for _, r in df.iterrows():
            h = row_from(r, pocket, hoods, limits)
            # solds are comps for a 2000+ sqft search: need real size + sane ppsf
            if not h or not h["sqft"] or h["sqft"] < fd.MIN_SQFT:
                continue
            h["sold_date"] = iso(r.get("last_sold_date"))
            if not h["sold_date"]:
                continue
            sp, lp = i(r.get("sold_price")), i(r.get("list_price"))
            if sp:
                h["price"] = sp
                h["ppsf"] = round(sp / h["sqft"])
            if not (200 <= h["ppsf"] <= 3000):        # drop bad-data rows
                continue
            if sp and lp and lp > 0:
                h["list_price"] = lp
                h["s2l"] = round(sp / lp * 100, 1)
            eb_sold.append(h)
        print(f"{loc} sold: {len(df)} rows")

    # merge with global de-dupe (Woodside etc. can arrive from both Redfin
    # spillover and the per-city pull — Redfin rows were added first and win)
    def key(h):
        return h["mls"] or (h["address"].upper(), h["zip"])

    have_a = {key(h) for h in data["active"]}
    new_active = [h for h in {key(h): h for h in eb_active}.values() if key(h) not in have_a]
    have_s = {(key(h), h["sold_date"]) for h in data["sold"]}
    new_sold = [h for h in eb_sold if (key(h), h["sold_date"]) not in have_s]

    all_sold = data["sold"] + new_sold
    for h in new_active:
        fd.gem_score(h, all_sold, today)

    data["active"].extend(new_active)
    data["sold"] = sorted(all_sold, key=lambda x: x["sold_date"], reverse=True)
    data["regions"] = sorted(set(h["pocket"] for h in data["active"]))
    DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))
    print(f"merged cities: +{len(new_active)} active, +{len(new_sold)} sold "
          f"(now {len(data['active'])} active, {len(data['sold'])} sold total)")


if __name__ == "__main__":
    main()
    # HomeHarvest's ThreadPoolExecutor workers are non-daemon: if a timed-out
    # scrape was abandoned, concurrent.futures' atexit hook joins its hung
    # workers forever (froze the 2026-08-07 run for 17h). Skip interpreter
    # shutdown entirely — everything is already written and printed.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
