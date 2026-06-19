#!/usr/bin/env python3
"""
build_projections.py  ->  projections.json

A self-contained, Marcel-style TRUE-TALENT projection for every hitter, built only
from the public MLB Stats API (no FanGraphs, no key). One record per hitter id:

    "665742": { "wrc": 158, "pa": 1840, "iso": 0.255 }

  wrc = projected wRC+ on the app's OPS-based scale  (round(projOPS / 0.720 * 100))
  iso = projected ISO (SLG - AVG)  -- powers the HR model's power base
  pa  = weighted recent plate appearances behind the projection (reliability hint)

WHY: a season-to-date rate is half noise in-season. The hitter model regresses a
hitter toward his OWN projected wRC+ (not league average); the HR model regresses his
power toward his OWN projected ISO. So a slow-starting star stays a star, and a
power bat in a quiet stretch keeps his pop, instead of both being dragged to league.

METHOD (Marcel, the standard baseline every real system is measured against):
  * Pull the last 3 seasons (this year + prior two) of OPS, ISO, and PA per hitter.
  * Weight recent years more (5 / 4 / 3), weighted again by each year's PA.
  * Regress toward league with REG pseudo-PA: thin samples shrink hard, full-time
    veterans barely shrink.
  * Convert OPS to the app's wRC+ scale with the SAME formula the hand-splits feed
    uses, so it's apples-to-apples.
  * Apply the Marcel aging curve from each hitter's current age (gentle: ~+0.6%/yr
    of youth below age 29, ~-0.3%/yr of decline above, capped at +/-8%).

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push projections.json to root of cushplayerprops/cush-data main -> cron.
"""

import json, sys, time, urllib.request

SEASON     = 2026
YEARS      = [2026, 2025, 2024]
YEAR_W     = {2026: 5.0, 2025: 4.0, 2024: 3.0}
LG_OPS     = 0.720
LG_ISO     = 0.150
REG        = 1500.0                       # regression strength, in weighted-PA units
AGE_PEAK   = 29                           # Marcel aging: peak age
AGE_UP     = 0.006                        # per-year gain below peak
AGE_DN     = 0.003                        # per-year decline above peak
AGE_CLAMP  = 0.08                         # cap the aging swing at +/-8%
OPS_DIV    = 0.720
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


def ages_for(ids):
    """Batched currentAge lookup: {id -> age}. One call per ~50 ids."""
    out = {}
    keys = list(ids)
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        data = _get("%s/people?personIds=%s" % (MLB_BASE, ",".join(str(k) for k in chunk)))
        try:
            for p in json.loads(data or '{"people":[]}').get("people", []):
                a = p.get("currentAge")
                if a is not None:
                    out[p["id"]] = float(a)
        except (TypeError, ValueError):
            pass
        time.sleep(0.05)
    return out


def age_factor(age):
    if age is None:
        return 1.0
    adj = (AGE_PEAK - age) * (AGE_UP if age < AGE_PEAK else AGE_DN)
    return max(1.0 - AGE_CLAMP, min(1.0 + AGE_CLAMP, 1.0 + adj))


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def year_lines(pid):
    """{season -> (ops, pa, iso)} from the yearByYear hitting splits (PA-weighted on trades)."""
    data = _get("%s/people/%d/stats?stats=yearByYear&group=hitting&gameType=R"
                % (MLB_BASE, pid))
    acc = {}    # yr -> [pa, ops_num, iso_num]  (numerators are PA-weighted)
    try:
        for sp in json.loads(data)["stats"][0]["splits"]:
            try:
                yr = int(sp.get("season"))
            except (TypeError, ValueError):
                continue
            st = sp.get("stat") or {}
            pa = _f(st.get("plateAppearances"))
            ops = _f(st.get("ops"))
            if pa <= 0 or ops <= 0:
                continue
            iso = max(0.0, _f(st.get("slg")) - _f(st.get("avg")))
            if yr in acc:
                acc[yr][0] += pa
                acc[yr][1] += ops * pa
                acc[yr][2] += iso * pa
            else:
                acc[yr] = [pa, ops * pa, iso * pa]
    except (KeyError, IndexError, TypeError):
        pass
    return {yr: (onum / pa, pa, inum / pa) for yr, (pa, onum, inum) in acc.items() if pa > 0}


def project(lines):
    """Marcel-ish (projOPS, weightedPA, projISO) from {season:(ops,pa,iso)}."""
    num_o = den = wpa = num_i = 0.0
    for yr in YEARS:
        if yr not in lines:
            continue
        ops, pa, iso = lines[yr]
        w = YEAR_W[yr] * pa
        num_o += w * ops
        num_i += w * iso
        den += w
        wpa += YEAR_W[yr] * pa
    if den <= 0:
        return None, 0.0, None
    proj_ops = (num_o + REG * LG_OPS) / (den + REG)
    proj_iso = (num_i + REG * LG_ISO) / (den + REG)
    return proj_ops, wpa, proj_iso


def main():
    print("collecting hitters ...", file=sys.stderr)
    ids = hitter_ids()
    print("  %d hitters" % len(ids), file=sys.stderr)
    ages = ages_for(ids)
    print("  %d ages" % len(ages), file=sys.stderr)
    out = {}
    items = sorted(ids.items())
    for i, (pid, name) in enumerate(items, 1):
        lines = year_lines(pid)
        time.sleep(0.05)
        proj_ops, wpa, proj_iso = project(lines)
        if proj_ops is None:
            continue
        af = age_factor(ages.get(pid))
        proj_ops *= af
        if proj_iso is not None:
            proj_iso *= af
        rec = {"wrc": int(round(proj_ops / OPS_DIV * 100)), "pa": int(round(wpa))}
        if proj_iso is not None:
            rec["iso"] = round(proj_iso, 3)
        out[str(pid)] = rec
        if i % 50 == 0:
            print("  %d/%d (%d written)" % (i, len(items), len(out)), file=sys.stderr)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d hitters" % (OUT_PATH, len(out)), file=sys.stderr)


if __name__ == "__main__":
    main()
