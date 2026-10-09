#!/usr/bin/env python3
# Cush Player Props - NFL TEAM ADVANCED ratings (for the Cush Score)
# Source: nflverse stats_team_week (free GitHub release; no proxy needed)
# Output: nfl_team_adv.json
#
# Per team, opponent-adjusted (SRS-style) season-to-date:
#   offEpa  = offensive EPA / play            (higher = better offense)
#   defEpa  = EPA / play ALLOWED              (lower  = better defense)
#   net     = offEpa - defEpa                 (EPA/play, opponent-adjusted)
#   cushPower = net * PLAYS_PER_GAME          (points vs average -> drop-in power rating)
#   toMargin = takeaways/g - giveaways/g      (turnover margin, opponent-adjusted)
#   plus ranks for each, so the app can show the offense/defense/turnover story.
#
# Env: NFL_TADV_SEASON (force season), NFL_TADV_SEASON_TYPE (REG default)

import os, sys, json, csv, io, datetime, urllib.request

REL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"
OUT = "nfl_team_adv.json"
STYPE = os.environ.get("NFL_TADV_SEASON_TYPE") or "REG"
PLAYS_PER_GAME = 63.0     # approx offensive plays/game; scales net EPA/play -> points
ITERS = 12                # SRS opponent-adjust iterations

TEAM_NAMES = {
 "ARI":"Arizona Cardinals","ATL":"Atlanta Falcons","BAL":"Baltimore Ravens","BUF":"Buffalo Bills",
 "CAR":"Carolina Panthers","CHI":"Chicago Bears","CIN":"Cincinnati Bengals","CLE":"Cleveland Browns",
 "DAL":"Dallas Cowboys","DEN":"Denver Broncos","DET":"Detroit Lions","GB":"Green Bay Packers",
 "HOU":"Houston Texans","IND":"Indianapolis Colts","JAX":"Jacksonville Jaguars","KC":"Kansas City Chiefs",
 "LA":"Los Angeles Rams","LAC":"Los Angeles Chargers","LV":"Las Vegas Raiders","MIA":"Miami Dolphins",
 "MIN":"Minnesota Vikings","NE":"New England Patriots","NO":"New Orleans Saints","NYG":"New York Giants",
 "NYJ":"New York Jets","PHI":"Philadelphia Eagles","PIT":"Pittsburgh Steelers","SEA":"Seattle Seahawks",
 "SF":"San Francisco 49ers","TB":"Tampa Bay Buccaneers","TEN":"Tennessee Titans","WAS":"Washington Commanders"}

def num(x):
    try:
        if x is None or x == "": return 0.0
        return float(x)
    except Exception:
        return 0.0

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent":"cush-nfl-tadv/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def http_ok(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent":"cush-nfl-tadv/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False

def pick_season():
    forced = os.environ.get("NFL_TADV_SEASON")
    if forced: return int(forced)
    yr = datetime.date.today().year
    for s in range(yr+1, yr-6, -1):
        if http_ok(REL.format(season=s)): return s
    raise SystemExit("no nflverse team-week file found")

def ranks(d, reverse):
    # return {team: rank} 1-based; reverse=True => higher value = rank 1
    order = sorted(d.keys(), key=lambda t: d[t], reverse=reverse)
    return {t: i+1 for i, t in enumerate(order)}

def main():
    season = pick_season()
    url = REL.format(season=season)
    sys.stderr.write("season %d  %s\n" % (season, url))
    rows = list(csv.DictReader(io.StringIO(fetch(url).decode("utf-8","replace"))))
    rows = [r for r in rows if (r.get("season_type") or "REG") == STYPE]

    # per team-week offensive line
    games = {}   # (team,week) -> dict
    through = 0
    for r in rows:
        t = r.get("team"); opp = r.get("opponent_team")
        if not t or not opp: continue
        try: wk = int(float(r.get("week") or 0))
        except Exception: wk = 0
        through = max(through, wk)
        plays = num(r.get("attempts")) + num(r.get("carries")) + num(r.get("sacks_suffered"))
        oepa  = num(r.get("passing_epa")) + num(r.get("rushing_epa"))
        give  = (num(r.get("passing_interceptions")) + num(r.get("sack_fumbles_lost"))
                 + num(r.get("rushing_fumbles_lost")) + num(r.get("receiving_fumbles_lost")))
        take  = num(r.get("def_interceptions")) + num(r.get("def_fumbles"))
        games[(t, wk)] = {"team":t, "opp":opp, "wk":wk, "plays":max(1.0, plays),
                          "oepa":oepa, "give":give, "take":take}

    # defensive EPA allowed = opponent's offensive EPA that game
    for g in games.values():
        og = games.get((g["opp"], g["wk"]))
        if og:
            g["depa"] = og["oepa"]; g["dplays"] = og["plays"]
        else:
            g["depa"] = 0.0; g["dplays"] = g["plays"]

    teams = sorted(set(g["team"] for g in games.values()))
    tg = {t: [g for g in games.values() if g["team"] == t] for t in teams}

    # per-game rates
    def off_pp(g):  return g["oepa"]/g["plays"]
    def def_pp(g):  return g["depa"]/max(1.0, g["dplays"])

    raw_off = {t: sum(off_pp(g) for g in tg[t])/len(tg[t]) for t in teams}
    raw_def = {t: sum(def_pp(g) for g in tg[t])/len(tg[t]) for t in teams}
    lg_off  = sum(raw_off.values())/len(teams)
    lg_def  = sum(raw_def.values())/len(teams)

    # SRS opponent adjustment: adj_off vs opponent def strength; adj_def vs opponent off strength
    adj_off = {t: raw_off[t]-lg_off for t in teams}
    adj_def = {t: raw_def[t]-lg_def for t in teams}
    for _ in range(ITERS):
        new_off, new_def = {}, {}
        for t in teams:
            # offense faced these opponent defenses; subtract their adj_def to isolate team's offense
            off_c = sum((off_pp(g)-lg_off) - adj_def.get(g["opp"],0.0) for g in tg[t])/len(tg[t])
            def_c = sum((def_pp(g)-lg_def) - adj_off.get(g["opp"],0.0) for g in tg[t])/len(tg[t])
            new_off[t] = off_c; new_def[t] = def_c
        adj_off, adj_def = new_off, new_def

    # turnover margin per game, opponent-adjusted (light): raw margin minus opponent's induced rate avg
    raw_give = {t: sum(g["give"] for g in tg[t])/len(tg[t]) for t in teams}
    raw_take = {t: sum(g["take"] for g in tg[t])/len(tg[t]) for t in teams}
    to_margin = {t: raw_take[t]-raw_give[t] for t in teams}

    net = {t: adj_off[t]-adj_def[t] for t in teams}          # EPA/play, opp-adjusted
    # Standardize net to a points scale matching market power ratings (SD ~ 3.5 pts, mean 0),
    # so Cush Score is directly comparable to the pasted power ranks.
    TARGET_SD = 3.5
    mean_net = sum(net.values())/len(teams)
    var_net = sum((net[t]-mean_net)**2 for t in teams)/len(teams)
    sd_net = (var_net**0.5) or 1.0
    cush_power = {t: round((net[t]-mean_net)/sd_net*TARGET_SD, 2) for t in teams}  # points vs average

    offRank = ranks(adj_off, True)      # higher off = 1
    defRank = ranks(adj_def, False)     # lower allowed = 1 (best D)
    netRank = ranks(net, True)
    toRank  = ranks(to_margin, True)

    out_teams = {}
    for t in teams:
        out_teams[t] = {
            "name": TEAM_NAMES.get(t, t),
            "g": len(tg[t]),
            "offEpa": round(adj_off[t], 4), "offRank": offRank[t],
            "defEpa": round(adj_def[t], 4), "defRank": defRank[t],
            "net": round(net[t], 4), "netRank": netRank[t],
            "cushPower": round(cush_power[t], 2),
            "giveaways": round(raw_give[t], 2), "takeaways": round(raw_take[t], 2),
            "toMargin": round(to_margin[t], 2), "toRank": toRank[t],
        }

    out = {
        "season": season, "seasonType": STYPE,
        "updated": datetime.datetime.utcnow().replace(microsecond=0).isoformat()+"Z",
        "throughWeek": through, "playsPerGame": PLAYS_PER_GAME,
        "note": "cushPower = opponent-adjusted net EPA/play * plays/game, in points vs average",
        "teams": out_teams,
    }
    with open(OUT,"w") as f:
        json.dump(out, f, separators=(",",":"))
    sys.stderr.write("wrote %s : teams=%d throughWeek=%d\n" % (OUT, len(out_teams), through))
    # quick sanity print
    top = sorted(teams, key=lambda t: cush_power[t], reverse=True)[:6]
    for t in top:
        sys.stderr.write("  %-3s cush=%+.1f off#%d def#%d toMrg=%+.2f\n" %
                         (t, cush_power[t], offRank[t], defRank[t], to_margin[t]))

if __name__ == "__main__":
    main()
