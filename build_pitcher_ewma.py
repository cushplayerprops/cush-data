#!/usr/bin/env python3
"""
build_pitcher_ewma.py  ->  pitcher_ewma.json

Recency feed for the Cush STRIKEOUT model. For every rostered pitcher it pulls
per-game whiff% (swinging-strikes / swings) from Baseball Savant, computes a
swing-weighted EWMA, and writes one record per pitcher id:

    "543037": { "ewma": 27.4, "season": 25.1, "effN": 246.0 }

  ewma   = recency-weighted whiff% (recent swings weighted more)
  season = full-season whiff%, SAME definition
  effN   = effective swings behind the EWMA (sum of decayed weights)

The strikeout model multiplies the matchup-blended whiff by a recency factor:

    whiff_used = whiff * (1 + (effN/(effN+KREC_K)) * (ewma/season - 1))

so a pitcher whose whiff is running hot lately gets a bounded bump to his
K-ability score, regressed by how many recent swings back it. Absent feed or
missing pitcher => factor is exactly 1.0 (season behavior). The displayed WHIFF
column keeps the season value; only the score moves.

WHY WHIFF: of the K-skill inputs, swinging-strike rate is the most stable and
predictive, and it stabilizes FAST for pitchers (~a few starts) -- so recency is
genuine signal here, not noise, and catches velocity/stuff dips that season stats
hide. That's why this is the highest-value pitcher-recency lever.

HALF_LIFE_SWINGS = 200 (~4-5 starts). Conservative-but-responsive; whiff stabilizes
faster than xwOBA so it's regressed a touch less than the hitter feed. Educated
default -- backtest to tune.

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push pitcher_ewma.json to root of cushplayerprops/cush-data main -> cron.
If you already pull Statcast for pitchers, reuse game_whiff_series + ewma_from_games
on that cached data instead of re-pulling here.
"""

import csv, io, json, sys, time, urllib.request

SEASON            = 2026
HALF_LIFE_SWINGS  = 200
MLB_BASE          = "https://statsapi.mlb.com/api/v1"
SAVANT_CSV        = "https://baseballsavant.mlb.com/statcast_search/csv"
OUT_PATH          = "pitcher_ewma.json"
REQUEST_PAUSE     = 1.2
TIMEOUT           = 60
LAMBDA            = 0.5 ** (1.0 / HALF_LIFE_SWINGS)
UA = {"User-Agent": "Mozilla/5.0 (cush-pewma-build)"}

# Savant pitch descriptions
WHIFF = {"swinging_strike", "swinging_strike_blocked", "missed_bunt", "swinging_pitchout"}
SWING = WHIFF | {"foul", "foul_tip", "hit_into_play", "foul_bunt", "bunt_foul_tip"}


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
        out[str(pid)] = {
            "ewma":   round(ewma, 2),
            "season": round(season_w, 2),
            "effN":   round(effn, 1),
        }
        if n % 25 == 0:
            print("  %d/%d (%d written)" % (n, len(ids), len(out)), file=sys.stderr)

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d pitchers (half-life %d swings)" %
          (OUT_PATH, len(out), HALF_LIFE_SWINGS), file=sys.stderr)


if __name__ == "__main__":
    main()
