"""
player_minutes.py
Checks all matches across 2 tournament calendars for all eligible players
in an uploaded JSON file. Outputs an interactive HTML report grouped by club.

Inputs (env vars):
  SP_API_KEY, SP_REFERER         - Stats Perform credentials
  TOURNAMENT_CALENDAR_1          - First tournament calendar ID
  TOURNAMENT_CALENDAR_2          - Second tournament calendar ID
  PLAYERS_FILE                   - Path to uploaded JSON players file

Persistent file (committed to repo):
  competition_mapping.json       - Maps internal competitionId -> SP tournamentCalendarId
                                   Edit manually if script reports unknown competitionId

XML structures used:
  playerCareer:  <membership contestantId="..." contestantName="...">
                   <stat tournamentCalendarId="..."/>
  match:         <matchInfo id="..." date="..." week="...">
                   <contestant id="..." name="..." position="home|away"/>
  matchStats:    <lineUp contestantId="..." contestantName="...">
                   <player playerId="..." matchName="..." position="..." shirtNumber="...">
                     <stat type="minsPlayed">32</stat>
"""

import os
import csv
import json
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY          = os.environ["SP_API_KEY"]
REFERER          = os.environ["SP_REFERER"]
TMCL_1           = os.environ["TOURNAMENT_CALENDAR_1"]
TMCL_2           = os.environ["TOURNAMENT_CALENDAR_2"]
PLAYERS_FILE     = os.environ["PLAYERS_FILE"]
MAPPING_FILE     = Path("competition_mapping.json")
OUTPUT_HTML      = Path("player_minutes_output.html")

BASE_URL = "https://api.performfeeds.com/soccerdata"
HEADERS  = {"Referer": REFERER}
TMCLS    = [t.strip() for t in [TMCL_1, TMCL_2] if t.strip()]


def get(url: str) -> ET.Element:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.text)


# ── Competition mapping ────────────────────────────────────────────────────────
def load_mapping() -> dict:
    if MAPPING_FILE.exists():
        return json.loads(MAPPING_FILE.read_text())
    return {}


def save_mapping(mapping: dict):
    MAPPING_FILE.write_text(json.dumps(mapping, indent=2))


# ── Load players JSON ──────────────────────────────────────────────────────────
def load_players() -> list[dict]:
    """Load eligible, non-blacklisted players from the uploaded JSON file."""
    raw = json.loads(Path(PLAYERS_FILE).read_text())

    # Support both a bare list and a dict wrapping a list
    if isinstance(raw, list):
        players = raw
    else:
        # Try to find the list inside the dict
        for v in raw.values():
            if isinstance(v, list):
                players = v
                break
        else:
            players = []

    eligible = [p for p in players if not p.get("isBlacklisted", False)]
    blacklisted = len(players) - len(eligible)
    print(f"  Loaded {len(eligible)} eligible players ({blacklisted} blacklisted, skipped).")
    return eligible


# ── Resolve competition mapping ────────────────────────────────────────────────
def resolve_tmcl_ids(players: list[dict], mapping: dict) -> tuple[dict, list[str]]:
    """
    Maps each player's internal competitionId to a SP tournamentCalendarId.
    Returns (updated_mapping, list_of_unknown_comp_ids).
    """
    unknown = []
    all_comp_ids = {p["competitionId"] for p in players if "competitionId" in p}

    for comp_id in all_comp_ids:
        if comp_id not in mapping:
            unknown.append(comp_id)

    return mapping, unknown


# ── Step 1: Player career ──────────────────────────────────────────────────────
def get_player_teams(player_id: str, tmcl_ids: set) -> tuple[dict, str]:
    """Returns ({contestantId: contestantName}, player_display_name)."""
    url = f"{BASE_URL}/playercareer/{API_KEY}?_fmt=xml&_rt=c&prsn={player_id}"
    try:
        root = get(url)
    except Exception as e:
        print(f"    playercareer failed: {e}")
        return {}, ""

    player_name = ""
    person = root.find("person")
    if person is not None:
        player_name = person.get("matchName", "")

    teams = {}
    for membership in root.iter("membership"):
        contestant_id   = membership.get("contestantId")
        contestant_name = membership.get("contestantName", contestant_id)
        for stat in membership.findall("stat"):
            if stat.get("tournamentCalendarId") in tmcl_ids:
                if contestant_id and contestant_id not in teams:
                    teams[contestant_id] = contestant_name

    return teams, player_name


# ── Step 2: Match feed ─────────────────────────────────────────────────────────
def get_matches_for_tmcl(tmcl_id: str, team_ids: set) -> list[dict]:
    """Returns sorted list of match dicts for a tournament calendar with pagination."""
    matches = []
    page_num = 1
    
    while True:
        url = (
            f"{BASE_URL}/match/{API_KEY}"
            f"?live=yes&_fmt=xml&_rt=c&_pgSz=100&_pgNm={page_num}&tmcl={tmcl_id}"
        )
        try:
            root = get(url)
        except Exception as e:
            print(f"    match feed failed for {tmcl_id} on page {page_num}: {e}")
            break

        page_matches_found = 0
        for mi in root.iter("matchInfo"):
            page_matches_found += 1
            match_id = mi.get("id")
            date     = mi.get("date", "").replace("Z", "")
            week     = mi.get("week", "")

            contestants = {}
            for c in mi.findall(".//contestant"):
                contestants[c.get("position")] = {
                    "id":   c.get("id", ""),
                    "name": c.get("name", ""),
                }

            home = contestants.get("home", {})
            away = contestants.get("away", {})

            if not ({home.get("id"), away.get("id")} & team_ids):
                continue

            matches.append({
                "match_id":  match_id,
                "date":      date,
                "week":      week,
                "home_id":   home.get("id", ""),
                "home_name": home.get("name", ""),
                "away_id":   away.get("id", ""),
                "away_name": away.get("name", ""),
                "tmcl_id":   tmcl_id,
            })

        if page_matches_found == 0:
            break
        page_num += 1

    return matches


# ── Step 3: Match stats ────────────────────────────────────────────────────────
def get_player_stats(match_id: str, player_id: str) -> dict | None:
    """Returns player stats dict or None if not in squad."""
    url = (
        f"{BASE_URL}/matchstats/{API_KEY}"
        f"/?detailed=yes&_rt=c&_fmt=xml&fx={match_id}"
    )
    try:
        root = get(url)
    except Exception as e:
        print(f"    matchstats failed for {match_id}: {e}")
        return None

    for lineup in root.iter("lineUp"):
        team_id   = lineup.get("contestantId", "")
        team_name = lineup.get("contestantName", team_id)

        for player in lineup.iter("player"):
            if player.get("playerId") != player_id:
                continue

            position   = player.get("position", "")
            shirt      = player.get("shirtNumber", "")
            match_name = player.get("matchName", "")

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

    return None


# ── HTML output ────────────────────────────────────────────────────────────────
def build_html(report: dict, tmcl_names: dict) ->
