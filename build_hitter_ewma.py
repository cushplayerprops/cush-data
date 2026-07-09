#!/usr/bin/env python3
"""
build_hitter_ewma.py  ->  hitter_ewma.json  +  hitter_recent.json

xwOBA EWMA recency feed, plus a last-N-plate-appearance slash line PER HAND
(vs RHP and vs LHP) computed from the same Savant pull. Fixed sample size =>
stable reliability, and vs-LHP just reaches further back to gather its N.
"""

import csv, io, json, sys, time, urllib.request, urllib.error
from datetime import date, timedelta

SEASON         = 2026
HALF_LIFE_PA   = 250
MLB_BASE       = "https://statsapi.mlb.com/api/v1"
SAVANT_CSV     = "https://baseballsavant.mlb.com/statcast_search/csv"
OUT_PATH       = "hitter_ewma.json"
OUT_RECENT     = "hitter_recent.json"
RECENT_PA      = 40
RECENT_MIN_PA  = 8
REQUEST_PAUSE  = 1.2
TIMEOUT        = 60
LAMBDA         = 0.5 ** (1.0 / HALF_LIFE_PA)

_NON_PA = {
    "caught_stealing_2b", "caught_stealing_3b", "caught_stealing_home",
    "pickoff_1b", "pickoff_2b", "pickoff_3b",
    "pickoff_caught_stealing_2b", "pickoff_caught_stealing_3b", "pickoff_caught_stealing_home",
    "stolen_base_2b", "stolen_base_3b", "stolen_base_home", "stolen_base",
    "wild_pitch", "passed_ball", "balk", "other_advance", "runner_double_play",
    "game_advisory", "batter_timeout",
}


def _slash_from_events(events):
    PA = AB = H = TB = BB = SO = HBP = SF = 0
    for ev in events:
        PA += 1
        if ev in ("walk", "intent_walk"):
            BB += 1
        elif ev == "hit_by_pitch":
            HBP += 1
        elif ev in ("sac_fly", "sac_fly_double_play"):
            SF += 1
        elif ev in ("sac_bunt", "sacrifice_bunt_double_play", "batter_interference", "catcher_interf"):
            pass
        else:
            AB += 1
            if ev == "single":
                H += 1; TB += 1
            elif ev == "double":
                H += 1; TB += 2
            elif ev == "triple":
                H += 1; TB += 3
            elif ev == "home_run":
                H += 1; TB += 4
            if ev in ("strikeout", "strikeout_double_play"):
                SO += 1
    if PA < RECENT_MIN_PA or AB <= 0:
        return None
    avg = H / AB
    obp_den = AB + BB + HBP + SF
    obp = (H + BB + HBP) / obp_den if obp_den > 0 else 0.0
    slg = TB / AB
    ops = obp + slg
    return {
        "wrcPlus": round((ops / 0.720) * 100) if ops > 0 else None,
        "iso": "%.3f" % (slg - avg),
        "kPct": "%.1f" % (SO / PA * 100.0),
        "bbPct": "%.1f" % (BB / PA * 100.0),
        "pa": PA,
    }


def last_n_slash(csv_text, phand, n):
    rdr = csv.DictReader(io.StringIO(csv_text))
    pas = []
    for row in rdr:
        if (row.get("p_throws") or "") != phand:
            continue
        ev = (row.get("events") or "").strip()
        if not ev or ev in _NON_PA:
            continue
        gd = row.get("game_date", "")
        try:
            abn = int(row.get("at_bat_number") or 0)
        except (TypeError, ValueError):
            abn = 0
        pas.append((gd, abn, ev))
    if not pas:
        return None
    pas.sort(key=lambda x: (x[0], x[1]))
    recent = [p[2] for p in (pas[-n:] if len(pas) > n else pas)]
    return _slash_from_events(recent)

UA = {"User-Agent": "Mozilla/5.0 (cush-ewma-build)"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(2.0 * (k + 1))
    print("  ! fetch failed:", last, file=sys.stderr)
    return None


def active_hitter_ids():
    ids = {}
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    for t in teams:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, t["id"]))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            pos = (p.get("position") or {}).get("abbreviation", "")
            if pos in ("P",):
                continue
            ids[p["person"]["id"]] = p["person"].get("fullName", "")
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
    games = {}
    rdr = csv.DictReader(io.StringIO(csv_text))
    for row in rdr:
        denom = _f(row.get("woba_denom"))
        if not denom:
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
    series.sort(key=lambda r: r[0])
    season = (s_num / s_den, s_den) if s_den > 0 else (None, 0.0)
    return series, season


def ewma_from_games(series):
    S = W = 0.0
    decay = 1.0
    for _, x, pa in reversed(series):
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
    print("recent window: last %d PA vs each hand" % RECENT_PA, file=sys.stderr)

    out = {}
    rec_out = {}
    _hdr_logged = False
    rec_samples = []
    for n, (pid, name) in enumerate(sorted(ids.items()), 1):
        csv_text = _get(savant_csv_url(pid))
        time.sleep(REQUEST_PAUSE)
        if not csv_text or "game_pk" not in csv_text.split("\n", 1)[0]:
            continue

        if not _hdr_logged:
            hdr = csv_text.split("\n", 1)[0]
            print("CSV header check -> p_throws:%s events:%s at_bat_number:%s"
                  % ("p_throws" in hdr, "events" in hdr, "at_bat_number" in hdr), file=sys.stderr)
            _hdr_logged = True
        rL = last_n_slash(csv_text, "L", RECENT_PA)
        rR = last_n_slash(csv_text, "R", RECENT_PA)
        if rL or rR:
            rec = {}
            if rL:
                rec["L"] = rL
            if rR:
                rec["R"] = rR
            rec_out[str(pid)] = rec
            if len(rec_samples) < 6:
                rec_samples.append((str(pid), name, rR, rL))

        series, (season_x, _) = game_xwoba_series(csv_text)
        if not series or season_x is None:
            continue
        ewma, effpa = ewma_from_games(series)
        if ewma is None or effpa <= 0:
            continue
        out[str(pid)] = {"ewma": round(ewma, 4), "season": round(season_x, 4), "effPa": round(effpa, 1)}
        if n % 25 == 0:
            print("  %d/%d  (%d written)" % (n, len(ids), len(out)), file=sys.stderr)

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d hitters (half-life %d PA)" % (OUT_PATH, len(out), HALF_LIFE_PA), file=sys.stderr)

    with open(OUT_RECENT, "w") as f:
        json.dump(rec_out, f, separators=(",", ":"))
    print("wrote %s : %d hitters (last %d PA per hand)" % (OUT_RECENT, len(rec_out), RECENT_PA), file=sys.stderr)

    def _fmt(x):
        return ("PA=%s wRC+=%s ISO=%s K%%=%s BB%%=%s"
                % (x.get("pa"), x.get("wrcPlus"), x.get("iso"), x.get("kPct"), x.get("bbPct"))) if x else "n/a"
    for pid, name, rR, rL in rec_samples:
        print("  sample %s %s: vsR[%s]  vsL[%s]" % (pid, name, _fmt(rR), _fmt(rL)), file=sys.stderr)


if __name__ == "__main__":
    main()
