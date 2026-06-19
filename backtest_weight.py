#!/usr/bin/env python3
"""
backtest_weight.py  ->  prints a report to the log (writes nothing to the repo)

WHAT THIS ANSWERS
-----------------
The Hitter Model ranks with:  score = W * hitterIndex + (1 - W) * pitcherIndex
The BAT/ARM slider is W. It's set to 0.70 (70% bat / 30% arm) by reasoning, not
measurement. This finds the W that best predicts real HITS / RUNS / RBIs.

HOW IT WORKS (leakage-free, every batter-game this season)
----------------------------------------------------------
Walks every 2026 game in DATE ORDER, accumulating each hitter's batting line and each
starter's results as it goes. For each batter in each game it records, using ONLY what
was known BEFORE that game:
  * the hitter's season-to-date OPS                 (stand-in for hitterIndex)
  * the opposing STARTER's season-to-date on-base-allowed per batter faced
                                                    (stand-in for pitcherIndex; higher
                                                     = more hittable = better for the bat)
  * the outcome that game: hits, runs, RBIs
Both inputs are standardized (z-scored) so W is a clean relative weight, the same way
the model blends two ~100-centered indices. Then it sweeps W from 0.0 to 1.0 and asks
which blend correlates best with real production -- and, the bettor's metric, which
blend's TOP-DECILE of games actually produced the most.

HOW TO READ IT
--------------
For each outcome (H+R+RBI, hits only, runs+RBI only) it prints, per W: the correlation
with real output and the top-10% lift (avg production of the highest-scored games vs the
field). Higher is better on both. The best-W lines and the W=0.70 reference tell you
whether today's default is right and which way to move it.

CAVEATS (honest):
  * OPS stands in for the model's richer hitterIndex (which adds handedness + xwOBA);
    on-base-allowed stands in for the pitcherIndex (xwOBA-allowed). So the best W is a
    strong GUIDE for the slider, not an exact transfer.
  * It predicts full-game production from the hitter + the STARTER -- which is exactly
    how the model uses the matchup (bullpen PAs add noise equally across all W).

RUN: one-off GitHub Action (workflow_dispatch). Pulls a boxscore per game (~1000+),
so it's a LONGER run than the other backtests. Read the report at the bottom.
DEPENDENCIES: standard library only. No API key.
"""

import json, sys, time, math, urllib.request

SEASON      = 2026
START_DATE  = "2026-03-15"
END_DATE    = "2026-06-19"
W_SWEEP     = [round(0.05 * i, 2) for i in range(0, 21)]   # 0.00 .. 1.00
MIN_BAT_AB  = 40       # hitter needs this many AB as-of to be a stable input
MIN_PIT_BF  = 80       # starter needs this many BF as-of (~2-3 starts)
LG_OPS      = 0.720
MLB_BASE    = "https://statsapi.mlb.com/api/v1"
TIMEOUT     = 30
UA = {"User-Agent": "Mozilla/5.0 (cush-wbt)"}


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
    """[(date, gamePk)] for Final regular-season games, in date order."""
    url = ("%s/schedule?sportId=1&startDate=%s&endDate=%s&gameType=R"
           % (MLB_BASE, START_DATE, END_DATE))
    data = _get(url)
    out = []
    try:
        for day in json.loads(data).get("dates", []):
            d = day.get("date", "")
            for g in day.get("games", []):
                st = ((g.get("status") or {}).get("abstractGameState") or "")
                if st == "Final":
                    out.append((d, g["gamePk"]))
    except (KeyError, TypeError, ValueError):
        pass
    return out


def parse_boxscore(gamepk):
    """-> {away:{starter,batters}, home:{starter,batters}} or None.
    batter = (pid,H,R,RBI,AB,BB,HBP,SF,TB); starter = (pid,H,BB,HBP,BF)."""
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
            sp = players.get("ID%d" % plist[0]) or {}
            ps = (sp.get("stats") or {}).get("pitching") or {}
            bf = _f(ps.get("battersFaced"))
            if bf > 0:
                starter = (plist[0], _f(ps.get("hits")), _f(ps.get("baseOnBalls")),
                           _f(ps.get("hitByPitch")), bf)
        batters = []
        for key, pl in players.items():
            bs = (pl.get("stats") or {}).get("batting") or {}
            ab = _f(bs.get("atBats")); bb = _f(bs.get("baseOnBalls"))
            hbp = _f(bs.get("hitByPitch")); sf = _f(bs.get("sacFlies"))
            if ab + bb + hbp + sf <= 0:
                continue
            h = _f(bs.get("hits")); d2 = _f(bs.get("doubles"))
            t3 = _f(bs.get("triples")); hr = _f(bs.get("homeRuns"))
            tb = h + d2 + 2 * t3 + 3 * hr
            batters.append((pl["person"]["id"], h, _f(bs.get("runs")), _f(bs.get("rbi")),
                            ab, bb, hbp, sf, tb))
        out[side] = {"starter": starter, "batters": batters}
    return out


def ops_of(H, AB, BB, HBP, SF, TB):
    den = AB + BB + HBP + SF
    obp = (H + BB + HBP) / den if den > 0 else 0.0
    slg = TB / AB if AB > 0 else 0.0
    return obp + slg


def collect(games, fetch=parse_boxscore):
    """games: [(date,gamePk)] date-ordered -> samples (batOPS, pitOB, H, R, RBI)."""
    bat = {}   # pid -> [AB,H,BB,HBP,SF,TB]
    pit = {}   # pid -> [BF,H,BB,HBP]
    samples = []
    for _, gp in games:
        box = fetch(gp)
        if not box:
            continue
        # record (as-of) BEFORE updating accumulators
        for side, opp in (("away", "home"), ("home", "away")):
            sp = box[opp]["starter"]
            if not sp:
                continue
            pp = pit.get(sp[0])
            if not pp or pp[0] < MIN_PIT_BF:
                continue
            pit_ob = (pp[1] + pp[2] + pp[3]) / pp[0] if pp[0] > 0 else None
            for b in box[side]["batters"]:
                ba = bat.get(b[0])
                if not ba or ba[0] < MIN_BAT_AB:
                    continue
                bops = ops_of(ba[1], ba[0], ba[2], ba[3], ba[4], ba[5])
                samples.append((bops, pit_ob, b[1], b[2], b[3]))
        # now fold this game into the accumulators
        for side in ("away", "home"):
            for b in box[side]["batters"]:
                a = bat.setdefault(b[0], [0.0] * 6)
                a[0] += b[4]; a[1] += b[1]; a[2] += b[5]
                a[3] += b[6]; a[4] += b[7]; a[5] += b[8]
            sp = box[side]["starter"]
            if sp:
                p = pit.setdefault(sp[0], [0.0] * 4)
                p[0] += sp[4]; p[1] += sp[1]; p[2] += sp[2]; p[3] += sp[3]
    return samples


def _z(vals):
    n = len(vals)
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / n) or 1.0
    return [(v - m) / sd for v in vals], m, sd


def _pearson(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da and db else 0.0


def _lift(scores, outs, frac=0.10):
    n = len(scores)
    k = max(1, int(n * frac))
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)[:k]
    top = sum(outs[i] for i in order) / k
    base = sum(outs) / n
    return top / base if base else 0.0


def report(samples):
    print("\n" + "=" * 70)
    print("BAT/ARM WEIGHT BACKTEST   (W = weight on the hitter; 1-W on the arm)")
    print("=" * 70)
    n = len(samples)
    if n < 500:
        print("  only %d samples -- too thin to trust." % n)
        return
    zb, _, _ = _z([s[0] for s in samples])     # hitter OPS, standardized
    zp, _, _ = _z([s[1] for s in samples])     # starter hittability, standardized
    outcomes = {
        "H + R + RBI": [s[2] + s[3] + s[4] for s in samples],
        "HITS only":   [s[2] for s in samples],
        "R + RBI":     [s[3] + s[4] for s in samples],
    }
    print("  samples: %d batter-games" % n)
    for label, outs in outcomes.items():
        print("\n  --- predicting %s ---" % label)
        best_c = best_l = None
        for W in W_SWEEP:
            comb = [W * zb[i] + (1 - W) * zp[i] for i in range(n)]
            c = _pearson(comb, outs)
            l = _lift(comb, outs)
            if best_c is None or c > best_c[1]:
                best_c = (W, c)
            if best_l is None or l > best_l[1]:
                best_l = (W, l)
            if W in (0.0, 0.5, 0.7, 0.8, 1.0):
                tag = "  <- current default" if W == 0.7 else ""
                print("    W=%.2f  corr %.4f   top10%% lift %.3f%s" % (W, c, l, tag))
        print("    best by correlation: W=%.2f (corr %.4f)" % best_c)
        print("    best by top-10%% lift: W=%.2f (lift %.3f)" % best_l)


def main():
    print("loading schedule ...", file=sys.stderr)
    games = schedule_gamepks()
    print("  %d final games" % len(games), file=sys.stderr)
    print("walking games (a boxscore each -- this is the slow part) ...", file=sys.stderr)
    t0 = time.time()
    # progress wrapper around the fetch
    state = {"i": 0}
    def fetch(gp):
        state["i"] += 1
        if state["i"] % 100 == 0:
            print("  %d/%d games (%.0fs)" % (state["i"], len(games), time.time() - t0),
                  file=sys.stderr)
        r = parse_boxscore(gp)
        time.sleep(0.05)
        return r
    samples = collect(games, fetch=fetch)
    report(samples)
    print("\nDone. Use the best-W consensus across H+R+RBI as the slider guide.")


if __name__ == "__main__":
    main()
