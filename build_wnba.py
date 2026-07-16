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
            p[prefix + "pf"] = num(r.get("PF"))       # personal fouls per game -> foul-trouble flag

    try:
        ingest(get("/leaguedashplayerstats", dash({"LastNGames": "0"})), "")
    except Exception as e:
        errors["playersSeason"] = str(e)
    try:
        ingest(get("/leaguedashplayerstats", dash({"LastNGames": "10"})), "r_")
    except Exception as e:
        errors["playersRecent"] = str(e)

    def ingest_zones(js):
        def _pct(fgm, fga):
            try:
                return round(float(fgm) / float(fga), 3) if (fgm not in (None, "") and fga not in (None, "", 0)) else None
            except Exception:
                return None
        for r in shot_zone_rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            p = players[pid]
            g = lambda z: num(r.get(z + "|FGA"))
            gm = lambda z: num(r.get(z + "|FGM"))
            lc, rc = g("Left Corner 3"), g("Right Corner 3")
            lcm, rcm = gm("Left Corner 3"), gm("Right Corner 3")
            p["z_ra"] = g("Restricted Area")
            p["z_paint"] = g("In The Paint (Non-RA)")
            p["z_mid"] = g("Mid-Range")
            p["z_corner3"] = round((lc or 0) + (rc or 0), 3)
            p["z_above3"] = g("Above the Break 3")
            # player FG% BY ZONE -> powers the Points / 3PM / 2PM efficiency Cush scores
            p["z_ra_pct"] = _pct(gm("Restricted Area"), g("Restricted Area"))
            p["z_paint_pct"] = _pct(gm("In The Paint (Non-RA)"), g("In The Paint (Non-RA)"))
            p["z_mid_pct"] = _pct(gm("Mid-Range"), g("Mid-Range"))
            p["z_corner3_pct"] = _pct((lcm or 0) + (rcm or 0), (lc or 0) + (rc or 0))
            p["z_above3_pct"] = _pct(gm("Above the Break 3"), g("Above the Break 3"))

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

    # Passing tracking -> how a PLAYER'S OWN ASSISTS split between 3s and 2s.
    # Every assist is on a made field goal (2 or 3; free throws are never assisted),
    # so the average points per assist gives the exact mix:
    #   ppa = AST_PTS_CREATED / AST   (lands between 2 and 3)
    #   astTo3 = ppa - 2 ;  astTo2 = 1 - astTo3
    # This is the passer side (where her dimes go), distinct from ast3Pct/ast2Pct
    # (the shooter side = how often her own makes are assisted).
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

    def ingest_passing(js):
        rs = rows(js)
        wrote = 0
        for r in rs:
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            ast = num(r.get("AST"))
            apc = num(r.get("AST_POINTS_CREATED"))
            if apc is None:
                apc = num(r.get("AST_PTS_CREATED"))  # fallback field name
            if not ast or ast <= 0 or apc is None:
                continue
            ppa = apc / ast
            a3 = ppa - 2.0
            if a3 < 0:
                a3 = 0.0
            if a3 > 1:
                a3 = 1.0
            players[pid]["astTo3"] = round(a3, 3)
            players[pid]["astTo2"] = round(1.0 - a3, 3)
            wrote += 1
        # diagnostic: if nothing was written, record what the endpoint actually returned
        # (rows=0 -> WNBA has no passing tracking; rows>0 -> a field-name mismatch, keys shown)
        if wrote == 0:
            keys = sorted(rs[0].keys())[:45] if rs else []
            errors["passing_diag"] = "rows=%d wrote=0 keys=%s" % (len(rs), ",".join(keys))

    try:
        ingest_passing(get("/leaguedashptstats", ptparams("Passing")))
    except Exception as e:
        errors["passing"] = str(e)

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

    try:
        pidx = get("/playerindex", {
            "College": "", "Country": "", "DraftPick": "", "DraftRound": "", "DraftYear": "",
            "Height": "", "Historical": "0", "LeagueID": LEAGUE, "Season": SEASON,
            "SeasonType": "Regular Season", "TeamID": "0", "Weight": "", "Active": "", "AllStar": "",
        })
        for r in rows(pidx):
            pid = r.get("PERSON_ID")
            b = pos_bucket(r.get("POSITION"))
            if pid in players and b:
                players[pid]["pos"] = b
    except Exception as e:
        errors["positions"] = str(e)

    # defense-vs-position accumulator: opp_abbr -> pos -> running totals (filled during ingest_logs)
    dvp_acc = {}
    # raw per-line capture (opp_abbr, pos, game_date, statline) so we can build an L10 DVP below
    dvp_raw = []

    def _fs(pts, reb, ast, stl, blk, tov):
        # PrizePicks WNBA fantasy score
        return (pts or 0) + 1.2 * (reb or 0) + 1.5 * (ast or 0) + 3 * (stl or 0) + 3 * (blk or 0) - (tov or 0)

    # Per-game logs (recent games) -> powers L10 hit-rate + out/usage flags.
    # PerMode=Totals gives each game's actual raw stat line.
    def ingest_logs(js):
        tmp = {}
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
            # defense-vs-position: attribute this opposing line to the defense (the opponent)
            pos = (players.get(pid) or {}).get("pos")
            mu = r.get("MATCHUP") or ""
            opp = None
            for sep in (" @ ", " vs. ", " vs "):
                if sep in mu:
                    opp = mu.split(sep)[-1].strip()
                    break
            if pos and opp and (mn or 0) >= 1:
                d = dvp_acc.setdefault(opp, {}).setdefault(pos, {"gp": 0, "fga": 0.0, "fg3a": 0.0, "twopa": 0.0, "ftm": 0.0, "fta": 0.0, "fs": 0.0, "pts": 0.0, "reb": 0.0, "oreb": 0.0, "dreb": 0.0, "ast": 0.0, "stl": 0.0, "blk": 0.0, "tov": 0.0})
                d["gp"] += 1
                d["fga"] += (fga or 0)
                d["fg3a"] += (fg3a or 0)
                d["twopa"] += ((fga or 0) - (fg3a or 0))
                d["ftm"] += (ftm or 0)
                d["fta"] += (fta or 0)
                d["fs"] += _fs(pts, reb, ast, stl, blk, tov)
                d["pts"] += (pts or 0)
                d["reb"] += (reb or 0)
                d["oreb"] += (oreb or 0)
                d["dreb"] += (dreb or 0)
                d["ast"] += (ast or 0)
                d["stl"] += (stl or 0)
                d["blk"] += (blk or 0)
                d["tov"] += (tov or 0)
                dvp_raw.append((opp, pos, r.get("GAME_DATE"), {
                    "fga": (fga or 0), "fg3a": (fg3a or 0), "twopa": ((fga or 0) - (fg3a or 0)),
                    "ftm": (ftm or 0), "fta": (fta or 0), "fs": _fs(pts, reb, ast, stl, blk, tov),
                    "pts": (pts or 0), "reb": (reb or 0), "oreb": (oreb or 0), "dreb": (dreb or 0),
                    "ast": (ast or 0), "stl": (stl or 0), "blk": (blk or 0), "tov": (tov or 0),
                }))
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

    # ---- ESPN scoreboard fallback: stats.wnba.com occasionally drops a game for a date;
    # cross-check ESPN's schedule and add any game the stats feed missed so the slate is complete. ----
    try:
        _ESPN_SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
        _ESPN2WNBA = {"CONN": "CON", "GS": "GSV", "GSW": "GSV", "LA": "LAS", "LV": "LVA",
                      "NY": "NYL", "PHO": "PHX", "WSH": "WAS", "POR": "PDX"}
        _abbr2id = {}
        for _tid, _ab in id2abbr.items():
            if _ab:
                _abbr2id[_ab] = _tid
        _have = set()
        for _g in games:
            _have.add((_g["date"], _g["home"]["id"], _g["away"]["id"]))
            _have.add((_g["date"], _g["away"]["id"], _g["home"]["id"]))

        def _espn_sb(ymd):
            _u = _ESPN_SB + "?dates=" + ymd + "&limit=50"
            try:
                _r = requests.get(_u, headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}, timeout=30)
                _r.raise_for_status()
                return _r.json()
            except Exception:
                try:
                    return get_url(_u)
                except Exception:
                    return {}

        def _abbr_of(c):
            a = ((c.get("team") or {}).get("abbreviation") or "").upper()
            return _ESPN2WNBA.get(a, a)

        _added, _unmapped = 0, []
        for (_gd, _dow) in _date_list():
            _mm, _dd, _yy = _gd.split("/")
            _js = _espn_sb(_yy + _mm + _dd)
            for _ev in (_js.get("events") or []):
                _comp = (_ev.get("competitions") or [{}])[0]
                _cs = _comp.get("competitors") or []
                _home = next((c for c in _cs if c.get("homeAway") == "home"), None)
                _away = next((c for c in _cs if c.get("homeAway") == "away"), None)
                if not _home or not _away:
                    continue
                _ha, _aa = _abbr_of(_home), _abbr_of(_away)
                _hid, _aid = _abbr2id.get(_ha), _abbr2id.get(_aa)
                if _hid is None or _aid is None:
                    _unmapped.append(_ha + "@" + _aa)
                    continue
                if (_gd, _hid, _aid) in _have or (_gd, _aid, _hid) in _have:
                    continue
                _st = ((_comp.get("status") or {}).get("type") or {})
                games.append({
                    "gameId": "espn_" + str(_ev.get("id")), "status": None,
                    "statusText": (_st.get("shortDetail") or _st.get("detail") or "").strip(),
                    "date": _gd, "day": _dow,
                    "home": {"id": _hid, "abbr": _ha},
                    "away": {"id": _aid, "abbr": _aa},
                })
                _have.add((_gd, _hid, _aid))
                _have.add((_gd, _aid, _hid))
                _added += 1
        if _added:
            errors["espn_sched_added"] = str(_added)
        if _unmapped:
            errors["espn_sched_unmapped"] = ",".join(sorted(set(_unmapped)))[:120]
    except Exception as _e:
        errors["espn_sched"] = str(_e)

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
            # opponent assist rate = share of allowed FGs that were assisted.
            # low = this defense forces self-creation (good spot for a self-creator).
            _oa, _of = r.get("OPP_AST"), r.get("OPP_FGM")
            try:
                t["oppAstRate"] = round(float(_oa) / float(_of), 3) if (_oa not in (None, "") and _of not in (None, "", 0)) else None
            except Exception:
                t["oppAstRate"] = None
    except Exception as e:
        errors["teamOpp"] = str(e)

    # Team OFFENSE free-throw volume -> how much this team DRAWS fouls (attacks the rim). A player who guards a
    # high-FTA-drawing opponent is at more foul-trouble risk. Stored as a rate (FTA per FGA) + raw per-game FTA.
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Base"}))):
            tid = r.get("TEAM_ID")
            t = teams.get(tid) or teams.setdefault(tid, {"id": tid, "abbr": id2abbr.get(tid) or r.get("TEAM_ABBREVIATION")})
            _fta, _fga = num(r.get("FTA")), num(r.get("FGA"))
            t["ftaOff"] = _fta
            t["fgaOff"] = _fga
            try:
                t["ftaRate"] = round(float(_fta) / float(_fga), 3) if (_fta not in (None, "") and _fga not in (None, "", 0)) else None
            except Exception:
                t["ftaRate"] = None
    except Exception as e:
        errors["teamBase"] = str(e)

    def ingest_team_zones(js):
        def _pct(fgm, fga):
            try:
                return round(float(fgm) / float(fga), 3) if (fgm not in (None, "") and fga not in (None, "", 0)) else None
            except Exception:
                return None
        for r in shot_zone_rows(js):
            tid = r.get("TEAM_ID")
            if tid is None:
                continue
            t = teams.get(tid) or teams.setdefault(tid, {"id": tid, "abbr": id2abbr.get(tid)})
            gg = lambda z: num(r.get(z + "|FGA"))
            gm = lambda z: num(r.get(z + "|FGM"))
            lc, rc = gg("Left Corner 3"), gg("Right Corner 3")
            lcm, rcm = gm("Left Corner 3"), gm("Right Corner 3")
            t["dz_ra"] = gg("Restricted Area")
            t["dz_paint"] = gg("In The Paint (Non-RA)")
            t["dz_mid"] = gg("Mid-Range")
            t["dz_corner3"] = round((lc or 0) + (rc or 0), 3)
            t["dz_above3"] = gg("Above the Break 3")
            # opponent FG% ALLOWED per zone (efficiency / "how easy the bucket is") -> powers efficiency-true Shredder/Lob
            t["dz_ra_pct"] = _pct(gm("Restricted Area"), gg("Restricted Area"))
            t["dz_paint_pct"] = _pct(gm("In The Paint (Non-RA)"), gg("In The Paint (Non-RA)"))
            t["dz_mid_pct"] = _pct(gm("Mid-Range"), gg("Mid-Range"))
            t["dz_corner3_pct"] = _pct((lcm or 0) + (rcm or 0), (lc or 0) + (rc or 0))
            t["dz_above3_pct"] = _pct(gm("Above the Break 3"), gg("Above the Break 3"))

    try:
        ingest_team_zones(get("/leaguedashteamshotlocations", dash({"MeasureType": "Opponent", "DistanceRange": "By Zone"})))
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

    # ===================== LAST-10-GAMES (L10) SCORING INPUTS =====================
    # Every input the dashboard's Cush score engine consumes, re-pulled over the last
    # 10 games and stored in a nested "l10" block on each player/team. Season fields
    # above are left untouched; the front-end overlays this block when L10 is selected.
    def _pct10(a, b):
        try:
            return round(float(a) / float(b), 3) if (a not in (None, "") and b not in (None, "", 0)) else None
        except Exception:
            return None

    # --- players: L10 base stats (remap the already-fetched r_* last-10 averages) ---
    _BASEMAP = {"gp": "r_gp", "min": "r_min", "fga": "r_fga", "fg3a": "r_fg3a", "pts": "r_pts",
                "ftm": "r_ftm", "fta": "r_fta", "ftPct": "r_ftPct", "reb": "r_reb", "ast": "r_ast",
                "stl": "r_stl", "blk": "r_blk", "tov": "r_tov", "pf": "r_pf"}
    for _p in players.values():
        _d = _p.setdefault("l10", {})
        for _k, _rk in _BASEMAP.items():
            if _p.get(_rk) is not None:
                _d[_k] = _p.get(_rk)

    # --- players: L10 shot zones (attempts + FG% by zone) ---
    def ingest_zones_l10(js):
        for r in shot_zone_rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            d = players[pid].setdefault("l10", {})
            g = lambda z: num(r.get(z + "|FGA"))
            gm = lambda z: num(r.get(z + "|FGM"))
            lc, rc = g("Left Corner 3"), g("Right Corner 3")
            lcm, rcm = gm("Left Corner 3"), gm("Right Corner 3")
            d["z_ra"] = g("Restricted Area")
            d["z_paint"] = g("In The Paint (Non-RA)")
            d["z_mid"] = g("Mid-Range")
            d["z_corner3"] = round((lc or 0) + (rc or 0), 3)
            d["z_above3"] = g("Above the Break 3")
            d["z_ra_pct"] = _pct10(gm("Restricted Area"), g("Restricted Area"))
            d["z_paint_pct"] = _pct10(gm("In The Paint (Non-RA)"), g("In The Paint (Non-RA)"))
            d["z_mid_pct"] = _pct10(gm("Mid-Range"), g("Mid-Range"))
            d["z_corner3_pct"] = _pct10((lcm or 0) + (rcm or 0), (lc or 0) + (rc or 0))
            d["z_above3_pct"] = _pct10(gm("Above the Break 3"), g("Above the Break 3"))

    try:
        ingest_zones_l10(get("/leaguedashplayershotlocations", dash({"DistanceRange": "By Zone", "LastNGames": "10"})))
    except Exception as e:
        errors["shotZones_l10"] = str(e)

    # --- players: L10 assisted% (catch&shoot proxy) ---
    def ingest_scoring_l10(js):
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            d = players[pid].setdefault("l10", {})
            d["ast3Pct"] = num(r.get("PCT_AST_3PM"))
            d["ast2Pct"] = num(r.get("PCT_AST_2PM"))
            d["astFgPct"] = num(r.get("PCT_AST_FGM"))

    try:
        ingest_scoring_l10(get("/leaguedashplayerstats", dash({"MeasureType": "Scoring", "LastNGames": "10"})))
    except Exception as e:
        errors["scoring_l10"] = str(e)

    # --- teams: L10 pace / opponent / base / zone-defense ---
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Advanced", "LastNGames": "10"}))):
            tid = r.get("TEAM_ID")
            if tid in teams:
                teams[tid].setdefault("l10", {})["pace"] = num(r.get("PACE"))
    except Exception as e:
        errors["teamPace_l10"] = str(e)
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Opponent", "LastNGames": "10"}))):
            tid = r.get("TEAM_ID")
            if tid not in teams:
                continue
            d = teams[tid].setdefault("l10", {})
            d["oppFga"] = num(r.get("OPP_FGA"))
            d["oppFg3a"] = num(r.get("OPP_FG3A"))
            d["oppFg3Pct"] = num(r.get("OPP_FG3_PCT"))
            d["oppFta"] = num(r.get("OPP_FTA"))
            d["oppAstRate"] = _pct10(r.get("OPP_AST"), r.get("OPP_FGM"))
    except Exception as e:
        errors["teamOpp_l10"] = str(e)
    try:
        for r in rows(get("/leaguedashteamstats", dash({"MeasureType": "Base", "LastNGames": "10"}))):
            tid = r.get("TEAM_ID")
            if tid not in teams:
                continue
            d = teams[tid].setdefault("l10", {})
            _fta, _fga = num(r.get("FTA")), num(r.get("FGA"))
            d["ftaOff"] = _fta
            d["fgaOff"] = _fga
            d["ftaRate"] = _pct10(_fta, _fga)
    except Exception as e:
        errors["teamBase_l10"] = str(e)

    def ingest_team_zones_l10(js):
        for r in shot_zone_rows(js):
            tid = r.get("TEAM_ID")
            if tid is None or tid not in teams:
                continue
            d = teams[tid].setdefault("l10", {})
            gg = lambda z: num(r.get(z + "|FGA"))
            gm = lambda z: num(r.get(z + "|FGM"))
            lc, rc = gg("Left Corner 3"), gg("Right Corner 3")
            lcm, rcm = gm("Left Corner 3"), gm("Right Corner 3")
            d["dz_ra"] = gg("Restricted Area")
            d["dz_paint"] = gg("In The Paint (Non-RA)")
            d["dz_mid"] = gg("Mid-Range")
            d["dz_corner3"] = round((lc or 0) + (rc or 0), 3)
            d["dz_above3"] = gg("Above the Break 3")
            d["dz_ra_pct"] = _pct10(gm("Restricted Area"), gg("Restricted Area"))
            d["dz_paint_pct"] = _pct10(gm("In The Paint (Non-RA)"), gg("In The Paint (Non-RA)"))
            d["dz_mid_pct"] = _pct10(gm("Mid-Range"), gg("Mid-Range"))
            d["dz_corner3_pct"] = _pct10((lcm or 0) + (rcm or 0), (lc or 0) + (rc or 0))
            d["dz_above3_pct"] = _pct10(gm("Above the Break 3"), gg("Above the Break 3"))

    try:
        ingest_team_zones_l10(get("/leaguedashteamshotlocations", dash({"MeasureType": "Opponent", "DistanceRange": "By Zone", "LastNGames": "10"})))
    except Exception as e:
        errors["teamZoneDef_l10"] = str(e)

    # --- teams: L10 defense-vs-position (only each defense's last 10 game-dates) ---
    try:
        by_opp_dates = {}
        for (opp_abbr, pos, gdate, sl) in dvp_raw:
            if opp_abbr and gdate:
                by_opp_dates.setdefault(opp_abbr, set()).add(gdate)
        last10 = {o: set(sorted(ds, reverse=True)[:10]) for o, ds in by_opp_dates.items()}
        _SUMK = ("fga", "fg3a", "twopa", "ftm", "fta", "fs", "pts", "reb", "oreb", "dreb", "ast", "stl", "blk", "tov")
        dvp_acc_l10 = {}
        for (opp_abbr, pos, gdate, sl) in dvp_raw:
            if not opp_abbr or gdate not in last10.get(opp_abbr, ()):
                continue
            d = dvp_acc_l10.setdefault(opp_abbr, {}).setdefault(pos, {"gp": 0})
            d["gp"] += 1
            for kk in _SUMK:
                d[kk] = d.get(kk, 0.0) + (sl.get(kk, 0) or 0)
        for opp_abbr, posmap in dvp_acc_l10.items():
            tid = abbr2id.get(opp_abbr)
            if tid is None or tid not in teams:
                continue
            dvp = {}
            for pos, d in posmap.items():
                gp = d.get("gp") or 0
                if gp <= 0:
                    continue
                dvp[pos] = {"gp": gp}
                for kk in _SUMK:
                    dvp[pos][kk] = round(d.get(kk, 0.0) / gp, 2)
            if dvp:
                teams[tid].setdefault("l10", {})["dvp"] = dvp
    except Exception as e:
        errors["dvp_l10"] = str(e)
    # =================== END LAST-10-GAMES (L10) INPUTS ===================

    # Synergy play types -> player OFFENSE (P&R ball handler / roll man) + team DEFENSE (PPP allowed).
    # WNBA synergy coverage is uncertain, so this is fully guarded and self-diagnosing: the
    # errors["synergy_diag"] counts tell us on the first run whether the WNBA exposes this feed.
    def synparams(playtype, grouping, port):
        return {
            "LeagueID": LEAGUE, "PerMode": "PerGame", "PlayType": playtype,
            "PlayerOrTeam": port, "SeasonType": "Regular Season",
            "SeasonYear": SEASON, "TypeGrouping": grouping,
        }

    def ingest_syn_off(js, pfx):
        n = 0
        for r in rows(js):
            pid = r.get("PLAYER_ID")
            if pid is None or pid not in players:
                continue
            players[pid][pfx + "Ppp"] = num(r.get("PPP"))       # points per possession
            players[pid][pfx + "Freq"] = num(r.get("POSS_PCT"))  # how often the player runs this action
            players[pid][pfx + "Pct"] = num(r.get("PERCENTILE"))
            n += 1
        return n

    def ingest_syn_def(js, pfx):
        n = 0
        for r in rows(js):
            tid = r.get("TEAM_ID")
            if tid is None or tid not in teams:
                continue
            teams[tid][pfx + "Ppp"] = num(r.get("PPP"))          # points per possession ALLOWED
            teams[tid][pfx + "Pct"] = num(r.get("PERCENTILE"))
            n += 1
        return n

    try:
        _snO1 = ingest_syn_off(get("/synergyplaytypes", synparams("PRBallHandler", "offensive", "P")), "prbh")
        _snO2 = ingest_syn_off(get("/synergyplaytypes", synparams("PRRollMan", "offensive", "P")), "prrm")
        _snD1 = ingest_syn_def(get("/synergyplaytypes", synparams("PRBallHandler", "defensive", "T")), "prbhDef")
        _snD2 = ingest_syn_def(get("/synergyplaytypes", synparams("PRRollMan", "defensive", "T")), "prrmDef")
        errors["synergy_diag"] = "prbhOff=%d prrmOff=%d prbhDef=%d prrmDef=%d" % (_snO1, _snO2, _snD1, _snD2)
    except Exception as e:
        errors["synergy"] = str(e)

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
