"""
player_minutes.py
Checks all matches of a season for a given player and reports minutes played.
Usage: called by GitHub Actions with env vars TOURNAMENT_CALENDAR_ID and PLAYER_ID.
"""

import os
import csv
import requests
import xml.etree.ElementTree as ET
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY              = os.environ["SP_API_KEY"]
REFERER              = os.environ["SP_REFERER"]
TOURNAMENT_CALENDAR  = os.environ["TOURNAMENT_CALENDAR_ID"]
PLAYER_ID            = os.environ["PLAYER_ID"]

BASE_URL  = "https://api.performfeeds.com/soccerdata"
HEADERS   = {"Referer": REFERER}
OUTPUT    = Path("player_minutes_output.csv")


def get(url: str) -> ET.Element:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.text)


# ── Step 1: Player career → find teams in this tournament calendar ────────────
def get_player_teams() -> dict:
    """Returns {contestantId: contestantName} for teams in the given tournament calendar."""
    print(f"Fetching player career for {PLAYER_ID} ...")
    url = f"{BASE_URL}/playercareer/{API_KEY}?_fmt=xml&_rt=c&prsn={PLAYER_ID}"
    root = get(url)

    teams = {}
    for spell in root.iter("spell"):
        tmcl = spell.get("tournamentCalendarId", "")
        if tmcl == TOURNAMENT_CALENDAR:
            contestant_id   = spell.get("contestantId")
            contestant_name = spell.get("contestantName", contestant_id)
            if contestant_id:
                teams[contestant_id] = contestant_name
                print(f"  Found team: {contestant_name} ({contestant_id})")

    if not teams:
        print("  WARNING: player has no spells in this tournament calendar.")
    return teams


# ── Step 2: Match feed → get all match IDs for those teams ───────────────────
def get_match_ids(team_ids: set) -> list[dict]:
    """Returns list of {match_id, date, home_id, home_name, away_id, away_name}."""
    print(f"Fetching match list for tournament calendar {TOURNAMENT_CALENDAR} ...")
    url = (
        f"{BASE_URL}/match/{API_KEY}"
        f"?live=yes&_fmt=xml&_rt=c&_pgSz=500&tmcl={TOURNAMENT_CALENDAR}"
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
                "name": c.get("name", c.get("id")),
            }

        home = contestants.get("home", {})
        away = contestants.get("away", {})

        # Only keep matches where at least one of the player's teams is involved
        involved = {home.get("id"), away.get("id")} & team_ids
        if not involved:
            continue

        matches.append({
            "match_id":  match_id,
            "date":      date,
            "home_id":   home.get("id"),
            "home_name": home.get("name", ""),
            "away_id":   away.get("id"),
            "away_name": away.get("name", ""),
        })

    matches.sort(key=lambda m: m["date"])
    print(f"  {len(matches)} match(es) found for player's team(s).")
    return matches


# ── Step 3: Match stats → find player minutes ─────────────────────────────────
def get_player_minutes(match_id: str) -> dict | None:
    """
    Returns dict with player stats for the given match, or None if not in squad.
    {
      "mins_played": int or None,
      "in_lineup": bool,
      "team_id": str,
      "team_name": str,
      "position": str,
    }
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

    for team_el in root.iter("contestantMatchStats"):
        team_id   = team_el.get("contestantId", "")
        team_name = team_el.get("contestantName", team_id)

        for player_el in team_el.iter("player"):
            if player_el.get("playerId") != PLAYER_ID:
                continue

            # Found the player — check lineup status
            in_lineup  = player_el.get("position") not in (None, "", "substitute") or \
                         player_el.get("subOn") == "yes" or \
                         _get_stat(player_el, "minsPlayed") is not None

            mins_played = _get_stat(player_el, "minsPlayed")
            position    = player_el.get("position", "")

            # Determine if actually in lineup vs. on bench unused
            played       = mins_played is not None and int(mins_played) > 0
            in_lineup_flag = player_el.get("formation_place") not in (None, "0") or played

            return {
                "team_id":    team_id,
                "team_name":  team_name,
                "in_lineup":  in_lineup_flag,
                "mins_played": int(mins_played) if mins_played is not None else None,
                "position":   position,
            }

    return None  # not in squad at all


def _get_stat(player_el: ET.Element, stat_type: str) -> str | None:
    for stat in player_el.findall("stat"):
        if stat.get("type") == stat_type:
            return stat.text
    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"Player ID:            {PLAYER_ID}")
    print(f"Tournament Calendar:  {TOURNAMENT_CALENDAR}")
    print(f"{'='*60}\n")

    # Step 1
    teams = get_player_teams()
    if not teams:
        print("No teams found — check TOURNAMENT_CALENDAR_ID and PLAYER_ID.")
        write_empty_output()
        return

    team_ids = set(teams.keys())

    # Step 2
    matches = get_match_ids(team_ids)
    if not matches:
        print("No matches found for this player's team(s).")
        write_empty_output()
        return

    # Step 3
    rows = []
    for m in matches:
        match_label = f"{m['home_name']} vs {m['away_name']}"
        print(f"  [{m['date']}] {match_label} ({m['match_id']}) ...", end=" ")

        stats = get_player_minutes(m["match_id"])

        if stats is None:
            status     = "Not in squad"
            mins       = ""
            team_name  = ""
            position   = ""
        elif not stats["in_lineup"] and (stats["mins_played"] is None or stats["mins_played"] == 0):
            status     = "In squad, did not play"
            mins       = "0"
            team_name  = stats["team_name"]
            position   = stats["position"]
        else:
            mins      = str(stats["mins_played"]) if stats["mins_played"] is not None else "0"
            team_name = stats["team_name"]
            position  = stats["position"]
            status    = "Played"

        print(status + (f" ({mins} min)" if mins and mins != "0" else ""))

        rows.append({
            "Date":        m["date"],
            "Match":       match_label,
            "Match ID":    m["match_id"],
            "Team":        team_name,
            "Position":    position,
            "Mins Played": mins,
            "Status":      status,
        })

    # Write CSV — always overwrite
    write_output(rows)
    print(f"\nDone. {len(rows)} matches written to {OUTPUT}.")


def write_output(rows: list[dict]):
    fieldnames = ["Date", "Match", "Match ID", "Team", "Position", "Mins Played", "Status"]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_empty_output():
    write_output([])


if __name__ == "__main__":
    main()
