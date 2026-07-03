#!/usr/bin/env python3
"""
build_wnba.py - fetch WNBA schedule + player 3PA/FGA/MIN + team pace from stats.wnba.com,
write wnba_stats.json for cushplayerprops.win. Runs from GitHub Actions.
stats.wnba.com blocks data-center IPs, so all requests are routed through the
ScrapeOps residential proxy (needs SCRAPEOPS_API_KEY env var / repo secret).
Requires: pip install requests
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

    teams = {}
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Advanced"}))):
            teams[r.get("TEAM_ID")] = {
                "id": r.get("TEAM_ID"), "abbr": r.get("TEAM_ABBREVIATION"),
                "pace": num(r.get("PACE")), "gp": num(r.get("GP")),
            }
    except Exception as e:
        errors["teamPace"] = str(e)

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
