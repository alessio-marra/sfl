"""
check_substitutions.py
Checks Stats Perform MAR feed for updated matches, then compares substitution
minutes (typeID 18/19) in MA3 against the saved state. Sends email if changes detected.

MA3 XML structure (from documentation):
  <matchEvents>
    <matchInfo id="..." date="..." lastUpdated="...">
      <description>Basel vs Zürich</description>
      <competition name="Super League" .../>
      <contestants>
        <contestant id="..." name="Basel" position="home"/>
        <contestant id="..." name="Zürich" position="away"/>
      </contestants>
    </matchInfo>
    <liveData>
      <event id="..." eventId="555" typeId="18" periodId="2" timeMin="57" timeSec="20"
             contestantId="..." playerId="..." playerName="G. Koloto" ...>
        <qualifier qualifierId="55" value="556"/>   <- links to partner eventId
      </event>
    </liveData>
  </matchEvents>
"""

import os
import json
import smtplib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY        = os.environ["SP_API_KEY"]
REFERER        = os.environ["SP_REFERER"]
EMAIL_FROM     = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_TO       = os.environ["EMAIL_TO"]
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "13"))

BASE_URL   = "https://api.performfeeds.com/soccerdata"
STATE_FILE = Path("state.json")
HEADERS    = {"Referer": REFERER}

SUB_OFF = "18"  # playerOff
SUB_ON  = "19"  # playerOn


# ── State helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── API calls ─────────────────────────────────────────────────────────────────
def fetch_mar(since: str) -> list[str]:
    """Return list of matchIDs updated since `since` (ISO 8601 UTC)."""
    url = (
        f"{BASE_URL}/matchreference/{API_KEY}/"
        f"?_rt=c&_fmt=xml&type=ma3&_rdlt={since}"
    )
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    return [mi.get("id") for mi in root.findall("matchInfo")]


def fetch_match_data(match_id: str) -> dict:
    """
    Fetch MA3 feed for a match.
    Returns:
    {
      "description": "Basel vs Zürich",
      "competition": "Super League",
      "date": "2026-02-08",
      "teams": { "contestantId": "Basel", ... },
      "substitutions": {
        "event_id": {
          "typeId": "18",
          "timeMin": "57",
          "timeSec": "20",
          "playerId": "...",
          "playerName": "G. Koloto",
          "teamId": "...",
          "teamName": "Basel",
          "partnerEventId": "556"   <- the linked playerOn/playerOff eventId
        }
      }
    }
    """
    url = (
        f"{BASE_URL}/matchevent/{API_KEY}/"
        f"?_fmt=xml&_rt=c&fx={match_id}"
    )
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    # Debug: show raw XML start to verify structure
    print(f"     XML preview: {r.text[:300].replace(chr(10), ' ')}")

    root = ET.fromstring(r.text)

    # ── Match info ────────────────────────────────────────────────────────────
    match_info = root.find("matchInfo")
    description = ""
    competition = ""
    date = ""
    teams = {}  # contestantId -> name

    if match_info is not None:
        desc_el = match_info.find("description")
        description = desc_el.text if desc_el is not None else ""

        comp_el = match_info.find("competition")
        if comp_el is not None:
            competition = comp_el.get("name", "") or comp_el.get("knownName", "")

        date = match_info.get("date", "").replace("Z", "")

        for c in match_info.findall(".//contestant"):
            teams[c.get("id")] = c.get("name", "")

    # ── Events (note: lowercase 'event' per MA3 doc) ──────────────────────────
    substitutions = {}
    live_data = root.find("liveData")
    if live_data is None:
        print(f"     WARNING: no <liveData> element found for match {match_id}")
        event_parent = root
    else:
        # MA3 structure: <liveData><events><event ...>
        events_el = live_data.find("events")
        event_parent = events_el if events_el is not None else live_data

    for event in event_parent.findall("event"):
        type_id = event.get("typeId")
        if type_id not in (SUB_OFF, SUB_ON):
            continue

        event_id   = event.get("id")
        event_id_num = event.get("eventId")  # numeric eventId used by qualifier 55
        contestant_id = event.get("contestantId", "")

        # Find qualifier 55 = link to partner event (playerOn <-> playerOff)
        partner_event_id = None
        for q in event.findall("qualifier"):
            if q.get("qualifierId") == "55":
                partner_event_id = q.get("value")
                break

        substitutions[event_id] = {
            "typeId":          type_id,
            "eventIdNum":      event_id_num,
            "timeMin":         event.get("timeMin"),
            "timeSec":         event.get("timeSec"),
            "playerId":        event.get("playerId"),
            "playerName":      event.get("playerName", ""),
            "teamId":          contestant_id,
            "teamName":        teams.get(contestant_id, contestant_id),
            "partnerEventId":  partner_event_id,
        }
        print(
            f"     Found: id={event_id} typeId={type_id} "
            f"player={event.get('playerName','')} "
            f"min={event.get('timeMin')}:{event.get('timeSec')} "
            f"team={teams.get(contestant_id, contestant_id)}"
        )

    if not substitutions:
        print(f"     (no substitution events found for match {match_id})")

    return {
        "description": description,
        "competition":  competition,
        "date":         date,
        "teams":        teams,
        "substitutions": substitutions,
    }


# ── Pair substitutions (OFF + ON) ─────────────────────────────────────────────
def pair_substitutions(subs: dict) -> list[dict]:
    """
    Group playerOff (18) and playerOn (19) events into substitution pairs
    using qualifier 55 (which holds the partner's eventId numeric value).
    Returns list of paired dicts.
    """
    # Build a lookup: eventIdNum -> event_id (the 'id' attribute)
    num_to_id = {v["eventIdNum"]: k for k, v in subs.items() if v["eventIdNum"]}

    paired = {}   # keyed by playerOff event_id
    visited = set()

    for event_id, ev in subs.items():
        if event_id in visited:
            continue
        if ev["typeId"] == SUB_OFF:
            # Find the matching playerOn via partnerEventId
            partner_num = ev.get("partnerEventId")
            partner_id  = num_to_id.get(partner_num) if partner_num else None
            partner_ev  = subs.get(partner_id) if partner_id else None

            paired[event_id] = {
                "off_event_id": event_id,
                "on_event_id":  partner_id,
                "timeMin":      ev["timeMin"],
                "timeSec":      ev["timeSec"],
                "playerOff":    ev["playerName"] or ev["playerId"],
                "playerOn":     partner_ev["playerName"] if partner_ev else "?",
                "teamName":     ev["teamName"],
                "teamId":       ev["teamId"],
            }
            visited.add(event_id)
            if partner_id:
                visited.add(partner_id)

    return list(paired.values())


# ── Diff logic ────────────────────────────────────────────────────────────────
def find_changes(match_id: str, old_data: dict, new_data: dict) -> list[dict]:
    """
    Compare substitution events by event_id.
    Only flags changes where timeMin OR timeSec changed.
    """
    changes = []
    old_subs = old_data.get("substitutions", {})
    new_subs = new_data.get("substitutions", {})

    # Build partner lookups for new data (to display playerOn name in changes)
    new_num_to_id = {v["eventIdNum"]: k for k, v in new_subs.items() if v["eventIdNum"]}

    for event_id, new_ev in new_subs.items():
        if new_ev["typeId"] != SUB_OFF:
            continue  # only track playerOff as the anchor; ON follows

        # Find partner playerOn name
        partner_num = new_ev.get("partnerEventId")
        partner_id  = new_num_to_id.get(partner_num) if partner_num else None
        player_on   = new_subs[partner_id]["playerName"] if partner_id and partner_id in new_subs else "?"

        if event_id not in old_subs:
            if old_subs:  # skip on first run (empty state)
                changes.append({
                    "match_id":    match_id,
                    "event_id":    event_id,
                    "change":      "NEW",
                    "playerOff":   new_ev["playerName"],
                    "playerOn":    player_on,
                    "teamName":    new_ev["teamName"],
                    "description": new_data["description"],
                    "competition": new_data["competition"],
                    "date":        new_data["date"],
                    "old_min":     None,
                    "old_sec":     None,
                    "new_min":     new_ev["timeMin"],
                    "new_sec":     new_ev["timeSec"],
                })
        else:
            old_ev = old_subs[event_id]
            min_changed = old_ev["timeMin"] != new_ev["timeMin"]
            sec_changed = old_ev["timeSec"] != new_ev["timeSec"]

            if min_changed or sec_changed:
                changes.append({
                    "match_id":    match_id,
                    "event_id":    event_id,
                    "change":      "TIME_CHANGED",
                    "min_changed": min_changed,
                    "playerOff":   new_ev["playerName"],
                    "playerOn":    player_on,
                    "teamName":    new_ev["teamName"],
                    "description": new_data["description"],
                    "competition": new_data["competition"],
                    "date":        new_data["date"],
                    "old_min":     old_ev["timeMin"],
                    "old_sec":     old_ev["timeSec"],
                    "new_min":     new_ev["timeMin"],
                    "new_sec":     new_ev["timeSec"],
                })

    for event_id, old_ev in old_subs.items():
        if old_ev["typeId"] != SUB_OFF:
            continue
        if event_id not in new_subs:
            changes.append({
                "match_id":    match_id,
                "event_id":    event_id,
                "change":      "REMOVED",
                "playerOff":   old_ev["playerName"],
                "playerOn":    "?",
                "teamName":    old_ev.get("teamName", ""),
                "description": old_data.get("description", match_id),
                "competition": old_data.get("competition", ""),
                "date":        old_data.get("date", ""),
                "old_min":     old_ev["timeMin"],
                "old_sec":     old_ev["timeSec"],
                "new_min":     None,
                "new_sec":     None,
            })

    return changes


# ── Email ─────────────────────────────────────────────────────────────────────
def _format_time(min_val, sec_val) -> str:
    return f"{min_val}'{str(sec_val).zfill(2)}\""


def _change_rows_html(changes: list[dict]) -> tuple[str, int]:
    """Build HTML table rows for all changes. Returns (html, action_count)."""
    action_count = 0
    rows = []

    for c in changes:
        if c["change"] == "TIME_CHANGED":
            old_time = _format_time(c["old_min"], c["old_sec"])
            new_time = _format_time(c["new_min"], c["new_sec"])
            if c["min_changed"]:
                action_count += 1
                badge = '<span style="background:#dc2626;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;">ACTION NEEDED</span>'
                time_cell = f'<span style="color:#6b7280;text-decoration:line-through">{old_time}</span> &rarr; <strong style="color:#dc2626">{new_time}</strong>'
            else:
                badge = '<span style="background:#d1d5db;color:#374151;padding:2px 8px;border-radius:4px;font-size:11px;">No action needed</span>'
                time_cell = f'<span style="color:#6b7280;text-decoration:line-through">{old_time}</span> &rarr; <strong>{new_time}</strong>'
        elif c["change"] == "NEW":
            new_time = _format_time(c["new_min"], c["new_sec"])
            badge = '<span style="background:#2563eb;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;">NEW</span>'
            time_cell = f'<strong style="color:#2563eb">{new_time}</strong>'
        elif c["change"] == "REMOVED":
            old_time = _format_time(c["old_min"], c["old_sec"])
            badge = '<span style="background:#f59e0b;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;">REMOVED</span>'
            time_cell = f'<span style="color:#6b7280;text-decoration:line-through">{old_time}</span>'
        else:
            continue

        player_on = c.get("playerOn", "?") if c["change"] != "REMOVED" else "—"
        rows.append(f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:10px 12px;font-size:13px;">{c['date']}</td>
          <td style="padding:10px 12px;font-size:13px;font-weight:600;">{c['description']}</td>
          <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{c['competition']}</td>
          <td style="padding:10px 12px;font-size:13px;">{c['teamName']}</td>
          <td style="padding:10px 12px;font-size:13px;">
            <span style="color:#16a34a;">&#x2B06; {player_on}</span><br>
            <span style="color:#dc2626;">&#x2B07; {c['playerOff']}</span>
          </td>
          <td style="padding:10px 12px;font-size:13px;">{time_cell}</td>
          <td style="padding:10px 12px;">{badge}</td>
        </tr>""")

    return "\n".join(rows), action_count


def send_email(changes: list[dict]):
    rows_html, action_count = _change_rows_html(changes)

    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    no_action = len(changes) - action_count

    summary_color = "#dc2626" if action_count else "#374151"
    summary_text = (
        f'<strong style="color:{summary_color}">{action_count} requiring action</strong>'
        + (f", {no_action} no action needed" if no_action else "")
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
  <div style="max-width:800px;margin:32px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">

    <!-- Header -->
    <div style="background:#1e3a5f;padding:20px 28px;">
      <p style="margin:0;color:#93c5fd;font-size:12px;text-transform:uppercase;letter-spacing:1px;">Stats Perform Monitor</p>
      <h1 style="margin:4px 0 0;color:#fff;font-size:20px;">Substitution Changes Detected</h1>
    </div>

    <!-- Summary bar -->
    <div style="background:#f8fafc;border-bottom:1px solid #e5e7eb;padding:12px 28px;font-size:13px;color:#374151;">
      {len(changes)} change(s) found &mdash; {summary_text} &mdash; <span style="color:#9ca3af">{run_time}</span>
    </div>

    <!-- Table -->
    <div style="padding:20px 28px;">
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f1f5f9;">
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Date</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Match</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Competition</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Team</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Players</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Time</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;letter-spacing:.5px;">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>

    <!-- Footer -->
    <div style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:12px 28px;font-size:12px;color:#9ca3af;">
      Please verify and update the NIS system where needed.
    </div>

  </div>
</body>
</html>"""

    subject = f"Stats Perform – {len(changes)} substitution change(s)"
    if action_count:
        subject += f" – {action_count} ACTION(S) NEEDED"

    msg = MIMEMultipart("alternative")
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_FROM, recipients, msg.as_string())

    print(f"Email sent - {len(changes)} change(s), {action_count} action(s) needed.")
# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    since   = (now_utc - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Checking MAR since {since} ...")

    state       = load_state()
    all_changes = []

    try:
        updated_matches = fetch_mar(since)
    except Exception as e:
        print(f"MAR call failed: {e}")
        raise

    print(f"   {len(updated_matches)} match(es) updated since {since}")

    for match_id in updated_matches:
        print(f"   Fetching MA3 for match {match_id} ...")
        try:
            new_data = fetch_match_data(match_id)
        except Exception as e:
            print(f"     MA3 call failed for {match_id}: {e}")
            continue

        old_data = state.get(match_id, {})
        changes  = find_changes(match_id, old_data, new_data)

        if changes:
            print(f"     {len(changes)} change(s) detected!")
            all_changes.extend(changes)
        else:
            print(f"     No changes.")

        # Store full match data (description, competition, date, substitutions)
        state[match_id] = {
            "description":   new_data["description"],
            "competition":   new_data["competition"],
            "date":          new_data["date"],
            "substitutions": new_data["substitutions"],
        }

    save_state(state)
    print(f"State saved ({len(state)} match(es) tracked).")

    if all_changes:
        send_email(all_changes)
    else:
        print("No substitution changes detected across all matches.")


if __name__ == "__main__":
    main()
