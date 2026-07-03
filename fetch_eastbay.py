#!/usr/bin/env python3
"""HomeScout East Bay module (runs in .venv — needs homeharvest).

The Peninsula pipeline (fetch_data.py) uses Redfin, but Fremont/Union City are
higher-volume Alameda County markets where Redfin's region IDs are unavailable
and its 350-row cap truncates. So East Bay comes entirely from Realtor.com via
HomeHarvest — queried per city (no neighbor leakage), no row cap — and merged
into the existing docs/data.json produced by fetch_data + enrich.

Adds Fremont and Union City active + pending listings (with AVM / remarks /
schools enrichment) and 24 months of sold comps (with sale-to-list), then
gem-scores each East Bay active against East Bay comps.
"""

import json
import sys
from datetime import datetime, timezone

import pandas as pd
from homeharvest import scrape_property

import fetch_data as fd
from enrich_data import OPP_KEYWORDS

DATA_FILE = fd.DOCS / "data.json"
CITIES = [("Fremont, CA", "Fremont"), ("Union City, CA", "Union City")]
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


def row_from(r, pocket):
    price = i(r.get("list_price")) or i(r.get("sold_price"))
    sqft = i(r.get("sqft"))
    if not price or not sqft:
        return None
    lat, lon = f(r.get("latitude")), f(r.get("longitude"))
    fb, hb = i(r.get("full_baths")) or 0, i(r.get("half_baths")) or 0
    return {
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
        "ppsf": round(price / sqft),
        "status": s(r.get("status")).strip().title(),
        "sold_date": None,
        "open_house": "",
        "url": s(r.get("property_url")),
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

    eb_active, eb_sold = [], []
    for loc, pocket in CITIES:
        for lt in ("for_sale", "pending"):
            try:
                df = scrape_property(location=loc, listing_type=lt,
                                     property_type=["single_family"])
            except Exception as e:
                print(f"{loc} {lt} failed: {e}", file=sys.stderr)
                continue
            for _, r in df.iterrows():
                h = row_from(r, pocket)
                if not h or h["sqft"] < fd.MIN_SQFT:
                    continue
                enrich_active(h, r)
                eb_active.append(h)
            print(f"{loc} {lt}: {len(df)} rows")
        try:
            df = scrape_property(location=loc, listing_type="sold",
                                 property_type=["single_family"], past_days=SOLD_DAYS)
        except Exception as e:
            print(f"{loc} sold failed: {e}", file=sys.stderr)
            continue
        for _, r in df.iterrows():
            h = row_from(r, pocket)
            if not h or h["sqft"] < fd.MIN_SQFT:      # keep comps relevant + file small
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

    # de-dupe actives by mls/address, gem-score against East Bay comps
    seen = {}
    for h in eb_active:
        seen[h["mls"] or (h["address"], h["zip"])] = h
    eb_active = list(seen.values())
    for h in eb_active:
        fd.gem_score(h, eb_sold, today)

    data["active"].extend(eb_active)
    data["sold"].extend(eb_sold)
    data["sold"].sort(key=lambda x: x["sold_date"], reverse=True)
    data["regions"] = sorted(set(h["pocket"] for h in data["active"]))
    DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))
    print(f"merged East Bay: +{len(eb_active)} active, +{len(eb_sold)} sold "
          f"(now {len(data['active'])} active, {len(data['sold'])} sold total)")


if __name__ == "__main__":
    main()
