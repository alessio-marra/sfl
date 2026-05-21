"""
player_minutes.py
Checks all matches of a season for a given player and reports minutes played.
Called by GitHub Actions with env vars TOURNAMENT_CALENDAR_ID and PLAYER_ID.

Confirmed XML structures:

  playerCareer:
    <playerCareer>
      <person id="..." matchName="...">
        <membership contestantId="..." contestantName="...">
          <stat tournamentCalendarId="..."/>
        </membership>
      </person>
    </playerCareer>

  match:
    <match>
      <matchInfo id="..." date="...">
        <contestants>
          <contestant id="..." name="..." position="home|away"/>
        </contestants>
      </matchInfo>
    </match>

  matchStats:
    <matchStats>
      <liveData>
        <lineUp contestantId="..." contestantName="...">
          <player playerId="..." matchName="..." position="..." shirtNumber="...">
            <stat type="minsPlayed">32</stat>
          </player>
        </lineUp>
      </liveData>
    </matchStats>
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
def get_player_teams() -> tuple[dict, str]:
    """
    Returns ({contestantId: contestantName}, player_display_name)
    for teams the player belonged to in the given tournament calendar.
    """
    print(f"Fetching player career for {PLAYER_ID} ...")
    url = f"{BASE_URL}/playercareer/{API_KEY}?_fmt=xml&_rt=c&prsn={PLAYER_ID}"
    root = get(url)

    player_name = ""
    person = root.find("person")
    if person is not None:
        player_name = person.get("matchName", "")

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

    return teams, player_name


# ── Step 2: Match feed → get all match IDs for those teams ────────────────────
def get_match_ids(team_ids: set) -> list[dict]:
    """
    Returns sorted list of match dicts for matches involving the player's team(s).
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
                "id":   c.get("id", ""),
                "name": c.get("name", ""),
            }

        home = contestants.get("home", {})
        away = contestants.get("away", {})

        # Only keep matches where at least one of the player's teams played
        if not ({home.get("id"), away.get("id")} & team_ids):
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
    Returns player stats dict for the match, or None if not in squad.

    Confirmed structure:
      <lineUp contestantId="..." contestantName="...">
        <player playerId="..." matchName="..." position="..." shirtNumber="...">
          <stat type="minsPlayed">32</stat>
        </player>
      </lineUp>

    position values:
      "Goalkeeper", "Defender", "Midfielder", "Attacker" → started
      "Substitute" → listed as sub (minsPlayed=0 means didn't come on)
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
            if player.get("playerId") != PLAYER_ID:
                continue

            position   = player.get("position", "")
            shirt      = player.get("shirtNumber", "")
            match_name = player.get("matchName", "")

            # minsPlayed is a child <stat type="minsPlayed"> element
            mins = 0
            for stat in player.findall("stat"):
                if stat.get("type") == "minsPlayed":
                    try:
                        mins = int(stat.text)
                    except (ValueError, TypeError):
                        mins = 0
                    break

            return {
                "team_id":     team_id,
                "team_name":   team_name,
                "player_name": match_name,
                "position":    position,
                "shirt":       shirt,
                "mins_played": mins,
            }

    return None  # not on team sheet at all


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"Player ID:            {PLAYER_ID}")
    print(f"Tournament Calendar:  {TOURNAMENT_CALENDAR}")
    print(f"{'='*60}\n")

    teams, player_name = get_player_teams()
    if not teams:
        print("No teams found — check TOURNAMENT_CALENDAR_ID and PLAYER_ID.")
        write_output([])
        return

    print(f"  Player name: {player_name}\n")
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
            status      = "Not in squad"
            mins        = ""
            team        = ""
            position    = ""
            shirt       = ""
            player_disp = player_name
        else:
            team        = stats["team_name"]
            position    = stats["position"]
            shirt       = stats["shirt"]
            player_disp = stats["player_name"] or player_name
            mins        = str(stats["mins_played"])

            if stats["mins_played"] == 0:
                status = "In squad, did not play"
            elif position == "Substitute":
                status = "Came on as sub"
            else:
                status = "Started"

        print(status + (f" — {mins} min" if mins and mins != "0" else ""))

        rows.append({
            "Date":        m["date"],
            "Match":       match_label,
            "Match ID":    m["match_id"],
            "Player":      player_disp,
            "Team":        team,
            "Position":    position,
            "Shirt":       shirt,
            "Mins Played": mins,
            "Status":      status,
        })

    write_output(rows)

    total_mins = sum(int(r["Mins Played"]) for r in rows if r["Mins Played"] not in ("", "0"))
    played     = sum(1 for r in rows if r["Status"] in ("Started", "Came on as sub"))
    in_squad   = sum(1 for r in rows if r["Status"] != "Not in squad")

    print(f"\n{'='*60}")
    print(f"Player:           {player_name}")
    print(f"Matches in squad: {in_squad}")
    print(f"Matches played:   {played}")
    print(f"Total minutes:    {total_mins}")
    print(f"Output:           {OUTPUT}")
    print(f"{'='*60}")


def write_output(rows: list[dict]):
    fieldnames = [
        "Date", "Match", "Match ID", "Player",
        "Team", "Position", "Shirt", "Mins Played", "Status"
    ]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
