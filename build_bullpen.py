#!/usr/bin/env python3
"""
build_bullpen.py  ->  bullpen.json

Per-team bullpen-quality feed for the Cush hitter model. For each MLB team it
aggregates its RELIEVERS' season lines and emits the four stats your pitcher
index (`pcushLayerZ`) already scores, keyed by team id:

    "147": { "fip": 3.92, "xwoba": null, "whip": 1.21, "woba": 0.308 }

The front-end runs each team through `pcushLayerZ`, ranks all 30 (1 = nastiest),
shows the rank in the OPP PEN column, and powers the HIDE TOP PENS filter. Absent
feed => column shows a dash, filter no-ops. Nothing breaks.

WHY THESE FOUR: they map 1:1 to pcushLayerZ's pred layer (fip + xwoba) and hist
layer (whip + woba), so the bullpen is scored on the SAME scale as the starter --
apples-to-apples. xwOBA-allowed needs Statcast, so it's left null here; the layer
handles the null gracefully (pred falls back to FIP). If you want sharper ranking,
add bullpen xwOBA-allowed from your Savant pull later -- the front-end already reads it.

NOTE ON RANKING: the column shows RANK, so the FIP constant (cFIP) below only shifts
every team uniformly and does not affect the ordering or the filter. Don't sweat it.

A reliever = a rostered pitcher whose season games-started is a minority of his
appearances (pure relievers have GS=0; swingmen split). Stats are summed, so
high-inning arms dominate the team aggregate naturally (IP-weighted by construction).

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push bullpen.json to root of cushplayerprops/cush-data main -> daily cron.
"""

import json, sys, time, urllib.request

SEASON   = 2026
MLB_BASE = "https://statsapi.mlb.com/api/v1"
OUT_PATH = "bullpen.json"
CFIP     = 3.10            # FIP constant (uniform offset; does not affect rank)
TIMEOUT  = 30
UA = {"User-Agent": "Mozilla/5.0 (cush-bullpen-build)"}

# wOBA linear weights (modern, ~recent seasons; uniform across teams so ranking-safe)
W = {"bb": 0.69, "hbp": 0.72, "1b": 0.89, "2b": 1.27, "3b": 1.62, "hr": 2.10}


def _get(url, tries=3):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            time.sleep(1.5 * (k + 1))
            last = e
    print("  ! fetch failed:", last, file=sys.stderr)
    return None


def ip_to_outs(ip_str):
    """'45.2' -> 137 outs (45 innings + 2 outs)."""
    try:
        whole, _, frac = str(ip_str).partition(".")
        return int(whole) * 3 + (int(frac) if frac else 0)
    except (ValueError, TypeError):
        return 0


def num(d, k):
    try:
        return float(d.get(k, 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def team_ids():
    data = _get(MLB_BASE + "/teams?sportId=1")
    return [(t["id"]) for t in json.loads(data or '{"teams":[]}')["teams"]]


def pitcher_season(pid):
    url = ("%s/people/%d/stats?stats=season&group=pitching&season=%d&gameType=R"
           % (MLB_BASE, pid, SEASON))
    data = _get(url)
    if not data:
        return None
    try:
        splits = json.loads(data)["stats"][0]["splits"]
        return splits[0]["stat"] if splits else None
    except (KeyError, IndexError):
        return None


def build_team(tid):
    """Aggregate reliever lines for one team -> {fip,xwoba,whip,woba} or None."""
    roster = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, tid))
    if not roster:
        return None
    agg = dict(outs=0, hr=0, bb=0, hbp=0, k=0, h=0, d2=0, d3=0, bf=0, sf=0, ibb=0)
    for p in json.loads(roster).get("roster", []):
        if (p.get("position") or {}).get("abbreviation") != "P":
            continue
        st = pitcher_season(p["person"]["id"])
        time.sleep(0.04)
        if not st:
            continue
        gp = num(st, "gamesPitched")
        gs = num(st, "gamesStarted")
        if gp <= 0 or gs >= 0.5 * gp:          # skip primary starters
            continue
        agg["outs"] += ip_to_outs(st.get("inningsPitched", "0"))
        agg["hr"]   += num(st, "homeRuns")
        agg["bb"]   += num(st, "baseOnBalls")
        agg["hbp"]  += num(st, "hitByPitch")
        agg["k"]    += num(st, "strikeOuts")
        agg["h"]    += num(st, "hits")
        agg["d2"]   += num(st, "doubles")
        agg["d3"]   += num(st, "triples")
        agg["bf"]   += num(st, "battersFaced")
        agg["sf"]   += num(st, "sacFlies")
        agg["ibb"]  += num(st, "intentionalWalks")

    ip = agg["outs"] / 3.0
    if ip < 20:                                # not enough bullpen sample yet
        return None

    fip  = (13 * agg["hr"] + 3 * (agg["bb"] + agg["hbp"]) - 2 * agg["k"]) / ip + CFIP
    whip = (agg["bb"] + agg["h"]) / ip
    singles = agg["h"] - agg["d2"] - agg["d3"] - agg["hr"]
    ubb = agg["bb"] - agg["ibb"]
    woba_num = (W["bb"] * ubb + W["hbp"] * agg["hbp"] + W["1b"] * max(singles, 0)
                + W["2b"] * agg["d2"] + W["3b"] * agg["d3"] + W["hr"] * agg["hr"])
    woba_den = (agg["bf"] - agg["bb"] - agg["hbp"] - agg["sf"]) + ubb + agg["sf"] + agg["hbp"]
    woba = (woba_num / woba_den) if woba_den > 0 else None

    return {
        "fip":   round(fip, 2),
        "xwoba": None,                         # add from Savant later for sharper rank
        "whip":  round(whip, 2),
        "woba":  round(woba, 3) if woba is not None else None,
    }


def main():
    out = {}
    ids = team_ids()
    print("teams:", len(ids), file=sys.stderr)
    for n, tid in enumerate(ids, 1):
        rec = build_team(tid)
        if rec:
            out[str(tid)] = rec
        print("  %d/%d  team %d  %s" % (n, len(ids), tid, rec or "skip"), file=sys.stderr)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d teams" % (OUT_PATH, len(out)), file=sys.stderr)


if __name__ == "__main__":
    main()
