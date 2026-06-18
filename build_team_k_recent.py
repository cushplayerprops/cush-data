#!/usr/bin/env python3
"""
build_team_k_recent.py  ->  team_k_recent.json

Opponent-strikeout feed for the Cush STRIKEOUT model. For each team it computes
recent (EWMA) and season strikeout rate, split by the handedness of the pitcher
faced, keyed by team id:

    "147": { "vsL": {"recent": 21.8, "season": 22.4, "effPa": 118.0},
             "vsR": {"recent": 24.9, "season": 23.7, "effPa": 304.0} }

  vsL = the team's K% when facing LHP   (used when the starter throws left)
  vsR = the team's K% when facing RHP   (used when the starter throws right)
  recent = ~2-3 week EWMA, season = full-season anchor, effPa = sample behind recent

The K model multiplies the pitcher's expected-K index by oppK/leagueK (regressed,
clamped) -- the log5 opponent term it was missing. Absent feed => factor 1.0.

METHOD / honest approximation: MLB's API gives clean SEASON K% vs L/R (statSplits)
but no per-game hand split, so 'recent' is built as the team's recent OVERALL K%
(EWMA of the hitting game log, ~300-PA half-life) scaled by the SEASON vs-hand ratio.
This captures a team's recent LEVEL shift (hot/cold, roster moves) while keeping its
season hand SHAPE -- it won't detect a team getting specifically better vs RHP
independent of overall, but 2-3 week single-hand samples are too thin to read that
reliably anyway. effPa is split by typical hand share (~0.72 RHP / 0.28 LHP) so the
thinner vsL side regresses harder toward season.

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push team_k_recent.json to root of cushplayerprops/cush-data main -> cron.
"""

import json, sys, time, urllib.request

SEASON          = 2026
HALF_LIFE_PA    = 300          # ~8 games; weights roughly the last 2-3 weeks heavily
RHP_SHARE       = 0.72         # typical share of a team's PAs vs RHP
MLB_BASE        = "https://statsapi.mlb.com/api/v1"
OUT_PATH        = "team_k_recent.json"
TIMEOUT         = 30
LAMBDA          = 0.5 ** (1.0 / HALF_LIFE_PA)
UA = {"User-Agent": "Mozilla/5.0 (cush-teamk-build)"}


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


def kpct(stat):
    try:
        so = float(stat.get("strikeOuts", 0) or 0)
        pa = float(stat.get("plateAppearances", 0) or 0)
        return (100.0 * so / pa) if pa > 0 else None, pa
    except (TypeError, ValueError):
        return None, 0.0


def season_splits(tid):
    """Return (overall_kpct, vL_kpct, vR_kpct)."""
    ov = _get("%s/teams/%d/stats?stats=season&group=hitting&season=%d&gameType=R"
              % (MLB_BASE, tid, SEASON))
    sp = _get("%s/teams/%d/stats?stats=statSplits&group=hitting&sitCodes=vl,vr&season=%d&gameType=R"
              % (MLB_BASE, tid, SEASON))
    overall = vL = vR = None
    try:
        overall = kpct(json.loads(ov)["stats"][0]["splits"][0]["stat"])[0]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        for s in json.loads(sp)["stats"][0]["splits"]:
            code = (s.get("split") or {}).get("code")
            k = kpct(s["stat"])[0]
            if code == "vl":
                vL = k
            elif code == "vr":
                vR = k
    except (KeyError, IndexError, TypeError):
        pass
    return overall, vL, vR


def recent_overall(tid):
    """EWMA of the hitting game log's K% by PA. Returns (recent_kpct, effPa)."""
    gl = _get("%s/teams/%d/stats?stats=gameLog&group=hitting&season=%d&gameType=R"
              % (MLB_BASE, tid, SEASON))
    try:
        splits = json.loads(gl)["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return None, 0.0
    games = []
    for s in splits:
        k, pa = kpct(s["stat"])
        if k is not None and pa > 0:
            games.append((s.get("date", ""), k, pa))
    games.sort(key=lambda r: r[0])
    S = Wt = 0.0
    decay = 1.0
    for _, k, pa in reversed(games):              # newest -> oldest
        w = decay * pa
        S += w * k
        Wt += w
        decay *= LAMBDA ** pa
    if Wt <= 0:
        return None, 0.0
    return S / Wt, Wt


def build_team(tid):
    ov, vL, vR = season_splits(tid)
    if ov is None or ov <= 0:
        return None
    rec, eff = recent_overall(tid)
    if rec is None:
        rec, eff = ov, 0.0
    def side(season_hand, share):
        if season_hand is None:
            season_hand = ov
        r = rec * (season_hand / ov) if ov else season_hand
        return {"recent": round(r, 1), "season": round(season_hand, 1),
                "effPa": round(eff * share, 1)}
    return {"vsL": side(vL, 1.0 - RHP_SHARE), "vsR": side(vR, RHP_SHARE)}


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
    print("wrote %s : %d teams (half-life %d PA)" % (OUT_PATH, len(out), HALF_LIFE_PA), file=sys.stderr)


if __name__ == "__main__":
    main()
