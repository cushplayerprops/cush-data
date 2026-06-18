#!/usr/bin/env python3
"""
build_hitter_ewma.py  ->  hitter_ewma.json

Recency feed for the Cush hitter model. For every hitter on an active roster it
pulls per-game xwOBA from Baseball Savant, computes a PA-weighted EWMA
(exponentially-weighted moving average), and writes one record per player id:

    "660271": { "ewma": 0.3584, "season": 0.3310, "effPa": 142.0 }

  ewma   = recency-weighted xwOBA (recent PAs weighted more; half-life below)
  season = full-season xwOBA, SAME definition (so the front-end can compare cleanly)
  effPa  = effective plate appearances behind the EWMA (sum of decayed weights).
           The app regresses the recency tilt by effPa/(effPa+REC_K), so low-sample
           hitters barely move off their season number -- no extra filtering needed.

The front-end (deploy with HITTER_EWMA_URL wired) reads this and applies an additive
xwOBA recency tilt to the hitter index, with an EXACT season fallback when a hitter
is missing or has effPa<=0. Absent feed => model behaves exactly as before.

DESIGN NOTES (the choices that matter for accuracy):
  * Metric = xwOBA, not wOBA. xwOBA strips batted-ball luck, is more predictive of
    future production, and stabilizes faster -> a better recency signal.
  * HALF_LIFE_PA = 250 (~last 60 games). Backtest-tuned: long+regressed wins. A short, streaky
    half-life feels clever but backtests WORSE -- it chases noise. Long + regressed
    captures genuine change (swing/health/role) while staying anchored to true talent.
  * Weighting is per-PA, not per-game, so a 5-PA day counts more than a 2-PA day.
  * This is an EDUCATED default. The only way to truly maximize accuracy is to backtest
    HALF_LIFE_PA (and the app's REC_K) against held-out games. Treat these as a strong
    starting point, then tune.

DEPENDENCIES: standard library only (urllib, csv, json, time). No API key.

DEPLOY: run this, push hitter_ewma.json to the ROOT of the main branch of
cushplayerprops/cush-data, add to the daily cron. If you already pull Statcast for
your BPI build, the cheapest path is to compute the EWMA from THAT cached event data
(reuse `game_xwoba_series` + `ewma_from_games` below) instead of re-pulling here.
"""

import csv, io, json, sys, time, urllib.request, urllib.error

SEASON         = 2026
HALF_LIFE_PA   = 250          # EWMA half-life in plate appearances
MLB_BASE       = "https://statsapi.mlb.com/api/v1"
SAVANT_CSV     = "https://baseballsavant.mlb.com/statcast_search/csv"
OUT_PATH       = "hitter_ewma.json"
REQUEST_PAUSE  = 1.2          # seconds between Savant pulls (be polite; it rate-limits)
TIMEOUT        = 60
LAMBDA         = 0.5 ** (1.0 / HALF_LIFE_PA)   # per-PA decay factor

UA = {"User-Agent": "Mozilla/5.0 (cush-ewma-build)"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            last = e
            time.sleep(2.0 * (k + 1))
    print("  ! fetch failed:", last, file=sys.stderr)
    return None


def active_hitter_ids():
    """Every position player / two-way bat on a current active roster."""
    ids = {}
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    for t in teams:
        tid = t["id"]
        url = "%s/teams/%d/roster?rosterType=active" % (MLB_BASE, tid)
        data = _get(url)
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            pos = (p.get("position") or {}).get("abbreviation", "")
            if pos in ("P",):          # skip pitchers; keep DH/IF/OF/C/TWP
                continue
            pid = p["person"]["id"]
            ids[pid] = p["person"].get("fullName", "")
        time.sleep(0.05)
    return ids


def savant_csv_url(pid):
    return (
        SAVANT_CSV +
        "?all=true&type=details&player_type=batter"
        "&hfSea=%d%%7C&group_by=name&min_pitches=0&min_results=0&min_pas=0"
        "&sort_col=pitches&player_event_sort=api_p_release_speed&sort_order=desc"
        "&batters_lookup%%5B%%5D=%d" % (SEASON, pid)
    )


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def game_xwoba_series(csv_text):
    """
    Parse a Savant per-pitch CSV into per-game xwOBA.
    Returns list of (game_date, xwoba_game, pa_game) and the season (xwoba, pa) totals.

    xwOBA per PA = estimated_woba_using_speedangle on batted balls,
                   else woba_value (BB/HBP/K and other PA outcomes).
    Denominator  = woba_denom (1 for PAs that count toward wOBA).
    """
    games = {}                       # game_pk -> [date, xnum_sum, denom_sum]
    rdr = csv.DictReader(io.StringIO(csv_text))
    for row in rdr:
        denom = _f(row.get("woba_denom"))
        if not denom:                # 0 / blank -> doesn't count
            continue
        est = _f(row.get("estimated_woba_using_speedangle"))
        wv  = _f(row.get("woba_value"))
        xnum = est if est is not None else (wv if wv is not None else 0.0)
        gpk = row.get("game_pk") or row.get("game_date")
        if gpk not in games:
            games[gpk] = [row.get("game_date", ""), 0.0, 0.0]
        games[gpk][1] += xnum
        games[gpk][2] += denom

    series, s_num, s_den = [], 0.0, 0.0
    for gpk, (d, xn, dn) in games.items():
        if dn <= 0:
            continue
        series.append((d, xn / dn, dn))
        s_num += xn
        s_den += dn
    series.sort(key=lambda r: r[0])  # chronological
    season = (s_num / s_den, s_den) if s_den > 0 else (None, 0.0)
    return series, season


def ewma_from_games(series):
    """PA-weighted EWMA over chronological [(date, xwoba_game, pa_game), ...]."""
    S = W = 0.0
    decay = 1.0
    for _, x, pa in reversed(series):      # newest -> oldest
        w = decay * pa
        S += w * x
        W += w
        decay *= LAMBDA ** pa
    if W <= 0:
        return None, 0.0
    return S / W, W


def main():
    print("collecting active hitters ...", file=sys.stderr)
    ids = active_hitter_ids()
    print("  %d hitters" % len(ids), file=sys.stderr)

    out = {}
    for n, (pid, name) in enumerate(sorted(ids.items()), 1):
        csv_text = _get(savant_csv_url(pid))
        time.sleep(REQUEST_PAUSE)
        if not csv_text or "game_pk" not in csv_text.split("\n", 1)[0]:
            continue
        series, (season_x, _) = game_xwoba_series(csv_text)
        if not series or season_x is None:
            continue
        ewma, effpa = ewma_from_games(series)
        if ewma is None or effpa <= 0:
            continue
        out[str(pid)] = {
            "ewma":  round(ewma, 4),
            "season": round(season_x, 4),
            "effPa": round(effpa, 1),
        }
        if n % 25 == 0:
            print("  %d/%d  (%d written)" % (n, len(ids), len(out)), file=sys.stderr)

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d hitters (half-life %d PA)" %
          (OUT_PATH, len(out), HALF_LIFE_PA), file=sys.stderr)


if __name__ == "__main__":
    main()
