#!/usr/bin/env python3
"""
build_wnba.py - WNBA feed for cushplayerprops.win. Schedule + player
3PA/FGA/PTS/MIN + shot zones + assisted rate + per-game logs + team pace +
opponent defense (totals, FT-allowed, AND by-zone) + player free throws from stats.wnba.com. Runs from GitHub
Actions via the ScrapeOps residential proxy (needs SCRAPEOPS_API_KEY secret).
Requires: pip install requests
"""

import os, json, time, datetime, re, unicodedata
import requests
from urllib.parse import urlencode

SEASON = os.environ.get("WNBA_SEASON", "2026")
LEAGUE = "10"
OUT    = os.environ.get("OUT", "wnba_stats.json")
BASE   = "https://stats.wnba.com/stats"

SCRAPEOPS_KEY = os.environ.get("SCRAPEOPS_API_KEY", "")
PROXY = "https://proxy.scrapeops.io/v1/"
ESPN_INJ = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries"

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
    target = BASE + path + "?" + urlencode(params)
    proxy_payload = {"api_key": SCRAPEOPS_KEY, "url": target, "residential": "true", "keep_headers": "true"}
    proxy_url = PROXY + "?" + urlencode(proxy_payload)
    last = None
    for i in range(tries):
        try:
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
                st = x; break
    else:
        st = rs[0] if rs else None
    if not st:
        return []
    H = st["headers"]
    return [dict(zip(H, row)) for row in st.get("rowSet", [])]


def shot_zone_rows(js):
    rs = (js or {}).get("resultSets") or {}
    if isinstance(rs, list):
        rs = rs[0] if rs else {}
    hdrs = rs.get("headers") or []
    zone_names, flat, skip = [], [], 5
    for h in hdrs:
        cn = h.get("columnNames") or []
        if "PLAYER_ID" in cn or "TEAM_ID" in cn:
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


def logs_params():
    return {
        "DateFrom": "", "DateTo": "", "GameSegment": "", "ISTRound": "", "LastNGames": "0",
        "LeagueID": LEAGUE, "Location": "", "MeasureType": "Base", "Month": "0",
        "OppTeamID": "0", "Outcome": "", "PORound": "0", "PerMode": "Totals", "Period": "0",
        "PlayerID": "", "Season": SEASON, "SeasonSegment": "", "SeasonType": "Regular Season",
        "ShotClockRange": "", "TeamID": "0", "VsConference": "", "VsDivision": "",
    }


def et_date():
    import zoneinfo
    now = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return now.strftime("%m/%d/%Y")


def pbnorm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get_url(full_url, tries=4, residential=True):
    payload = {"api_key": SCRAPEOPS_KEY, "url": full_url}
    if residential:
        payload["residential"] = "true"
    proxy_url = PROXY + "?" + urlencode(payload)
    last = None
    for i in range(tries):
        try:
            r = requests.get(proxy_url, timeout=130)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            print(f"  retry {i+1}/{tries} for proxied url: {e}")
            time.sleep(3 + i * 3)
    raise last


def fetch_injuries_raw():
    # ESPN site.api is public; try direct first (free), fall back to the ScrapeOps proxy.
    try:
        r = requests.get(ESPN_INJ, headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        js = r.json()
        if (js or {}).get("injuries"):
            return js
        print("  injuries direct returned no groups, trying proxy")
    except Exception as e:
        print("  injuries direct failed, trying proxy:", e)
    return get_url(ESPN_INJ)


def parse_injuries(js):
    res = {}
    groups = (js or {}).get("injuries") or []
    for grp in groups:
        items = grp.get("injuries") if isinstance(grp, dict) else None
        if items is None:
            items = [grp]
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            ath = it.get("athlete") or {}
            nm = ath.get("displayName") or ath.get("fullName") or ath.get("shortName")
            if not nm:
                continue
            status = (it.get("status") or "").strip()
            if not status:
                typ = it.get("type") or {}
                if isinstance(typ, dict):
                    status = (typ.get("description") or typ.get("name") or "").strip()
            detail = it.get("shortComment") or it.get("longComment") or ""
            k = pbnorm(nm)
            if k:
                res[k] = {"status": status, "detail": (detail or "")[:180]}
    return res


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
                "gameId": g.get("GAME_ID"), "status": g.get("GAME_STATUS_ID"),
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
            p = players.setdefault(pid, {"id": pid, "name": r.get("PLAYER_NAME"),
                "teamId": r.get("TEAM_ID"), "teamAbbr": r.get("TEAM_ABBREVIATION")})
            p[prefix + "gp"] = num(r.get("GP"))
            p[prefix + "min"] = num(r.get("MIN"))
            p[prefix + "fga"] = num(r.get("FGA"))
            p[prefix + "fg3a"] = num(r.get("FG3A"))
            p[prefix + "pts"] = num(r.get("PTS"))
            p[prefix + "ftm"] = num(r.get("FTM"))
            p[prefix + "fta"] = num(r.get("FTA"))
            p[prefix + "ftPct"] = num(r.get("FT_PCT"))

    try:
        ingest(get("/leaguedashplayerstats", dash({"LastNGames": "0"})), "")
    except Exception as e:
        errors["playersSeason"] = str(e)
    try:
        ingest(get("/leaguedashplayerstats", dash({"LastNGames": "10"})), "r_")
    except Exception as e:
        errors["playersRecent"] = str(e)

    def ingest_zones(js):
        for r in shot_zone_rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            p = players[pid]
            g = lambda z: num(r.get(z + "|FGA"))
            lc, rc = g("Left Corner 3"), g("Right Corner 3")
            p["z_ra"] = g("Restricted Area")
            p["z_paint"] = g("In The Paint (Non-RA)")
            p["z_mid"] = g("Mid-Range")
            p["z_corner3"] = round((lc or 0) + (rc or 0), 3)
            p["z_above3"] = g("Above the Break 3")

    try:
        ingest_zones(get("/leaguedashplayershotlocations", dash({"DistanceRange": "By Zone"})))
    except Exception as e:
        errors["shotZones"] = str(e)

    def ingest_scoring(js):
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            p = players[pid]
            p["ast3Pct"] = num(r.get("PCT_AST_3PM"))
            p["astFgPct"] = num(r.get("PCT_AST_FGM"))

    try:
        ingest_scoring(get("/leaguedashplayerstats", dash({"MeasureType": "Scoring"})))
    except Exception as e:
        errors["scoring"] = str(e)

    # Per-game logs (recent games) -> powers L10 hit-rate + out/usage flags.
    # PerMode=Totals gives each game's actual raw stat line.
    def ingest_logs(js):
        tmp = {}
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None:
                continue
            tmp.setdefault(pid, []).append({
                "d": r.get("GAME_DATE"),
                "min": num(r.get("MIN")),
                "fga": num(r.get("FGA")),
                "fg3a": num(r.get("FG3A")),
                "fg3m": num(r.get("FG3M")),
                "pts": num(r.get("PTS")),
                "ftm": num(r.get("FTM")),
                "fta": num(r.get("FTA")),
            })
        for pid, gl in tmp.items():
            gl.sort(key=lambda g: (g.get("d") or ""), reverse=True)
            recent = gl[:12]
            if pid in players:
                players[pid]["log"] = recent
            else:
                players[pid] = {"id": pid, "log": recent}

    try:
        ingest_logs(get("/playergamelogs", logs_params()))
    except Exception as e:
        errors["gameLogs"] = str(e)

    id2abbr = {}
    for p in players.values():
        if p.get("teamId") is not None and p.get("teamAbbr"):
            id2abbr[p["teamId"]] = p["teamAbbr"]

    teams = {}
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Advanced"}))):
            tid = r.get("TEAM_ID")
            teams[tid] = {"id": tid, "abbr": id2abbr.get(tid) or r.get("TEAM_ABBREVIATION"),
                "pace": num(r.get("PACE")), "gp": num(r.get("GP"))}
    except Exception as e:
        errors["teamPace"] = str(e)

    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Opponent"}))):
            tid = r.get("TEAM_ID")
            t = teams.get(tid) or teams.setdefault(tid, {"id": tid, "abbr": id2abbr.get(tid) or r.get("TEAM_ABBREVIATION")})
            t["oppFga"] = num(r.get("OPP_FGA"))
            t["oppFg3a"] = num(r.get("OPP_FG3A"))
            t["oppFg3Pct"] = num(r.get("OPP_FG3_PCT"))
            t["oppFta"] = num(r.get("OPP_FTA"))
    except Exception as e:
        errors["teamOpp"] = str(e)

    def ingest_team_zones(js):
        for r in shot_zone_rows(js):
            tid = r.get("TEAM_ID")
            if tid is None:
                continue
            t = teams.get(tid) or teams.setdefault(tid, {"id": tid, "abbr": id2abbr.get(tid)})
            gg = lambda z: num(r.get(z + "|FGA"))
            lc, rc = gg("Left Corner 3"), gg("Right Corner 3")
            t["dz_ra"] = gg("Restricted Area")
            t["dz_paint"] = gg("In The Paint (Non-RA)")
            t["dz_mid"] = gg("Mid-Range")
            t["dz_corner3"] = round((lc or 0) + (rc or 0), 3)
            t["dz_above3"] = gg("Above the Break 3")

    try:
        ingest_team_zones(get("/leaguedashteamshotlocations", dash({"MeasureType": "Opponent", "DistanceRange": "By Zone"})))
    except Exception as e:
        errors["teamZoneDef"] = str(e)

    # Real player availability from ESPN (Out / Doubtful / Questionable / Day-To-Day).
    inj_map, inj_matched = {}, 0
    try:
        inj_map = parse_injuries(fetch_injuries_raw())
    except Exception as e:
        errors["injuries"] = str(e)
    if inj_map:
        for _pl in players.values():
            _nm = _pl.get("name")
            if not _nm:
                continue
            _st = inj_map.get(pbnorm(_nm))
            if _st:
                _pl["injStatus"] = _st["status"]
                if _st.get("detail"):
                    _pl["injDetail"] = _st["detail"]
                inj_matched += 1

    out = {
        "updated": datetime.datetime.utcnow().isoformat() + "Z",
        "season": SEASON, "gameDate": game_date,
        "counts": {"games": len(games), "players": len(players), "teams": len(teams), "injListed": len(inj_map), "injMatched": inj_matched},
        "games": games, "teams": teams, "players": players,
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
