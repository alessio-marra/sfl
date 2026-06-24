"""
check_scoreplay.py
Checks ScorePlay API twice daily for new media with tag 704382 (player headshots).
Sends an email if new media has been uploaded since the last run.

API: POST https://media.scoreplay.io/v1/media/search?api_key=KEY
Response: {"media": [{ID, CreatedAt, name, thumbnail_url, files:[{details, url}], ...}]}
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


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": [], "last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── API ───────────────────────────────────────────────────────────────────────
def fetch_media() -> list[dict]:
    url  = f"{API_BASE}/media/search?api_key={SP_API_KEY}"
    body = {
        "media_type": "photo",
        "tag_options": [HEADSHOT_TAG_ID],
    }
    r = requests.post(url, json=body, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("media", [])


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
            dt       = datetime.fromisoformat(created.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            date_str = created

        thumb    = item.get("thumbnail_url", "")
        orig_url = get_original_url(item)

        thumb_html = (
            f'<img src="{thumb}" style="width:60px;height:60px;object-fit:cover;border-radius:4px;" />'
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
      <h1 style="margin:4px 0 0;color:#fff;font-size:20px;">
        New Player Headshots Uploaded</h1>
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
    print(f"\n{'='*55}")
    print(f"ScorePlay headshot monitor — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Tag ID: {HEADSHOT_TAG_ID}")
    print(f"{'='*55}\n")

    state    = load_state()
    seen_ids = set(str(i) for i in state.get("seen_ids", []))
    print(f"State loaded — {len(seen_ids)} previously seen ID(s).")

    first_run = len(seen_ids) == 0

    print("Fetching media from ScorePlay ...")
    all_items = fetch_media()
    print(f"  {len(all_items)} total item(s) returned.")

    new_items = [item for item in all_items if str(item.get("ID", "")) not in seen_ids]
    print(f"  {len(new_items)} new item(s).")

    if new_items and not first_run:
        send_email(new_items)
    elif first_run:
        print("First run — baseline established, no email sent.")
    else:
        print("No new media — no email sent.")

    all_ids = list(seen_ids) + [str(item.get("ID", "")) for item in new_items]
    state["seen_ids"] = all_ids
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"State saved — {len(all_ids)} total ID(s) tracked.")


if __name__ == "__main__":
    main()
