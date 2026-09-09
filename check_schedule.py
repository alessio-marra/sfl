"""
check_schedule.py
Monitors Stats Perform MA1 feed for match date/time changes across
Super League, Challenge League, and Playoffs.

Initial run: fetches all matches via MA1 match feed, saves baseline.
Subsequent runs: checks MAR (type=ma1, 90min lookback) for updated matches,
fetches their MA1 data, compares date/time against state, sends email on change.
"""

import os
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY             = os.environ["SP_API_KEY"]
REFERER             = os.environ["SP_REFERER"]
EMAIL_FROM          = os.environ["EMAIL_FROM"]
EMAIL_TO            = os.environ["EMAIL_TO_SCHEDULE"]
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]

BASE_URL   = "https://api.performfeeds.com/soccerdata"
HEADERS    = {"Referer": REFERER}
STATE_FILE = Path("schedule_state.json")

# Competition IDs to monitor
COMPETITION_IDS = "8v97rcbthsxmzqk4ufxws9mug,e0lck99w8meo9qoalfrxgo33o,8872tjohi4vpok4s2mtphxj9x"

MAR_LOOKBACK_MINUTES = 90


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_xml(url: str) -> ET.Element:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.text)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── MA1 match feed — initial bootstrap ───────────────────────────────────────
def fetch_all_matches() -> dict:
    """
    Fetches all matches across monitored competitions for the current season.
    Returns {matchId: {date, time, description, competition}}.
    """
    print("Fetching all matches from MA1 match feed...")
    matches = {}
    page = 1

    # Date range covering current season
    date_filter = "[2026-07-01T00:01:00Z TO 2027-06-30T23:59:59Z]"

    while True:
        url = (
            f"{BASE_URL}/match/{API_KEY}"
            f"?live=yes&_fmt=xml&_rt=c&_pgSz=100&_pgNm={page}"
            f"&comp={COMPETITION_IDS}&mt.mDt={date_filter}"
        )
        try:
            root = get_xml(url)
        except Exception as e:
            print(f"  Match feed page {page} failed: {e}")
            break

        # Check for error code (end of pages)
        error_els = [el for el in root.iter() if el.tag.endswith("errorCode")]
        if error_els:
            print(f"  End of match feed at page {page} (code: {error_els[0].text})")
            break

        page_count = 0
        for mi in root.iter("matchInfo"):
            match_id    = mi.get("id")
            date        = mi.get("date", "").replace("Z", "")
            time        = mi.get("time", "").replace("Z", "")
            local_date  = mi.get("localDate", "")
            local_time  = mi.get("localTime", "")

            desc_el     = mi.find("description")
            description = desc_el.text if desc_el is not None else ""

            comp_el     = mi.find("competition")
            competition = comp_el.get("name", "") if comp_el is not None else ""

            stage_el    = mi.find("stage")
            stage       = stage_el.text if stage_el is not None else ""

            week        = mi.get("week", "")

            if match_id:
                matches[match_id] = {
                    "date":        date,
                    "time":        time,
                    "local_date":  local_date,
                    "local_time":  local_time,
                    "description": description,
                    "competition": competition,
                    "stage":       stage,
                    "week":        week,
                }
                page_count += 1

        print(f"  Page {page}: {page_count} matches")

        if page_count == 0:
            break
        page += 1

    print(f"  Total: {len(matches)} matches fetched.")
    return matches


# ── MAR feed — incremental check ─────────────────────────────────────────────
def fetch_mar_updated_match_ids() -> set[str]:
    """Returns match IDs updated in the last MAR_LOOKBACK_MINUTES via type=ma1."""
    now_utc   = datetime.now(timezone.utc)
    since_str = (now_utc - timedelta(minutes=MAR_LOOKBACK_MINUTES)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    print(f"Checking MAR (type=ma1) since {since_str} ...")
    url = (
        f"{BASE_URL}/matchreference/{API_KEY}/"
        f"?_rt=c&_fmt=xml&type=ma1&_rdlt={since_str}"
    )
    try:
        root = get_xml(url)
    except Exception as e:
        print(f"  MAR call failed: {e}")
        return set()

    ids = {mi.get("id") for mi in root.iter("matchInfo") if mi.get("id")}
    print(f"  MAR returned {len(ids)} updated match(es).")
    return ids


# ── MA1 single match fetch ────────────────────────────────────────────────────
def fetch_match_details(match_id: str) -> dict | None:
    """Fetches current date/time for a single match via MA1."""
    url = (
        f"{BASE_URL}/match/{API_KEY}"
        f"?live=yes&_fmt=xml&_rt=c&fx={match_id}"
    )
    try:
        root = get_xml(url)
    except Exception as e:
        print(f"    MA1 fetch failed for {match_id}: {e}")
        return None

    for mi in root.iter("matchInfo"):
        if mi.get("id") == match_id:
            desc_el     = mi.find("description")
            description = desc_el.text if desc_el is not None else ""
            comp_el     = mi.find("competition")
            competition = comp_el.get("name", "") if comp_el is not None else ""
            stage_el    = mi.find("stage")
            stage       = stage_el.text if stage_el is not None else ""

            return {
                "date":        mi.get("date", "").replace("Z", ""),
                "time":        mi.get("time", "").replace("Z", ""),
                "local_date":  mi.get("localDate", ""),
                "local_time":  mi.get("localTime", ""),
                "description": description,
                "competition": competition,
                "stage":       stage,
                "week":        mi.get("week", ""),
            }
    return None


# ── Graph API email ───────────────────────────────────────────────────────────
def get_graph_token() -> str:
    import msal
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to obtain Graph token: {result.get('error_description', result)}"
        )
    return result["access_token"]


def send_email(changes: list[dict]):
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = ""
    for c in changes:
        old_dt = f"{c['old_date']} {c['old_time']} UTC" if c['old_date'] else "—"
        new_dt = f"{c['new_date']} {c['new_time']} UTC" if c['new_date'] else "—"
        new_local = f"{c['new_local_date']} {c['new_local_time']}" if c['new_local_date'] else "—"

        date_changed = c["old_date"] != c["new_date"]
        time_changed = c["old_time"] != c["new_time"]
        changes_str  = " + ".join(
            (["Date"] if date_changed else []) +
            (["Time"] if time_changed else [])
        )

        rows += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{c['competition']}</td>
          <td style="padding:10px 12px;font-size:13px;font-weight:600;">{c['description']}</td>
          <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{c.get('stage','')}</td>
          <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{c.get('week','')}</td>
          <td style="padding:10px 12px;font-size:13px;">
            <span style="color:#6b7280;text-decoration:line-through;">{old_dt}</span><br>
            <strong style="color:#dc2626;">{new_dt}</strong><br>
            <span style="font-size:11px;color:#9ca3af;">Local: {new_local}</span>
          </td>
          <td style="padding:10px 12px;">
            <span style="background:#dc2626;color:#fff;padding:2px 8px;border-radius:4px;
                         font-size:11px;font-weight:700;">{changes_str}</span>
          </td>
        </tr>"""

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
  <div style="max-width:800px;margin:32px auto;background:#fff;border-radius:8px;
              overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">
    <div style="background:#1e3a5f;padding:20px 28px;">
      <p style="margin:0;color:#93c5fd;font-size:12px;text-transform:uppercase;letter-spacing:1px;">
        Stats Perform Monitor</p>
      <h1 style="margin:4px 0 0;color:#fff;font-size:20px;">Match Schedule Change Detected</h1>
    </div>
    <div style="background:#f8fafc;border-bottom:1px solid #e5e7eb;padding:12px 28px;
                font-size:13px;color:#374151;">
      <strong>{len(changes)}</strong> match(es) rescheduled &mdash;
      <span style="color:#9ca3af">{run_time}</span>
    </div>
    <div style="padding:20px 28px;">
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f1f5f9;">
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Competition</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Match</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Stage</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Week</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Date/Time</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       color:#6b7280;letter-spacing:.5px;">Change</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    <div style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:12px 28px;
                font-size:12px;color:#9ca3af;">
      Automated notification — Stats Perform schedule monitor.
    </div>
  </div>
</body>
</html>"""

    subject    = f"Stats Perform – {len(changes)} match schedule change(s) detected"
    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    token      = get_graph_token()

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "from": {
                "emailAddress": {
                    "address": EMAIL_FROM,
                    "name": "Stats Perform Schedule Monitor"
                }
            },
            "toRecipients": [
                {"emailAddress": {"address": r}} for r in recipients
            ],
        },
        "saveToSentItems": "false"
    }

    response = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{EMAIL_FROM}/sendMail",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    if response.status_code == 202:
        print(f"Email sent — {len(changes)} change(s).")
    else:
        raise RuntimeError(
            f"Graph API send failed: {response.status_code} — {response.text}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*55}")
    print(f"Schedule monitor — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}\n")

    state      = load_state()
    first_run  = len(state) == 0

    if first_run:
        print("=== BOOTSTRAP MODE ===")
        matches = fetch_all_matches()
        save_state(matches)
        print(f"Baseline saved — {len(matches)} matches. No email sent.")
        return

    print("=== INCREMENTAL MODE ===")
    updated_ids = fetch_mar_updated_match_ids()

    if not updated_ids:
        print("No updated matches. Done.")
        save_state(state)
        return

    changes = []

    for match_id in updated_ids:
        print(f"  Checking {match_id} ...")
        current = fetch_match_details(match_id)

        if current is None:
            print(f"    Could not fetch details — skipping.")
            continue

        if match_id not in state:
            # New match added to the feed — save as baseline, no email
            print(f"    New match: {current['description']} — saved as baseline.")
            state[match_id] = current
            continue

        saved = state[match_id]
        date_changed = saved["date"] != current["date"]
        time_changed = saved["time"] != current["time"]

        if date_changed or time_changed:
            print(
                f"    CHANGE: {current['description']} — "
                f"date: {saved['date']}→{current['date']}, "
                f"time: {saved['time']}→{current['time']}"
            )
            changes.append({
                "match_id":    match_id,
                "description": current["description"],
                "competition": current["competition"],
                "stage":       current.get("stage", ""),
                "week":        current.get("week", ""),
                "old_date":    saved["date"],
                "old_time":    saved["time"],
                "new_date":    current["date"],
                "new_time":    current["time"],
                "new_local_date": current.get("local_date", ""),
                "new_local_time": current.get("local_time", ""),
            })
            # Update state with new date/time
            state[match_id] = current
        else:
            print(f"    No date/time change.")

    save_state(state)

    if changes:
        send_email(changes)
    else:
        print("No date/time changes found.")


if __name__ == "__main__":
    main()
