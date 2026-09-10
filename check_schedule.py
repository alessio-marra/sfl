"""
check_schedule.py
Monitors Stats Perform MA1 feed for match date/time changes and new fixtures
across Super League, Challenge League, and Playoffs.

Bootstrap: fetches all current season matches, sends email with all new fixtures.
Incremental: checks MAR every hour, emails on date/time changes or new fixtures.
Both types are combined in a single email with two clear sections.
Auto-purge: runs on June 10th each year to clean previous season from state.
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

BASE_URL         = "https://api.performfeeds.com/soccerdata"
HEADERS          = {"Referer": REFERER}
STATE_FILE       = Path("schedule_state.json")
COMPETITION_IDS  = "8v97rcbthsxmzqk4ufxws9mug,e0lck99w8meo9qoalfrxgo33o,8872tjohi4vpok4s2mtphxj9x"
MAR_LOOKBACK_MIN = 90


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


def purge_old_season(state: dict) -> dict:
    """On June 10th each year, remove matches from the previous season."""
    now = datetime.now(timezone.utc)
    if not (now.month == 6 and now.day == 10):
        return state
    season_start_year = now.year - 1
    cutoff = f"{season_start_year}-07-01"
    before = len(state)
    state  = {k: v for k, v in state.items() if v.get("date", "") >= cutoff}
    print(f"[PURGE] June 10th purge: {before} → {len(state)} matches kept.")
    return state


def extract_match_data(mi: ET.Element) -> dict:
    desc_el  = mi.find("description")
    comp_el  = mi.find("competition")
    stage_el = mi.find("stage")
    return {
        "date":        mi.get("date", "").replace("Z", ""),
        "time":        mi.get("time", "").replace("Z", ""),
        "local_date":  mi.get("localDate", ""),
        "local_time":  mi.get("localTime", ""),
        "description": desc_el.text if desc_el is not None else "",
        "competition": comp_el.get("name", "") if comp_el is not None else "",
        "stage":       stage_el.text if stage_el is not None else "",
        "week":        mi.get("week", ""),
    }


# ── MA1 match feed — bootstrap ────────────────────────────────────────────────
def fetch_all_matches() -> dict:
    now               = datetime.now(timezone.utc)
    season_start_year = now.year if now.month >= 7 else now.year - 1
    date_filter       = (
        f"[{season_start_year}-07-01T00:01:00Z TO "
        f"{season_start_year + 1}-06-30T23:59:59Z]"
    )
    print(f"Fetching all matches for season {season_start_year}/{season_start_year+1}...")

    matches = {}
    page    = 1

    while True:
        url = (
            f"{BASE_URL}/match/{API_KEY}"
            f"?live=yes&_fmt=xml&_rt=c&_pgSz=100&_pgNm={page}"
            f"&comp={COMPETITION_IDS}&mt.mDt={date_filter}"
        )
        try:
            root = get_xml(url)
        except Exception as e:
            print(f"  Page {page} failed: {e}")
            break

        error_els = [el for el in root.iter() if el.tag.endswith("errorCode")]
        if error_els:
            print(f"  End of feed at page {page}.")
            break

        page_count = 0
        for mi in root.iter("matchInfo"):
            match_id = mi.get("id")
            if match_id:
                matches[match_id] = extract_match_data(mi)
                page_count += 1

        print(f"  Page {page}: {page_count} matches")
        if page_count == 0:
            break
        page += 1

    print(f"  Total: {len(matches)} matches fetched.")
    return matches


# ── MA1 single match ──────────────────────────────────────────────────────────
def fetch_match_details(match_id: str) -> dict | None:
    url = f"{BASE_URL}/match/{API_KEY}?live=yes&_fmt=xml&_rt=c&fx={match_id}"
    try:
        root = get_xml(url)
    except Exception as e:
        print(f"    MA1 fetch failed for {match_id}: {e}")
        return None
    for mi in root.iter("matchInfo"):
        if mi.get("id") == match_id:
            return extract_match_data(mi)
    return None


# ── MAR feed ──────────────────────────────────────────────────────────────────
def fetch_mar_updated_ids() -> set[str]:
    now_utc   = datetime.now(timezone.utc)
    since_str = (now_utc - timedelta(minutes=MAR_LOOKBACK_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
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


# ── Graph API ─────────────────────────────────────────────────────────────────
def get_graph_token() -> str:
    import msal
    app    = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Graph token failed: {result.get('error_description', result)}")
    return result["access_token"]


def send_graph_email(subject: str, html_body: str):
    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    token      = get_graph_token()
    payload    = {
        "message": {
            "subject": subject,
            "body":    {"contentType": "HTML", "content": html_body},
            "from":    {"emailAddress": {"address": EMAIL_FROM, "name": "Stats Perform Schedule Monitor"}},
            "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        },
        "saveToSentItems": "false"
    }
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{EMAIL_FROM}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    if r.status_code == 202:
        print(f"Email sent: {subject}")
    else:
        raise RuntimeError(f"Graph API send failed: {r.status_code} — {r.text}")


# ── Email builder ─────────────────────────────────────────────────────────────
def build_email(reschedules: list[dict], new_fixtures: list[dict]) -> str:
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Count summary for header
    parts = []
    if reschedules:
        parts.append(f"<strong>{len(reschedules)}</strong> updated fixture(s)")
    if new_fixtures:
        parts.append(f"<strong>{len(new_fixtures)}</strong> new fixture(s)")
    summary = " &mdash; ".join(parts)

    # ── Updated fixtures section ──────────────────────────────────────────────
    reschedule_section = ""
    if reschedules:
        rows = ""
        for m in sorted(reschedules, key=lambda x: (x["new_date"], x["new_time"])):
            old_dt    = f"{m['old_date']} {m['old_time']} UTC"
            new_dt    = f"{m['new_date']} {m['new_time']} UTC"
            new_local = f"{m.get('new_local_date','')} {m.get('new_local_time','')}".strip()
            date_ch   = m["old_date"] != m["new_date"]
            time_ch   = m["old_time"] != m["new_time"]
            badge     = " + ".join((["Date"] if date_ch else []) + (["Time"] if time_ch else []))

            rows += f"""
            <tr style="border-bottom:1px solid #e5e7eb;">
              <td style="padding:10px 12px;font-size:13px;color:#6b7280;white-space:nowrap;">{m.get('competition','')}</td>
              <td style="padding:10px 12px;font-size:13px;font-weight:600;">{m.get('description','')}</td>
              <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{m.get('stage','')}</td>
              <td style="padding:10px 12px;font-size:13px;color:#6b7280;text-align:center;">{m.get('week','')}</td>
              <td style="padding:10px 12px;font-size:13px;">
                <span style="color:#6b7280;text-decoration:line-through;">{old_dt}</span>
              </td>
              <td style="padding:10px 12px;font-size:13px;">
                <strong style="color:#dc2626;">{new_dt}</strong><br>
                <span style="font-size:11px;color:#9ca3af;">Local: {new_local}</span>
              </td>
              <td style="padding:10px 12px;">
                <span style="background:#dc2626;color:#fff;padding:2px 8px;border-radius:4px;
                             font-size:11px;font-weight:700;">{badge}</span>
              </td>
            </tr>"""

        reschedule_section = f"""
        <div style="padding:20px 28px 0;">
          <h2 style="font-size:14px;font-weight:700;color:#dc2626;margin:0 0 12px;
                     text-transform:uppercase;letter-spacing:.5px;">
            &#9888; Updated Fixtures ({len(reschedules)})
          </h2>
          <table style="width:100%;border-collapse:collapse;">
            <thead>
              <tr style="background:#fff5f5;">
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Competition</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Match</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Stage</th>
                <th style="padding:8px 12px;text-align:center;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Week</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Previous Date/Time</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">New Date/Time</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Change</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    # ── New fixtures section ──────────────────────────────────────────────────
    new_fixtures_section = ""
    if new_fixtures:
        rows = ""
        for m in sorted(new_fixtures, key=lambda x: (x.get("date",""), x.get("time",""))):
            dt    = f"{m.get('date','')} {m.get('time','')} UTC"
            local = f"{m.get('local_date','')} {m.get('local_time','')}".strip()

            rows += f"""
            <tr style="border-bottom:1px solid #e5e7eb;">
              <td style="padding:10px 12px;font-size:13px;color:#6b7280;white-space:nowrap;">{m.get('competition','')}</td>
              <td style="padding:10px 12px;font-size:13px;font-weight:600;">{m.get('description','')}</td>
              <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{m.get('stage','')}</td>
              <td style="padding:10px 12px;font-size:13px;color:#6b7280;text-align:center;">{m.get('week','')}</td>
              <td style="padding:10px 12px;font-size:13px;">
                <strong>{dt}</strong><br>
                <span style="font-size:11px;color:#9ca3af;">Local: {local}</span>
              </td>
            </tr>"""

        # Add divider between sections if both present
        divider = '<div style="height:1px;background:#e5e7eb;margin:20px 28px;"></div>' if reschedules else ""

        new_fixtures_section = f"""
        {divider}
        <div style="padding:20px 28px 0;">
          <h2 style="font-size:14px;font-weight:700;color:#2563eb;margin:0 0 12px;
                     text-transform:uppercase;letter-spacing:.5px;">
            &#128197; New Fixtures ({len(new_fixtures)})
          </h2>
          <table style="width:100%;border-collapse:collapse;">
            <thead>
              <tr style="background:#eff6ff;">
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Competition</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Match</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Stage</th>
                <th style="padding:8px 12px;text-align:center;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Week</th>
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Date/Time (UTC)</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
  <div style="max-width:900px;margin:32px auto;background:#fff;border-radius:8px;
              overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">

    <div style="background:#1e3a5f;padding:20px 28px;">
      <p style="margin:0;color:#93c5fd;font-size:12px;text-transform:uppercase;letter-spacing:1px;">
        Stats Perform Monitor</p>
      <h1 style="margin:4px 0 0;color:#fff;font-size:20px;">Match Schedule Update</h1>
    </div>

    <div style="background:#f8fafc;border-bottom:1px solid #e5e7eb;padding:12px 28px;
                font-size:13px;color:#374151;">
      {summary} &mdash; <span style="color:#9ca3af">{run_time}</span>
    </div>

    {reschedule_section}
    {new_fixtures_section}

    <div style="padding:20px 28px;"><!-- spacer --></div>

    <div style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:12px 28px;
                font-size:12px;color:#9ca3af;">
      Automated notification — Stats Perform schedule monitor.
    </div>
  </div>
</body>
</html>"""


def build_subject(reschedules: list, new_fixtures: list) -> str:
    parts = []
    if reschedules:
        parts.append(f"{len(reschedules)} updated fixture(s)")
    if new_fixtures:
        parts.append(f"{len(new_fixtures)} new fixture(s)")
    return f"Stats Perform – {' & '.join(parts)}"


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*55}")
    print(f"Schedule monitor — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}\n")

    state     = load_state()
    first_run = len(state) == 0

    # ── Bootstrap ─────────────────────────────────────────────────────────────
    if first_run:
        print("=== BOOTSTRAP MODE ===")
        matches = fetch_all_matches()

        if not matches:
            print("No matches returned — aborting.")
            return

        save_state(matches)

        # All matches are new fixtures — send combined email
        new_fixtures = sorted(
            [{"description": v["description"], "competition": v["competition"],
              "stage": v["stage"], "week": v["week"], "date": v["date"],
              "time": v["time"], "local_date": v["local_date"], "local_time": v["local_time"]}
             for v in matches.values()],
            key=lambda x: (x["date"], x["time"])
        )
        html = build_email(reschedules=[], new_fixtures=new_fixtures)
        send_graph_email(
            subject   = f"Stats Perform – {len(new_fixtures)} new fixture(s) — season schedule loaded",
            html_body = html,
        )
        print(f"Bootstrap complete — {len(matches)} matches, email sent.")
        return

    # ── June 10th purge ───────────────────────────────────────────────────────
    state = purge_old_season(state)

    # ── Incremental ───────────────────────────────────────────────────────────
    print("=== INCREMENTAL MODE ===")
    updated_ids = fetch_mar_updated_ids()

    if not updated_ids:
        print("No updated matches. Done.")
        save_state(state)
        return

    reschedules  = []
    new_fixtures = []

    for match_id in updated_ids:
        print(f"  Checking {match_id} ...")
        current = fetch_match_details(match_id)

        if current is None:
            print(f"    Could not fetch — skipping.")
            continue

        if match_id not in state:
            print(f"    NEW fixture: {current['description']}")
            new_fixtures.append(current)
            state[match_id] = current
            continue

        saved        = state[match_id]
        date_changed = saved["date"] != current["date"]
        time_changed = saved["time"] != current["time"]

        if date_changed or time_changed:
            print(f"    CHANGE: {current['description']} — "
                  f"{saved['date']} {saved['time']} → {current['date']} {current['time']}")
            reschedules.append({
                "description":    current["description"],
                "competition":    current["competition"],
                "stage":          current.get("stage", ""),
                "week":           current.get("week", ""),
                "old_date":       saved["date"],
                "old_time":       saved["time"],
                "new_date":       current["date"],
                "new_time":       current["time"],
                "new_local_date": current.get("local_date", ""),
                "new_local_time": current.get("local_time", ""),
            })
            state[match_id] = current
        else:
            print(f"    No date/time change.")

    save_state(state)

    if reschedules or new_fixtures:
        html = build_email(reschedules=reschedules, new_fixtures=new_fixtures)
        send_graph_email(
            subject   = build_subject(reschedules, new_fixtures),
            html_body = html,
        )
    else:
        print("No date/time changes and no new fixtures.")


if __name__ == "__main__":
    main()
