#!/usr/bin/env python3
"""Tag active listings in docs/data.json with fire / flood hazard zones.

Reads docs/hazards.geojson (see hazards_download.py) and stamps each active:
  fire_zone:  "Very High" | "High" | absent
  flood_zone: "AE" | "AH" | "AO" | "A" | "VE" | "X-0.2%" | absent

Runs in update.sh after fetch_cities so the merged active set is tagged.
Stdlib only; point-in-polygon reused from fetch_data.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    reno_tag(data)   # before slim(): scoring reads the full 500-char remarks
    hist = track_history(data)   # first-seen + price cuts across ALL cities (was RWC-only)
    send_reno_alerts(data, hist)
    slim(data)   # drop bytes the browser never reads → faster/more reliable Pages builds
    DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))
    print(f"hazard-tagged {len(data['active'])} actives: "
          f"{n_fire} in fire zones, {n_flood} in flood zones")


# --- renovation candidates -------------------------------------------------
# A reno play buys small/dated stock on good dirt below the pocket's renovated
# $/sqft, so the usual 2000+ sqft / turnkey filters would hide exactly these.
RENO_KEYWORDS = re.compile(
    r"fixer|tlc|as.?is|original condition|opportunit|contractor|remodel|expand"
    r"|potential|estate sale|probate|sweat|bring your|value.?add|land value"
    r"|diamond in the rough|cosmetic|needs work", re.I)
RENO_MAX_PRICE = 2_800_000   # leaves ~$500K+ of reno budget under the $3.3M cap
RENO_ALERT_CITIES = ("Redwood City", "Emerald Hills", "Menlo Park", "Atherton")


def reno_tag(data):
    """Score actives as renovation candidates (0-5); reno=True at score>=4.

    +2 fixer language (opp_flags or remarks), +1 pre-1975, +1 lot>=6000 sqft,
    +1 asking $/sqft <85% of the pocket's top-quartile sold $/sqft (a proxy
    for what renovated product resells at, shipped as reno_bench)."""
    by_pocket = {}
    for s in data["sold"]:
        if s.get("ppsf") and s.get("pocket"):
            by_pocket.setdefault(s["pocket"], []).append(s["ppsf"])
    bench = {}
    for p, pp in by_pocket.items():
        if len(pp) >= 8:
            pp.sort()
            bench[p] = pp[int(len(pp) * 0.75)]
    n = 0
    for h in data["active"]:
        for k in ("reno", "reno_score", "reno_bench"):
            h.pop(k, None)
        if h.get("type") == "lot" or not h.get("price") or h["price"] > RENO_MAX_PRICE:
            continue
        fixerish = bool(h.get("opp_flags")) or bool(RENO_KEYWORDS.search(h.get("remarks") or ""))
        old = (h.get("year") or 9999) < 1975
        biglot = (h.get("lot") or 0) >= 6000
        b = bench.get(h.get("pocket"))
        cheap = bool(h.get("ppsf") and b and h["ppsf"] < b * 0.85)
        score = 2 * fixerish + old + biglot + cheap
        if score >= 3:
            h["reno_score"] = score
            if b:
                h["reno_bench"] = b
            if score >= 4:
                h["reno"] = True
                n += 1
    print(f"reno-tagged {n} candidates (score>=4)")


def send_reno_alerts(data, hist):
    """Discord-alert each reno candidate (score>=4, RWC/Menlo area) once ever.

    Same opt-in config as fetch_data.send_alerts: ~/.homescout_alerts.json
    {"discord_webhook_url": ...}. Alerted keys are remembered in history.json
    (reno_alerted flag) so 4-hourly refreshes don't repeat themselves."""
    cfg_path = Path.home() / ".homescout_alerts.json"
    if not cfg_path.exists():
        return
    try:
        url = json.loads(cfg_path.read_text()).get("discord_webhook_url")
    except Exception:
        return
    if not url:
        return
    pending = []
    for h in data["active"]:
        if not h.get("reno") or h.get("city") not in RENO_ALERT_CITIES:
            continue
        key = h.get("mls") or h.get("address")
        e = hist.get(key)
        if e is None or e.get("reno_alerted"):
            continue
        pending.append((h, e))
    if not pending:
        return
    # Discord post stays digestible at 8; the rest alert on later runs (only
    # posted candidates get their reno_alerted flag set).
    pending.sort(key=lambda x: (-x[0]["reno_score"], -(x[0].get("gem_pct") or 0)))
    lines = []
    for h, e in pending[:8]:
        e["reno_alerted"] = True
        gem = " · gem {:+.0f}%".format(h["gem_pct"]) if h.get("gem_pct") is not None else ""
        bench = " · reno bench ${}/sqft".format(h["reno_bench"]) if h.get("reno_bench") else ""
        lines.append("🔨 **{}** ({}) — ${:,} · {:,} sqft · lot {:,} · y{} · "
                     "reno {}/5{}{}\n{}".format(
                         h["address"], h["pocket"], h["price"], h.get("sqft") or 0,
                         h.get("lot") or 0, h.get("year") or "?",
                         h["reno_score"], gem, bench, h["url"]))
    body = json.dumps({"content": "🏡 **HomeScout reno radar**\n" + "\n\n".join(lines)}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
        HISTORY_FILE.write_text(json.dumps(hist, separators=(",", ":")))
        print("sent {} reno alert(s) to Discord".format(len(lines)))
    except Exception as e:
        print("reno alert send failed: {}".format(e), file=sys.stderr)


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
    return hist


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
