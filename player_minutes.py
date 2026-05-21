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
"""

import os
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

    if isinstance(raw, list):
        players = raw
    else:
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


# ── Global Match Stats Cache (Critical Optimization) ───────────────────────────
match_stats_cache = {}

def get_match_stats(match_id: str) -> dict:
    """
    Fetches match stats for a match_id ONCE and caches the results for all players.
    Drastically drops processing time from 11+ minutes to seconds.
    """
    if match_id in match_stats_cache:
        return match_stats_cache[match_id]

    url = f"{BASE_URL}/matchstats/{API_KEY}/?detailed=yes&_rt=c&_fmt=xml&fx={match_id}"
    try:
        root = get(url)
        lineups = list(root.iter("lineUp"))
        if not lineups:
            res = {"is_played": False, "players": {}}
        else:
            players_map = {}
            for lineup in lineups:
                team_id   = lineup.get("contestantId", "")
                team_name = lineup.get("contestantName", team_id)

                for player in lineup.iter("player"):
                    pid = player.get("playerId")
                    if not pid:
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

                    players_map[pid] = {
                        "team_id":     team_id,
                        "team_name":   team_name,
                        "player_name": match_name,
                        "position":    position,
                        "shirt":       shirt,
                        "mins_played": mins,
                    }
            res = {"is_played": len(players_map) > 0, "players": players_map}
    except Exception as e:
        print(f"    matchstats failed for {match_id}: {e}")
        res = {"is_played": False, "players": {}}

    match_stats_cache[match_id] = res
    return res


# ── HTML output ────────────────────────────────────────────────────────────────
def build_html(report: dict, tmcl_names: dict, all_weeks_by_tmcl: dict) -> str:
    tabs_html = ""
    panels_html = ""

    for i, (tmcl_id, clubs) in enumerate(report.items()):
        active_tab   = "active" if i == 0 else ""
        active_panel = "block" if i == 0 else "none"
        tmcl_label   = tmcl_names.get(tmcl_id, tmcl_id)
        weeks        = all_weeks_by_tmcl.get(tmcl_id, [])

        tabs_html += f'<button class="tab-btn {active_tab}" onclick="showTab(event, \'{tmcl_id}\')">{tmcl_label}</button>'

        # Calculate club totals for sorting clubs by most total minutes first
        club_totals = {}
        for club_name, players_dict in clubs.items():
            club_total = 0
            for pname, pdata in players_dict.items():
                for m in pdata["matches"]:
                    if m["status"] not in ["Match not played yet", "Not in squad"]:
                        club_total += (m["mins"] or 0)
            club_totals[club_name] = club_total

        # Sort clubs descending by total minutes
        sorted_clubs = sorted(clubs.items(), key=lambda item: club_totals[item[0]], reverse=True)

        clubs_html = ""
        for club_name, players_dict in sorted_clubs:
            week_headers = "".join(f'<th class="week-th">W{w}</th>' for w in weeks)

            # Sort players descending by total minutes
            def get_player_total(pdata):
                total = 0
                for m in pdata["matches"]:
                    if m["status"] not in ["Match not played yet", "Not in squad"]:
                        total += (m["mins"] or 0)
                return total

            sorted_players = sorted(players_dict.items(), key=lambda item: get_player_total(item[1]), reverse=True)

            rows_html = ""
            for pname, pdata in sorted_players:
                # Group matches by week to support double matchweeks properly
                week_map = defaultdict(list)
                for m in pdata["matches"]:
                    week_map[m["week"]].append(m)

                cells = ""
                total = 0
                for w in weeks:
                    matches_in_week = week_map.get(w, [])
                    if not matches_in_week:
                        cells += '<td class="mins-cell empty-cell">—</td>'
                    elif all(m["status"] == "Match not played yet" for m in matches_in_week):
                        cells += '<td class="mins-cell unplayed-cell"></td>'
                    else:
                        played_matches = [m for m in matches_in_week if m["status"] not in ["Match not played yet", "Not in squad"]]
                        if played_matches:
                            week_mins = sum(m["mins"] or 0 for m in played_matches)
                            total += week_mins
                            cells += f'<td class="mins-cell normal-cell">{week_mins}</td>'
                        else:
                            cells += '<td class="mins-cell empty-cell">—</td>'

                shirt_str = f'#{pdata["shirt"]} ' if pdata.get("shirt") else ""
                rows_html += f"""
                <tr>
                  <td class="player-name">{shirt_str}{pname}</td>
                  {cells}
                  <td class="mins-cell total">{total}</td>
                </tr>"""

            clubs_html += f"""
            <div class="club-section">
              <div class="club-header" onclick="toggleClub(this)">
                <span class="arrow">▶</span> {club_name} <span class="club-total-badge">({club_totals[club_name]} mins tracked)</span>
              </div>
              <div class="club-body" style="display:none;">
                <table class="mins-table">
                  <thead>
                    <tr>
                      <th class="player-th">Player</th>
                      {week_headers}
                      <th class="week-th">Total</th>
                    </tr>
                  </thead>
                  <tbody>{rows_html}</tbody>
                </table>
              </div>
            </div>"""

        panels_html += f"""
        <div id="panel-{tmcl_id}" class="tab-panel" style="display:{active_panel};">
          <h2 class="tmcl-title">{tmcl_label}</h2>
          {clubs_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Player Minutes Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; background: #f3f4f6; color: #1f2937; }}
  .header {{ background: #1e3a5f; color: #fff; padding: 20px 32px; }}
  .header h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .header p {{ font-size: 12px; color: #93c5fd; text-transform: uppercase; letter-spacing: 1px; }}
  .tabs {{ background: #fff; border-bottom: 2px solid #e5e7eb; padding: 0 32px; display: flex; gap: 4px; }}
  .tab-btn {{ padding: 14px 24px; border: none; background: none; cursor: pointer;
              font-size: 14px; font-weight: 600; color: #6b7280; border-bottom: 3px solid transparent;
              margin-bottom: -2px; transition: all .15s; }}
  .tab-btn:hover {{ color: #1e3a5f; }}
  .tab-btn.active {{ color: #1e3a5f; border-bottom-color: #1e3a5f; }}
  .content {{ padding: 24px 32px; max-width: 100%; overflow-x: auto; }}
  .tmcl-title {{ font-size: 16px; font-weight: 700; margin-bottom: 16px; color: #1e3a5f; }}
  .club-section {{ margin-bottom: 12px; border: 1px solid #e5e7eb; border-radius: 8px;
                   overflow: hidden; background: #fff; }}
  .club-header {{ padding: 12px 16px; background: #f1f5f9; cursor: pointer;
                  font-weight: 700; font-size: 14px; display: flex; align-items: center;
                  gap: 8px; user-select: none; }}
  .club-header:hover {{ background: #e2e8f0; }}
  .club-total-badge {{ font-size: 12px; font-weight: normal; color: #6b7280; margin-left: auto; }}
  .arrow {{ font-size: 11px; transition: transform .2s; display: inline-block; }}
  .arrow.open {{ transform: rotate(90deg); }}
  .club-body {{ overflow-x: auto; }}
  .mins-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .player-th {{ padding: 8px 12px; text-align: left; background: #f8fafc;
                font-size: 11px; text-transform: uppercase; color: #6b7280;
                letter-spacing: .5px; white-space: nowrap; min-width: 160px; }}
  .week-th {{ padding: 8px 6px; text-align: center; background: #f8fafc;
              font-size: 11px; text-transform: uppercase; color: #6b7280;
              letter-spacing: .5px; min-width: 42px; }}
  .player-name {{ padding: 8px 12px; font-size: 13px; white-space: nowrap;
                  border-bottom: 1px solid #f1f5f9; }}
  .mins-cell {{ padding: 6px 4px; text-align: center; font-size: 12px; font-weight: 600;
                border-bottom: 1px solid #f1f5f9; background: #fff; color: #1f2937; }}
  .mins-cell.unplayed-cell {{ background: #e5e7eb; color: #9ca3af; }}
  .mins-cell.empty-cell {{ color: #9ca3af; font-weight: normal; }}
  .mins-cell.normal-cell {{ color: #1f2937; }}
  .mins-cell.total  {{ color: #1e3a5f; background: #eff6ff; font-weight: 700; }}
</style>
</head>
<body>
<div class="header">
  <p>Stats Perform Monitor</p>
  <h1>Player Minutes Report</h1>
</div>
<div class="tabs">{tabs_html}</div>
<div class="content">
  {panels_html}
</div>
<script>
  function showTab(event, tmclId) {{
    document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('panel-' + tmclId).style.display = 'block';
    event.target.classList.add('active');
  }}
  function toggleClub(header) {{
    const body = header.nextElementSibling;
    const arrow = header.querySelector('.arrow');
    const isOpen = body.style.display !== 'none';
    body.style.display = isOpen ? 'none' : 'block';
    arrow.classList.toggle('open', !isOpen);
  }}
</script>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"Tournament Calendar 1: {TMCL_1}")
    print(f"Tournament Calendar 2: {TMCL_2}")
    print(f"Players file:          {PLAYERS_FILE}")
    print(f"{'='*60}\n")

    mapping = load_mapping()
    print(f"Competition mapping loaded: {len(mapping)} entries")

    print("Loading players ...")
    players = load_players()
    if not players:
        print("No eligible players found.")
        return

    unknown_comp_ids = set()
    for p in players:
        cid = p.get("competitionId", "")
        if cid and cid not in mapping:
            unknown_comp_ids.add(cid)

    if unknown_comp_ids:
        print("\n" + "="*60)
        print("ERROR: Unknown competitionId(s) found in players file:")
        for cid in unknown_comp_ids:
            print(f"  {cid}")
        print("\nPlease add them to competition_mapping.json in your repo:")
        print(json.dumps({cid: "<SP_tournamentCalendarId>" for cid in unknown_comp_ids}, indent=2))
        print(f"\nAvailable tournament calendars: {TMCL_1}, {TMCL_2}")
        print("="*60)
        exit(1)

    print("\nFetching match lists ...")
    report = {tmcl: {} for tmcl in TMCLS}
    tmcl_names = {}
    tmcl_matches = {}

    for tmcl_id in TMCLS:
        url = (
            f"{BASE_URL}/match/{API_KEY}"
            f"?live=yes&_fmt=xml&_rt=c&_pgSz=100&tmcl={tmcl_id}"
        )
        try:
            root = get(url)
        except Exception as e:
            print(f"  match feed failed for {tmcl_id}: {e}")
            tmcl_matches[tmcl_id] = []
            continue

        comp_name = ""
        season_name = ""
        for comp in root.iter("competition"):
            comp_name = comp.get("name", "")
            if comp_name:
                break
        for tc in root.iter("tournamentCalendar"):
            season_name = tc.text or ""
            if season_name:
                break

        if comp_name and season_name:
            tmcl_names[tmcl_id] = f"{comp_name} {season_name}"
        elif comp_name:
            tmcl_names[tmcl_id] = comp_name
        elif season_name:
            tmcl_names[tmcl_id] = season_name
        else:
            tmcl_names[tmcl_id] = tmcl_id

        matches = []
        for mi in root.iter("matchInfo"):
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

            m = {
                "match_id":  match_id,
                "date":      date,
                "week":      week,
                "home_id":   home.get("id", ""),
                "home_name": home.get("name", ""),
                "away_id":   away.get("id", ""),
                "away_name": away.get("name", ""),
                "tmcl_id":   tmcl_id,
            }
            matches.append(m)

        tmcl_matches[tmcl_id] = sorted(matches, key=lambda x: x["date"])
        print(f"  {tmcl_id} ({tmcl_names[tmcl_id]}): {len(matches)} matches")

    # Collect all unique weeks from the competition feed to show every matchday
    all_weeks_by_tmcl = {}
    for tmcl_id in TMCLS:
        weeks = set()
        for m in tmcl_matches.get(tmcl_id, []):
            if m["week"]:
                weeks.add(m["week"])
        all_weeks_by_tmcl[tmcl_id] = sorted(list(weeks), key=lambda w: int(w) if w.isdigit() else 0)

    print(f"\nProcessing {len(players)} players ...")
    total = len(players)

    for idx, player in enumerate(players, 1):
        player_id  = player.get("id", "")
        first_name = player.get("firstName", "")
        last_name  = player.get("lastName", "")
        comp_id    = player.get("competitionId", "")

        if not player_id:
            continue

        player_tmcl = mapping.get(comp_id)
        if not player_tmcl:
            continue

        teams, sp_name = get_player_teams(player_id, {player_tmcl})
        display_name = sp_name or f"{first_name} {last_name}".strip()

        if not teams:
            print(f"  [{idx}/{total}] {display_name} — no team found in tmcl, skipping")
            continue

        print(f"  [{idx}/{total}] {display_name} — teams: {', '.join(teams.values())}")
        team_ids = set(teams.keys())

        relevant_matches = [
            m for m in tmcl_matches.get(player_tmcl, [])
            if {m["home_id"], m["away_id"]} & team_ids
        ]

        if not relevant_matches:
            continue

        for m in relevant_matches:
            match_id = m["match_id"]
            stats_res = get_match_stats(match_id)

            player_team_id = None
            for tid in team_ids:
                if tid in {m["home_id"], m["away_id"]}:
                    player_team_id = tid
                    break
            club_name = teams.get(player_team_id, list(teams.values())[0])

            if not stats_res["is_played"]:
                status = "Match not played yet"
                mins   = None
                shirt  = ""
            else:
                players_map = stats_res["players"]
                if player_id not in players_map:
                    status = "Not in squad"
                    mins   = None
                    shirt  = ""
                else:
                    p_stats = players_map[player_id]
                    shirt = p_stats["shirt"]
                    if p_stats["mins_played"] == 0:
                        status = "In squad, did not play"
                        mins   = 0
                    elif p_stats["position"] == "Substitute":
                        status = "Came on as sub"
                        mins   = p_stats["mins_played"]
                    else:
                        status = "Started"
                        mins   = p_stats["mins_played"]

            match_label = f"{m['home_name']} vs {m['away_name']}"

            if club_name not in report[player_tmcl]:
                report[player_tmcl][club_name] = {}
            if display_name not in report[player_tmcl][club_name]:
                report[player_tmcl][club_name][display_name] = {
                    "player_id": player_id,
                    "shirt":     shirt,
                    "matches":   []
                }
            elif shirt and not report[player_tmcl][club_name][display_name]["shirt"]:
                report[player_tmcl][club_name][display_name]["shirt"] = shirt

            report[player_tmcl][club_name][display_name]["matches"].append({
                "week":   m["week"],
                "date":   m["date"],
                "match":  match_label,
                "mins":   mins,
                "status": status,
            })

    print("\nBuilding HTML report ...")
    html = build_html(report, tmcl_names, all_weeks_by_tmcl)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Report written to {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
