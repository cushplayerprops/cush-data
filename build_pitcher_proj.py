#!/usr/bin/env python3
"""
build_pitcher_proj.py  ->  pitcher_proj.json

A self-contained, Marcel-style TRUE-TALENT strikeout projection for every pitcher,
built only from the public MLB Stats API (no Savant, no key). One record per pitcher:

    "594798": { "k": 31.8, "bf": 410 }

  k  = projected strikeout rate, as a PERCENT of batters faced (SO / BF * 100).
       Same units the Strikeout Model's kEff/kScore use (kScore = kEff/22*100), so it
       drops straight in: projScore = k / 22 * 100.
  bf = this season's batters faced -- the sample weight. The model regresses a
       pitcher's observed K ability toward this projection by bf: a 350-BF starter
       keeps most of his observed number, a 70-BF call-up leans on the projection.

WHY: like the hitter projection, a multi-year true-talent K rate is a steadier anchor
than a partial season -- it smooths a fluky stretch and rescues small-sample arms.
K% stabilizes faster than wOBA, so the lift is smaller than the hitter side, but it
still helps most where the season sample is thin.

METHOD (Marcel):
  * Pull last 3 seasons (this year + prior two) of SO and BF per pitcher.
  * K% per season = SO / BF. Weight recent years 5 / 4 / 3, weighted again by BF.
  * Regress toward league K% with REG_K pseudo-BF (thin samples shrink toward league).
  (No aging here -- pitcher aging is noisy/role-dependent; left out of v1, like hitter v1.)

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push pitcher_proj.json to root of cushplayerprops/cush-data main -> cron.
"""

import json, sys, time, urllib.request

SEASON     = 2026
YEARS      = [2026, 2025, 2024]
YEAR_W     = {2026: 5.0, 2025: 4.0, 2024: 3.0}
LG_K       = 0.220                         # league strikeout rate (per BF)
REG_K      = 200.0                         # regression strength, in BF units
MLB_BASE   = "https://statsapi.mlb.com/api/v1"
OUT_PATH   = "pitcher_proj.json"
TIMEOUT    = 30
UA = {"User-Agent": "Mozilla/5.0 (cush-pproj-build)"}


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


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pitcher_ids():
    ids = {}
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    for t in teams:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, t["id"]))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            if (p.get("position") or {}).get("abbreviation") == "P":
                ids[p["person"]["id"]] = 1
        time.sleep(0.04)
    return list(ids)


def year_lines(pid):
    """{season -> (k_rate, bf)} from yearByYear pitching (BF-weighted across stints)."""
    data = _get("%s/people/%d/stats?stats=yearByYear&group=pitching&gameType=R" % (MLB_BASE, pid))
    acc = {}    # yr -> [bf, so]
    try:
        for sp in json.loads(data)["stats"][0]["splits"]:
            try:
                yr = int(sp.get("season"))
            except (TypeError, ValueError):
                continue
            st = sp.get("stat") or {}
            bf = _f(st.get("battersFaced"))
            so = _f(st.get("strikeOuts"))
            if bf <= 0:
                continue
            if yr in acc:
                acc[yr][0] += bf
                acc[yr][1] += so
            else:
                acc[yr] = [bf, so]
    except (KeyError, IndexError, TypeError):
        pass
    return {yr: (so / bf, bf) for yr, (bf, so) in acc.items() if bf > 0}


def project(lines):
    """Marcel (projK_rate, currentBF) from {season:(k_rate, bf)}."""
    num = den = 0.0
    for yr in YEARS:
        if yr not in lines:
            continue
        kr, bf = lines[yr]
        w = YEAR_W[yr] * bf
        num += w * kr
        den += w
    if den <= 0:
        return None, 0.0
    proj = (num + REG_K * LG_K) / (den + REG_K)
    cur_bf = lines.get(SEASON, (0.0, 0.0))[1]
    return proj, cur_bf


def main():
    print("collecting pitchers ...", file=sys.stderr)
    ids = pitcher_ids()
    print("  %d pitchers" % len(ids), file=sys.stderr)
    out = {}
    for i, pid in enumerate(sorted(ids), 1):
        lines = year_lines(pid)
        time.sleep(0.05)
        proj, cur_bf = project(lines)
        if proj is None:
            continue
        out[str(pid)] = {"k": round(proj * 100, 1), "bf": int(round(cur_bf))}
        if i % 50 == 0:
            print("  %d/%d (%d written)" % (i, len(ids), len(out)), file=sys.stderr)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d pitchers" % (OUT_PATH, len(out)), file=sys.stderr)


if __name__ == "__main__":
    main()
