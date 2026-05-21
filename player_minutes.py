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
            # Try to auto-map: if only 2 tmcls, map in order
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
def get_
