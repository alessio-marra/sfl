  """
check_scoreplay.py
Checks ScorePlay API twice daily for new media with tag 704382 (player headshots).
Sends an email if new media has been uploaded since the last run.

State file: scoreplay_state.json — stores last seen media IDs per sub-collection.
Auth: api_key query parameter on every request.

API pattern (from ScorePlay docs):
  POST https://api.scoreplay.io/media/search?api_key=KEY
  Body: {"tags": {"include": {"and": [704382]}}, "page": 1, "size": 100, "sort": {"field": "createdAt", "order": "desc"}}

Each media object contains:
  - id
  - title / name
  - createdAt
  - collection: {id, name}         <- top-level collection
  - subCollection: {id, name}      <- club name
  - tags: [{id, name}, ...]
  - urls: {photographer, compressed, thumbnail}
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
SP_API_KEY     = os.environ["SP_API_KEY"]
EMAIL_FROM     = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_TO       = os.environ["EMAIL_TO"]

HEADSHOT_TAG_ID = 704382
API_BASE        = "https://api.scoreplay.io"
STATE_FILE      = Path("scoreplay_state.json")
PAGE_SIZE       = 100   # max per page


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    """State: {"seen_ids": ["id1", "id2", ...], "last_run": "ISO datetime"}"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": [], "last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── API ───────────────────────────────────────────────────────────────────────
def search_media(page: int = 1) -> dict:
    """
    POST /media/search — returns all media with the headshot tag.
    Sorted by createdAt desc so newest items are first.
    """
    url  = f"{API_BASE}/media/search?api_key={SP_API_KEY}"
    body = {
        "tags": {
            "include": {
                "and": [HEADSHOT_TAG_ID]
            }
        },
        "sort": {
            "field": "createdAt",
            "order": "desc"
        },
        "page":  page,
        "size":  PAGE_SIZE,
    }
    r = requests.post(url, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_all_recent_media(seen_ids: set) -> list[dict]:
    """
    Fetch pages until we hit media we've already seen, or run out of pages.
    Returns only NEW (unseen) media items.
    """
    new_items = []
    page      = 1

    while True:
        print(f"  Fetching page {page} ...")
        data  = search_media(page)

        # Handle both list response and wrapped response
        items = data if isinstance(data, list) else data.get("data", data.get("items", data.get("results", [])))

        if not items:
            break

        for item in items:
            media_id = str(item.get("id", ""))
            if media_id in seen_ids:
                # Hit already-seen content — stop paginating
                return new_items
            new_items.append(item)

        # If fewer than PAGE_SIZE returned, we've reached the end
        if len(items) < PAGE_SIZE:
            break

        page += 1

    return new_items


# ── Email ─────────────────────────────────────────────────────────────────────
def build_media_rows(items: list[dict]) -> str:
    rows = ""
    for item in items:
        media_id    = item.get("id", "")
        title       = item.get("title") or item.get("name") or item.get("fileName", "Untitled")
        created_at  = item.get("createdAt", "")
        # Format date
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            date_str = created_at

        # Sub-collection = club name
        sub_col  = item.get("subCollection") or {}
        club     = sub_col.get("name", "") if isinstance(sub_col, dict) else ""

        # Collection
        col      = item.get("collection") or {}
        coll_name = col.get("name", "") if isinstance(col, dict) else ""

        # Thumbnail URL
        urls      = item.get("urls") or {}
        thumb_url = urls.get("thumbnail") or urls.get("compressed") or ""

        thumb_html = (
            f'<img src="{thumb_url}" style="width:60px;height:60px;object-fit:cover;border-radius:4px;" />'
            if thumb_url else '<span style="color:#9ca3af;font-size:11px;">No preview</span>'
        )

        rows += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:10px 12px;">{thumb_html}</td>
          <td style="padding:10px 12px;font-size:13px;font-weight:600;">{title}</td>
          <td style="padding:10px 12px;font-size:13px;color:#059669;font-weight:600;">{club}</td>
          <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{coll_name}</td>
          <td style="padding:10px 12px;font-size:13px;color:#9ca3af;">{date_str}</td>
        </tr>"""

    return rows


def send_email(new_items: list[dict]):
    run_time  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows_html = build_media_rows(new_items)

    # Group by club for summary
    clubs = {}
    for item in new_items:
        sub_col = item.get("subCollection") or {}
        club    = sub_col.get("name", "Unknown") if isinstance(sub_col, dict) else "Unknown"
        clubs[club] = clubs.get(club, 0) + 1

    summary_items = "".join(
        f'<span style="display:inline-block;background:#dcfce7;color:#15803d;padding:3px 10px;'
        f'border-radius:12px;font-size:12px;font-weight:600;margin:2px;">{club} ({count})</span>'
        for club, count in sorted(clubs.items())
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
  <div style="max-width:800px;margin:32px auto;background:#fff;border-radius:8px;
              overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">

    <div style="background:#1e3a5f;padding:20px 28px;">
      <p style="margin:0;color:#93c5fd;font-size:12px;text-transform:uppercase;letter-spacing:1px;">
        ScorePlay Monitor</p>
      <h1 style="margin:4px 0 0;color:#fff;font-size:20px;">
        New Player Headshots Uploaded</h1>
    </div>

    <div style="background:#f8fafc;border-bottom:1px solid #e5e7eb;padding:12px 28px;
                font-size:13px;color:#374151;">
      <strong>{len(new_items)}</strong> new headshot(s) detected &mdash;
      {summary_items} &mdash;
      <span style="color:#9ca3af">{run_time}</span>
    </div>

    <div style="padding:20px 28px;">
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f1f5f9;">
            <th style="padding:8px 12px;text-align:left;font-size:11px;
                       text-transform:uppercase;color:#6b7280;letter-spacing:.5px;width:70px;">
              Preview</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;
                       text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">
              File</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;
                       text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">
              Club</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;
                       text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">
              Collection</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;
                       text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">
              Uploaded</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>

    <div style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:12px 28px;
                font-size:12px;color:#9ca3af;">
      This is an automated notification from the ScorePlay media monitor.
    </div>
  </div>
</body>
</html>"""

    subject = f"ScorePlay – {len(new_items)} new headshot(s) uploaded"
    if clubs:
        subject += f" ({', '.join(sorted(clubs.keys()))})"

    msg = MIMEMultipart("alternative")
    msg["From"]    = f"ScorePlay Monitor <{EMAIL_FROM}>"
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    with smtplib.SMTP("smtp.office365.com", 587) as smtp:
        smtp.starttls()
        smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_FROM, recipients, msg.as_string())

    print(f"Email sent — {len(new_items)} new item(s) to {len(recipients)} recipient(s).")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*55}")
    print(f"ScorePlay headshot monitor — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Tag ID: {HEADSHOT_TAG_ID}")
    print(f"{'='*55}\n")

    state    = load_state()
    seen_ids = set(state.get("seen_ids", []))
    print(f"State loaded — {len(seen_ids)} previously seen media ID(s).")

    # First run: just build the baseline, no email
    first_run = len(seen_ids) == 0

    new_items = fetch_all_recent_media(seen_ids)
    print(f"\n{len(new_items)} new media item(s) found.")

    if new_items and not first_run:
        send_email(new_items)
    elif first_run:
        print("First run — baseline established, no email sent.")
    else:
        print("No new media — no email sent.")

    # Update state: add new IDs, keep all existing IDs
    all_ids = list(seen_ids) + [str(item.get("id", "")) for item in new_items]
    state["seen_ids"]  = all_ids
    state["last_run"]  = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"State saved — {len(all_ids)} total media ID(s) tracked.")


if __name__ == "__main__":
    main()
