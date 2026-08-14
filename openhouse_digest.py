#!/usr/bin/env python3
"""HomeScout weekend open-house digest.

Runs Saturday morning (its own launchd agent). Reads docs/data.json, finds every
active listing in the target cities with an open house between now and the end of
the coming weekend, and emails + texts a grouped, time-sorted digest. Reuses
alert_new's send helpers, gates, and Gmail token.

Run with Aria's venv python (carries the Gmail API libs):
  "/Users/nprabhak/Claude Bot/personal-assistant/.venv/bin/python" openhouse_digest.py
"""

import sys
from datetime import date, datetime, timedelta

import alert_new as A

OH_FMT = "%B-%d-%Y %I:%M %p"          # e.g. "August-15-2026 02:00 PM"


def parse_oh(s):
    try:
        return datetime.strptime(s.strip(), OH_FMT)
    except Exception:
        return None


def collect(cfg):
    data = A.load(A.DATA, None)
    if not data:
        return []
    now = datetime.now()
    end = datetime.combine(date.today() + timedelta(days=(6 - date.today().weekday()) % 7),
                           datetime.max.time())          # end of the coming Sunday
    out = []
    for h in data.get("active", []):
        if not A.is_live(h) or not h.get("open_house") or not A.passes_gate(h, cfg):
            continue
        dt = parse_oh(h["open_house"])
        if dt and now <= dt <= end:
            out.append((dt, h))
    out.sort(key=lambda x: x[0])
    return out


def build_email(items, cfg):
    by_day = {}
    for dt, h in items:
        by_day.setdefault(dt.strftime("%A %b %-d"), []).append((dt, h))
    body = ""
    for day, rows in by_day.items():
        cards = "".join(A.card_html(h, f"🚪 Open house {dt.strftime('%-I:%M %p')}") for dt, h in rows)
        body += (f'<div style="font-size:17px;font-weight:700;font-family:Georgia,serif;margin:18px 0 4px">'
                 f'{day} ({len(rows)})</div>'
                 f'<table style="width:100%;border-collapse:collapse">{cards}</table>')
    sub = f"🏡 HomeScout: {len(items)} open house{'s' if len(items) != 1 else ''} this weekend"
    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:0 auto;color:#2a2018">
      <div style="font-size:22px;font-weight:700;font-family:Georgia,serif">🏡 Open houses this weekend</div>
      <div style="font-size:13px;color:#6b5e4e;margin:4px 0 6px">
        Redwood City / Menlo Park / Atherton · <a href="{A.DASH}" style="color:#c0392b">open the dashboard →</a></div>
      {body}
    </div>"""
    return sub, html


def build_sms(items):
    lines = [f"HomeScout: {len(items)} open house{'s' if len(items) != 1 else ''} this weekend"]
    for dt, h in items[:4]:
        tag = "*" if h.get("target") else "-"
        lines.append(f"{tag} {dt.strftime('%a %-I:%M%p')} {A.fmt_price(h.get('price'))} {h.get('address', '')}")
    if len(items) > 4:
        lines.append(f"+{len(items) - 4} more")
    lines.append(A.DASH)
    return "\n".join(lines)


def main():
    cfg = A.load(A.CONFIG, {})
    items = collect(cfg)
    if not items:
        print("no open houses in the target areas this weekend")
        return
    recipients = [r for r in cfg.get("recipients", []) if r and "@" in r]
    sms_addrs = [a for a in cfg.get("sms_recipients", []) if a and "@" in a]

    if (recipients or sms_addrs) and not A.gmail_ok():
        A.signal_note(f"⚠️ HomeScout: Gmail token expired — weekend open-house digest not sent "
                      f"({len(items)} open houses). Re-authorize and Publish the OAuth app.")
        print("gmail token unusable; sent Signal warning", file=sys.stderr)
        recipients, sms_addrs = [], []

    if recipients:
        sub, html = build_email(items, cfg)
        try:
            A.send(sub, html, recipients)
            print(f"emailed {len(items)} open houses to {', '.join(recipients)}")
        except Exception as e:
            print(f"email send failed: {e}", file=sys.stderr)

    sms_text = build_sms(items)
    sig = [n for n in cfg.get("signal_recipients", []) if n]
    if sig:
        A.send_signal(sms_text, sig)
    if sms_addrs:
        try:
            sent = A.send_sms_email(sms_text, sms_addrs)
            print(f"SMS {len(items)} open houses to {', '.join(sent) or '(none)'}")
        except Exception as e:
            print(f"sms send failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
