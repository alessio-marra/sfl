"""
player_minutes.py
Optimized Match-First Engine: Iterates through matches exactly ONCE globally.
Fetches each lineup exactly once, mapping minutes instantly to any present eligible players.
"""

import os
import json
import requests
import xml.etree.ElementTree as ET
from pathlib import Path

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


def load_mapping() -> dict:
    if MAPPING_FILE.exists():
        return json.loads(MAPPING_FILE.read_text())
    return {}


def load_players() -> list[dict]:
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
    print(f"  Loaded {len(eligible)} eligible players.")
    return eligible


def process_match_lineup(match_id: str, eligible_ids: set, fallback_names: dict, m_info: dict, report: dict, tmcl_id: str):
    """Fetches a single match lineup once and scans it for all tracked players."""
    url = f"{BASE_URL}/matchstats/{API_KEY}/?detailed=yes&_rt=c&_fmt=xml&fx={match_id}"
    try:
        root = get(url)
    except Exception as e:
        print(f"    matchstats failed for {match_id}: {e}")
        return

    for lineup in root.iter("lineUp"):
        tid = lineup.get("contestantId", "")
        tname = lineup.get("contestantName", tid) or "Unknown Club"
        
        for p in lineup.iter("player"):
            pid = p.get("playerId")
            
            # Instantly match using structural ID check
            if pid in eligible_ids:
                mins = 0
                for stat in p.findall("stat"):
                    if stat.get("type") == "minsPlayed":
                        try:
                            mins = int(stat.text)
                        except:
                            mins = 0
                        break

                display_name = p.get("matchName", "") or fallback_names.get(pid, pid)
                shirt = p.get("shirtNumber", "")
                position = p.get("position", "")

                if mins == 0:
                    status = "In squad, did not play"
                elif position == "Substitute":
                    status = "Came on as sub"
                else:
                    status = "Started"

                # Structural Report Construction
                if tname not in report[tmcl_id]:
                    report[tmcl_id][tname] = {}
                if display_name not in report[tmcl_id][tname]:
                    report[tmcl_id][tname][display_name] = {"player_id": pid, "shirt": shirt, "matches": []}
                elif shirt and not report[tmcl_id][tname][display_name]["shirt"]:
                    report[tmcl_id][tname][display_name]["shirt"] = shirt

                report[tmcl_id][tname][display_name]["matches"].append({
                    "week": m_info["week"],
                    "date": m_info["date"],
                    "match": f"{m_info['home_name']} vs {m_info['away_name']}",
                    "mins": mins,
                    "status": status,
                })


def build_html(report: dict, tmcl_names: dict) -> str:
    all_weeks_by_tmcl = {}
    for tmcl_id, clubs in report.items():
        weeks = set()
        for club_name, players in clubs.items():
            for pname, pdata in players.items():
                for m in pdata["matches"]:
                    if m["week"]:
                        weeks.add(m["week"])
        all_weeks_by_tmcl[tmcl_id] = sorted(weeks, key=lambda w: int(w) if w.isdigit() else 0)

    tabs_html = ""
    panels_html = ""

    for i, (tmcl_id, clubs) in enumerate(report.items()):
        active_tab = "active" if i == 0 else ""
        active_panel = "block" if i == 0 else "none"
        tmcl_label = tmcl_names.get(tmcl_id, tmcl_id)
        weeks = all_weeks_by_tmcl[tmcl_id]

        tabs_html += f'<button class="tab-btn {active_tab}" onclick="showTab(\'{tmcl_id}\')">{tmcl_label}</button>'

        club_totals = {}
        for club_name, players in clubs.items():
            total_club_mins = 0
            for pname, pdata in players.items():
                for m in pdata["matches"]:
                    if m["status"] not in ["Not in squad", "In squad, did not play"]:
                        total_club_mins += (m["mins"] or 0)
            club_totals[club_name] = total_club_mins

        sorted_clubs = sorted(clubs.keys(), key=lambda c: club_totals[c], reverse=True)

        clubs_html = ""
        for club_name in sorted_clubs:
            players = clubs[club_name]
            week_headers = "".join(f'<th class="week-th">W{w}</th>' for w in weeks)
            
            player_totals = {}
            for pname, pdata in players.items():
                p_total = 0
                for m in pdata["matches"]:
                    if m["status"] not in ["Not in squad", "In squad, did not play"]:
                        p_total += (m["mins"] or 0)
                player_totals[pname] = p_total

            sorted_players = sorted(players.keys(), key=lambda p: player_totals[p], reverse=True)

            rows_html = ""
            for pname in sorted_players:
                pdata = players[pname]
                week_map = {m["week"]: m for m in pdata["matches"]}
                cells = ""
                
                for w in weeks:
                    m = week_map.get(w)
                    if m is None or m["status"] == "Not in squad":
                        cells += '<td class="mins-cell grey">—</td>'
                    elif m["status"] == "In squad, did not play":
                        cells += '<td class="mins-cell orange">0</td>'
                    else:
                        mins = m["mins"] or 0
                        color = "green" if mins >= 60 else "yellow" if mins > 0 else "orange"
                        cells += f'<td class="mins-cell {color}">{mins}</td>'

                shirt_str = f'#{pdata["shirt"]} ' if pdata.get("shirt") else ""
                rows_html += f"""
                <tr>
                  <td class="player-name">{shirt_str}{pname}</td>
                  {cells}
                  <td class="mins-cell total">{player_totals[pname]}</td>
                </tr>"""

            clubs_html += f"""
            <div class="club-section">
              <div class="club-header" onclick="toggleClub(this)">
                <span class="arrow">▶</span> {club_name} <span class="club-total-badge">({club_totals[club_name]} mins total)</span>
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
          <h2 class="tmcl-title">{tmcl_label}</h2>
          {clubs_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Player Minutes Report</title>
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
  .mins-cell {{ padding: 6px 4px; text-align: center; font-size: 12px; font-weight: 600; border-bottom: 1px solid #f1f5f9; }}
  .mins-cell.green  {{ color: #15803d; background: #dcfce7; }}
  .mins-cell.yellow {{ color: #92400e; background: #fef9c3; }}
  .mins-cell.orange {{ color: #9a3412; background: #ffedd5; }}
  .mins-cell.grey   {{ color: #9ca3af; }}
  .mins-cell.total  {{ color: #1e3a5f; background: #eff6ff; font-weight: 700; }}
  .legend {{ display: flex; gap: 16px; margin-bottom: 16px; font-size: 12px; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-dot {{ width: 14px; height: 14px; border-radius: 3px; }}
</style>
</head>
<body>
<div class="header"><h1>Player Minutes Report</h1></div>
<div class="tabs">{tabs_html}</div>
<div class="content">
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#dcfce7;border:1px solid #15803d"></div> ≥60 min</div>
    <div class="legend-item"><div class="legend-dot" style="background:#fef9c3;border:1px solid #92400e"></div> &lt;60 min</div>
    <div class="legend-item"><div class="legend-dot" style="background:#ffedd5;border:1px solid #9a3412"></div> In squad, 0 min</div>
    <div class="legend-item"><div class="legend-dot" style="background:#fff;border:1px solid #9ca3af"></div> Not in squad / no data</div>
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
    print(f"\nTournament Calendar 1: {TMCL_1}")
    print(f"Tournament Calendar 2: {TMCL_2}\n")

    mapping = load_mapping()
    players = load_players()
    if not players:
        return

    report = {tmcl: {} for tmcl in TMCLS}
    tmcl_names = {}
    tmcl_matches = {}

    eligible_ids = {p["id"] for p in players if "id" in p}
    player_display_names = {p["id"]: f"{p.get('firstName', '')} {p.get('lastName', '')}".strip() for p in players if "id" in p}

    # Step 1: Collect structural calendar maps (Paginated properly)
    for tmcl_id in TMCLS:
        matches = []
        page_num = 1
        comp_name = ""
        season_name = ""
        
        while True:
            url = f"{BASE_URL}/match/{API_KEY}?live=yes&_fmt=xml&_rt=c&_pgSz=100&_pgNm={page_num}&tmcl={tmcl_id}"
            try:
                root = get(url)
            except Exception as e:
                print(f"  match feed failed on page {page_num}: {e}")
                break

            if page_num == 1:
                for comp in root.iter("competition"):
                    comp_name = comp.get("name", "")
                    break
                for tc in root.iter("tournamentCalendar"):
                    season_name = tc.text or ""
                    break
                
                if comp_name and season_name:
                    tmcl_names[tmcl_id] = f"{comp_name} - {season_name}"
                else:
                    tmcl_names[tmcl_id] = season_name or tmcl_id

            page_matches_found = 0
            for mi in root.iter("matchInfo"):
                page_matches_found += 1
                contestants = {}
                for c in mi.findall(".//contestant"):
                    contestants[c.get("position")] = {"id": c.get("id", ""), "name": c.get("name", "")}
                
                matches.append({
                    "match_id": mi.get("id"),
                    "date": mi.get("date", "").replace("Z", ""),
                    "week": mi.get("week", ""),
                    "home_id": contestants.get("home", {}).get("id", ""),
                    "home_name": contestants.get("home", {}).get("name", ""),
                    "away_id": contestants.get("away", {}).get("id", ""),
                    "away_name": contestants.get("away", {}).get("name", ""),
                })

            if page_matches_found == 0:
                break
            page_num += 1

        tmcl_matches[tmcl_id] = sorted(matches, key=lambda x: x["date"])
        print(f"  {tmcl_id} ({tmcl_names.get(tmcl_id, tmcl_id)}): {len(matches)} matches cataloged.")

    # Step 2: Iterate through matches ONCE total. Single Pass Processing.
    for tmcl_id in TMCLS:
        all_matches = tmcl_matches.get(tmcl_id, [])
        total_matches = len(all_matches)
        print(f"\nProcessing {total_matches} match sheets for {tmcl_names.get(tmcl_id, tmcl_id)}...")
        
        for idx, m in enumerate(all_matches, 1):
            if idx % 20 == 0 or idx == total_matches:
                print(f"  Progress: Match {idx}/{total_matches} scanned...")
                
            process_match_lineup(
                match_id=m["match_id"],
                eligible_ids=eligible_ids,
                fallback_names=player_display_names,
                m_info=m,
                report=report,
                tmcl_id=tmcl_id
            )

    print("\nBuilding HTML report ...")
    html = build_html(report, tmcl_names)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Report written to {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
