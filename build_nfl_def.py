#!/usr/bin/env python3
# Cush Player Props - NFL DEFENSE / IDP pipeline
# Source: nflverse-data weekly player stats (free, GitHub release assets; no proxy/bot-detection needed)
# Output: nfl_def.json  ->  { season, updated, throughWeek, players[], funnel{} }
#
#   players[]  = per defensive player: season IDP line + per-game log (for trend / hit-rate)
#   funnel{}   = per OFFENSE team: tackles ALLOWED to each defensive position bucket,
#                per game + rank 1..32 (1 = allows the MOST = best matchup for an Over).
#                group = DL / LB / DB   ;   detail = EDGE / IDL / ILB / OLB / CB / S
#
# Env overrides: NFL_DEF_SEASON (force a season), NFL_DEF_SEASON_TYPE (REG default)

import os, sys, json, csv, io, time, datetime, urllib.request

REL = "https://github.com/nflverse/nflverse-data/releases/download/player_stats/stats_player_week_{season}.csv"
OUT = "nfl_def.json"
STYPE = os.environ.get("NFL_DEF_SEASON_TYPE") or "REG"
MIN_SEASON_COMB = 5          # drop players with < this many combined tackles on the season (noise)
LOG_FIELDS = ["w", "opp", "solo", "ast", "comb", "tfl", "sack", "qbh", "pd", "int"]

# ---- position bucketing -------------------------------------------------------
# position_group gives DL / LB / DB cleanly (excludes WR/RB/TE/OL/QB/SPEC tacklers).
GROUPS = ("DL", "LB", "DB")
def detail_bucket(pos):
    p = (pos or "").upper()
    if p in ("DE",):                return "EDGE"
    if p in ("DT", "NT"):           return "IDL"
    if p in ("ILB", "MLB", "LB"):   return "ILB"
    if p in ("OLB",):               return "OLB"
    if p in ("CB",):                return "CB"
    if p in ("FS", "SS", "S", "DB"):return "S"
    return None
DETAIL_ORDER = ["EDGE", "IDL", "ILB", "OLB", "CB", "S"]

def num(x):
    try:
        if x is None or x == "": return 0.0
        return float(x)
    except Exception:
        return 0.0

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cush-nfl-def/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def http_ok(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "cush-nfl-def/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False

def pick_season():
    forced = os.environ.get("NFL_DEF_SEASON")
    if forced:
        return int(forced)
    this_year = datetime.date.today().year
    for s in range(this_year + 1, this_year - 6, -1):   # newest first
        if http_ok(REL.format(season=s)):
            return s
    raise SystemExit("no nflverse player-week file found")

def main():
    season = pick_season()
    url = REL.format(season=season)
    sys.stderr.write("season %d  %s\n" % (season, url))
    raw = fetch(url)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    rows = [r for r in rows if (r.get("season_type") or "REG") == STYPE]
    sys.stderr.write("rows (%s): %d\n" % (STYPE, len(rows)))

    # keep real defenders only
    defrows = [r for r in rows if (r.get("position_group") or "") in GROUPS]

    through = 0
    for r in defrows:
        try: through = max(through, int(float(r.get("week") or 0)))
        except Exception: pass

    # ---- per-player aggregation ------------------------------------------------
    players = {}   # id -> acc
    for r in defrows:
        pid = r.get("player_id") or r.get("gsis_id") or r.get("player_name")
        if not pid: continue
        team = r.get("team") or r.get("recent_team") or ""
        p = players.get(pid)
        if p is None:
            p = players[pid] = {
                "id": pid,
                "name": r.get("player_display_name") or r.get("player_name") or "",
                "team": team, "pos": r.get("position") or "", "posGroup": r.get("position_group") or "",
                "g": 0, "solo": 0.0, "ast": 0.0, "comb": 0.0, "tfl": 0.0,
                "sack": 0.0, "qbh": 0.0, "pd": 0.0, "int": 0.0, "ff": 0.0,
                "_log": [],
            }
        p["team"] = team  # latest team
        p["pos"] = r.get("position") or p["pos"]
        solo = num(r.get("def_tackles_solo")); ast = num(r.get("def_tackle_assists"))
        comb = solo + ast
        tfl = num(r.get("def_tackles_for_loss")); sack = num(r.get("def_sacks"))
        qbh = num(r.get("def_qb_hits")); pd = num(r.get("def_pass_defended"))
        inter = num(r.get("def_interceptions")); ff = num(r.get("def_fumbles_forced"))
        p["g"] += 1
        p["solo"] += solo; p["ast"] += ast; p["comb"] += comb; p["tfl"] += tfl
        p["sack"] += sack; p["qbh"] += qbh; p["pd"] += pd; p["int"] += inter; p["ff"] += ff
        try: wk = int(float(r.get("week") or 0))
        except Exception: wk = 0
        p["_log"].append({"w": wk, "opp": r.get("opponent_team") or "",
                          "solo": round(solo), "ast": round(ast), "comb": round(comb),
                          "tfl": round(tfl, 1), "sack": round(sack, 1),
                          "qbh": round(qbh), "pd": round(pd), "int": round(inter)})

    plist = []
    for p in players.values():
        if p["comb"] < MIN_SEASON_COMB:
            continue
        g = max(1, p["g"])
        log = sorted(p["_log"], key=lambda x: x["w"])
        rec = {
            "id": p["id"], "name": p["name"], "team": p["team"],
            "pos": p["pos"], "posGroup": p["posGroup"], "g": p["g"],
            "comb": round(p["comb"]), "combG": round(p["comb"]/g, 1),
            "solo": round(p["solo"]), "soloG": round(p["solo"]/g, 1),
            "ast": round(p["ast"]),  "astG":  round(p["ast"]/g, 1),
            "tfl": round(p["tfl"], 1), "sack": round(p["sack"], 1),
            "qbh": round(p["qbh"]), "pd": round(p["pd"]),
            "int": round(p["int"]), "ff": round(p["ff"]),
            "log": [[r[k] for k in LOG_FIELDS] for r in log],
        }
        plist.append(rec)
    plist.sort(key=lambda x: x["combG"], reverse=True)

    # ---- matchup funnel: tackles ALLOWED to each position by each offense -------
    # offense = defensive player's opponent_team.  bucket by group + detail.
    off = {}   # team -> {"weeks":set, "group":{DL:sum}, "detail":{EDGE:sum}}
    for r in defrows:
        offense = r.get("opponent_team") or ""
        if not offense: continue
        grp = r.get("position_group") or ""
        if grp not in GROUPS: continue
        comb = num(r.get("def_tackles_solo")) + num(r.get("def_tackle_assists"))
        try: wk = int(float(r.get("week") or 0))
        except Exception: wk = 0
        o = off.get(offense)
        if o is None:
            o = off[offense] = {"weeks": set(),
                                "group": {k: 0.0 for k in GROUPS},
                                "detail": {k: 0.0 for k in DETAIL_ORDER}}
        o["weeks"].add(wk)
        o["group"][grp] += comb
        db = detail_bucket(r.get("position"))
        if db: o["detail"][db] += comb

    # per-game rates
    funnel = {}
    for team, o in off.items():
        g = max(1, len(o["weeks"]))
        funnel[team] = {
            "g": len(o["weeks"]),
            "group": {k: {"perG": round(o["group"][k]/g, 2)} for k in GROUPS},
            "detail": {k: {"perG": round(o["detail"][k]/g, 2)} for k in DETAIL_ORDER},
        }
    # ranks (1 = most allowed = green/favorable)
    def rank_bucket(section, keys):
        for k in keys:
            arr = sorted(funnel.keys(), key=lambda t: funnel[t][section][k]["perG"], reverse=True)
            n = len(arr)
            for i, t in enumerate(arr):
                funnel[t][section][k]["rank"] = i + 1
                funnel[t][section][k]["n"] = n
    rank_bucket("group", GROUPS)
    rank_bucket("detail", DETAIL_ORDER)

    out = {
        "season": season,
        "seasonType": STYPE,
        "updated": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "throughWeek": through,
        "detailOrder": DETAIL_ORDER,
        "logFields": LOG_FIELDS,
        "players": plist,
        "funnel": funnel,
    }
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    sys.stderr.write("wrote %s : players=%d funnel_teams=%d throughWeek=%d\n"
                     % (OUT, len(plist), len(funnel), through))

if __name__ == "__main__":
    main()
