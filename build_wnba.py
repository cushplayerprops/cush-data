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


def classify_injury(detail, it):
    """WNBA feed's top-level 'status' AND type field are coarse -- both read 'Out'
    even for questionable/day-to-day players. The beat-writer comment is the one field
    that reliably distinguishes them, so classify from it first, then fall back."""
    dl = (detail or "").lower()
    if "questionable" in dl:
        return "Questionable"
    if "doubtful" in dl:
        return "Doubtful"
    if ("day-to-day" in dl or "day to day" in dl or "game-time decision" in dl or "game time decision" in dl):
        return "Day-To-Day"
    if "probable" in dl and "improbable" not in dl:
        return "Probable"
    if re.search(r"\bout\b", dl) or any(kw in dl for kw in (
            "will miss", "sidelined", "re-evaluated", "reevaluated", "remainder of the season",
            "won't return", "will not play", "won't play", "inactive", "did not return", "torn acl")):
        return "Out"
    # comment said nothing decisive -> fall back to the coarse type / status fields
    typ = (it or {}).get("type") or {}
    abbr = (typ.get("abbreviation") or "").upper().strip()
    tname = (typ.get("name") or "").upper()
    ABBR = {"O": "Out", "D": "Doubtful", "Q": "Questionable",
            "DD": "Day-To-Day", "DTD": "Day-To-Day", "GTD": "Day-To-Day"}
    if abbr in ABBR:
        return ABBR[abbr]
    if "QUESTION" in tname:
        return "Questionable"
    if "DOUBT" in tname:
        return "Doubtful"
    if "DAYTODAY" in tname or "DAY_TO_DAY" in tname:
        return "Day-To-Day"
    if "OUT" in tname:
        return "Out"
    return ((it or {}).get("status") or "").strip() or "Out"


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
            detail = it.get("shortComment") or it.get("longComment") or ""
            status = classify_injury(detail, it)
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

    # WNBA has few games per night, and PrizePicks/Underdog post lines a couple days out.
    # Pull today + the next couple days so the board isn't empty and future-day props show.
    def _date_list():
        import zoneinfo
        forced = os.environ.get("WNBA_DATE")
        days = int(os.environ.get("WNBA_DAYS", "3"))
        base = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        if forced:
            try:
                base = datetime.datetime.strptime(forced, "%m/%d/%Y")
            except Exception:
                pass
            return [(base.strftime("%m/%d/%Y"), base.strftime("%a"))]
        return [((base + datetime.timedelta(days=o)).strftime("%m/%d/%Y"),
                 (base + datetime.timedelta(days=o)).strftime("%a")) for o in range(max(1, days))]

    games, team_abbr = [], {}
    seen_games = set()
    for (gd, dow) in _date_list():
        try:
            sb = get("/scoreboardv2", {"DayOffset": "0", "GameDate": gd, "LeagueID": LEAGUE})
            for r in rows(sb, "LineScore"):
                if r.get("TEAM_ID") is not None:
                    team_abbr[r["TEAM_ID"]] = r.get("TEAM_ABBREVIATION")
            for g in rows(sb, "GameHeader"):
                gid = g.get("GAME_ID")
                if gid in seen_games:
                    continue
                seen_games.add(gid)
                games.append({
                    "gameId": gid, "status": g.get("GAME_STATUS_ID"),
                    "statusText": (g.get("GAME_STATUS_TEXT") or "").strip(),
                    "date": gd, "day": dow,
                    "home": {"id": g.get("HOME_TEAM_ID"), "abbr": team_abbr.get(g.get("HOME_TEAM_ID"))},
                    "away": {"id": g.get("VISITOR_TEAM_ID"), "abbr": team_abbr.get(g.get("VISITOR_TEAM_ID"))},
                })
        except Exception as e:
            errors.setdefault("schedule", str(e))
    # backfill any abbreviations that were missing when a future-date game was first seen
    for g in games:
        if g["home"]["abbr"] is None:
            g["home"]["abbr"] = team_abbr.get(g["home"]["id"])
        if g["away"]["abbr"] is None:
            g["away"]["abbr"] = team_abbr.get(g["away"]["id"])

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
            p[prefix + "reb"] = num(r.get("REB"))
            p[prefix + "ast"] = num(r.get("AST"))
            p[prefix + "stl"] = num(r.get("STL"))
            p[prefix + "blk"] = num(r.get("BLK"))
            p[prefix + "tov"] = num(r.get("TOV"))

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
            p["ast2Pct"] = num(r.get("PCT_AST_2PM"))
            p["astFgPct"] = num(r.get("PCT_AST_FGM"))

    try:
        ingest_scoring(get("/leaguedashplayerstats", dash({"MeasureType": "Scoring"})))
    except Exception as e:
        errors["scoring"] = str(e)

    # Catch-&-Shoot vs Pull-Up shot distribution (player tracking) -> powers the C&S / PULL funnels.
    def ptparams(measure):
        return {
            "College": "", "Conference": "", "Country": "", "DateFrom": "", "DateTo": "",
            "Division": "", "DraftPick": "", "DraftYear": "", "GameScope": "", "Height": "",
            "LastNGames": "0", "LeagueID": LEAGUE, "Location": "", "Month": "0",
            "OpponentTeamID": "0", "Outcome": "", "PORound": "0", "PerMode": "PerGame",
            "PlayerExperience": "", "PlayerOrTeam": "Player", "PlayerPosition": "",
            "PtMeasureType": measure, "Season": SEASON, "SeasonSegment": "",
            "SeasonType": "Regular Season", "StarterBench": "", "TeamID": "0",
            "VsConference": "", "VsDivision": "", "Weight": "",
        }

    def ingest_pt(js, fld, key):
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            players[pid][key] = num(r.get(fld))

    try:
        ingest_pt(get("/leaguedashptstats", ptparams("CatchShoot")), "CATCH_SHOOT_FGA", "csFga")
    except Exception as e:
        errors["catchShoot"] = str(e)
    try:
        ingest_pt(get("/leaguedashptstats", ptparams("PullUpShot")), "PULL_UP_FGA", "puFga")
    except Exception as e:
        errors["pullUp"] = str(e)

    # Player positions (single call) -> G/F/C bucket. Needed for defense-vs-position aggregation.
    def pos_bucket(s):
        head = (s or "").upper().strip().split("-")[0].strip()
        if head.startswith("G"):
            return "G"
        if head.startswith("F"):
            return "F"
        if head.startswith("C"):
            return "C"
        return None

    roster_ids = set()
    try:
        pidx = get("/playerindex", {
            "College": "", "Country": "", "DraftPick": "", "DraftRound": "", "DraftYear": "",
            "Height": "", "Historical": "0", "LeagueID": LEAGUE, "Season": SEASON,
            "SeasonType": "Regular Season", "TeamID": "0", "Weight": "", "Active": "", "AllStar": "",
        })
        for r in rows(pidx):
            pid = r.get("PERSON_ID")
            b = pos_bucket(r.get("POSITION"))
            _tid = r.get("TEAM_ID")
            if pid is not None and _tid:
                roster_ids.add(pid)
            if pid in players and b:
                players[pid]["pos"] = b
    except Exception as e:
        errors["positions"] = str(e)

    # defense-vs-position accumulator: opp_abbr -> pos -> running totals (filled during ingest_logs)
    dvp_acc = {}

    def _fs(pts, reb, ast, stl, blk, tov):
        # PrizePicks WNBA fantasy score
        return (pts or 0) + 1.2 * (reb or 0) + 1.5 * (ast or 0) + 3 * (stl or 0) + 3 * (blk or 0) - (tov or 0)

    # Per-game logs (recent games) -> powers L10 hit-rate + out/usage flags.
    # PerMode=Totals gives each game's actual raw stat line.
    def ingest_logs(js):
        tmp = {}
        dvp_rows = []  # (opp, pos, date, stats) -> filtered to each defense's last 10 games
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None:
                continue
            mn = num(r.get("MIN"))
            fga = num(r.get("FGA")); fg3a = num(r.get("FG3A")); ftm = num(r.get("FTM")); fta = num(r.get("FTA")); pts = num(r.get("PTS"))
            reb = num(r.get("REB")); ast = num(r.get("AST"))
            oreb = num(r.get("OREB")); dreb = num(r.get("DREB"))
            stl = num(r.get("STL")); blk = num(r.get("BLK")); tov = num(r.get("TOV"))
            tmp.setdefault(pid, []).append({
                "d": r.get("GAME_DATE"),
                "min": mn,
                "fga": fga,
                "fg3a": fg3a,
                "fg3m": num(r.get("FG3M")),
                "pts": pts,
                "ftm": ftm,
                "fta": num(r.get("FTA")),
                "reb": reb, "oreb": oreb, "dreb": dreb, "ast": ast, "stl": stl, "blk": blk, "tov": tov,
            })
            # defense-vs-position: attribute this opposing line to the defense (the opponent).
            # collected first, then restricted to each defense's last 10 games below.
            pos = (players.get(pid) or {}).get("pos")
            mu = r.get("MATCHUP") or ""
            opp = None
            for sep in (" @ ", " vs. ", " vs "):
                if sep in mu:
                    opp = mu.split(sep)[-1].strip()
                    break
            if pos and opp and (mn or 0) >= 1:
                dvp_rows.append((opp, pos, r.get("GAME_DATE"), {
                    "fga": fga, "fg3a": fg3a, "ftm": ftm, "fta": fta, "pts": pts,
                    "reb": reb, "oreb": oreb, "dreb": dreb, "ast": ast, "stl": stl, "blk": blk, "tov": tov,
                }))
        # OPPONENT STATS OVER THE LAST 10 GAMES: keep only each defense's 10 most-recent game dates
        opp_dates = {}
        for (_opp, _pos, _dt, _st) in dvp_rows:
            opp_dates.setdefault(_opp, set()).add(_dt or "")
        opp_keep = {_opp: set(sorted(_ds, reverse=True)[:10]) for _opp, _ds in opp_dates.items()}
        for (_opp, _pos, _dt, _st) in dvp_rows:
            if (_dt or "") not in opp_keep.get(_opp, set()):
                continue
            d = dvp_acc.setdefault(_opp, {}).setdefault(_pos, {"gp": 0, "fga": 0.0, "fg3a": 0.0, "twopa": 0.0, "ftm": 0.0, "fta": 0.0, "fs": 0.0, "pts": 0.0, "reb": 0.0, "oreb": 0.0, "dreb": 0.0, "ast": 0.0, "stl": 0.0, "blk": 0.0, "tov": 0.0})
            d["gp"] += 1
            d["fga"] += (_st["fga"] or 0)
            d["fg3a"] += (_st["fg3a"] or 0)
            d["twopa"] += ((_st["fga"] or 0) - (_st["fg3a"] or 0))
            d["ftm"] += (_st["ftm"] or 0)
            d["fta"] += (_st["fta"] or 0)
            d["fs"] += _fs(_st["pts"], _st["reb"], _st["ast"], _st["stl"], _st["blk"], _st["tov"])
            d["pts"] += (_st["pts"] or 0)
            d["reb"] += (_st["reb"] or 0)
            d["oreb"] += (_st["oreb"] or 0)
            d["dreb"] += (_st["dreb"] or 0)
            d["ast"] += (_st["ast"] or 0)
            d["stl"] += (_st["stl"] or 0)
            d["blk"] += (_st["blk"] or 0)
            d["tov"] += (_st["tov"] or 0)
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
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Advanced", "LastNGames": "10"}))):
            tid = r.get("TEAM_ID")
            teams[tid] = {"id": tid, "abbr": id2abbr.get(tid) or r.get("TEAM_ABBREVIATION"),
                "pace": num(r.get("PACE")), "gp": num(r.get("GP"))}
    except Exception as e:
        errors["teamPace"] = str(e)

    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Opponent", "LastNGames": "10"}))):
            tid = r.get("TEAM_ID")
            t = teams.get(tid) or teams.setdefault(tid, {"id": tid, "abbr": id2abbr.get(tid) or r.get("TEAM_ABBREVIATION")})
            t["oppFga"] = num(r.get("OPP_FGA"))
            t["oppFg3a"] = num(r.get("OPP_FG3A"))
            t["oppFg3Pct"] = num(r.get("OPP_FG3_PCT"))
            t["oppFta"] = num(r.get("OPP_FTA"))
            # opponent assist rate = share of allowed FGs that were assisted.
            # low = this defense forces self-creation (good spot for a self-creator).
            _oa, _of = r.get("OPP_AST"), r.get("OPP_FGM")
            try:
                t["oppAstRate"] = round(float(_oa) / float(_of), 3) if (_oa not in (None, "") and _of not in (None, "", 0)) else None
            except Exception:
                t["oppAstRate"] = None
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
        ingest_team_zones(get("/leaguedashteamshotlocations", dash({"MeasureType": "Opponent", "DistanceRange": "By Zone", "LastNGames": "10"})))
    except Exception as e:
        errors["teamZoneDef"] = str(e)

    # attach defense-vs-position per-game allowances (G/F/C) to each team
    abbr2id = {v: k for k, v in id2abbr.items()}
    for opp_abbr, posmap in dvp_acc.items():
        tid = abbr2id.get(opp_abbr)
        if tid is None or tid not in teams:
            continue
        dvp = {}
        for pos, d in posmap.items():
            gp = d.get("gp") or 0
            if gp <= 0:
                continue
            dvp[pos] = {
                "gp": gp,
                "fga": round(d["fga"] / gp, 2),
                "fg3a": round(d["fg3a"] / gp, 2),
                "twopa": round(d["twopa"] / gp, 2),
                "ftm": round(d["ftm"] / gp, 2),
                "fta": round(d["fta"] / gp, 2),
                "fs": round(d["fs"] / gp, 2),
                "pts": round(d["pts"] / gp, 2),
                "reb": round(d["reb"] / gp, 2),
                "oreb": round(d["oreb"] / gp, 2),
                "dreb": round(d["dreb"] / gp, 2),
                "ast": round(d["ast"] / gp, 2),
                "stl": round(d["stl"] / gp, 2),
                "blk": round(d["blk"] / gp, 2),
                "tov": round(d["tov"] / gp, 2),
            }
        if dvp:
            teams[tid]["dvp"] = dvp

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

    # PLAYER CHIPS = SEASON RATES, qualifier >=5 GP and >=16 MPG, current rosters only.
    MIN_GP, MIN_MPG = 5, 16
    _kept = {}
    for _pid, _p in players.items():
        if (_p.get("gp") or 0) < MIN_GP or (_p.get("min") or 0) < MIN_MPG:
            continue
        if roster_ids and _pid not in roster_ids:
            continue
        _kept[_pid] = _p
    players = _kept

    out = {
        "updated": datetime.datetime.utcnow().isoformat() + "Z",
        "season": SEASON, "gameDate": game_date,
        "counts": {"games": len(games), "players": len(players), "teams": len(teams), "injListed": len(inj_map), "injMatched": inj_matched, "posMatched": sum(1 for p in players.values() if p.get("pos")), "dvpTeams": sum(1 for t in teams.values() if t.get("dvp"))},
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
