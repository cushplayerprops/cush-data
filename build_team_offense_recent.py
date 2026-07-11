#!/usr/bin/env python3
"""
build_team_offense_recent.py  ->  team_offense_recent.json

Opponent-OFFENSE feed for the Cush pitcher models (the run/ER + Fantasy side, which is
currently blind to how good the lineup a pitcher faces actually is). For each team it
computes a wRC+-style offense index -- OPS / 0.720 * 100, the SAME proxy the app already
uses for hitters (see build_hand_splits.py) -- split by the handedness of the pitcher
faced, over three windows: season, last 30 days, last 14 days. Keyed by team id:

    "147": {
      "vsL": {"season": 104, "l30": 110, "l14": 96},
      "vsR": {"season": 99,  "l30": 103, "l14": 101},
      "paL30": 690, "paL14": 300
    }

  vsL = the team's offense when facing LHP  (use when the opposing starter throws left)
  vsR = the team's offense when facing RHP  (use when the opposing starter throws right)
  100 = league-average offense; >100 = better/hotter hitting team => tougher spot for the pitcher.
  paL30 / paL14 = plate appearances behind each recent window, so the app can regress thin
  early-window samples toward season.

METHOD (same honest approximation as build_team_k_recent.py): MLB's API gives a clean SEASON
OPS vs L/R (statSplits) but NO per-game hand split. So the recent windows are the team's recent
OVERALL offense (aggregated from the hitting game log over the last N days, raw counting totals
recombined into OPS) scaled by the SEASON vs-hand SHAPE. This catches a lineup running hot/cold
or a roster change while keeping its season platoon shape; it won't detect a team getting better
specifically vs one hand independent of overall, but 2-4 week single-hand samples are too thin to
read that reliably anyway.

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push team_offense_recent.json to root of cushplayerprops/cush-data main -> cron.
"""

import json, sys, time, urllib.request
from datetime import date, timedelta

SEASON   = 2026
MLB_BASE = "https://statsapi.mlb.com/api/v1"
OUT_PATH = "team_offense_recent.json"
TIMEOUT  = 30
LG_OPS   = 0.720          # league-average OPS baseline -> wRC+ index of 100 (matches the app)
WIN_L30  = 30
WIN_L14  = 14
UA = {"User-Agent": "Mozilla/5.0 (cush-teamoff-build)"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            last = e
            time.sleep(1.5 * (k + 1))
    print("  ! fetch failed:", last, file=sys.stderr)
    return None


def _f(stat, key):
    try:
        return float(stat.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _new_tot():
    return {"ab": 0.0, "h": 0.0, "bb": 0.0, "hbp": 0.0, "sf": 0.0,
            "d": 0.0, "t": 0.0, "hr": 0.0, "pa": 0.0}


def _add(tot, st):
    tot["ab"]  += _f(st, "atBats")
    tot["h"]   += _f(st, "hits")
    tot["bb"]  += _f(st, "baseOnBalls")
    tot["hbp"] += _f(st, "hitByPitch")
    tot["sf"]  += _f(st, "sacFlies")
    tot["d"]   += _f(st, "doubles")
    tot["t"]   += _f(st, "triples")
    tot["hr"]  += _f(st, "homeRuns")
    tot["pa"]  += _f(st, "plateAppearances")


def ops_from_totals(t):
    """OPS from summed counting stats; returns (ops, pa) or (None, pa)."""
    ab, h, bb, hbp, sf = t["ab"], t["h"], t["bb"], t["hbp"], t["sf"]
    obp_den = ab + bb + hbp + sf
    if ab <= 0 or obp_den <= 0:
        return None, t["pa"]
    obp = (h + bb + hbp) / obp_den
    tb  = h + t["d"] + 2 * t["t"] + 3 * t["hr"]      # total bases = H + 2B + 2*3B + 3*HR
    slg = tb / ab
    return obp + slg, t["pa"]


def wrc(ops):
    return round((ops / LG_OPS) * 100) if (ops and ops > 0) else None


def season_ops_splits(tid):
    """Return (overall_ops, vL_ops, vR_ops) for the season."""
    ov = _get("%s/teams/%d/stats?stats=season&group=hitting&season=%d&gameType=R"
              % (MLB_BASE, tid, SEASON))
    sp = _get("%s/teams/%d/stats?stats=statSplits&group=hitting&sitCodes=vl,vr&season=%d&gameType=R"
              % (MLB_BASE, tid, SEASON))
    overall = vL = vR = None
    try:
        overall = _f(json.loads(ov)["stats"][0]["splits"][0]["stat"], "ops") or None
    except (KeyError, IndexError, TypeError):
        pass
    try:
        for s in json.loads(sp)["stats"][0]["splits"]:
            code = (s.get("split") or {}).get("code")
            o = _f(s["stat"], "ops") or None
            if code == "vl":
                vL = o
            elif code == "vr":
                vR = o
    except (KeyError, IndexError, TypeError):
        pass
    return overall, vL, vR


def recent_windows(tid):
    """One game-log fetch -> ((l30_ops, pa30), (l14_ops, pa14))."""
    gl = _get("%s/teams/%d/stats?stats=gameLog&group=hitting&season=%d&gameType=R"
              % (MLB_BASE, tid, SEASON))
    try:
        splits = json.loads(gl)["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return (None, 0.0), (None, 0.0)
    cut30 = (date.today() - timedelta(days=WIN_L30)).isoformat()
    cut14 = (date.today() - timedelta(days=WIN_L14)).isoformat()
    t30, t14 = _new_tot(), _new_tot()
    for s in splits:
        d = s.get("date", "")
        if not d or d < cut30:
            continue
        st = s.get("stat", {})
        _add(t30, st)
        if d >= cut14:
            _add(t14, st)
    return ops_from_totals(t30), ops_from_totals(t14)


def build_team(tid):
    ov, vL, vR = season_ops_splits(tid)
    if not ov or ov <= 0:
        return None
    (l30_ops, pa30), (l14_ops, pa14) = recent_windows(tid)

    def side(season_hand):
        sh = season_hand if (season_hand and season_hand > 0) else ov
        ratio = (sh / ov) if ov else 1.0
        out = {"season": wrc(sh)}
        out["l30"] = wrc(l30_ops * ratio) if l30_ops else out["season"]
        out["l14"] = wrc(l14_ops * ratio) if l14_ops else out["season"]
        return out

    return {"vsL": side(vL), "vsR": side(vR),
            "paL30": round(pa30), "paL14": round(pa14)}


def main():
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    out = {}
    for n, t in enumerate(teams, 1):
        rec = build_team(t["id"])
        if rec:
            out[str(t["id"])] = rec
        print("  %d/%d team %d %s" % (n, len(teams), t["id"], rec or "skip"), file=sys.stderr)
        time.sleep(0.1)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d teams (windows: season / L%d / L%d)"
          % (OUT_PATH, len(out), WIN_L30, WIN_L14), file=sys.stderr)


if __name__ == "__main__":
    main()
