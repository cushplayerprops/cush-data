#!/usr/bin/env python3
"""
build_wnba.py - fetch WNBA schedule + player 3PA/FGA/MIN + shot zones + catch&shoot/
pull-up splits + team pace + opponent defense from stats.wnba.com, write
wnba_stats.json for cushplayerprops.win. Runs from GitHub Actions. stats.wnba.com
blocks data-center IPs, so all requests route through the ScrapeOps residential
proxy (needs SCRAPEOPS_API_KEY secret). Requires: pip install requests
"""

import os, json, time, datetime
import requests
from urllib.parse import urlencode

SEASON = os.environ.get("WNBA_SEASON", "2026")
LEAGUE = "10"                       # 10 = WNBA
OUT    = os.environ.get("OUT", "wnba_stats.json")
BASE   = "https://stats.wnba.com/stats"

SCRAPEOPS_KEY = os.environ.get("SCRAPEOPS_API_KEY", "")
PROXY = "https://proxy.scrapeops.io/v1/"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.wnba.com/",
    "Origin": "https://www.wnba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}


def get(path, params, tries=4):
    # Build the real stats.wnba.com URL (with its query string), then hand the
    # whole thing to ScrapeOps so it fetches it from a residential IP for us.
    target = BASE + path + "?" + urlencode(params)
    proxy_payload = {
        "api_key": SCRAPEOPS_KEY,
        "url": target,
        "residential": "true",   # datacenter IPs are what stats.wnba.com blocks
        "keep_headers": "true",  # forward the NBA-stats headers defined above
    }
    proxy_url = PROXY + "?" + urlencode(proxy_payload)
    last = None
    for i in range(tries):
        try:
            # ScrapeOps retries on its side for up to ~2 min, so give it 130s.
            r = requests.get(proxy_url, headers=HEADERS, timeout=130)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            print(f"  retry {i+1}/{tries} for {path}: {e}")
            time.sleep(3 + i * 3)
    raise last


def rows(js, name=None):
    rs = (js or {}).get("resultSets") or []
    st = None
    if name:
        for x in rs:
            if x.get("name") == name:
                st = x
                break
    else:
        st = rs[0] if rs else None
    if not st:
        return []
    H = st["headers"]
    return [dict(zip(H, row)) for row in st.get("rowSet", [])]


def shot_zone_rows(js):
    # leaguedashplayershotlocations returns a special two-tier header:
    # one header lists zone names, the other is the flat column list
    # (PLAYER_ID, ..., then FGM/FGA/FG_PCT repeated per zone).
    rs = (js or {}).get("resultSets") or {}
    if isinstance(rs, list):
        rs = rs[0] if rs else {}
    hdrs = rs.get("headers") or []
    zone_names, flat, skip = [], [], 5
    for h in hdrs:
        cn = h.get("columnNames") or []
        if "PLAYER_ID" in cn:
            flat = cn
        else:
            zone_names = cn
        if h.get("columnsToSkip") is not None:
            skip = h.get("columnsToSkip")
    if not flat:
        return []
    out = []
    for row in rs.get("rowSet", []):
        d = {}
        for i in range(min(skip, len(flat), len(row))):
            d[flat[i]] = row[i]
        idx = skip
        for z in zone_names:
            for stat in ("FGM", "FGA", "FG_PCT"):
                if idx < len(row):
                    d[f"{z}|{stat}"] = row[idx]
                idx += 1
        out.append(d)
    return out


def dash(extra):
    p = {
        "College": "", "Conference": "", "Country": "", "DateFrom": "", "DateTo": "",
        "Division": "", "DraftPick": "", "DraftYear": "", "GameScope": "", "GameSegment": "",
        "Height": "", "LastNGames": "0", "LeagueID": LEAGUE, "Location": "",
        "MeasureType": "Base", "Month": "0", "OpponentTeamID": "0", "Outcome": "",
        "PORound": "0", "PaceAdjust": "N", "PerMode": "PerGame", "Period": "0",
        "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N", "Rank": "N",
        "Season": SEASON, "SeasonSegment": "", "SeasonType": "Regular Season",
        "ShotClockRange": "", "StarterBench": "", "TeamID": "0", "VsConference": "",
        "VsDivision": "", "Weight": "",
    }
    p.update(extra)
    return p


def ptshot_params(general_range):
    # leaguedashplayerptshot has a different parameter set than the dash endpoints
    return {
        "CloseDefDistRange": "", "College": "", "Conference": "", "Country": "",
        "DateFrom": "", "DateTo": "", "Division": "", "DraftPick": "", "DraftYear": "",
        "DribbleRange": "", "GameSegment": "", "GeneralRange": general_range,
        "Height": "", "LastNGames": "0", "LeagueID": LEAGUE, "Location": "",
        "Month": "0", "OpponentTeamID": "0", "Outcome": "", "PORound": "0",
        "PerMode": "PerGame", "Period": "0", "PlayerExperience": "", "PlayerPosition": "",
        "Season": SEASON, "SeasonSegment": "", "SeasonType": "Regular Season",
        "ShotClockRange": "", "ShotDistRange": "", "StarterBench": "", "TeamID": "0",
        "TouchTimeRange": "", "VsConference": "", "VsDivision": "", "Weight": "",
    }


def et_date():
    import zoneinfo
    now = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return now.strftime("%m/%d/%Y")


def num(v):
    if v is None or v == "":
        return None
    try:
        return round(float(v), 3)
    except Exception:
        return None


def main():
    if not SCRAPEOPS_KEY:
        print("WARNING: SCRAPEOPS_API_KEY not set -- requests will likely fail (403).")

    errors = {}
    game_date = os.environ.get("WNBA_DATE", et_date())

    games, team_abbr = [], {}
    try:
        sb = get("/scoreboardv2", {"DayOffset": "0", "GameDate": game_date, "LeagueID": LEAGUE})
        for r in rows(sb, "LineScore"):
            if r.get("TEAM_ID") is not None:
                team_abbr[r["TEAM_ID"]] = r.get("TEAM_ABBREVIATION")
        for g in rows(sb, "GameHeader"):
            games.append({
                "gameId": g.get("GAME_ID"),
                "status": g.get("GAME_STATUS_ID"),
                "statusText": (g.get("GAME_STATUS_TEXT") or "").strip(),
                "home": {"id": g.get("HOME_TEAM_ID"), "abbr": team_abbr.get(g.get("HOME_TEAM_ID"))},
                "away": {"id": g.get("VISITOR_TEAM_ID"), "abbr": team_abbr.get(g.get("VISITOR_TEAM_ID"))},
            })
    except Exception as e:
        errors["schedule"] = str(e)

    players = {}

    def ingest(js, prefix):
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None:
                continue
            p = players.setdefault(pid, {
                "id": pid, "name": r.get("PLAYER_NAME"),
                "teamId": r.get("TEAM_ID"), "teamAbbr": r.get("TEAM_ABBREVIATION"),
            })
            p[prefix + "gp"] = num(r.get("GP"))
            p[prefix + "min"] = num(r.get("MIN"))
            p[prefix + "fga"] = num(r.get("FGA"))
            p[prefix + "fg3a"] = num(r.get("FG3A"))
            p[prefix + "pts"] = num(r.get("PTS"))

    try:
        ingest(get("/leaguedashplayerstats", dash({"LastNGames": "0"})), "")
    except Exception as e:
        errors["playersSeason"] = str(e)
    try:
        ingest(get("/leaguedashplayerstats", dash({"LastNGames": "10"})), "r_")
    except Exception as e:
        errors["playersRecent"] = str(e)

    # Shot zones: split each player's FGA into rim / paint / mid / corner3 / above-break3
    def ingest_zones(js):
        for r in shot_zone_rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            p = players[pid]
            g = lambda z: num(r.get(z + "|FGA"))
            lc, rc = g("Left Corner 3"), g("Right Corner 3")
            p["z_ra"]      = g("Restricted Area")
            p["z_paint"]   = g("In The Paint (Non-RA)")
            p["z_mid"]     = g("Mid-Range")
            p["z_corner3"] = round((lc or 0) + (rc or 0), 3)
            p["z_above3"]  = g("Above the Break 3")

    try:
        ingest_zones(get("/leaguedashplayershotlocations", dash({"DistanceRange": "By Zone"})))
    except Exception as e:
        errors["shotZones"] = str(e)

    # Catch & Shoot vs Pull-Up: how each player generates their attempts
    def ingest_ptshot(js, prefix):
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            p = players[pid]
            p[prefix + "fga"]  = num(r.get("FGA"))
            p[prefix + "fg3a"] = num(r.get("FG3A"))
            p[prefix + "freq"] = num(r.get("FGA_FREQUENCY"))

    try:
        ingest_ptshot(get("/leaguedashplayerptshot", ptshot_params("Catch and Shoot")), "cs_")
    except Exception as e:
        errors["catchShoot"] = str(e)
    try:
        ingest_ptshot(get("/leaguedashplayerptshot", ptshot_params("Pull Ups")), "pu_")
    except Exception as e:
        errors["pullUp"] = str(e)

    # players carry TEAM_ABBREVIATION; use them to fill team abbreviations
    id2abbr = {}
    for p in players.values():
        if p.get("teamId") is not None and p.get("teamAbbr"):
            id2abbr[p["teamId"]] = p["teamAbbr"]

    teams = {}
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Advanced"}))):
            tid = r.get("TEAM_ID")
            teams[tid] = {
                "id": tid, "abbr": id2abbr.get(tid) or r.get("TEAM_ABBREVIATION"),
                "pace": num(r.get("PACE")), "gp": num(r.get("GP")),
            }
    except Exception as e:
        errors["teamPace"] = str(e)

    # Opponent defense: how many FGA / 3PA each team ALLOWS per game (matchup lever)
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Opponent"}))):
            tid = r.get("TEAM_ID")
            t = teams.get(tid)
            if t is None:
                t = teams.setdefault(tid, {"id": tid, "abbr": id2abbr.get(tid) or r.get("TEAM_ABBREVIATION")})
            t["oppFga"]    = num(r.get("OPP_FGA"))
            t["oppFg3a"]   = num(r.get("OPP_FG3A"))
            t["oppFg3Pct"] = num(r.get("OPP_FG3_PCT"))
    except Exception as e:
        errors["teamOpp"] = str(e)

    out = {
        "updated": datetime.datetime.utcnow().isoformat() + "Z",
        "season": SEASON,
        "gameDate": game_date,
        "counts": {"games": len(games), "players": len(players), "teams": len(teams)},
        "games": games,
        "teams": teams,
        "players": players,
    }
    if errors:
        out["errors"] = errors

    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))

    print("WROTE", OUT, out["counts"], ("ERRORS: " + json.dumps(errors)) if errors else "")
    if not players and not games:
        raise SystemExit("no data fetched -- stats.wnba.com likely blocked this runner")


if __name__ == "__main__":
    main()
