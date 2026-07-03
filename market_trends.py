#!/usr/bin/env python3
"""HomeScout market-temperature + seasonality builder.

Runs in .venv (needs homeharvest) but reuses fetch_data's stdlib helpers for
the Redfin pull + neighborhood classification. Produces docs/market.json:

  - 5 years of monthly sold volume, median $/sqft, median days-on-market
    (Redfin) for three scopes: Targets, Redwood City, Menlo Park.
  - 3 years of monthly sale-to-list % and % over-asking (Realtor via
    HomeHarvest) for the same scopes.
  - A composite 0-100 "market temperature" (0 = deep buyer's market,
    100 = frenzied seller's market) per month.
  - Seasonality: the average temperature / sale-to-list / DOM for each
    calendar month, pooled across all years — i.e. which months of the
    year structurally favor buyers vs sellers.

Non-fatal in update.sh: a failure here never blocks the listing dashboard.
"""

import json
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
from homeharvest import scrape_property

import fetch_data as fd

DOCS = fd.DOCS
LOOKBACK_DAYS = 1825            # 5 years for Redfin volume/ppsf/DOM
S2L_DAYS = 1095                 # 3 years for Realtor sale-to-list
# fine price bands so each Redfin response stays under the 350-row cap over 5y
BANDS = ["max_price=1100000",
         "min_price=1100001&max_price=1400000",
         "min_price=1400001&max_price=1650000",
         "min_price=1650001&max_price=1900000",
         "min_price=1900001&max_price=2150000",
         "min_price=2150001&max_price=2400000",
         "min_price=2400001&max_price=2700000",
         "min_price=2700001&max_price=3100000",
         "min_price=3100001&max_price=3700000",
         "min_price=3700001&max_price=4600000",
         "min_price=4600001&max_price=6500000",
         "min_price=6500001"]
REGIONS = [("Redwood City", "region_id=15525&region_type=6"),
           ("Menlo Park", "region_id=11961&region_type=6")]


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 1) if xs else None


def month_key(iso):
    return iso[:7]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def temperature(s2l, pct_over, dom):
    """0-100 composite. Higher = hotter seller's market."""
    parts = []
    if s2l is not None:
        parts.append(clamp((s2l - 98) / (108 - 98) * 100, 0, 100))
    if pct_over is not None:
        parts.append(clamp(pct_over, 0, 100))
    if dom is not None:
        parts.append(clamp((45 - dom) / (45 - 7) * 100, 0, 100))
    return round(sum(parts) / len(parts)) if parts else None


def scope_of(pocket):
    """Which aggregation buckets a pocket belongs to (a home can be in several)."""
    buckets = []
    if pocket in fd.TARGET_POCKETS:
        buckets.append("Targets")
    if pocket in ("Menlo Park", "West Menlo (county)"):
        buckets.append("Menlo Park")
    else:
        buckets.append("Redwood City")  # RWC + its county islands
    return buckets


def main():
    hoods = fd.load_neighborhoods()
    limits = fd.load_city_limits()

    # ---- Redfin: 5y sold, per-home, classified into pockets ----
    # month -> scope -> lists
    ppsf = defaultdict(lambda: defaultdict(list))
    dom = defaultdict(lambda: defaultdict(list))
    vol = defaultdict(lambda: defaultdict(int))
    for name, region in REGIONS:
        print(f"[{datetime.now(timezone.utc).isoformat()}] {name}: 5y sold pull...")
        for band in BANDS:
            rows = fd.fetch_csv(region, f"sold_within_days={LOOKBACK_DAYS}&{band}")
            if len(rows) >= 350:
                print(f"  WARNING: {name} {band} hit 350-row cap", file=sys.stderr)
            for r in rows:
                city = (r.get("CITY") or "").strip()
                if city.upper() in fd.EXCLUDE_CITIES:
                    continue
                sd = fd.parse_sold_date(r.get("SOLD DATE"))
                price = fd.to_int(r.get("PRICE"))
                sqft = fd.to_int(r.get("SQUARE FEET"))
                if not sd or not price or not sqft:
                    continue
                lat, lon = fd.to_float(r.get("LATITUDE")), fd.to_float(r.get("LONGITUDE"))
                zc = (r.get("ZIP OR POSTAL CODE") or "").strip()[:5]
                pocket = fd.classify(lon, lat, zc, city, hoods, limits)
                mk = month_key(sd)
                d = fd.to_int(r.get("DAYS ON MARKET"))
                for scope in scope_of(pocket):
                    ppsf[mk][scope].append(round(price / sqft))
                    if d is not None:
                        dom[mk][scope].append(d)
                    vol[mk][scope] += 1
            time.sleep(2)

    # ---- Realtor/HomeHarvest: 3y sale-to-list ----
    s2l = defaultdict(lambda: defaultdict(list))       # month->scope->[ratio%]
    over = defaultdict(lambda: defaultdict(list))       # month->scope->[0/1]
    for loc in ("Redwood City, CA", "Menlo Park, CA"):
        try:
            df = scrape_property(location=loc, listing_type="sold",
                                 property_type=["single_family"], past_days=S2L_DAYS)
            print(f"realtor {loc}: {len(df)} sold rows")
        except Exception as e:
            print(f"realtor {loc} failed: {e}", file=sys.stderr)
            continue
        for _, r in df.iterrows():
            lp, sp = fd.to_int(r.get("list_price")), fd.to_int(r.get("sold_price"))
            sold = r.get("last_sold_date")
            if not lp or not sp or lp <= 0 or sold is None or pd.isna(sold):
                continue
            lat, lon = fd.to_float(r.get("latitude")), fd.to_float(r.get("longitude"))
            zc = str(r.get("zip_code") or "")[:5]
            city = str(r.get("city") or "")
            pocket = fd.classify(lon, lat, zc, city, hoods, limits)
            mk = month_key(str(sold)[:10])
            ratio = sp / lp * 100
            if ratio < 70 or ratio > 150:   # drop obvious data errors
                continue
            for scope in scope_of(pocket):
                s2l[mk][scope].append(round(ratio, 1))
                over[mk][scope].append(1 if sp > lp else 0)

    # ---- assemble monthly series + seasonality ----
    all_months = sorted(set(ppsf) | set(s2l))
    scopes = ["Targets", "Redwood City", "Menlo Park"]
    monthly = {s: [] for s in scopes}
    seas_acc = {s: defaultdict(lambda: defaultdict(list)) for s in scopes}  # scope->cal_month->metric->[vals]

    for mk in all_months:
        for s in scopes:
            m_s2l = med(s2l[mk][s])
            m_over = (round(sum(over[mk][s]) / len(over[mk][s]) * 100)
                      if over[mk][s] else None)
            m_dom = med(dom[mk][s])
            m_ppsf = med(ppsf[mk][s])
            n = vol[mk][s]
            temp = temperature(m_s2l, m_over, m_dom)
            rec = {"month": mk, "n": n, "ppsf": m_ppsf, "dom": m_dom,
                   "s2l": m_s2l, "pct_over": m_over, "temp": temp}
            monthly[s].append(rec)
            cal = int(mk[5:7])
            for metric, val in (("temp", temp), ("s2l", m_s2l), ("dom", m_dom)):
                if val is not None:
                    seas_acc[s][cal][metric].append(val)

    seasonality = {}
    for s in scopes:
        seasonality[s] = {m: {metric: med(seas_acc[s][m][metric])
                              for metric in ("temp", "s2l", "dom")}
                          for m in range(1, 13)}

    # current temperature = median of the last 3 months that have a temp
    current = {}
    for s in scopes:
        recent = [r["temp"] for r in monthly[s][-4:] if r["temp"] is not None]
        current[s] = round(st.mean(recent)) if recent else None

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scopes": scopes,
        "monthly": monthly,
        "seasonality": seasonality,
        "current_temp": current,
    }
    (DOCS / "market.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote docs/market.json: {len(all_months)} months, "
          f"current temp Targets={current.get('Targets')}")


if __name__ == "__main__":
    main()
