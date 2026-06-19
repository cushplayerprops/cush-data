#!/usr/bin/env python3
"""
backtest_projection.py  ->  prints a report to the log (writes nothing to the repo)

WHAT THIS ANSWERS
-----------------
The hitter & HR models now regress a hitter's season-to-date toward his OWN Marcel
projection instead of toward league average. Two open questions:

  1. Does regressing toward the projection actually forecast better than the old
     regress-toward-league? (Is the change real?)
  2. What regression STRENGTH is best -- how hard should a small sample be pulled
     toward the projection? (Tunes the model's shrink constant.)

This walks forward through the season with NO look-ahead and settles both, using the
same OPS-based metric the projection itself uses.

HOW IT WORKS (per hitter, leakage-free)
---------------------------------------
At each split point, using ONLY games before it, it forms the season-to-date OPS and
the Marcel projection as-of that date (prior 2 seasons + this year so far). It then
builds two estimates -- one regressed toward league, one toward the projection, across
a sweep of regression strengths -- and checks which lands closer to the hitter's
ACTUAL OPS over his NEXT chunk of plate appearances. Lowest error wins.

HOW TO READ IT
--------------
Two blocks (regress-to-LEAGUE and regress-to-PROJECTION), each listing RMSE by
strength K. Lower is better. The verdict line says whether projection beats league and
at what K. Also prints season-to-date (no regression) and pure-projection as anchors.
This is a forecasting test, so absolute RMSE is dominated by future-sample noise; the
COMPARISON between the two targets is the signal.

(No aging in the as-of projection here -- this isolates the regression-target question;
aging is a separate, tiny term.)

RUN: one-off GitHub Action (workflow_dispatch). Pulls game logs + prior seasons, so it
takes a while. Read the report at the bottom of the log.
DEPENDENCIES: standard library only. No API key.
"""

import json, sys, time, math, urllib.request

SEASON       = 2026
PRIOR_YEARS  = [2025, 2024]
YEAR_W       = {2026: 5.0, 2025: 4.0, 2024: 3.0}
LG_OPS       = 0.720
REG_PROJ     = 1500.0                 # Marcel internal regression (matches the feed)
K_SWEEP      = [50, 100, 150, 250, 400, 600]   # model shrink strengths to test (PA)
MIN_HIST_PA  = 100
FUT_PA       = 60
STRIDE       = 5
MIN_GAMES    = 25
MLB_BASE     = "https://statsapi.mlb.com/api/v1"
TIMEOUT      = 30
UA = {"User-Agent": "Mozilla/5.0 (cush-projbt)"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            last = e
            time.sleep(1.5 * (k + 1))
    return None


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def hitter_ids():
    ids = {}
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    for t in teams:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, t["id"]))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            if (p.get("position") or {}).get("abbreviation") != "P":
                ids[p["person"]["id"]] = 1
        time.sleep(0.04)
    return list(ids)


def prior_lines(pid):
    """{2025:(ops,pa), 2024:(ops,pa)} from yearByYear."""
    data = _get("%s/people/%d/stats?stats=yearByYear&group=hitting&gameType=R" % (MLB_BASE, pid))
    out = {}
    try:
        for sp in json.loads(data)["stats"][0]["splits"]:
            yr = int(sp.get("season"))
            if yr not in PRIOR_YEARS:
                continue
            st = sp.get("stat") or {}
            pa, ops = _f(st.get("plateAppearances")), _f(st.get("ops"))
            if pa > 0 and ops > 0:
                p0, o0 = out.get(yr, (0.0, 0.0))
                npa = p0 + pa
                out[yr] = (npa, (o0 * p0 + ops * pa) / npa)
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return {yr: (o, p) for yr, (p, o) in out.items()}


def game_components(pid):
    """2026 per-game [H,AB,BB,HBP,SF,TB,PA], chronological."""
    data = _get("%s/people/%d/stats?stats=gameLog&group=hitting&season=%d&gameType=R"
                % (MLB_BASE, pid, SEASON))
    rows = []
    try:
        for sp in json.loads(data)["stats"][0]["splits"]:
            st = sp.get("stat") or {}
            pa = _f(st.get("plateAppearances"))
            if pa <= 0:
                continue
            rows.append((sp.get("date", ""),
                         _f(st.get("hits")), _f(st.get("atBats")), _f(st.get("baseOnBalls")),
                         _f(st.get("hitByPitch")), _f(st.get("sacFlies")), _f(st.get("totalBases")), pa))
    except (KeyError, IndexError, TypeError):
        pass
    rows.sort(key=lambda r: r[0])
    return rows


def ops_of(H, AB, BB, HBP, SF, TB):
    den_obp = AB + BB + HBP + SF
    obp = (H + BB + HBP) / den_obp if den_obp > 0 else 0.0
    slg = TB / AB if AB > 0 else 0.0
    return obp + slg


def marcel(obs_ops, obs_pa, prior):
    num = YEAR_W[2026] * obs_pa * obs_ops
    den = YEAR_W[2026] * obs_pa
    for yr in PRIOR_YEARS:
        if yr in prior:
            o, p = prior[yr]
            num += YEAR_W[yr] * p * o
            den += YEAR_W[yr] * p
    return (num + REG_PROJ * LG_OPS) / (den + REG_PROJ)


def collect(records):
    """records: list of (prior_dict, games_list) -> samples (obs, obs_pa, proj, future, weight)."""
    samples = []
    for prior, games in records:
        n = len(games)
        # cumulative prefix of components
        cum = [[0.0] * 7]
        for g in games:
            prev = cum[-1]
            cum.append([prev[j] + g[1 + j] for j in range(7)])   # H,AB,BB,HBP,SF,TB,PA
        for s in range(0, n - 1, STRIDE):
            c = cum[s + 1]                       # through game s (inclusive)
            obs_pa = c[6]
            if obs_pa < MIN_HIST_PA:
                continue
            obs = ops_of(c[0], c[1], c[2], c[3], c[4], c[5])
            # future window
            fend = s + 1
            fpa = 0.0
            while fend < n and fpa < FUT_PA:
                fpa += games[fend][7]
                fend += 1
            if fpa < FUT_PA:
                continue
            cf = cum[fend]
            fut = ops_of(*(cf[j] - c[j] for j in range(6)))
            proj = marcel(obs, obs_pa, prior)
            samples.append((obs, obs_pa, proj, fut, fpa))
    return samples


def wrmse(est_fn, samples):
    num = den = 0.0
    for obs, opa, proj, fut, w in samples:
        e = est_fn(obs, opa, proj)
        num += w * (e - fut) ** 2
        den += w
    return math.sqrt(num / den) if den else None


def report(samples):
    print("\n" + "=" * 66)
    print("PROJECTION REGRESSION BACKTEST  (predicting next %d PA of OPS)" % FUT_PA)
    print("=" * 66)
    n = len(samples)
    if n < 200:
        print("  only %d samples -- too thin to trust." % n)
        return
    season = wrmse(lambda o, p, pj: o, samples)
    pureproj = wrmse(lambda o, p, pj: pj, samples)
    print("  samples: %d" % n)
    print("  anchors:  season-to-date (no regress) RMSE %.5f   |   pure projection RMSE %.5f"
          % (season, pureproj))

    def block(title, target):
        print("  " + title)
        best = None
        for K in K_SWEEP:
            r = wrmse(lambda o, p, pj: (o * p + (LG_OPS if target == "lg" else pj) * K) / (p + K), samples)
            if best is None or r < best[1]:
                best = (K, r)
            tag = "  (model uses ~100-150)" if K in (100, 150) else ""
            print("    K=%-4d  RMSE %.5f%s" % (K, r, tag))
        return best

    lg_best = block("regress toward LEAGUE average:", "lg")
    pj_best = block("regress toward PROJECTION:", "pj")

    print("  " + "-" * 60)
    print("  best regress-to-LEAGUE:     K=%-4d  RMSE %.5f" % lg_best)
    print("  best regress-to-PROJECTION: K=%-4d  RMSE %.5f" % pj_best)
    if lg_best[1] and pj_best[1]:
        imp = 100.0 * (lg_best[1] - pj_best[1]) / lg_best[1]
        verdict = ("PROJECTION wins by %.1f%%" % imp) if imp > 0 else ("LEAGUE wins by %.1f%%" % (-imp))
        print("  VERDICT: %s ; best projection strength K=%d" % (verdict, pj_best[0]))


def main():
    print("collecting hitters ...", file=sys.stderr)
    ids = hitter_ids()
    print("  %d hitters" % len(ids), file=sys.stderr)
    records = []
    for i, pid in enumerate(sorted(ids), 1):
        games = game_components(pid)
        time.sleep(0.06)
        if len(games) < MIN_GAMES:
            continue
        prior = prior_lines(pid)
        time.sleep(0.06)
        records.append((prior, games))
        if i % 40 == 0:
            print("  %d/%d (%d usable)" % (i, len(ids), len(records)), file=sys.stderr)
    samples = collect(records)
    report(samples)
    print("\nDone. If PROJECTION wins, the change is validated; set the shrink near the best K.")


if __name__ == "__main__":
    main()
