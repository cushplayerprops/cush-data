#!/usr/bin/env python3
"""
build_projections.py  ->  projections.json

A self-contained, Marcel-style TRUE-TALENT projection for every hitter, built only
from the public MLB Stats API (no FanGraphs, no key). One record per hitter id:

    "665742": { "wrc": 158, "pa": 1840 }

  wrc = projected wRC+ on the app's OPS-based scale  (round(projOPS / 0.720 * 100))
  pa  = weighted recent plate appearances behind the projection (a reliability hint)

WHY: the hitter model's talent anchor is season-to-date, which in-season is still
half noise. A projection regresses a small sample toward the player's OWN multi-year
level instead of toward league average -- so a star off to a slow start is treated
like a star, not a league-average bat. The front end uses this as the REGRESSION
TARGET: high-PA hitters barely move, small samples get pulled toward their projection.

METHOD (Marcel, the standard baseline every real system is measured against):
  * Pull the last 3 seasons (this year + prior two) of OPS and PA per hitter.
  * Weight recent years more (5 / 4 / 3), weighted again by each year's PA.
  * Regress toward league OPS with REG pseudo-PA, so thin samples shrink hard and
    full-time veterans barely shrink.
  * Convert to the app's wRC+ scale with the SAME OPS formula the hand-splits feed
    uses, so it's apples-to-apples with the model's existing wRC+.
  (Aging adjustment is intentionally omitted in v1 -- it's the smallest Marcel term;
   easy to add later from birthDate if we want it.)

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push projections.json to root of cushplayerprops/cush-data main -> cron.
"""

import json, sys, time, urllib.request

SEASON     = 2026
YEARS      = [2026, 2025, 2024]          # most-recent first
YEAR_W     = {2026: 5.0, 2025: 4.0, 2024: 3.0}
LG_OPS     = 0.720                        # league baseline (matches the app's wRC+ divisor)
REG        = 1500.0                       # regression strength, in weighted-PA units
OPS_DIV    = 0.720                        # wRC+ = round(OPS / OPS_DIV * 100)
MLB_BASE   = "https://statsapi.mlb.com/api/v1"
OUT_PATH   = "projections.json"
TIMEOUT    = 30
UA = {"User-Agent": "Mozilla/5.0 (cush-proj-build)"}


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


def hitter_ids():
    ids = {}
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    for t in teams:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, t["id"]))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            if (p.get("position") or {}).get("abbreviation") != "P":
                ids[p["person"]["id"]] = p["person"].get("fullName", "")
        time.sleep(0.04)
    return ids


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def year_lines(pid):
    """{season:int -> (ops, pa)} from the yearByYear hitting splits."""
    data = _get("%s/people/%d/stats?stats=yearByYear&group=hitting&gameType=R"
                % (MLB_BASE, pid))
    out = {}
    try:
        for sp in json.loads(data)["stats"][0]["splits"]:
            try:
                yr = int(sp.get("season"))
            except (TypeError, ValueError):
                continue
            st = sp.get("stat") or {}
            pa = _f(st.get("plateAppearances"))
            ops = _f(st.get("ops"))
            if pa > 0 and ops > 0:
                # a player can have multiple split rows in a season (trades); sum PA, PA-weight OPS
                if yr in out:
                    p0, o0 = out[yr]
                    npa = p0 + pa
                    out[yr] = (npa, (o0 * p0 + ops * pa) / npa) if npa > 0 else (npa, ops)
                else:
                    out[yr] = (pa, ops)
    except (KeyError, IndexError, TypeError):
        pass
    # normalize to {yr:(ops,pa)}
    return {yr: (o, p) for yr, (p, o) in out.items()}


def project(lines):
    """Marcel-ish projected OPS + weighted PA from {season:(ops,pa)}."""
    num = den = wpa = 0.0
    for yr in YEARS:
        if yr not in lines:
            continue
        ops, pa = lines[yr]
        w = YEAR_W[yr] * pa
        num += w * ops
        den += w
        wpa += YEAR_W[yr] * pa
    if den <= 0:
        return None, 0.0
    proj_ops = (num + REG * LG_OPS) / (den + REG)
    return proj_ops, wpa


def main():
    print("collecting hitters ...", file=sys.stderr)
    ids = hitter_ids()
    print("  %d hitters" % len(ids), file=sys.stderr)
    out = {}
    items = sorted(ids.items())
    for i, (pid, name) in enumerate(items, 1):
        lines = year_lines(pid)
        time.sleep(0.05)
        proj_ops, wpa = project(lines)
        if proj_ops is None:
            continue
        out[str(pid)] = {"wrc": int(round(proj_ops / OPS_DIV * 100)),
                         "pa": int(round(wpa))}
        if i % 50 == 0:
            print("  %d/%d (%d written)" % (i, len(items), len(out)), file=sys.stderr)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d hitters" % (OUT_PATH, len(out)), file=sys.stderr)


if __name__ == "__main__":
    main()
