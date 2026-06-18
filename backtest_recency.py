#!/usr/bin/env python3
"""
backtest_recency.py  ->  prints a report to the log (writes nothing to the repo)

WHAT THIS ANSWERS
-----------------
Your models use recency (EWMA) levers whose half-lives were educated guesses:
  - HITTER side: xwOBA EWMA, currently HALF-LIFE 130 PA
  - PITCHER side: whiff% EWMA, currently HALF-LIFE 200 swings

This script measures, walking forward through the season with NO look-ahead, whether
a recency-weighted number forecasts a player's NEAR-FUTURE performance better than his
season-to-date number -- and WHICH half-life does it best. It is a metric-level test
(does recent xwOBA predict future xwOBA?), not a full re-run of the cush score, so it
isolates the one thing we want to tune: the half-life.

HOW IT WORKS (per player, leakage-free)
---------------------------------------
Walk a split point through the player's game log. At each split, using ONLY games
before it, compute (a) season-to-date mean and (b) EWMA at several half-lives. Then
look at the player's ACTUAL production over the NEXT chunk of games (the "future
window"). Whichever predictor lands closest to that future, across thousands of
(player, split) samples, is the better forecast. Lower error = better.

HOW TO READ THE OUTPUT
----------------------
For each model it prints a table:
    predictor      RMSE     corr    vs season
Lower RMSE is better; "vs season" is the % improvement over the season-to-date number.
The line marked  <<< BEST  is the half-life that forecasts best. It also reports the
best linear blend weight (how much to lean on recency vs season), which maps to how
hard to regress the lever. Compare BEST to the half-life you're currently running.

A small or negative improvement means recency is barely helping for that metric -- also
useful to know. This is a forecasting test, so absolute RMSE is dominated by the
irreducible noise in a short future window; the COMPARISON between predictors is the
signal, not the absolute number.

RUN: best as a one-off GitHub Action (workflow_dispatch). It pulls a lot from Savant,
so it takes a while (~15-30 min). Read the report at the bottom of the Actions log.
DEPENDENCIES: standard library only. No API key.
"""

import csv, io, math, sys, time, urllib.request

SEASON      = 2026
MLB_BASE    = "https://statsapi.mlb.com/api/v1"
SAVANT_CSV  = "https://baseballsavant.mlb.com/statcast_search/csv"
TIMEOUT     = 60
PAUSE       = 1.1
UA = {"User-Agent": "Mozilla/5.0 (cush-backtest)"}
_NULLISH = ("", "null", "NA", "nan", "NaN")

# ---- HITTER (xwOBA) test settings ----
H_HALF_LIVES = [60, 90, 130, 180, 250]   # PA; 130 is the value currently shipped
H_MIN_HIST   = 100                        # need >=100 PA of history before a split counts
H_FUT_PA     = 50                         # future window size
H_STRIDE     = 5                          # step the split every N games (keeps samples independent)
H_MIN_GAMES  = 22

# ---- PITCHER (whiff%) test settings ----
P_HALF_LIVES = [120, 160, 200, 280, 400]  # swings; 200 is the value currently shipped
P_MIN_HIST   = 150
P_FUT_SW     = 80
P_STRIDE     = 3
P_MIN_GAMES  = 12

WHIFF = {"swinging_strike", "swinging_strike_blocked", "missed_bunt", "swinging_pitchout"}
SWING = WHIFF | {"foul", "foul_tip", "hit_into_play", "foul_bunt", "bunt_foul_tip"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            last = e
            time.sleep(2.0 * (k + 1))
    print("  ! fetch failed:", last, file=sys.stderr)
    return None


def _fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def roster_ids(want_pitchers):
    """All rostered players; want_pitchers=True -> P, else position players."""
    ids = {}
    teams = _get(MLB_BASE + "/teams?sportId=1")
    import json
    teams = json.loads(teams or '{"teams":[]}')["teams"]
    for t in teams:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, t["id"]))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            is_p = (p.get("position") or {}).get("abbreviation") == "P"
            if is_p == want_pitchers:
                ids[p["person"]["id"]] = p["person"].get("fullName", "")
        time.sleep(0.04)
    return ids


def savant_url(pid, player_type):
    return (
        SAVANT_CSV +
        "?all=true&type=details&player_type=%s"
        "&hfSea=%d%%7C&group_by=name&min_pitches=0&min_results=0&min_pas=0"
        "&sort_col=pitches&player_event_sort=api_p_release_speed&sort_order=desc"
        "&%ss_lookup%%5B%%5D=%d" % (player_type, SEASON, player_type, pid)
    )


def xwoba_games(csv_text):
    """Batter per-game xwOBA -> chronological [(date, xwoba, pa)]."""
    games = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        den = _fnum(row.get("woba_denom"), 0.0)
        if not den or den <= 0:
            continue
        desc = (row.get("description") or "").strip()
        xev = (row.get("estimated_woba_using_speedangle") or "").strip()
        val = _fnum(xev) if (desc == "hit_into_play" and xev not in _NULLISH) else None
        if val is None:
            val = _fnum(row.get("woba_value"), 0.0)
        gpk = row.get("game_pk") or row.get("game_date")
        if gpk not in games:
            games[gpk] = [row.get("game_date", ""), 0.0, 0.0]
        games[gpk][1] += val
        games[gpk][2] += den
    out = [(d, num / den, den) for (d, num, den) in games.values() if den > 0]
    out.sort(key=lambda r: r[0])
    return out


def whiff_games(csv_text):
    """Pitcher per-game whiff% -> chronological [(date, whiff_pct, swings)]."""
    games = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        desc = (row.get("description") or "").strip()
        if desc not in SWING:
            continue
        gpk = row.get("game_pk") or row.get("game_date")
        if gpk not in games:
            games[gpk] = [row.get("game_date", ""), 0, 0]
        games[gpk][2] += 1
        if desc in WHIFF:
            games[gpk][1] += 1
    out = [(d, 100.0 * w / sw, sw) for (d, w, sw) in games.values() if sw > 0]
    out.sort(key=lambda r: r[0])
    return out


# ---------- predictors (as-of split index s, using games 0..s) ----------
def ewma_asof(series, s, half_life):
    lam = 0.5 ** (1.0 / half_life)
    S = Wt = 0.0
    decay = 1.0
    for j in range(s, -1, -1):                 # newest -> oldest within history
        _, v, w = series[j]
        ww = decay * w
        S += ww * v
        Wt += ww
        decay *= lam ** w
    return (S / Wt) if Wt > 0 else None


def cummean_asof(series, s):
    num = den = 0.0
    for j in range(s + 1):
        _, v, w = series[j]
        num += v * w
        den += w
    return (num / den) if den > 0 else None


def future_mean(series, start, target_w):
    num = den = 0.0
    for j in range(start, len(series)):
        _, v, w = series[j]
        num += v * w
        den += w
        if den >= target_w:
            return num / den, den
    return None, 0.0                            # not enough future remaining


# ---------- weighted stats ----------
def wrmse(P, A, W):
    den = sum(W)
    return math.sqrt(sum(w * (p - a) ** 2 for p, a, w in zip(P, A, W)) / den) if den else None


def wcorr(P, A, W):
    sw = sum(W)
    if sw <= 0:
        return 0.0
    mp = sum(w * p for p, w in zip(P, W)) / sw
    ma = sum(w * a for a, w in zip(A, W)) / sw
    cov = sum(w * (p - mp) * (a - ma) for p, a, w in zip(P, A, W)) / sw
    vp = sum(w * (p - mp) ** 2 for p, w in zip(P, W)) / sw
    va = sum(w * (a - ma) ** 2 for a, w in zip(A, W)) / sw
    return cov / math.sqrt(vp * va) if vp > 0 and va > 0 else 0.0


def collect_samples(series_list, half_lives, min_hist, fut_w, stride):
    preds = {"season": []}
    for H in half_lives:
        preds["ewma%d" % H] = []
    outs, wts = [], []
    for series in series_list:
        n = len(series)
        cumw = []
        t = 0.0
        for (_, _, w) in series:
            t += w
            cumw.append(t)
        for s in range(0, n - 1, stride):
            if cumw[s] < min_hist:
                continue
            fm, fw = future_mean(series, s + 1, fut_w)
            if fm is None:
                continue
            sea = cummean_asof(series, s)
            if sea is None:
                continue
            row = {"season": sea}
            ok = True
            for H in half_lives:
                e = ewma_asof(series, s, H)
                if e is None:
                    ok = False
                    break
                row["ewma%d" % H] = e
            if not ok:
                continue
            for k, v in row.items():
                preds[k].append(v)
            outs.append(fm)
            wts.append(fw)
    return preds, outs, wts


def best_blend(ewma_vals, season_vals, A, W):
    best_w, best_r = None, None
    for i in range(0, 11):
        w = i / 10.0
        P = [w * e + (1 - w) * s for e, s in zip(ewma_vals, season_vals)]
        r = wrmse(P, A, W)
        if r is not None and (best_r is None or r < best_r):
            best_w, best_r = w, r
    return best_w, best_r


def report(title, preds, outs, wts, half_lives, shipped, unit):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)
    n = len(outs)
    if n < 200:
        print("  only %d samples -- too thin to trust; widen the player pool." % n)
        return
    base = wrmse(preds["season"], outs, wts)
    rows = []
    for key, label in [("season", "season-to-date")] + [("ewma%d" % H, "EWMA %d %s" % (H, unit)) for H in half_lives]:
        r = wrmse(preds[key], outs, wts)
        c = wcorr(preds[key], outs, wts)
        imp = 100.0 * (base - r) / base if base else 0.0
        rows.append((key, label, r, c, imp))
    best_key = min((row for row in rows if row[0] != "season"), key=lambda x: x[2])[0]
    print("  samples: %d   future window: %d %s" % (n, (H_FUT_PA if unit == "PA" else P_FUT_SW), unit))
    print("  %-18s %9s %7s %11s" % ("predictor", "RMSE", "corr", "vs season"))
    for key, label, r, c, imp in rows:
        mark = "  <<< BEST" if key == best_key else ""
        ship = "  (current)" if key == ("ewma%d" % shipped) else ""
        vs = "  --" if key == "season" else ("%+.1f%%" % imp)
        print("  %-18s %9.5f %7.2f %11s%s%s" % (label, r, c, vs, ship, mark))
    bestH = int(best_key.replace("ewma", ""))
    bw, br = best_blend(preds[best_key], preds["season"], outs, wts)
    print("  best single half-life: %d %s   (you currently run %d %s)" % (bestH, unit, shipped, unit))
    print("  best blend: %.0f%% EWMA / %.0f%% season  -> RMSE %.5f" % (bw * 100, (1 - bw) * 100, br))
    if br is not None and base:
        print("  blend improvement over season: %+.1f%%" % (100.0 * (base - br) / base))


def gather(ids, player_type, parser, min_games, label):
    series_list = []
    items = sorted(ids.items())
    for i, (pid, name) in enumerate(items, 1):
        txt = _get(savant_url(pid, player_type))
        time.sleep(PAUSE)
        if not txt or "game_pk" not in txt.split("\n", 1)[0]:
            continue
        g = parser(txt)
        if len(g) >= min_games:
            series_list.append(g)
        if i % 25 == 0:
            print("  %s %d/%d  (%d usable)" % (label, i, len(items), len(series_list)), file=sys.stderr)
    return series_list


def main():
    print("=== Cush recency backtest ===", file=sys.stderr)

    print("collecting hitters ...", file=sys.stderr)
    hids = roster_ids(want_pitchers=False)
    hseries = gather(hids, "batter", xwoba_games, H_MIN_GAMES, "hit")
    hp, ho, hw = collect_samples(hseries, H_HALF_LIVES, H_MIN_HIST, H_FUT_PA, H_STRIDE)
    report("HITTER  xwOBA recency  (predicting next %d PA of xwOBA)" % H_FUT_PA,
           hp, ho, hw, H_HALF_LIVES, shipped=130, unit="PA")

    print("collecting pitchers ...", file=sys.stderr)
    pids = roster_ids(want_pitchers=True)
    pseries = gather(pids, "pitcher", whiff_games, P_MIN_GAMES, "pit")
    pp, po, pw = collect_samples(pseries, P_HALF_LIVES, P_MIN_HIST, P_FUT_SW, P_STRIDE)
    report("PITCHER  whiff%% recency  (predicting next %d swings of whiff%%)" % P_FUT_SW,
           pp, po, pw, P_HALF_LIVES, shipped=200, unit="swings")

    print("\nDone. Compare BEST to (current); if they differ, tune the half-life in the build script.")


if __name__ == "__main__":
    main()
