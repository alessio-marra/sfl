"""
player_minutes.py
Stateful Automated Minutes Pipeline (Match-First Architecture)
- Autodetects active season tournament calendars via OT2 feed.
- Bootstraps full historic stats on initial run.
- Uses MAR incremental change tracking for subsequent daily runs.
- Removes competition constraints, color elements, and tracking data overhead.
"""

import os
import json
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── Configuration ─────────────────────────────────────────────────────────────
API_KEY          = os.environ["SP_API_KEY"]
REFERER          = os.environ["SP_REFERER"]
PLAYERS_FILE     = os.environ.get("PLAYERS_FILE", "players.json")

STATE_FILE       = Path("minutes_state.json")
OUTPUT_HTML      = Path("index.html")
BASE_URL         = "https://api.performfeeds.com/soccerdata"
HEADERS          = {"Referer": REFERER}

# Fixed structural Competition Constants
COMPETITIONS = {
    "e0lck99w8meo9qoalfrxgo33o": "Super League",
    "8v97rcbthsxmzqk4ufxws9mug": "Challenge League"
}


def get_xml(url: str) -> ET.Element:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.text)


def load_eligible_player_ids() -> set[str]:
    p_path = Path(PLAYERS_FILE)
    if not p_path.exists():
        print(f"CRITICAL: {PLAYERS_FILE} not found.")
        return set()
    raw = json.loads(p_path.read_text())
    # Handle list or nested object profiles safely
    players = raw if isinstance(raw, list) else next(iter(raw.values()), [])
    return {p["id"] for p in players if p.get("id") and not p.get("isBlacklisted", False)}


def fetch_active_tournament_calendars() -> dict[str, str]:
    """Queries OT2 feed to dynamically discover the active season calendar IDs."""
    print("Fetching active season IDs from OT2 feed...")
    url = f"{BASE_URL}/tournamentcalendar/{API_KEY}/active/authorized?_fmt=xml&_rt=c"
    root = get_xml(url)
    
    discovered = {}
    for comp in root.iter("competition"):
        comp_id = comp.get("id")
        if comp_id in COMPETITIONS:
            for tc in comp.findall("tournamentCalendar"):
                if tc.get("active") == "yes":
                    discovered[comp_id] = tc.get("id")
                    print(f"  -> Found Active Calendar for {COMPETITIONS[comp_id]}: {tc.get('id')} ({tc.get('name')})")
    return discovered


def fetch_mar_updated_fixtures(lookback_hours: int = 25) -> set[str]:
    """Queries MAR feed to discover which matches were updated within the lookback window."""
    now_utc = datetime.now(timezone.utc)
    since_str = (now_utc - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Checking MAR feed for changes since: {since_str}")
    
    url = f"{BASE_URL}/matchreference/{API_KEY}/?type=matchstats&since={since_str}&_fmt=xml&_rt=c"
    try:
        root = get_xml(url)
    except Exception as e:
        print(f"  MAR call skipped or failed: {e}")
        return set()

    updated_ids = set()
    for match in root.iter("matchInfo"):
        m_id = match.get("id")
        if m_id:
            updated_ids.add(m_id)
    print(f"  -> MAR flagged {len(updated_ids)} globally modified match(es).")
    return updated_ids


def fetch_entire_calendar_fixtures(tmcl_id: str) -> list[dict]:
    """Fetches every scheduled fixture inside a calendar (Used during Bootstrap)."""
    matches = []
    page_num = 1
    while True:
        url = f"{BASE_URL}/match/{API_KEY}?live=yes&_fmt=xml&_rt=c&_pgSz=100&_pgNm={page_num}&tmcl={tmcl_id}"
        root = get_xml(url)
        
        page_count = 0
        for mi in root.iter("matchInfo"):
            page_count += 1
            contestants = {}
            for c in mi.findall(".//contestant"):
                contestants[c.get("position")] = c.get("name", "")

            matches.append({
                "match_id": mi.get("id"),
                "date": mi.get("date", "").replace("Z", ""),
                "week": mi.get("week", ""),
                "home_name": contestants.get("home", ""),
                "away_name": contestants.get("away", ""),
                "tmcl_id": tmcl_id
            })
            
        if page_count == 0:
            break
        page_num += 1
    return matches


def process_match_sheet(match_id: str, eligible_ids: set[str], fallback_info: dict) -> dict:
    """Parses a specific matchstats file and extractions metrics for targeted player IDs."""
    url = f"{BASE_URL}/matchstats/{API_KEY}/?detailed=yes&_rt=c&_fmt=xml&fx={match_id}"
    try:
        root = get_xml(url)
    except Exception as e:
        print(f"    Failed to download match sheet {match_id}: {e}")
        return {}

    match_players_data = {}
    
    for lineup in root.iter("lineUp"):
        # Prioritize structural officialName over shorthand elements
        club_name = lineup.get("officialName") or lineup.get("contestantName") or "Unknown Club"
        
        for p in lineup.iter("player"):
            pid = p.get("playerId")
            if pid in eligible_ids:
                mins = 0
                for stat in p.findall("stat"):
                    if stat.get("type") == "minsPlayed":
                        try:
                            mins = int(stat.text)
                        except:
                            mins = 0
                        break
                
                position = p.get("position", "")
                if mins == 0:
                    status = "In squad, did not play"
                elif position == "Substitute":
                    status = "Came on as sub"
                else:
                    status = "Started"

                match_players_data[pid] = {
                    "player_name": p.get("matchName") or f"{p.get('firstName', '')} {p.get('lastName', '')}".strip() or pid,
                    "club_name": club_name,
                    "shirt": p.get("shirtNumber", ""),
                    "mins": mins,
                    "status": status,
                    "week": fallback_info.get("week", ""),
                    "match_label": f"{fallback_info.get('home_name', 'TBD')} vs {fallback_info.get('away_name', 'TBD')}"
                }
    return match_players_data


def build_html_dashboard(state: dict, active_calendars: dict) -> str:
    """Compiles the dynamic, uncolored presentation layout from state matrix data."""
    # Invert mapping for easier lookup
    tmcl_to_label = {v: COMPETITIONS[k] for k, v in active_calendars.items()}
    
    report = {tmcl: {} for tmcl in active_calendars.values()}
    all_weeks_by_tmcl = {tmcl: set() for tmcl in active_calendars.values()}

    # Group flat match records into structural tree hierarchies
    for match_id, mdata in state.items():
        tmcl_id = mdata.get("tmcl_id")
        if tmcl_id not in report:
            continue
            
        week = mdata.get("week", "")
        if week:
            all_weeks_by_tmcl[tmcl_id].add(week)
            
        for pid, p in mdata.get("players", {}).items():
            cname = p["club_name"]
            pname = p["player_name"]
            
            if cname not in report[tmcl_id]:
                report[tmcl_id][cname] = {}
            if pname not in report[tmcl_id][cname]:
                report[tmcl_id][cname][pname] = {"shirt": p["shirt"], "matches": {}}
                
            report[tmcl_id][cname][pname]["matches"][week] = {
                "mins": p["mins"],
                "status": p["status"]
            }

    tabs_html = ""
    panels_html = ""

    for idx, tmcl_id in enumerate(active_calendars.values()):
        active_tab = "active" if idx == 0 else ""
        active_panel = "block" if idx == 0 else "none"
        tmcl_label = tmcl_to_label.get(tmcl_id, tmcl_id)
        
        sorted_weeks = sorted(all_weeks_by_tmcl[tmcl_id], key=lambda w: int(w) if w.isdigit() else 0)
        tabs_html += f'<button class="tab-btn {active_tab}" onclick="showTab(\'{tmcl_id}\')">{tmcl_label}</button>'

        clubs_data = report.get(tmcl_id, {})
        club_totals = {}
        for cname, plist in clubs_data.items():
            total = 0
            for pname, pinfo in plist.items():
                total += sum(m["mins"] for m in pinfo["matches"].values())
            club_totals[cname] = total

        sorted_clubs = sorted(clubs_data.keys(), key=lambda c: club_totals[c], reverse=True)
        clubs_html = ""
        
        for cname in sorted_clubs:
            week_headers = "".join(f'<th class="week-th">W{w}</th>' for w in sorted_weeks)
            
            plist = clubs_data[cname]
            player_totals = {}
            for pname, pinfo in plist.items():
                player_totals[pname] = sum(m["mins"] for m in pinfo["matches"].values())
                
            sorted_players = sorted(plist.keys(), key=lambda p: player_totals[p], reverse=True)
            rows_html = ""
            
            for pname in sorted_players:
                pinfo = plist[pname]
                cells = ""
                for w in sorted_weeks:
                    m = pinfo["matches"].get(w)
                    if m is None:
                        cells += '<td class="mins-cell">—</td>'
                    elif m["status"] == "In squad, did not play":
                        cells += '<td class="mins-cell">0</td>'
                    else:
                        cells += f'<td class="mins-cell">{m["mins"]}</td>'
                        
                shirt_str = f'#{pinfo["shirt"]} ' if pinfo["shirt"] else ""
                rows_html += f"""
                <tr>
                  <td class="player-name">{shirt_str}{pname}</td>
                  {cells}
                  <td class="mins-cell total">{player_totals[pname]}</td>
                </tr>"""

            clubs_html += f"""
            <div class="club-section">
              <div class="club-header" onclick="toggleClub(this)">
                <span class="arrow">▶</span> {cname} <span class="club-total-badge">({club_totals[cname]} mins total)</span>
              </div>
              <div class="club-body" style="display:none;">
                <table class="mins-table">
                  <thead><tr><th class="player-th">Player</th>{week_headers}<th class="week-th">Total</th></tr></thead>
                  <tbody>{rows_html}</tbody>
                </table>
              </div>
            </div>"""

        panels_html += f"""
        <div id="panel-{tmcl_id}" class="tab-panel" style="display:{active_panel};">
          <h2 class="tmcl-title">{tmcl_label} Matrix</h2>
          {clubs_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Player Minutes Monitor</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; background: #f3f4f6; color: #1f2937; }}
  .header {{ background: #1e3a5f; color: #fff; padding: 20px 32px; }}
  .header h1 {{ font-size: 20px; }}
  .tabs {{ background: #fff; border-bottom: 2px solid #e5e7eb; padding: 0 32px; display: flex; gap: 4px; }}
  .tab-btn {{ padding: 14px 24px; border: none; background: none; cursor: pointer; font-size: 14px; font-weight: 600; color: #6b7280; border-bottom: 3px solid transparent; }}
  .tab-btn.active {{ color: #1e3a5f; border-bottom-color: #1e3a5f; }}
  .content {{ padding: 24px 32px; }}
  .tmcl-title {{ font-size: 16px; font-weight: 700; margin-bottom: 16px; color: #1e3a5f; }}
  .club-section {{ margin-bottom: 12px; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; background: #fff; }}
  .club-header {{ padding: 12px 16px; background: #f1f5f9; cursor: pointer; font-weight: 700; font-size: 14px; display: flex; align-items: center; gap: 8px; }}
  .club-total-badge {{ font-size: 12px; font-weight: normal; color: #6b7280; margin-left: auto; padding-right: 8px; }}
  .arrow {{ font-size: 11px; transition: transform .2s; display: inline-block; }}
  .arrow.open {{ transform: rotate(90deg); }}
  .mins-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .player-th {{ padding: 8px 12px; text-align: left; background: #f8fafc; color: #6b7280; }}
  .week-th {{ padding: 8px 6px; text-align: center; background: #f8fafc; color: #6b7280; min-width: 42px; }}
  .player-name {{ padding: 8px 12px; font-size: 13px; white-space: nowrap; border-bottom: 1px solid #f1f5f9; }}
  .mins-cell {{ padding: 6px 4px; text-align: center; font-size: 12px; font-weight: 600; border-bottom: 1px solid #f1f5f9; color: #1f2937; }}
  .mins-cell.total  {{ color: #1e3a5f; background: #eff6ff; font-weight: 700; }}
  .legend {{ display: flex; gap: 16px; margin-bottom: 16px; font-size: 12px; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; color: #4b5563; }}
</style>
</head>
<body>
<div class="header"><h1>Player Minutes Monitor Dashboard</h1></div>
<div class="tabs">{tabs_html}</div>
<div class="content">
  <div class="legend">
    <div class="legend-item"><strong>—</strong> Not in squad</div>
    <div class="legend-item"><strong>0</strong> In squad, 0 minutes played</div>
    <div class="legend-item"><strong>Numbers</strong> Actual minutes played</div>
  </div>
  {panels_html}
</div>
<script>
  function showTab(tmclId) {{
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


def main():
    eligible_ids = load_eligible_player_ids()
    if not eligible_ids:
        return

    active_calendars = fetch_active_tournament_calendars()
    calendar_to_comp = {v: k for k, v in active_calendars.items()}
    active_calendar_ids = set(active_calendars.values())

    # Load file engine database state
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            print(f"Loaded existing database state containing {len(state)} matches.")
        except Exception as e:
            print(f"Failed parsing database state. Starting clean: {e}")

    # Determine runtime approach: Bootstrap vs. Incremental
    if not state:
        print("\n=== STARTING BOOTSTRAP MODE ===")
        all_season_fixtures = []
        for tmcl_id in active_calendar_ids:
            print(f"Cataloging entire match calendar for {COMPETITIONS[calendar_to_comp[tmcl_id]]}...")
            all_season_fixtures.extend(fetch_entire_calendar_fixtures(tmcl_id))

        print(f"Scanning total pool of {len(all_season_fixtures)} match sheets...")
        for idx, f in enumerate(all_season_fixtures, 1):
            if idx % 20 == 0 or idx == len(all_season_fixtures):
                print(f"  Progress: Match {idx}/{len(all_season_fixtures)} scanned.")
                
            res = process_match_sheet(f["match_id"], eligible_ids, f)
            state[f["match_id"]] = {
                "week": f["week"],
                "date": f["date"],
                "tmcl_id": f["tmcl_id"],
                "home_name": f["home_name"],
                "away_name": f["away_name"],
                "players": res
            }
    else:
        print("\n=== STARTING INCREMENTAL DAILY MODE ===")
        changed_match_ids = fetch_mar_updated_fixtures(lookback_hours=25)
        
        if not changed_match_ids:
            print("No new match updates detected via MAR feed.")
        else:
            # Re-verify full definitions for modified entries using standard calendar feed maps
            full_schedule_map = {}
            for tmcl_id in active_calendar_ids:
                for f in fetch_entire_calendar_fixtures(tmcl_id):
                    full_schedule_map[f["match_id"]] = f

            for m_id in changed_match_ids:
                f_info = full_schedule_map.get(m_id)
                if not f_info:
                    continue  # Ignore external competitions outside target group
                    
                print(f"  Updating state record for Match: {f_info['home_name']} vs {f_info['away_name']} (W{f_info['week']})")
                res = process_match_sheet(m_id, eligible_ids, f_info)
                
                state[m_id] = {
                    "week": f_info["week"],
                    "date": f_info["date"],
                    "tmcl_id": f_info["tmcl_id"],
                    "home_name": f_info["home_name"],
                    "away_name": f_info["away_name"],
                    "players": res
                }

    # Clean legacy keys out of database that don't fit our targets anymore
    purged_state = {k: v for k, v in state.items() if v.get("tmcl_id") in active_calendar_ids}

    # Save state back to repository disk
    STATE_FILE.write_text(json.dumps(purged_state, indent=2))
    print(f"Saved update back to {STATE_FILE}.")

    # Generate layout output file
    print("Regenerating user view dashboard...")
    html_out = build_html_dashboard(purged_state, active_calendars)
    OUTPUT_HTML.write_text(html_out, encoding="utf-8")
    print(f"Successfully deployed: {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
