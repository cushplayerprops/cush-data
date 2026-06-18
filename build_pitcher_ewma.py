#!/usr/bin/env python3
"""
build_pitcher_ewma.py  ->  pitcher_ewma.json

Recency feed for the Cush models. For every rostered pitcher it pulls per-pitch
data from Baseball Savant once, and computes TWO recency signals per pitcher id:

    "543037": { "ewma": 27.4, "season": 25.1, "effN": 246.0,
                "xwEwma": 0.318, "xwSeason": 0.305, "xwEffN": 210.0 }

  WHIFF (strikeout model):
    ewma   = recency-weighted whiff% (swinging strikes / swings)
    season = full-season whiff%
    effN   = effective swings behind the EWMA

  xwOBA-ALLOWED (hitter model, pitcher side):
    xwEwma   = recency-weighted xwOBA allowed
    xwSeason = full-season xwOBA allowed
    xwEffN   = effective PAs behind the EWMA

The strikeout model uses the whiff block (recent stuff). The HITTER model uses the
xwOBA-allowed block: if a starter's recent xwOBA-allowed is worse (higher) than his
season mark, the matchup tilts toward the hitter, regressed by xwEffN. Both are
inert (no tilt) when the field is missing -- so old consumers that only read the
whiff fields keep working unchanged.

xwOBA-ALLOWED METHOD (standard Savant reconstruction): for each PA-ending event
(woba_denom > 0) use xwOBA-on-contact (estimated_woba_using_speedangle) for balls
in play, and the wOBA value for non-contact outcomes (K = 0, BB/HBP weights).
Sum / PA per game, then PA-weighted EWMA.

HALF_LIFE_SWINGS = 200 (~4-5 starts); HALF_LIFE_PA_XW = 200 PA (~7-9 starts).
xwOBA stabilizes slower than whiff, so it is regressed at least as hard. Educated
defaults -- backtest to tune.

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push pitcher_ewma.json to root of cushplayerprops/cush-data main -> cron.
"""

import csv, io, json, sys, time, urllib.request

SEASON            = 2026
HALF_LIFE_SWINGS  = 200
HALF_LIFE_PA_XW   = 200
MLB_BASE          = "https://statsapi.mlb.com/api/v1"
SAVANT_CSV        = "https://baseballsavant.mlb.com/statcast_search/csv"
OUT_PATH          = "pitcher_ewma.json"
REQUEST_PAUSE     = 1.2
TIMEOUT           = 60
LAMBDA            = 0.5 ** (1.0 / HALF_LIFE_SWINGS)
LAMBDA_XW         = 0.5 ** (1.0 / HALF_LIFE_PA_XW)
UA = {"User-Agent": "Mozilla/5.0 (cush-pewma-build)"}

# Savant pitch descriptions
WHIFF = {"swinging_strike", "swinging_strike_blocked", "missed_bunt", "swinging_pitchout"}
SWING = WHIFF | {"foul", "foul_tip", "hit_into_play", "foul_bunt", "bunt_foul_tip"}
_NULLISH = ("", "null", "NA", "nan", "NaN")


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


def pitcher_ids():
    ids = {}
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    for t in teams:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, t["id"]))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            if (p.get("position") or {}).get("abbreviation") == "P":
                ids[p["person"]["id"]] = p["person"].get("fullName", "")
        time.sleep(0.05)
    return ids


def savant_csv_url(pid):
    return (
        SAVANT_CSV +
        "?all=true&type=details&player_type=pitcher"
        "&hfSea=%d%%7C&group_by=name&min_pitches=0&min_results=0&min_pas=0"
        "&sort_col=pitches&player_event_sort=api_p_release_speed&sort_order=desc"
        "&pitchers_lookup%%5B%%5D=%d" % (SEASON, pid)
    )


def game_whiff_series(csv_text):
    """Per-pitch CSV -> list of (game_date, whiff_pct, swings) + season (whiff_pct, swings)."""
    games = {}                                   # game_pk -> [date, whiffs, swings]
    rdr = csv.DictReader(io.StringIO(csv_text))
    for row in rdr:
        desc = (row.get("description") or "").strip()
        if desc not in SWING:
            continue
        gpk = row.get("game_pk") or row.get("game_date")
        if gpk not in games:
            games[gpk] = [row.get("game_date", ""), 0, 0]
        games[gpk][2] += 1                       # swing
        if desc in WHIFF:
            games[gpk][1] += 1                   # whiff

    series, tw, ts = [], 0, 0
    for gpk, (d, w, sw) in games.items():
        if sw <= 0:
            continue
        series.append((d, 100.0 * w / sw, sw))
        tw += w
        ts += sw
    series.sort(key=lambda r: r[0])
    season = (100.0 * tw / ts, ts) if ts > 0 else (None, 0)
    return series, season


def _fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def game_xwoba_series(csv_text):
    """Per-pitch CSV -> per-game xwOBA-allowed [(date, xwoba, pa)] + season (xwoba, pa).

    PA-ending events are those with woba_denom > 0. For balls in play we credit
    xwOBA-on-contact (estimated_woba_using_speedangle); for everything else
    (strikeouts, walks, HBP) we credit the actual wOBA value (K -> 0, BB/HBP -> their
    weights). That reconstruction IS xwOBA. Falls back to woba_value when a BIP has
    no Statcast estimate.
    """
    games = {}                                   # game_pk -> [date, num, den]
    rdr = csv.DictReader(io.StringIO(csv_text))
    for row in rdr:
        den = _fnum(row.get("woba_denom"), 0.0)
        if not den or den <= 0:
            continue                             # not a PA-ending event
        desc = (row.get("description") or "").strip()
        xev = (row.get("estimated_woba_using_speedangle") or "").strip()
        val = None
        if desc == "hit_into_play" and xev not in _NULLISH:
            val = _fnum(xev)
        if val is None:
            val = _fnum(row.get("woba_value"), 0.0)
        gpk = row.get("game_pk") or row.get("game_date")
        if gpk not in games:
            games[gpk] = [row.get("game_date", ""), 0.0, 0.0]
        games[gpk][1] += val
        games[gpk][2] += den

    series, tn, td = [], 0.0, 0.0
    for gpk, (d, num, den) in games.items():
        if den <= 0:
            continue
        series.append((d, num / den, den))
        tn += num
        td += den
    series.sort(key=lambda r: r[0])
    season = (tn / td, td) if td > 0 else (None, 0.0)
    return series, season


def ewma_from_games(series):
    """Swing-weighted EWMA over chronological [(date, whiff_pct, swings), ...]."""
    S = Wt = 0.0
    decay = 1.0
    for _, wpct, sw in reversed(series):         # newest -> oldest
        w = decay * sw
        S += w * wpct
        Wt += w
        decay *= LAMBDA ** sw
    if Wt <= 0:
        return None, 0.0
    return S / Wt, Wt


def ewma_pa(series, lam):
    """PA-weighted EWMA over chronological [(date, value, pa), ...]."""
    S = Wt = 0.0
    decay = 1.0
    for _, v, pa in reversed(series):            # newest -> oldest
        w = decay * pa
        S += w * v
        Wt += w
        decay *= lam ** pa
    if Wt <= 0:
        return None, 0.0
    return S / Wt, Wt


def main():
    print("collecting pitchers ...", file=sys.stderr)
    ids = pitcher_ids()
    print("  %d pitchers" % len(ids), file=sys.stderr)

    out = {}
    for n, (pid, name) in enumerate(sorted(ids.items()), 1):
        csv_text = _get(savant_csv_url(pid))
        time.sleep(REQUEST_PAUSE)
        if not csv_text or "game_pk" not in csv_text.split("\n", 1)[0]:
            continue
        series, (season_w, _) = game_whiff_series(csv_text)
        if not series or season_w is None:
            continue
        ewma, effn = ewma_from_games(series)
        if ewma is None or effn <= 0:
            continue
        rec = {
            "ewma":   round(ewma, 2),
            "season": round(season_w, 2),
            "effN":   round(effn, 1),
        }
        # xwOBA-allowed recency (hitter model). Added when available; absent => no tilt.
        xser, (xseason, _) = game_xwoba_series(csv_text)
        if xser and xseason is not None:
            xewma, xeff = ewma_pa(xser, LAMBDA_XW)
            if xewma is not None and xeff > 0:
                rec["xwEwma"]   = round(xewma, 3)
                rec["xwSeason"] = round(xseason, 3)
                rec["xwEffN"]   = round(xeff, 1)
        out[str(pid)] = rec
        if n % 25 == 0:
            print("  %d/%d (%d written)" % (n, len(ids), len(out)), file=sys.stderr)

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d pitchers (whiff hl %d swings, xwOBA hl %d PA)" %
          (OUT_PATH, len(out), HALF_LIFE_SWINGS, HALF_LIFE_PA_XW), file=sys.stderr)


if __name__ == "__main__":
    main()
