"""
check_substitutions.py
Checks Stats Perform MAR feed for updated matches, then compares substitution
minutes (typeID 18/19) in MA3 against the saved state. Sends email if changes detected.
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
API_KEY        = os.environ["SP_API_KEY"]          # your Stats Perform key (k016oy2f3ex51wf9uajvvkt6s)
REFERER        = os.environ["SP_REFERER"]          # whitelisted referer header
EMAIL_FROM     = os.environ["EMAIL_FROM"]          # sender Gmail address
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]      # Gmail App Password
EMAIL_TO       = os.environ["EMAIL_TO"]            # recipient(s), comma-separated
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "13"))  # hours to look back for MAR

BASE_URL   = "https://api.performfeeds.com/soccerdata"
STATE_FILE = Path("state.json")

HEADERS = {"Referer": REFERER}

SUB_EVENT_TYPES = {"18", "19"}  # 18 = playerOff, 19 = playerOn


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


def fetch_substitutions(match_id: str) -> dict:
    """
    Return substitution events for a match.
    Structure: { event_id: { typeId, playerId, playerName, matchMinute, ... } }
    """
    url = (
        f"{BASE_URL}/matchevent/{API_KEY}/"
        f"?_fmt=xml&_rt=c&fx={match_id}"
    )
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    subs = {}
    # The MA3 XML nests events inside <MatchData><MatchEvents><Event ...>
    for event in root.iter("Event"):
        type_id = event.get("typeId") or event.get("TypeId") or event.get("type_id")
        if type_id not in SUB_EVENT_TYPES:
            continue
        event_id = event.get("id") or event.get("eventId")
        subs[event_id] = {
            "typeId":       type_id,
            "matchMinute":  event.get("timeMin") or event.get("matchMinute") or event.get("min"),
            "playerId":     event.get("playerId") or event.get("player_id"),
            "playerName":   event.get("playerName") or "",
            "teamId":       event.get("teamId") or "",
        }
    return subs


# ── Diff logic ────────────────────────────────────────────────────────────────
def find_changes(match_id: str, old_subs: dict, new_subs: dict) -> list[dict]:
    changes = []

    for event_id, new in new_subs.items():
        if event_id not in old_subs:
            # New substitution event appeared
            changes.append({
                "match_id": match_id,
                "event_id": event_id,
                "change":   "NEW",
                "typeId":   new["typeId"],
                "playerId": new["playerId"],
                "playerName": new["playerName"],
                "old_minute": None,
                "new_minute": new["matchMinute"],
            })
        elif old_subs[event_id]["matchMinute"] != new["matchMinute"]:
            # Minute changed
            changes.append({
                "match_id":   match_id,
                "event_id":   event_id,
                "change":     "MINUTE_CHANGED",
                "typeId":     new["typeId"],
                "playerId":   new["playerId"],
                "playerName": new["playerName"],
                "old_minute": old_subs[event_id]["matchMinute"],
                "new_minute": new["matchMinute"],
            })

    for event_id in old_subs:
        if event_id not in new_subs:
            changes.append({
                "match_id": match_id,
                "event_id": event_id,
                "change":   "REMOVED",
                "typeId":   old_subs[event_id]["typeId"],
                "playerId": old_subs[event_id]["playerId"],
                "playerName": old_subs[event_id].get("playerName", ""),
                "old_minute": old_subs[event_id]["matchMinute"],
                "new_minute": None,
            })

    return changes


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(changes: list[dict]):
    type_label = {"18": "playerOff ⬅️", "19": "playerOn ➡️"}

    lines = []
    for c in changes:
        label = type_label.get(c["typeId"], f"typeID {c['typeId']}")
        player = c["playerName"] or c["playerId"]
        if c["change"] == "MINUTE_CHANGED":
            lines.append(
                f"  • Match {c['match_id']} | {label} | {player} | "
                f"minute {c['old_minute']} → {c['new_minute']}"
            )
        elif c["change"] == "NEW":
            lines.append(
                f"  • Match {c['match_id']} | {label} | {player} | "
                f"NEW event @ minute {c['new_minute']}"
            )
        elif c["change"] == "REMOVED":
            lines.append(
                f"  • Match {c['match_id']} | {label} | {player} | "
                f"REMOVED (was minute {c['old_minute']})"
            )

    body = (
        f"Stats Perform — Substitution Changes Detected\n"
        f"{'=' * 50}\n"
        f"Run time (UTC): {datetime.now(timezone.utc).isoformat()}\n\n"
        + "\n".join(lines)
        + "\n\nPlease verify and update the federation system if needed."
    )

    msg = MIMEMultipart()
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg["Subject"] = f"⚽ Stats Perform — {len(changes)} substitution change(s) detected"
    msg.attach(MIMEText(body, "plain"))

    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_FROM, recipients, msg.as_string())

    print(f"📧 Email sent — {len(changes)} change(s).")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    since   = (now_utc - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"🔍 Checking MAR since {since} ...")

    state = load_state()
    all_changes = []

    try:
        updated_matches = fetch_mar(since)
    except Exception as e:
        print(f"❌ MAR call failed: {e}")
        raise

    print(f"   {len(updated_matches)} match(es) updated since {since}")

    for match_id in updated_matches:
        print(f"   ↳ Fetching MA3 for match {match_id} ...")
        try:
            new_subs = fetch_substitutions(match_id)
        except Exception as e:
            print(f"     ⚠️  MA3 call failed for {match_id}: {e}")
            continue

        old_subs = state.get(match_id, {})
        changes  = find_changes(match_id, old_subs, new_subs)

        if changes:
            print(f"     🔔 {len(changes)} change(s) detected!")
            all_changes.extend(changes)

        # Always update state with latest data
        state[match_id] = new_subs

    save_state(state)
    print(f"💾 State saved ({len(state)} match(es) tracked).")

    if all_changes:
        send_email(all_changes)
    else:
        print("✅ No substitution changes detected.")


if __name__ == "__main__":
    main()
