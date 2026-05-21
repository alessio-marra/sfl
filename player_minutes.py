"""
player_minutes.py
Checks all matches of a season for a given player and reports minutes played.
Called by GitHub Actions with env vars TOURNAMENT_CALENDAR_ID and PLAYER_ID.

XML structures confirmed from live API:
  playerCareer: <playerCareer><person><membership contestantId="..."><stat tournamentCalendarId="..."/></membership>
  matchStats:   <matchStats><liveData><lineUp contestantId="..."><player id="..." minsPlayed="..."/>
"""

import os
import csv
import requests
import xml.etree.ElementTree as ET
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY             = os.environ["SP_API_KEY"]
REFERER             = os.environ["SP_REFERER"]
TOURNAMENT_CALENDAR = os.environ["TOURNAMENT_CALENDAR_ID"]
PLAYER_ID           = os.environ["PLAYER_ID"]

BASE_URL = "https://api.performfeeds.com/soccerdata"
HEADERS  = {"Referer": REFERER}
OUTPUT   = Path("player_minutes_output.csv")


def get(url: str) -> ET.Element:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.text)


# ── Step 1: Player career → find teams in this tournament calendar ─────────────
def get_player_teams() -> dict:
    """
    Returns {contestantId: contestantName} for teams the player belonged to
    in the given tournament calendar.

    XML: <membership contestantId="..." contestantName="...">
           <stat tournamentCalendarId="..." />
         </membership>
    """
    print(f"Fetching player career for {PLAYER_ID} ...")
    url = f"{BASE_URL}/playercareer/{API_KEY}?_fmt=xml&_rt=c&prsn={PLAYER_ID}"
    root = get(url)

    teams = {}
    for membership in root.iter("membership"):
        contestant_id   = membership.get("contestantId")
        contestant_name = membership.get("contestantName", contestant_id)
        for stat in membership.findall("stat"):
            if stat.get("tournamentCalendarId") == TOURNAMENT_CALENDAR:
                if contestant_id and contestant_id not in teams:
                    teams[contestant_id] = contestant_name
                    print(f"  Found team: {contestant_name} ({contestant_id})")

    if not teams:
        print("  WARNING: player has no appearances in this tournament calendar.")
    return teams


# ── Step 2: Match feed → get all match IDs for those teams ────────────────────
def get_match_ids(team_ids: set) -> list[dict]:
    """
    Returns list of match dicts for matches involving the player's team(s).

    XML: <matchInfo id="..." date="...">
           <contestants>
             <contestant id="..." name="..." position="home|away"/>
           </contestants>
         </matchInfo>
    """
    print(f"Fetching match list for tournament calendar {TOURNAMENT_CALENDAR} ...")
    url = (
        f"{BASE_URL}/match/{API_KEY}"
        f"?live=yes&_fmt=xml&_rt=c&_pgSz=100&tmcl={TOURNAMENT_CALENDAR}"
    )
    root = get(url)

    matches = []
    for mi in root.iter("matchInfo"):
        match_id = mi.get("id")
        date     = mi.get("date", "").replace("Z", "")

        contestants = {}
        for c in mi.findall(".//contestant"):
            contestants[c.get("position")] = {
                "id":   c.get("id"),
                "name": c.get("name", c.get("id", "")),
            }

        home = contestants.get("home", {})
        away = contestants.get("away", {})

        involved = {home.get("id"), away.get("id")} & team_ids
        if not involved:
            continue

        matches.append({
            "match_id":  match_id,
            "date":      date,
            "home_id":   home.get("id", ""),
            "home_name": home.get("name", ""),
            "away_id":   away.get("id", ""),
            "away_name": away.get("name", ""),
        })

    matches.sort(key=lambda m: m["date"])
    print(f"  {len(matches)} match(es) found for player's team(s).")
    return matches


# ── Step 3: Match stats → find player minutes ──────────────────────────────────
def get_player_stats(match_id: str) -> dict | None:
    """
    Returns player stats for the match, or None if not in squad.

    XML structure (confirmed from live API):
      <matchStats>
        <liveData>
          <lineUp contestantId="..." contestantName="...">
            <player id="..." position="..." status="Start|Sub" 
                    minsPlayed="..." subOn="0|1" subOff="0|1" shirtNumber="..."/>
          </lineUp>
        </liveData>
      </matchStats>

    status="Start"  → started the match
    status="Sub"    → was a substitute (may or may not have come on)
    subOn="1"       → came on during the match
    minsPlayed      → minutes actually played (0 if listed but didn't play)
    """
    url = (
        f"{BASE_URL}/matchstats/{API_KEY}"
        f"/?detailed=yes&_rt=c&_fmt=xml&fx={match_id}"
    )
    try:
        root = get(url)
    except Exception as e:
        print(f"    matchstats call failed: {e}")
        return None

    for lineup in root.iter("lineUp"):
        team_id   = lineup.get("contestantId", "")
        team_name = lineup.get("contestantName", team_id)

        for player in lineup.iter("player"):
            if player.get("id") != PLAYER_ID:
                continue

            status      = player.get("status", "")       # "Start" or "Sub"
            mins_str    = player.get("minsPlayed", "0")
            sub_on      = player.get("subOn", "0") == "1"
            position    = player.get("position", "")
            shirt       = player.get("shirtNumber", "")

            try:
                mins = int(mins_str)
            except ValueError:
                mins = 0

            return {
                "team_id":    team_id,
                "team_name":  team_name,
                "status":     status,
                "mins_played": mins,
                "sub_on":     sub_on,
                "position":   position,
                "shirt":      shirt,
            }

    return None  # not on team sheet


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"Player ID:            {PLAYER_ID}")
    print(f"Tournament Calendar:  {TOURNAMENT_CALENDAR}")
    print(f"{'='*60}\n")

    teams = get_player_teams()
    if not teams:
        print("No teams found — check TOURNAMENT_CALENDAR_ID and PLAYER_ID.")
        write_output([])
        return

    team_ids = set(teams.keys())

    matches = get_match_ids(team_ids)
    if not matches:
        print("No matches found for this player's team(s).")
        write_output([])
        return

    rows = []
    for m in matches:
        match_label = f"{m['home_name']} vs {m['away_name']}"
        print(f"  [{m['date']}] {match_label} ...", end=" ")

        stats = get_player_stats(m["match_id"])

        if stats is None:
            status   = "Not in squad"
            mins     = ""
            team     = ""
            position = ""
            shirt    = ""
        elif stats["mins_played"] == 0 and not stats["sub_on"]:
            status   = "In squad, did not play"
            mins     = "0"
            team     = stats["team_name"]
            position = stats["position"]
            shirt    = stats["shirt"]
        else:
            mins     = str(stats["mins_played"])
            team     = stats["team_name"]
            position = stats["position"]
            shirt    = stats["shirt"]
            if stats["status"] == "Start":
                status = "Started"
            else:
                status = "Came on as sub"

        print(status + (f" — {mins} min" if mins and mins != "0" else ""))

        rows.append({
            "Date":        m["date"],
            "Match":       match_label,
            "Match ID":    m["match_id"],
            "Team":        team,
            "Position":    position,
            "Shirt":       shirt,
            "Mins Played": mins,
            "Status":      status,
        })

    write_output(rows)

    total_mins = sum(int(r["Mins Played"]) for r in rows if r["Mins Played"] not in ("", "0"))
    played     = sum(1 for r in rows if r["Status"] in ("Started", "Came on as sub"))
    print(f"\n{'='*60}")
    print(f"Matches in squad: {sum(1 for r in rows if r['Status'] != 'Not in squad')}")
    print(f"Matches played:   {played}")
    print(f"Total minutes:    {total_mins}")
    print(f"Output written to {OUTPUT}")
    print(f"{'='*60}")


def write_output(rows: list[dict]):
    fieldnames = ["Date", "Match", "Match ID", "Team", "Position", "Shirt", "Mins Played", "Status"]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
