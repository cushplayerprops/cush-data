#!/usr/bin/env python3
"""
backtest_hr_weight.py  ->  prints a report to the log (writes nothing to the repo)

WHAT THIS ANSWERS
-----------------
The HR Model ranks with:  score = W * powerIndex + (1 - W) * pitcherHRindex
Its BAT/ARM slider is at 0.70 by default, untested. This finds the W that best
predicts real HOME RUNS (and total bases / extra-base hits as steadier secondary reads).

HOW IT WORKS (leakage-free, every batter-game this season)
----------------------------------------------------------
Walks every 2026 game in DATE ORDER. For each batter in each game it records, using
ONLY what was known BEFORE that game:
  * the hitter's season-to-date ISO  (SLG - AVG)      -> stand-in for powerIndex
  * the opposing STARTER's season-to-date HR allowed per batter faced
                                                       -> stand-in for pitcherHRindex
                                                          (higher = more homer-prone)
  * the outcome that game: HR, total bases, extra-base hits
Both inputs are standardized (z-scored), then it sweeps W from 0 to 1 and asks which
blend correlates best with real power output -- and which blend's TOP-DECILE of games
actually produced the most (the bettor's metric).

CAVEATS (honest):
  * ISO stands in for the model's powerIndex (which also folds in launch angle);
    HR-allowed-per-BF stands in for the pitcher index (which also folds in barrels).
    So the best W is a strong GUIDE for the HR slider, not an exact transfer.
  * HR is a rare event, so its correlation numbers are small and noisy -- lean on the
    TOP-10% LIFT line and on the steadier TOTAL BASES / XBH reads for the verdict.

RUN: one-off GitHub Action (workflow_dispatch). A boxscore per game (~1000+), so it's
a LONGER run. Read the report at the bottom.
DEPENDENCIES: standard library only. No API key.
"""

import json, sys, time, math, urllib.request

SEASON      = 2026
START_DATE  = "2026-03-15"
END_DATE    = "2026-06-19"
W_SWEEP     = [round(0.05 * i, 2) for i in range(0, 21)]
MIN_BAT_AB  = 50
MIN_PIT_BF  = 120      # HR-allowed is noisy; ask for a bit more before trusting it
MLB_BASE    = "https://statsapi.mlb.com/api/v1"
TIMEOUT     = 30
UA = {"User-Agent": "Mozilla/5.0 (cush-hrwbt)"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            last = e
            time.sleep(1.2 * (k + 1))
    return None


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def schedule_gamepks():
    url = ("%s/schedule?sportId=1&startDate=%s&endDate=%s&gameType=R"
           % (MLB_BASE, START_DATE, END_DATE))
    data = _get(url)
    out = []
    try:
        for day in json.loads(data).get("dates", []):
            for g in day.get("games", []):
                if ((g.get("status") or {}).get("abstractGameState") or "") == "Final":
                    out.append((day.get("date", ""), g["gamePk"]))
    except (KeyError, TypeError, ValueError):
        pass
    return out


def parse_boxscore(gamepk):
    """-> {away:{starter,batters}, home:{starter,batters}} or None.
    batter=(pid,HR,2B,3B,H,AB,TB); starter=(pid,HRallowed,BF)."""
    data = _get("%s/game/%d/boxscore" % (MLB_BASE, gamepk))
    try:
        teams = json.loads(data)["teams"]
    except (KeyError, TypeError, ValueError):
        return None
    out = {}
    for side in ("away", "home"):
        t = teams.get(side) or {}
        players = t.get("players") or {}
        plist = t.get("pitchers") or []
        starter = None
        if plist:
            ps = ((players.get("ID%d" % plist[0]) or {}).get("stats") or {}).get("pitching") or {}
            bf = _f(ps.get("battersFaced"))
            if bf > 0:
                starter = (plist[0], _f(ps.get("homeRuns")), bf)
        batters = []
        for pl in players.values():
            bs = (pl.get("stats") or {}).get("batting") or {}
            ab = _f(bs.get("atBats"))
            if ab + _f(bs.get("baseOnBalls")) + _f(bs.get("hitByPitch")) + _f(bs.get("sacFlies")) <= 0:
                continue
            h = _f(bs.get("hits")); d2 = _f(bs.get("doubles"))
            t3 = _f(bs.get("triples")); hr = _f(bs.get("homeRuns"))
            tb = h + d2 + 2 * t3 + 3 * hr
            batters.append((pl["person"]["id"], hr, d2, t3, h, ab, tb))
        out[side] = {"starter": starter, "batters": batters}
    return out


def collect(games, fetch=parse_boxscore):
    """-> samples (batISO, pitHRrate, HR, TB, XBH)."""
    bat = {}   # pid -> [AB,H,TB]
    pit = {}   # pid -> [BF,HR]
    samples = []
    for _, gp in games:
        box = fetch(gp)
        if not box:
            continue
        for side, opp in (("away", "home"), ("home", "away")):
            sp = box[opp]["starter"]
            if not sp:
                continue
            pp = pit.get(sp[0])
            if not pp or pp[0] < MIN_PIT_BF:
                continue
            pit_hr = pp[1] / pp[0]
            for b in box[side]["batters"]:
                ba = bat.get(b[0])
                if not ba or ba[0] < MIN_BAT_AB:
                    continue
                iso = (ba[2] - ba[1]) / ba[0] if ba[0] > 0 else 0.0   # (TB-H)/AB
                hr, d2, t3, tb = b[1], b[2], b[3], b[6]
                samples.append((iso, pit_hr, hr, tb, d2 + t3 + hr))
        for side in ("away", "home"):
            for b in box[side]["batters"]:
                a = bat.setdefault(b[0], [0.0] * 3)
                a[0] += b[5]; a[1] += b[4]; a[2] += b[6]
            sp = box[side]["starter"]
            if sp:
                p = pit.setdefault(sp[0], [0.0] * 2)
                p[0] += sp[2]; p[1] += sp[1]
    return samples


def _z(vals):
    n = len(vals); m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / n) or 1.0
    return [(v - m) / sd for v in vals]


def _pearson(a, b):
    n = len(a); ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a)); db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da and db else 0.0


def _lift(scores, outs, frac=0.10):
    n = len(scores); k = max(1, int(n * frac))
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)[:k]
    top = sum(outs[i] for i in order) / k
    base = sum(outs) / n
    return top / base if base else 0.0


def report(samples):
    print("\n" + "=" * 70)
    print("HR MODEL WEIGHT BACKTEST   (W = weight on power; 1-W on the HR-prone arm)")
    print("=" * 70)
    n = len(samples)
    if n < 500:
        print("  only %d samples -- too thin to trust." % n)
        return
    zb = _z([s[0] for s in samples])      # hitter ISO
    zp = _z([s[1] for s in samples])      # starter HR-allowed
    outcomes = {
        "HOME RUNS":            [s[2] for s in samples],
        "TOTAL BASES":          [s[3] for s in samples],
        "XBH (2B+3B+HR)":       [s[4] for s in samples],
    }
    print("  samples: %d batter-games" % n)
    for label, outs in outcomes.items():
        print("\n  --- predicting %s ---" % label)
        best_c = best_l = None
        for W in W_SWEEP:
            comb = [W * zb[i] + (1 - W) * zp[i] for i in range(n)]
            c = _pearson(comb, outs); l = _lift(comb, outs)
            if best_c is None or c > best_c[1]:
                best_c = (W, c)
            if best_l is None or l > best_l[1]:
                best_l = (W, l)
            if W in (0.0, 0.5, 0.65, 0.7, 1.0):
                tag = "  <- current default" if W == 0.7 else ""
                print("    W=%.2f  corr %.4f   top10%% lift %.3f%s" % (W, c, l, tag))
        print("    best by correlation: W=%.2f (corr %.4f)" % best_c)
        print("    best by top-10%% lift: W=%.2f (lift %.3f)" % best_l)


def main():
    print("loading schedule ...", file=sys.stderr)
    games = schedule_gamepks()
    print("  %d final games" % len(games), file=sys.stderr)
    print("walking games (a boxscore each -- slow part) ...", file=sys.stderr)
    t0 = time.time(); state = {"i": 0}
    def fetch(gp):
        state["i"] += 1
        if state["i"] % 100 == 0:
            print("  %d/%d (%.0fs)" % (state["i"], len(games), time.time() - t0), file=sys.stderr)
        r = parse_boxscore(gp); time.sleep(0.05); return r
    samples = collect(games, fetch=fetch)
    report(samples)
    print("\nDone. Lean on TOP-10%% lift + the TOTAL BASES / XBH reads (HR alone is noisy).")


if __name__ == "__main__":
    main()
