"""
check_scoreplay.py
Checks ScorePlay API twice daily for new media with tag 704382 (player headshots).
Sends an email if new media has been uploaded since the last run.

Optimisation: since API returns newest first, we stop fetching pages as soon
as we hit a CreatedAt older than last_run. On first run we just save baseline.

API: POST https://media.scoreplay.io/v1/media/search?api_key=KEY
Body: {"media_type": "photo", "tag_options": [704382], "page": N, "limit": 50}
Response: {"media": [{ID, CreatedAt, name, thumbnail_url, files:[{details, url}]}]}
"""

import os
import json
import smtplib
import requests
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SP_API_KEY     = os.environ["SP_API_KEY_SCOREPLAY"]
EMAIL_FROM     = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_TO       = os.environ["EMAIL_TO_SCOREPLAY"]

HEADSHOT_TAG_ID = 704382
API_BASE        = "https://media.scoreplay.io/v1"
STATE_FILE      = Path("scoreplay_state.json")
PAGE_SIZE       = 50


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── API ───────────────────────────────────────────────────────────────────────
def parse_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def fetch_new_media(since: datetime | None) -> list[dict]:
    """
    Fetch pages newest-first and stop as soon as CreatedAt < since.
    On first run (since=None), fetch only page 1 to establish baseline IDs.
    Returns list of new media items.
    """
    new_items = []
    page      = 1

    while True:
        print(f"  Fetching page {page} ...")
        url  = f"{API_BASE}/media/search?api_key={SP_API_KEY}"
        body = {
            "media_type":  "photo",
            "tag_options": [HEADSHOT_TAG_ID],
            "page":        page,
            "limit":       PAGE_SIZE,
        }
        r = requests.post(
            url, json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30
        )
        r.raise_for_status()
        items = r.json().get("media", [])

        if not items:
            break

        for item in items:
            created_str = item.get("CreatedAt", "")
            try:
                created = parse_dt(created_str)
            except Exception:
                created = None

            # First run: just collect IDs from page 1 as baseline, don't add to new_items
            if since is None:
                new_items.append(item)
                continue

            # Stop as soon as item is older than last run
            if created and created <= since:
                print(f"    Reached item older than last run ({created_str}) — stopping.")
                return new_items

            new_items.append(item)

        # If fewer items than page size, we've reached the end
        if len(items) < PAGE_SIZE:
            break

        # On first run only fetch page 1 for baseline
        if since is None:
            break

        page += 1

    return new_items


# ── Email ─────────────────────────────────────────────────────────────────────
def get_original_url(item: dict) -> str:
    for f in (item.get("files") or []):
        if f.get("details") == "photographer_quality":
            return f.get("url", "")
    return item.get("original_url", "")


def build_rows(items: list[dict]) -> str:
    rows = ""
    for item in items:
        name     = item.get("name", "—")
        created  = item.get("CreatedAt", "")
        try:
            dt       = parse_dt(created)
            date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            date_str = created

        thumb    = item.get("thumbnail_url", "")
        orig_url = get_original_url(item)

        thumb_html = (
            f'<img src="{thumb}" width="60" height="60" style="width:60px;height:60px;object-fit:cover;border-radius:4px;display:block;" />'
            if thumb else '<span style="color:#9ca3af;font-size:11px;">—</span>'
        )
        dl_html = (
            f'<a href="{orig_url}" style="color:#1e3a5f;font-weight:600;font-size:12px;">Download</a>'
            if orig_url else "—"
        )

        rows += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:10px 12px;">{thumb_html}</td>
          <td style="padding:10px 12px;font-size:13px;font-weight:600;word-break:break-all;">{name}</td>
          <td style="padding:10px 12px;font-size:13px;color:#9ca3af;white-space:nowrap;">{date_str}</td>
          <td style="padding:10px 12px;">{dl_html}</td>
        </tr>"""
    return rows


def send_email(new_items: list[dict]):
    run_time  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows_html = build_rows(new_items)

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
  <div style="max-width:800px;margin:32px auto;background:#fff;border-radius:8px;
              overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">
    <div style="background:#1e3a5f;padding:20px 28px;">
      <p style="margin:0;color:#93c5fd;font-size:12px;text-transform:uppercase;letter-spacing:1px;">
        ScorePlay Monitor</p>
      <h1 style="margin:4px 0 0;color:#fff;font-size:20px;">New Player Headshots Uploaded</h1>
    </div>
    <div style="background:#f8fafc;border-bottom:1px solid #e5e7eb;padding:12px 28px;
                font-size:13px;color:#374151;">
      <strong>{len(new_items)}</strong> new headshot(s) detected &mdash;
      <span style="color:#9ca3af">{run_time}</span>
    </div>
    <div style="padding:20px 28px;">
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f1f5f9;">
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;width:70px;">Preview</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">File name</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Uploaded</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Original</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <div style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:12px 28px;
                font-size:12px;color:#9ca3af;">
      Automated notification — ScorePlay headshot monitor.
    </div>
  </div>
</body>
</html>"""

    subject = f"ScorePlay – {len(new_items)} new headshot(s) uploaded"

    msg = MIMEMultipart("alternative")
    msg["From"]    = f"ScorePlay Monitor <{EMAIL_FROM}>"
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_FROM, recipients, msg.as_string())


    print(f"Email sent — {len(new_items)} item(s) to {len(recipients)} recipient(s).")



# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*55}")
    print(f"ScorePlay headshot monitor — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Tag ID: {HEADSHOT_TAG_ID}")
    print(f"{'='*55}\n")

    state     = load_state()
    last_run  = state.get("last_run")
    first_run = last_run is None

    if first_run:
        print("First run — will fetch page 1 as baseline only.")
        since = None
    else:
        since = parse_dt(last_run)
        print(f"Last run: {last_run} — fetching only items newer than this.")

    items = fetch_new_media(since)
    print(f"\n{len(items)} item(s) {'found as baseline' if first_run else 'new since last run'}.")

    if items and not first_run:
         send_email(items)
    elif first_run:
        print(f"Baseline established with {len(items)} item(s) — no email sent.")
    else:
        print("No new media — no email sent.")

    state["last_run"] = now.isoformat()
    save_state(state)
    print(f"State saved. Next run will check for items newer than {now.isoformat()}")


if __name__ == "__main__":
    main()
