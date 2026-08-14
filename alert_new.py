#!/usr/bin/env python3
"""HomeScout new-listing email alerts.

Diffs the freshly written docs/data.json against last run's complete snapshot
(docs/data.prev.json, saved by fetch_data before it overwrites) and emails a
digest of listings that appeared THIS run. Sends via Gmail API using Aria's
existing OAuth token (scope gmail.send) — no new credentials, cron-safe.

Recipients + gates live in alert_config.json. A key ledger (alerted.json)
prevents re-sending the same listing on later cycles. First run with no prev
snapshot seeds the ledger silently and sends nothing.

Run with Aria's venv python (it carries google-api-python-client):
  "/Users/nprabhak/Claude Bot/personal-assistant/.venv/bin/python" alert_new.py
Non-fatal in update.sh: any failure here must never block the data refresh.
"""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS = HERE / "docs"
DATA = DOCS / "data.json"
PREV = DOCS / "data.prev.json"
LEDGER = HERE / "alerted.json"
CONFIG = HERE / "alert_config.json"
TOKEN = Path("/Users/nprabhak/Claude Bot/personal-assistant/data/.tokens/google_token.json")
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
DASH = "https://tintinsharks.github.io/homescout/"


def listing_key(h):
    return h.get("mls") or h.get("url") or (h.get("address", "") + "|" + h.get("zip", ""))


def load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def is_live(h):
    st = (h.get("status") or "").lower()
    return not any(w in st for w in ("pending", "contingent", "sold", "closed"))


def passes_gate(h, cfg):
    cities = cfg.get("cities")
    if cities:                               # hard city allowlist
        allow = {c.strip().lower() for c in cities}
        if (h.get("city") or "").strip().lower() not in allow:
            return False
    if h.get("target"):                      # target pockets bypass price/sqft gates
        return True
    if cfg.get("targets_only"):
        return False
    if h.get("type") == "lot" and not cfg.get("include_lots", True):
        return False
    price = h.get("price") or 0
    if cfg.get("max_price") and price > cfg["max_price"]:
        return False
    sqft = h.get("sqft")
    if h.get("type") != "lot" and cfg.get("min_sqft") and sqft and sqft < cfg["min_sqft"]:
        return False
    return True


def fmt_price(n):
    if not n:
        return "—"
    return f"${n/1e6:.2f}M" if n >= 1_000_000 else f"${n/1e3:.0f}K"


def deal_score(h):
    g, a = h.get("gem_pct"), h.get("vs_est_pct")
    vals = [v for v in (g, a) if v is not None]
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return round(min(g, a) * 0.6 + max(g, a) * 0.4)


def card_html(h, note=""):
    ds = deal_score(h)
    specs = " · ".join(filter(None, [
        f"{int(h['beds'])} bd" if h.get("beds") else None,
        f"{h['baths']:g} ba" if h.get("baths") else None,
        f"{h['sqft']:,} sqft" if h.get("sqft") else None,
        f"{h['lot']:,} sqft lot" if h.get("type") == "lot" and h.get("lot") else None,
        f"built {h['year']}" if h.get("year") else None,
    ]))
    ppsf = f"${h['ppsf']:,}/sqft" if h.get("ppsf") else ""
    badges = []
    if h.get("target"):
        badges.append('<span style="background:#faf0dd;color:#b5761f;font-weight:700;'
                      'padding:1px 7px;border-radius:8px;font-size:11px">🎯 target pocket</span>')
    if ds is not None and ds >= 8:
        badges.append(f'<span style="background:#e8f4ea;color:#2f7d43;font-weight:700;'
                      f'padding:1px 7px;border-radius:8px;font-size:11px">💎 deal score {ds:+d}</span>')
    for f in (h.get("opp_flags") or [])[:2]:
        badges.append(f'<span style="background:#f0ece3;color:#6b5e4e;padding:1px 7px;'
                      f'border-radius:8px;font-size:11px">{f.replace("-", " ").title()}</span>')
    photo = h.get("photo") or ""
    img = (f'<img src="{photo}" width="150" height="105" '
           f'style="object-fit:cover;border-radius:8px;display:block" alt="">' if photo else "")
    note_html = (f'<div style="font-size:13px;font-weight:700;color:#2f7d43;margin-top:3px">{note}</div>'
                 if note else "")
    return f"""
    <tr>
      <td style="padding:12px 0;border-bottom:1px solid #eee;vertical-align:top;width:160px">{img}</td>
      <td style="padding:12px 0 12px 14px;border-bottom:1px solid #eee;vertical-align:top">
        <div style="font-size:18px;font-weight:700;color:#c0392b;font-family:Georgia,serif">
          {fmt_price(h.get('price'))} <span style="color:#7a6a56;font-size:13px;font-weight:400">{ppsf}</span></div>
        <div style="font-size:15px;color:#2a2018;margin:2px 0">
          <a href="{h.get('url', DASH)}" style="color:#2a2018;text-decoration:none">{h.get('address', '')}</a></div>
        <div style="font-size:13px;color:#6b5e4e">{h.get('city', '')} · {h.get('pocket', '')}</div>
        <div style="font-size:13px;color:#6b5e4e;margin-top:2px">{specs}</div>
        {note_html}
        <div style="margin-top:6px">{' '.join(badges)}</div>
      </td>
    </tr>"""


def _section(title, items):
    """items: list of (home, note_str). Returns an HTML block or ''."""
    if not items:
        return ""
    rows = "".join(card_html(h, note) for h, note in items)
    return (f'<div style="font-size:17px;font-weight:700;font-family:Georgia,serif;'
            f'margin:18px 0 4px">{title}</div>'
            f'<table style="width:100%;border-collapse:collapse">{rows}</table>')


def build_digest(new, cuts, stale, cfg):
    """new: [home]; cuts/stale: [(home, note)]. One combined email."""
    new.sort(key=lambda h: (not h.get("target"), -((deal_score(h) or -99)), h.get("price") or 0))
    parts = []
    if new:
        parts.append(f"{len(new)} new")
    if cuts:
        parts.append(f"{len(cuts)} price cut{'s' if len(cuts) != 1 else ''}")
    if stale:
        parts.append(f"{len(stale)} sitting")
    sub = "🏡 HomeScout: " + ", ".join(parts)
    body = (_section(f"🆕 New listings ({len(new)})", [(h, "") for h in new])
            + _section(f"📉 Price cuts ({len(cuts)})", cuts)
            + _section(f"⏳ Been sitting ({len(stale)})", stale))
    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:0 auto;color:#2a2018">
      <div style="font-size:22px;font-weight:700;font-family:Georgia,serif">🏡 HomeScout update</div>
      <div style="font-size:13px;color:#6b5e4e;margin:4px 0 6px">
        {datetime.now().strftime('%A %b %-d, %-I:%M %p')} ·
        <a href="{DASH}" style="color:#c0392b">open the dashboard →</a></div>
      {body}
      <div style="font-size:11px;color:#a89b86;margin-top:16px;line-height:1.5">
        Redwood City / Menlo Park / Atherton · ≤ {fmt_price(cfg.get('max_price'))},
        {cfg.get('min_sqft', 0):,}+ sqft (target pockets always included).
        Price cuts ≥ {cfg.get('min_cut_pct', 1)}%; "sitting" = {cfg.get('stale_days', 45)}+ days listed.</div>
    </div>"""
    return sub, html


def build_sms_digest(new, cuts, stale):
    """Compact ASCII summary for SMS gateways."""
    head = []
    if new:
        head.append(f"{len(new)} new")
    if cuts:
        head.append(f"{len(cuts)} cut{'s' if len(cuts) != 1 else ''}")
    if stale:
        head.append(f"{len(stale)} sitting")
    lines = ["HomeScout: " + ", ".join(head)]
    for h in new[:2]:
        tag = "*" if h.get("target") else "-"
        lines.append(f"{tag} NEW {fmt_price(h.get('price'))} {h.get('address', '')}")
    for h, note in cuts[:2]:
        lines.append(f"v CUT {fmt_price(h.get('price'))} {h.get('address', '')}")
    for h, note in stale[:1]:
        lines.append(f". SIT {fmt_price(h.get('price'))} {h.get('address', '')}")
    lines.append(DASH)          # full https:// URL so SMS auto-linkers keep the /homescout path
    return "\n".join(lines)


def send_sms_email(text, addresses):
    """Send a PLAIN-TEXT email to carrier email-to-SMS gateways (@vtext.com,
    @tmomail.net, ...). Reuses the same Gmail credentials as send()."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from email.mime.text import MIMEText
    import base64

    creds = Credentials.from_authorized_user_file(str(TOKEN), [GMAIL_SEND])
    if not creds.valid:
        creds.refresh(Request())
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sent = []
    for addr in addresses:
        try:
            msg = MIMEText(text, "plain")
            msg["To"] = addr
            msg["Subject"] = ""          # gateways put the body in the SMS, not the subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            sent.append(addr)
        except Exception as e:
            print(f"sms gateway {addr} failed: {e}", file=sys.stderr)
    return sent


def send_signal(text, numbers):
    """Text via signal-cli (already linked). Each number is a separate send so
    one bad number doesn't drop the rest."""
    import subprocess
    sig = "/opt/homebrew/bin/signal-cli"
    sent = []
    for num in numbers:
        try:
            subprocess.run([sig, "send", "-m", text, num], check=True,
                           capture_output=True, timeout=60)
            sent.append(num)
        except Exception as e:
            print(f"signal send to {num} failed: {e}", file=sys.stderr)
    return sent


def signal_note(text):
    """Signal note-to-self — independent of the Gmail token, so it still works
    when the token has lapsed. Used as a dead-man's alarm."""
    import subprocess
    try:
        subprocess.run(["/opt/homebrew/bin/signal-cli", "send", "--note-to-self", "-m", text],
                       check=True, capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"signal note failed: {e}", file=sys.stderr)
        return False


def gmail_ok():
    """True if the Gmail token is usable (valid or refreshable)."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        c = Credentials.from_authorized_user_file(str(TOKEN), [GMAIL_SEND])
        if not c.valid:
            c.refresh(Request())
        return bool(c.valid)
    except Exception as e:
        print(f"gmail token check failed: {e}", file=sys.stderr)
        return False


def send(sub, html, recipients):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from email.mime.text import MIMEText
    import base64

    creds = Credentials.from_authorized_user_file(str(TOKEN), [GMAIL_SEND])
    if not creds.valid:
        creds.refresh(Request())
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    msg = MIMEText(html, "html")
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = sub
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def main():
    cfg = load(CONFIG, {})
    recipients = [r for r in cfg.get("recipients", []) if r and "@" in r]
    data = load(DATA, None)
    if not data:
        print("no data.json; skip", file=sys.stderr)
        return
    active = [h for h in data.get("active", []) if is_live(h)]
    cur_keys = {listing_key(h) for h in active}

    ledger = load(LEDGER, {})
    prev = load(PREV, None)
    if prev is None:
        # first run: seed the ledger with everything so we never blast the backlog
        today = date.today().isoformat()
        LEDGER.write_text(json.dumps({k: today for k in cur_keys}))
        print(f"seeded ledger with {len(cur_keys)} listings; no email (first run)")
        return

    prev_keys = {listing_key(h) for h in prev.get("active", [])}
    prev_by_key = {listing_key(h): h for h in prev.get("active", [])}
    seen = set(ledger) | prev_keys
    new = [h for h in active if listing_key(h) not in seen and passes_gate(h, cfg)]

    # record every current key so nothing re-alerts, prune >60d old entries
    today = date.today().isoformat()
    cutoff = (datetime.now() - __import__("datetime").timedelta(days=60)).date().isoformat()
    for k in cur_keys:
        ledger.setdefault(k, today)
    ledger = {k: d for k, d in ledger.items() if d >= cutoff or k in cur_keys}
    LEDGER.write_text(json.dumps(ledger))

    # --- price cuts & newly-stale, diffed against last run's snapshot ---
    # Both fire once (prev==current on the next run) and only for listings that
    # existed last run, so new listings never double-count and there's no backlog blast.
    min_cut_pct = cfg.get("min_cut_pct", 1.0)
    min_cut_abs = cfg.get("min_cut_abs", 20000)
    stale_days = cfg.get("stale_days", 45)
    cuts, stale = [], []
    for h in active:
        k = listing_key(h)
        p = prev_by_key.get(k)
        if not p or not passes_gate(h, cfg):
            continue
        op, np = p.get("price"), h.get("price")
        if op and np and op > np:
            drop, pct = op - np, (op - np) / op * 100
            if pct >= min_cut_pct and drop >= min_cut_abs:
                cuts.append((h, f"📉 Cut {fmt_price(drop)} ({pct:.0f}%) — was {fmt_price(op)}"))
                continue                                   # a cut this run isn't also "newly stale"
        pd, cd = p.get("dom"), h.get("dom")
        if cd and 0 < cd <= 365 and pd is not None and pd < stale_days <= cd:
            stale.append((h, f"⏳ Now {cd} days on market — no longer fresh"))

    if not (new or cuts or stale):
        print("nothing to alert this run")
        return

    # dead-man's alarm: if the Gmail token has lapsed, both email and SMS-gateway
    # are down — warn via Signal (token-independent) instead of failing silently.
    uses_gmail = bool(recipients) or bool([a for a in cfg.get("sms_recipients", []) if a])
    if uses_gmail and not gmail_ok():
        signal_note("⚠️ HomeScout: Gmail token expired — email + text alerts are DOWN "
                    f"({len(new)} new / {len(cuts)} cuts / {len(stale)} sitting not sent). "
                    "Re-authorize and Publish the OAuth app.")
        print("gmail token unusable; sent Signal warning, skipping email/SMS", file=sys.stderr)
        recipients = []
        cfg = {**cfg, "sms_recipients": []}

    summary = f"{len(new)} new / {len(cuts)} cuts / {len(stale)} sitting"
    if recipients:
        sub, html = build_digest(new, cuts, stale, cfg)
        try:
            send(sub, html, recipients)
            print(f"emailed [{summary}] to {', '.join(recipients)}")
        except Exception as e:
            print(f"email send failed: {e}", file=sys.stderr)

    sms_text = build_sms_digest(new, cuts, stale)
    sig_nums = [n for n in cfg.get("signal_recipients", []) if n]
    if sig_nums:
        send_signal(sms_text, sig_nums)
    sms_addrs = [a for a in cfg.get("sms_recipients", []) if a and "@" in a]
    if sms_addrs:
        try:
            sent = send_sms_email(sms_text, sms_addrs)
            print(f"SMS [{summary}] to {', '.join(sent) or '(none)'}")
        except Exception as e:
            print(f"sms send failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
