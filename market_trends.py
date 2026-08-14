#!/usr/bin/env python3
"""HomeScout market-temperature + seasonality builder.

Runs in .venv (needs homeharvest); reuses fetch_data's classification helpers.
A single 10-year Realtor sold pass over every covered city produces
docs/market.json:

  - monthly sold volume, median $/sqft, sale-to-list %, % over-asking and
    days-on-market per scope (Targets / Redwood City / Menlo Park /
    Peninsula (other) / East Bay), back to 2016.
  - a composite 0-100 "market temperature" (0 = deep buyer's market,
    100 = frenzied seller's market) per month.
  - seasonality: average temperature / sale-to-list / DOM per calendar month
    pooled across all years — which months structurally favor buyers.
  - pocket_ppsf: per-pocket monthly median $/sqft series (10y) that powers
    the dashboard's long-run sold-trends chart.

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
LOOKBACK_DAYS = 3650            # Realtor retains sold data a full 10 years back


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


PENINSULA_OTHER = {"San Carlos", "Belmont", "San Mateo", "Foster City",
                   "Burlingame", "Hillsborough", "Millbrae", "San Bruno",
                   "South San Francisco", "Daly City", "Pacifica",
                   "Half Moon Bay", "Woodside", "Portola Valley",
                   "Palo Alto", "East Palo Alto"}


def scope_of(pocket):
    """Which aggregation buckets a pocket belongs to (a home can be in several)."""
    buckets = []
    if pocket in fd.TARGET_POCKETS:
        buckets.append("Targets")
    if pocket in ("Menlo Park", "West Menlo (county)"):
        buckets.append("Menlo Park")
    elif pocket in PENINSULA_OTHER:
        buckets.append("Peninsula (other)")
    else:
        buckets.append("Redwood City")  # RWC + its county islands
    return buckets


def main():
    hoods = fd.load_neighborhoods()
    limits = fd.load_city_limits()

    # ---- Realtor/HomeHarvest: ONE 10-year sold pass over every covered city.
    # Realtor retains sold price, list price, sqft and days-on-MLS a full decade
    # back, so a single source now feeds ppsf/volume (all years), sale-to-list,
    # DOM, the temperature composite, and the per-pocket $/sqft series.
    ppsf = defaultdict(lambda: defaultdict(list))       # month->scope->[$psf]
    dom = defaultdict(lambda: defaultdict(list))        # month->scope->[days]
    vol = defaultdict(lambda: defaultdict(int))         # month->scope->count
    s2l = defaultdict(lambda: defaultdict(list))        # month->scope->[ratio%]
    over = defaultdict(lambda: defaultdict(list))       # month->scope->[0/1]
    pocket_ppsf = defaultdict(lambda: defaultdict(list))  # month->pocket->[$psf]
    # per-pocket monthly accumulators for the Neighborhood Heat panel
    p_s2l = defaultdict(lambda: defaultdict(list))        # month->pocket->[ratio%]
    p_over = defaultdict(lambda: defaultdict(list))       # month->pocket->[0/1]
    p_dom = defaultdict(lambda: defaultdict(list))        # month->pocket->[days]
    p_price = defaultdict(lambda: defaultdict(list))      # month->pocket->[$]
    p_n = defaultdict(lambda: defaultdict(int))           # month->pocket->count

    cities = ["Redwood City, CA", "Menlo Park, CA", "Atherton, CA"] + \
             sorted(c + ", CA" for c in PENINSULA_OTHER)
    for loc in cities:
        try:
            df = scrape_property(location=loc, listing_type="sold",
                                 property_type=["single_family"],
                                 past_days=LOOKBACK_DAYS)
            print(f"realtor {loc}: {len(df)} sold rows (10y)")
        except Exception as e:
            print(f"realtor {loc} failed: {e}", file=sys.stderr)
            continue
        for _, r in df.iterrows():
            sp = fd.to_int(r.get("sold_price"))
            sold = r.get("last_sold_date")
            if not sp or sold is None or pd.isna(sold):
                continue
            lat, lon = fd.to_float(r.get("latitude")), fd.to_float(r.get("longitude"))
            zc = str(r.get("zip_code") or "")[:5]
            city = str(r.get("city") or "")
            pocket = fd.classify(lon, lat, zc, city, hoods, limits)
            mk = month_key(str(sold)[:10])
            scopes_hit = scope_of(pocket)
            sqft = fd.to_int(r.get("sqft"))
            p_n[mk][pocket] += 1
            p_price[mk][pocket].append(sp)
            if sqft and sqft >= 1500:
                p = round(sp / sqft)
                if 150 <= p <= 4000:
                    pocket_ppsf[mk][pocket].append(p)
                    pocket_ppsf[mk]["All coverage"].append(p)
                    for scope in scopes_hit:
                        ppsf[mk][scope].append(p)
            for scope in scopes_hit:
                vol[mk][scope] += 1
            lp = fd.to_int(r.get("list_price"))
            if lp and lp > 0:
                ratio = sp / lp * 100
                if 70 <= ratio <= 150:      # drop obvious data errors
                    p_s2l[mk][pocket].append(round(ratio, 1))
                    p_over[mk][pocket].append(1 if sp > lp else 0)
                    for scope in scopes_hit:
                        s2l[mk][scope].append(round(ratio, 1))
                        over[mk][scope].append(1 if sp > lp else 0)
            dm = fd.to_int(r.get("days_on_mls"))
            if dm is not None and 0 <= dm <= 400:
                p_dom[mk][pocket].append(dm)
                for scope in scopes_hit:
                    dom[mk][scope].append(dm)
        time.sleep(2)

    # ---- assemble monthly series + seasonality ----
    all_months = sorted(set(ppsf) | set(s2l))
    scopes = ["Targets", "Redwood City", "Menlo Park", "Peninsula (other)"]
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

    # ---- per-pocket monthly $/sqft series (10y) for the sold-trends chart ----
    pocket_series = defaultdict(dict)
    for mk in sorted(pocket_ppsf):
        for pocket, vals in pocket_ppsf[mk].items():
            if len(vals) >= 2 or pocket == "All coverage":
                pocket_series[pocket][mk] = [int(st.median(vals)), len(vals)]
    # keep only pockets with enough history to chart meaningfully
    pocket_series = {p: v for p, v in pocket_series.items() if len(v) >= 12}

    # ---- Neighborhood Heat: trailing-12mo per-pocket snapshot ----
    # temperature + sale-to-list + over-ask + DOM + median price/$psf and a
    # YoY $/sqft change (last 12mo median vs the prior 12mo). Feeds the panel.
    def midx(mk):                      # 'YYYY-MM' -> absolute month index
        return int(mk[:4]) * 12 + int(mk[5:7])

    heat = {}
    if all_months:
        latest = midx(max(all_months))
        last12 = {mk for mk in set(p_n) if latest - 11 <= midx(mk) <= latest}
        prior12 = {mk for mk in set(p_n) if latest - 23 <= midx(mk) <= latest - 12}
        pockets = {pk for mk in last12 for pk in p_n[mk]}
        for pk in pockets:
            n = sum(p_n[mk].get(pk, 0) for mk in last12)
            if n < 3:                  # too thin to characterise
                continue
            s2l_v = [v for mk in last12 for v in p_s2l[mk].get(pk, [])]
            over_v = [v for mk in last12 for v in p_over[mk].get(pk, [])]
            dom_v = [v for mk in last12 for v in p_dom[mk].get(pk, [])]
            price_v = [v for mk in last12 for v in p_price[mk].get(pk, [])]
            ppsf_v = [v for mk in last12 for v in pocket_ppsf[mk].get(pk, [])]
            ppsf_prior = [v for mk in prior12 for v in pocket_ppsf[mk].get(pk, [])]
            m_s2l = med(s2l_v)
            m_over = round(sum(over_v) / len(over_v) * 100) if over_v else None
            m_dom = med(dom_v)
            now_ppsf = med(ppsf_v)
            was_ppsf = med(ppsf_prior)
            yoy = (round((now_ppsf - was_ppsf) / was_ppsf * 100)
                   if now_ppsf and was_ppsf else None)
            heat[pk] = {
                "n": n,
                "median": int(st.median(price_v)) if price_v else None,
                "ppsf": int(now_ppsf) if now_ppsf else None,
                "yoy": yoy,
                "s2l": m_s2l,
                "over": m_over,
                "dom": m_dom,
                "temp": temperature(m_s2l, m_over, m_dom),
            }

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scopes": scopes,
        "monthly": monthly,
        "seasonality": seasonality,
        "current_temp": current,
        "pocket_ppsf": pocket_series,
        "neighborhood_heat": heat,
    }
    (DOCS / "market.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote docs/market.json: {len(all_months)} months, "
          f"{len(pocket_series)} pocket series, "
          f"current temp Targets={current.get('Targets')}")


if __name__ == "__main__":
    main()
