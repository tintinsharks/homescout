#!/usr/bin/env python3
"""HomeScout enrichment pass (runs in .venv — needs homeharvest).

Pulls Realtor.com data via HomeHarvest and merges per-listing enrichment into
docs/data.json written by fetch_data.py: Realtor AVM estimate, last-sold
date/price, tax, HOA, listing remarks + opportunity keyword flags, schools.

Non-fatal by design: update.sh continues publishing even if this step fails.
"""

import json
import re
import sys
from pathlib import Path

import pandas as pd
from homeharvest import scrape_property

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "docs" / "data.json"

OPP_KEYWORDS = [
    "fixer", "tlc", "as-is", "as is", "probate", "estate sale", "trust sale",
    "original condition", "first time on", "opportunity", "contractor",
    "adu", "in-law", "in law", "expand", "sweat equity", "bring your",
    "sold as", "needs work", "cosmetic", "potential",
]


def norm_addr(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    toks = s.split()
    return " ".join(toks[:2]) if len(toks) >= 2 else s


def to_i(v):
    try:
        if pd.isna(v):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def main():
    data = json.loads(DATA_FILE.read_text())

    frames = []
    for loc in ("Redwood City, CA", "Menlo Park, CA", "Atherton, CA"):
        for lt in ("for_sale", "pending"):
            try:
                df = scrape_property(location=loc,
                                     listing_type=lt, property_type=["single_family"])
                frames.append(df)
                print(f"realtor.com {loc} {lt}: {len(df)} rows")
            except Exception as e:
                print(f"scrape {loc} {lt} failed: {e}", file=sys.stderr)
    if not frames:
        sys.exit(1)
    df = pd.concat(frames, ignore_index=True)

    by_mls = {}
    by_addr = {}
    for _, r in df.iterrows():
        mid = str(r.get("mls_id") or "").strip()
        if mid:
            by_mls[mid] = r
        by_addr[norm_addr(r.get("full_street_line"))] = r

    matched = 0
    for h in data["active"]:
        r = by_mls.get(h["mls"])
        if r is None:
            r = by_addr.get(norm_addr(h["address"]))
        if r is None:
            continue
        matched += 1
        est = to_i(r.get("estimated_value"))
        h["est_value"] = est
        if est:
            h["vs_est_pct"] = round((est - h["price"]) / est * 100, 1)
        h["assessed"] = to_i(r.get("assessed_value"))
        h["tax"] = to_i(r.get("tax"))
        h["hoa"] = to_i(r.get("hoa_fee"))
        lsd = r.get("last_sold_date")
        h["last_sold"] = str(lsd)[:10] if lsd is not None and not pd.isna(lsd) else None
        h["last_sold_price"] = to_i(r.get("last_sold_price"))
        text = "" if pd.isna(r.get("text")) else str(r.get("text"))
        h["remarks"] = text[:500]
        low = text.lower()
        h["opp_flags"] = sorted({k.upper().replace(" ", "-") for k in OPP_KEYWORDS if k in low})
        schools = r.get("nearby_schools")
        if isinstance(schools, str):
            h["schools"] = schools[:200]
        elif isinstance(schools, list):
            h["schools"] = ", ".join(map(str, schools[:4]))

    # sale-to-list enrichment for sold comps (Realtor keeps original list price)
    try:
        skey = {}
        n = 0
        for loc in ("Redwood City, CA", "Menlo Park, CA", "Atherton, CA"):
            sdf = scrape_property(location=loc, listing_type="sold",
                                  property_type=["single_family"], past_days=365)
            print(f"realtor.com {loc} sold: {len(sdf)} rows")
            for _, r in sdf.iterrows():
                k = norm_addr(r.get("full_street_line")) + " " + str(r.get("zip_code") or "")[:5]
                skey[k] = r
        for h in data["sold"]:
            r = skey.get(norm_addr(h["address"]) + " " + h["zip"])
            if r is None:
                continue
            lp, sp = to_i(r.get("list_price")), to_i(r.get("sold_price"))
            if lp and sp:
                h["list_price"] = lp
                h["s2l"] = round(sp / lp * 100, 1)
                n += 1
            ld = r.get("list_date")
            if ld is not None and not pd.isna(ld):
                h["list_date"] = str(ld)[:10]
        print(f"sale-to-list attached to {n} sold rows")
    except Exception as e:
        print(f"sold enrich failed: {e}", file=sys.stderr)

    DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))
    n_est = sum(1 for h in data["active"] if h.get("est_value"))
    n_flag = sum(1 for h in data["active"] if h.get("opp_flags"))
    print(f"enriched {matched}/{len(data['active'])} active listings "
          f"({n_est} with AVM estimate, {n_flag} with opportunity flags)")


if __name__ == "__main__":
    main()
