#!/usr/bin/env python3
"""
backtest_hitter_park.py
=======================
Leakage-free, walk-forward backtest that answers ONE question:

    Does the PARK FACTOR actually help predict hitter fantasy scoring,
    and is Coors's real effect as big as the cush model assumes?

The cush hitter score adds a park term `(parkHR - 1.00) * 25` on top of
talent + matchup. For Coors that is a flat +4.5 (L) / +5.25 (R) every day,
which floats the whole Coors lineup to the top of the board. This script
measures whether that park bump has real predictive signal -- by rebuilding
each hitter's ACTUAL PrizePicks fantasy points from box scores and testing
the projection WITH park vs WITHOUT park.

PrizePicks HITTER fantasy scoring (used to recompute ACTUAL fantasy points):
    single +3   double +5   triple +8   home run +10
    run +2      RBI +2      walk +2      hit-by-pitch +2      stolen base +5

WHY THIS IS LEAKAGE-FREE:
    For every game we project from ONLY that hitter's PRIOR games (strictly
    earlier in their own chronological log). The game being graded never
    feeds its own projection. Priors reset each season.

WHAT IT REPORTS:
    1. Baseline fit  : recent-form projection (no park) vs actual.
    2. With-park fit  : baseline * park multiplier vs actual -- does it improve?
    3. Marginal test  : regress actual ~ baseline + (park-1). Park's coefficient
                        and the R^2 gain are the headline -- "does park add
                        anything beyond recent form?"
    4. Park buckets   : mean actual/baseline ratio per park-factor band
                        (monotonic rising => park matters).
    5. Coors / high-park slice : how much hitters REALLY outscore their own
                        baseline at altitude, vs the literal park number.

WHAT IT CANNOT ANSWER:
    Whether Coors overs CASH at PrizePicks/Underdog's inflated lines -- that
    needs historical book lines we have no feed for. This tests the PROJECTION,
    not the market price.

DATA SOURCE : MLB Stats API (https://statsapi.mlb.com) -- public, free, no key.
DEPENDENCIES: Python 3.8+ standard library ONLY. No pip install.

USAGE:
    python backtest_hitter_park.py --selftest          # no network; validates math
    python backtest_hitter_park.py                      # default: 2024, top 150 bats
    python backtest_hitter_park.py --seasons 2023,2024 --max-hitters 200
    python backtest_hitter_park.py --csv-out hitter_park.csv
"""

import argparse
import json
import math
import sys
import time
import urllib.request
import urllib.error

API = "https://statsapi.mlb.com/api/v1"
UA = {"User-Agent": "cush-player-props-hitter-park/1.0 (+research)"}

# Park HR factors copied byte-for-byte from the app's PARK_HR map.
# We use the average of the L/R factor as a single venue multiplier
# (the L/R split is second-order for the "does park matter" question).
PARK_HR = {
    "Coors Field": (1.18, 1.21), "Las Vegas Ballpark": (1.18, 1.21),
    "Great American Ball Park": (1.13, 1.17), "Yankee Stadium": (1.22, 1.03),
    "Citizens Bank Park": (1.11, 1.09), "Globe Life Field": (1.05, 1.05),
    "Wrigley Field": (1.04, 1.04), "American Family Field": (1.05, 1.03),
    "Chase Field": (1.04, 1.04), "Truist Park": (1.03, 1.03),
    "Rogers Centre": (1.03, 1.03), "Dodger Stadium": (1.02, 1.03),
    "Rate Field": (1.00, 1.04), "Guaranteed Rate Field": (1.00, 1.04),
    "Daikin Park": (1.00, 1.03), "Minute Maid Park": (1.00, 1.03),
    "Nationals Park": (1.01, 1.01), "Angel Stadium": (1.01, 1.01),
    "Sutter Health Park": (1.07, 1.08),
    "Oriole Park at Camden Yards": (1.07, 1.13),
    "Target Field": (0.99, 0.99), "Citi Field": (0.98, 0.98),
    "Petco Park": (0.99, 0.97), "Progressive Field": (0.98, 0.98),
    "Tropicana Field": (0.98, 0.98),
    "George M. Steinbrenner Field": (1.12, 0.98),
    "loanDepot park": (0.97, 0.97), "Busch Stadium": (0.97, 0.95),
    "T-Mobile Park": (0.97, 0.95), "Fenway Park": (0.97, 0.93),
    "Comerica Park": (0.96, 0.94), "Kauffman Stadium": (1.10, 1.10),
    "PNC Park": (1.00, 0.90), "Oracle Park": (0.85, 0.98),
}


def park_mult(venue):
    """Single venue multiplier = mean(L, R) HR factor. Unknown venue -> 1.00."""
    rec = PARK_HR.get(venue)
    if rec is None:
        return 1.00
    return (rec[0] + rec[1]) / 2.0


# --------------------------------------------------------------------------- #
#  Pure-Python stats helpers (no numpy / scipy)
# --------------------------------------------------------------------------- #
def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    return pearson(_ranks(xs), _ranks(ys))


def _solve(A, b):
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return [float("nan")] * n
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(n):
            if r != col:
                f = M[r][col] / pv
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def ols(X, y):
    """OLS. X = feature rows (include intercept col). Returns (coef, R^2)."""
    n, p = len(X), len(X[0])
    A = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for i in range(n):
        xi, yi = X[i], y[i]
        for a in range(p):
            b[a] += xi[a] * yi
            for c in range(p):
                A[a][c] += xi[a] * xi[c]
    coef = _solve(A, b)
    if any(math.isnan(c) for c in coef):
        return coef, float("nan")
    yhat = [sum(coef[a] * X[i][a] for a in range(p)) for i in range(n)]
    ybar = mean(y)
    ss_res = sum((y[i] - yhat[i]) ** 2 for i in range(n))
    ss_tot = sum((v - ybar) ** 2 for v in y)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return coef, r2


# --------------------------------------------------------------------------- #
#  Hitter fantasy scoring (PrizePicks)
# --------------------------------------------------------------------------- #
def hitter_fp(singles, doubles, triples, hr, runs, rbi, bb, hbp, sb):
    return (3 * singles + 5 * doubles + 8 * triples + 10 * hr
            + 2 * runs + 2 * rbi + 2 * bb + 2 * hbp + 5 * sb)


def fp_from_line(st):
    """ACTUAL fantasy points from a box-score hitting stat object."""
    hits = int(st.get("hits", 0) or 0)
    dbl = int(st.get("doubles", 0) or 0)
    tpl = int(st.get("triples", 0) or 0)
    hr = int(st.get("homeRuns", 0) or 0)
    singles = max(0, hits - dbl - tpl - hr)
    runs = int(st.get("runs", 0) or 0)
    rbi = int(st.get("rbi", 0) or 0)
    bb = int(st.get("baseOnBalls", 0) or 0)
    hbp = int(st.get("hitByPitch", 0) or 0)
    sb = int(st.get("stolenBases", 0) or 0)
    return hitter_fp(singles, dbl, tpl, hr, runs, rbi, bb, hbp, sb)


# --------------------------------------------------------------------------- #
#  Walk-forward baseline projection (PRIOR GAMES ONLY)
# --------------------------------------------------------------------------- #
class Cfg:
    def __init__(self, a):
        self.min_prior = a.min_prior_games
        self.halflife = a.halflife


def baseline_proj(prior_fp, cfg):
    """Expected fantasy this game = (EWMA or flat) mean of prior-game FP."""
    n = len(prior_fp)
    if cfg.halflife and cfg.halflife > 0:
        w = [0.5 ** ((n - 1 - i) / cfg.halflife) for i in range(n)]
    else:
        w = [1.0] * n
    W = sum(w)
    return sum(w[i] * prior_fp[i] for i in range(n)) / W


def walk_forward(games, cfg):
    """games: chronological list for ONE hitter-season. Yields graded records.
    Each game dict: fp (actual), pf (park mult), venue, name, date, season."""
    prior_fp = []
    for g in games:
        if len(prior_fp) >= cfg.min_prior:
            base = baseline_proj(prior_fp, cfg)
            yield {
                "baseline": base,
                "withpark": base * g["pf"],
                "pf": g["pf"],
                "actual": g["fp"],
                "venue": g["venue"],
                "name": g["name"], "date": g["date"], "season": g["season"],
            }
        prior_fp.append(g["fp"])


# --------------------------------------------------------------------------- #
#  MLB Stats API access
# --------------------------------------------------------------------------- #
def fetch_json(url, retries=3, sleep=0.5):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            last = e
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n  -> {last}")


def get_schedule_venues(season):
    """{gamePk: venueName} for every regular-season game that season."""
    url = (f"{API}/schedule?sportId=1&season={season}&gameType=R")
    data = fetch_json(url)
    out = {}
    for d in data.get("dates", []):
        for g in d.get("games", []):
            pk = g.get("gamePk")
            ven = ((g.get("venue") or {}).get("name") or "")
            if pk:
                out[pk] = ven
    return out


def get_universe(season, min_pa, max_hitters, verbose):
    """Return [(player_id, name, pa)] for hitters, most PA first."""
    url = (f"{API}/stats?stats=season&group=hitting&season={season}"
           f"&sportId=1&gameType=R&playerPool=All&limit=3000")
    data = fetch_json(url)
    rows = []
    for grp in data.get("stats", []):
        for sp in grp.get("splits", []):
            st = sp.get("stat", {})
            pl = sp.get("player", {})
            pa = int(st.get("plateAppearances", 0) or 0)
            if pa >= min_pa and pl.get("id"):
                rows.append((pl["id"], pl.get("fullName", str(pl["id"])), pa))
    rows.sort(key=lambda t: t[2], reverse=True)
    rows = rows[:max_hitters]
    if verbose:
        print(f"  [{season}] universe: {len(rows)} hitters "
              f"(pa>={min_pa}, capped at {max_hitters})")
    return rows


def get_game_log(pid, season, venues):
    """Chronological list of regular-season games for one hitter-season."""
    url = (f"{API}/people/{pid}/stats?stats=gameLog&group=hitting"
           f"&season={season}&gameType=R")
    data = fetch_json(url)
    out = []
    for grp in data.get("stats", []):
        for sp in grp.get("splits", []):
            st = sp.get("stat", {})
            pa = int(st.get("plateAppearances", 0) or 0)
            if pa <= 0:
                continue  # didn't bat
            pk = (sp.get("game", {}) or {}).get("gamePk", 0)
            venue = venues.get(pk, "")
            out.append({
                "date": sp.get("date", ""),
                "gamepk": pk,
                "fp": fp_from_line(st),
                "pf": park_mult(venue),
                "venue": venue,
                "season": season,
            })
    out.sort(key=lambda r: (r["date"], r["gamepk"]))
    return out


# --------------------------------------------------------------------------- #
#  Reporting
# --------------------------------------------------------------------------- #
def fnum(x, d=3):
    return "nan" if (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def decile_lift(pred, act, label):
    N = len(pred)
    order = sorted(range(N), key=lambda i: pred[i], reverse=True)
    k = max(1, N // 10)
    top = mean([act[i] for i in order[:k]])
    bot = mean([act[i] for i in order[-k:]])
    ov = mean(act)
    print(f"  {label:14} top10% actual {fnum(top):>7}  (x{fnum(top/ov)})"
          f"   bot10% {fnum(bot):>7}  spread {fnum(top - bot):>6}")


def report(recs, cfg):
    N = len(recs)
    print("\n" + "=" * 72)
    print("HITTER PARK-FACTOR BACKTEST  -- walk-forward, leakage-free")
    print("=" * 72)
    if N == 0:
        print("No graded games. Lower --min-prior-games or --min-pa.")
        return
    base = [r["baseline"] for r in recs]
    wpark = [r["withpark"] for r in recs]
    act = [r["actual"] for r in recs]
    pf = [r["pf"] for r in recs]
    nhit = len({r["name"] for r in recs})
    seasons = sorted({r["season"] for r in recs})

    print(f"Seasons        : {','.join(map(str, seasons))}")
    print(f"Hitters        : {nhit}")
    print(f"Graded games   : {N}")
    print(f"Min prior games: {cfg.min_prior}   Half-life: "
          f"{cfg.halflife if cfg.halflife else 'off (flat)'}")
    print(f"Mean actual FP : {fnum(mean(act))}")

    print("\n-- OVERALL FIT (does multiplying by park help ranking?) --")
    print(f"  baseline (no park)  Pearson r {fnum(pearson(base, act))}   "
          f"Spearman {fnum(spearman(base, act))}")
    print(f"  baseline x park     Pearson r {fnum(pearson(wpark, act))}   "
          f"Spearman {fnum(spearman(wpark, act))}")
    dr = pearson(wpark, act) - pearson(base, act)
    print(f"  --> park changes Pearson r by {fnum(dr, 4)}  "
          f"({'helps' if dr > 0 else 'hurts/no help'})")

    print("\n-- DECILE LIFT (do high projections deliver?) --")
    decile_lift(base, act, "no park")
    decile_lift(wpark, act, "with park")

    # Marginal regression: actual ~ 1 + baseline + (pf-1).  The headline.
    Xb = [[1.0, r["baseline"]] for r in recs]
    cb, r2b = ols(Xb, act)
    Xp = [[1.0, r["baseline"], r["pf"] - 1.0] for r in recs]
    cp, r2p = ols(Xp, act)
    print("\n-- MARGINAL PARK TEST (actual ~ baseline + (park-1)) --")
    print(f"  baseline only         : R^2 = {fnum(r2b, 4)}")
    print(f"  baseline + park       : R^2 = {fnum(r2p, 4)}   "
          f"(gain {fnum(r2p - r2b, 4)})")
    print(f"  park coefficient      : {fnum(cp[2], 2)} fantasy pts "
          f"per +1.00 of park factor")
    print(f"  => a 1.20 park (Coors) implies +{fnum(cp[2] * 0.20, 2)} FP "
          f"vs neutral, IF the coefficient is real")
    print("  (coef near 0 or negative R^2 gain => park adds little/no signal)")

    # Park-factor buckets: mean actual/baseline ratio per band.
    print("\n-- PARK BUCKETS (mean actual/baseline ratio; rising = park real) --")
    bands = [("<0.97", -9, 0.97), ("0.97-1.00", 0.97, 1.00),
             ("1.00-1.05", 1.00, 1.05), ("1.05-1.10", 1.05, 1.10),
             (">=1.10", 1.10, 9)]
    print(f"  {'band':12} {'N':>6} {'mean act':>9} {'mean base':>10} "
          f"{'ratio':>7}")
    for lab, lo, hi in bands:
        idx = [i for i in range(N) if lo <= pf[i] < hi]
        if not idx:
            print(f"  {lab:12} {0:>6}        --         --      --")
            continue
        ma = mean([act[i] for i in idx])
        mb = mean([base[i] for i in idx])
        ratio = ma / mb if mb > 0 else float("nan")
        print(f"  {lab:12} {len(idx):>6} {fnum(ma):>9} {fnum(mb):>10} "
              f"{fnum(ratio):>7}")

    # Coors-specific + high-park slice
    print("\n-- COORS / HIGH-PARK SLICE (the spots that float to your board top) --")
    for lab, pred in [("Coors Field only", lambda r: r["venue"] == "Coors Field"),
                      ("park >= 1.10 (Coors/Vegas/GAB/KC..)",
                       lambda r: r["pf"] >= 1.10)]:
        idx = [i for i in range(N) if pred(recs[i])]
        if not idx:
            print(f"  {lab}: 0 games")
            continue
        ma = mean([act[i] for i in idx])
        mb = mean([base[i] for i in idx])
        ratio = ma / mb if mb > 0 else float("nan")
        pf_mean = mean([pf[i] for i in idx])
        print(f"  {lab}:")
        print(f"     games {len(idx)}   mean actual {fnum(ma)}   "
              f"mean baseline {fnum(mb)}")
        print(f"     REAL boost actual/baseline = x{fnum(ratio)}  "
              f"(+{fnum((ratio - 1) * 100, 1)}%)")
        print(f"     literal park factor = x{fnum(pf_mean)}  "
              f"(model treats it as +{fnum((pf_mean - 1) * 100, 1)}%)")
    print("  (if REAL boost << literal park factor, the park term is overstated)")
    print("=" * 72)


def write_csv(recs, path):
    import csv
    cols = ["season", "date", "name", "venue", "pf",
            "baseline", "withpark", "actual"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in recs:
            w.writerow({c: r[c] for c in cols})
    print(f"\nWrote {len(recs)} graded games -> {path}")


# --------------------------------------------------------------------------- #
#  Self-test (NO NETWORK)
# --------------------------------------------------------------------------- #
def run_selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    print("SELFTEST (no network) -------------------------------------------")
    # 1 single,1 double,1 HR,2 runs,3 RBI,1 BB,1 SB -> 3+5+10+4+6+2+5 = 35
    check("hitter_fp example == 35",
          hitter_fp(1, 1, 0, 1, 2, 3, 1, 0, 1) == 35)
    line = {"hits": 3, "doubles": 1, "triples": 0, "homeRuns": 1,
            "runs": 2, "rbi": 3, "baseOnBalls": 1, "hitByPitch": 0,
            "stolenBases": 1}
    # singles = 3-1-0-1 = 1 -> same 35
    check("fp_from_line example == 35", fp_from_line(line) == 35)
    check("park_mult Coors == 1.195",
          abs(park_mult("Coors Field") - 1.195) < 1e-9)
    check("park_mult unknown == 1.00",
          abs(park_mult("Nowhere Dome") - 1.00) < 1e-9)
    check("pearson perfect == 1", abs(pearson([1, 2, 3], [2, 4, 6]) - 1) < 1e-9)

    # OLS recovers y = 4 + 2a - 3b
    X = [[1, a, b] for a in range(6) for b in range(6)]
    y = [4 + 2 * a - 3 * b for a in range(6) for b in range(6)]
    coef, r2 = ols(X, y)
    check("OLS recovers (4,2,-3), R^2=1",
          all(abs(coef[i] - t) < 1e-6 for i, t in enumerate((4, 2, -3)))
          and abs(r2 - 1) < 1e-9)

    # Walk-forward respects priors
    class A:
        min_prior_games = 5
        halflife = 0
    cfg = Cfg(A)
    games = [{"fp": 6.0, "pf": 1.0, "venue": "X", "name": "T",
              "date": f"2024-04-{d:02d}", "season": 2024}
             for d in range(1, 21)]
    graded = list(walk_forward(games, cfg))
    check("walk_forward grades N-min_prior games",
          len(graded) == len(games) - cfg.min_prior)
    # constant-fp hitter: baseline should equal the constant, withpark scales
    check("baseline of constant series == constant",
          abs(graded[0]["baseline"] - 6.0) < 1e-9)

    print("-----------------------------------------------------------------")
    print("SELFTEST: " + ("ALL PASSED" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def run_backtest(a):
    cfg = Cfg(a)
    seasons = [int(s) for s in str(a.seasons).split(",") if s.strip()]
    print(f"Fetching MLB data for seasons {seasons} ...")
    recs = []
    for season in seasons:
        print(f"  [{season}] loading schedule (gamePk -> venue) ...")
        venues = get_schedule_venues(season)
        print(f"  [{season}] {len(venues)} games mapped to venues")
        universe = get_universe(season, a.min_pa, a.max_hitters, a.verbose)
        for i, (pid, name, pa) in enumerate(universe, 1):
            try:
                log = get_game_log(pid, season, venues)
            except RuntimeError as e:
                print(f"  ! skip {name}: {e}")
                continue
            for g in log:
                g["name"] = name
            recs.extend(walk_forward(log, cfg))
            if a.verbose:
                print(f"  [{season}] {i}/{len(universe)} {name} "
                      f"({len(log)} games)")
            time.sleep(a.sleep)
    report(recs, cfg)
    if a.csv_out:
        write_csv(recs, a.csv_out)
    return 0


def main():
    p = argparse.ArgumentParser(description="Hitter park-factor backtest")
    p.add_argument("--seasons", default="2024",
                   help="comma-separated seasons, e.g. 2023,2024")
    p.add_argument("--min-pa", type=int, default=150,
                   help="min plate appearances to enter the hitter universe")
    p.add_argument("--max-hitters", type=int, default=150,
                   help="cap universe size (most PA first)")
    p.add_argument("--min-prior-games", type=int, default=20,
                   help="prior games required before a game is graded")
    p.add_argument("--halflife", type=float, default=0.0,
                   help="EWMA half-life in games (0 = flat/equal weights)")
    p.add_argument("--sleep", type=float, default=0.35,
                   help="seconds between API calls (be polite)")
    p.add_argument("--csv-out", default="",
                   help="optional path to write per-game results CSV")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--selftest", action="store_true",
                   help="run offline math self-test and exit")
    a = p.parse_args()
    if a.selftest:
        sys.exit(run_selftest())
    sys.exit(run_backtest(a))


if __name__ == "__main__":
    main()
