#!/usr/bin/env python3
"""
build_pitcher_hand_splits.py  —  cush-data pipeline step

Builds pitcher_hand_splits.json: per-pitcher Statcast EXPECTED stats ALLOWED,
split by the OPPOSING BATTER's hand (the `stand` column):

    entry["L"] = {xwoba,xba,xslg, kpct,bbpct, xwoba_l30,xba_l30,xslg_l30, kpct_l30,bbpct_l30, pa}  # allowed vs LHB
    entry["R"] = { ... }                                            # allowed vs RHB

This is the pitcher-side mirror of batter_hand_splits.json's .L / .R buckets.
The CUSH front-end reads pitcherHandSplit[pid].L / .R to build the PITCHER index
for the HRR (xBA + xSLG) and Total Bases (xSLG) models — matching the hitter's
vs-hand xBA/xSLG against the pitcher's allowed vs-hand xBA/xSLG.

Aggregation mirrors enrich_hand_splits_xstats.py / savant.js line-for-line
(estimated_ba / _slg / _woba_using_speedangle summed over AB / wOBA-denom),
validated to reproduce Baseball Savant's official leaderboard.

Efficiency: ONE Savant search per pitcher per window returns every PA-ending
event; rows are split into L / R by the CSV `stand` column — so it's 2 fetches
per pitcher (season + rolling L30), not 4.

Stdlib only (urllib) — no pip installs. Idempotent + re-runnable. Runs in the
daily Build-Cush-feeds Action after the hitter hand-split steps.

Env (all optional):
    YEAR           Statcast season (default: current UTC year)
    WORKERS        parallel requests (default 6)
    L30_DAYS       rolling recency window in days (default 30)
    SEASON_MIN_AB  min at-bats to write a season xBA/xSLG bucket (default 20)
    L30_MIN_AB     min at-bats to write an L30 xBA/xSLG bucket (default 15)
    L30_MIN_WDEN   min wOBA denominator to write an L30 xwOBA bucket (default 18)
"""

import json, os, sys, csv, io, time, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor

YEAR    = os.environ.get("YEAR") or str(time.gmtime().tm_year)
SEASON  = int(YEAR)
WORKERS = int(os.environ.get("WORKERS", "6"))
OUT_FILE = "pitcher_hand_splits.json"

L30_DAYS      = int(os.environ.get("L30_DAYS", "30"))
SEASON_MIN_AB = int(os.environ.get("SEASON_MIN_AB", "20"))
L30_MIN_AB    = int(os.environ.get("L30_MIN_AB", "15"))
L30_MIN_WDEN  = float(os.environ.get("L30_MIN_WDEN", "18"))

STATSAPI = "https://statsapi.mlb.com/api/v1"
SAVANT   = "https://baseballsavant.mlb.com"
AB_EXCLUDE = {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_fly_double_play",
              "sac_bunt", "sac_bunt_double_play", "catcher_interf"}

_TODAY   = datetime.date.today()
L30_FROM = (_TODAY - datetime.timedelta(days=L30_DAYS)).isoformat()
L30_TO   = _TODAY.isoformat()


def get_json(url, tries=3):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                                     # noqa: BLE001
            if a == tries - 1:
                print("  ! get %s: %s" % (url, e), file=sys.stderr)
                return {}
            time.sleep(0.8 * (a + 1))
    return {}


def statcast_url(pid, dfrom=None, dto=None):
    # Every regular-season PA-ending event for this pitcher; we split L/R by `stand`.
    u = (SAVANT + "/statcast_search/csv?all=true&type=details&player_type=pitcher"
         "&pitchers_lookup%5B%5D=" + str(pid) +
         "&hfSea=" + YEAR + "%7C&hfGT=R%7C&min_pitches=0&min_results=0")
    if dfrom and dto:
        u += "&game_date_gt=" + dfrom + "&game_date_lt=" + dto
    return u


def fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8", "replace")


def aggregate_by_stand(text):
    """Return {'L': {...}, 'R': {...}} of allowed expected stats, split by batter stand."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return None
    idx = {k.strip(): i for i, k in enumerate(rows[0])}
    need = ["events", "type", "stand", "woba_denom", "woba_value",
            "estimated_woba_using_speedangle", "estimated_ba_using_speedangle",
            "estimated_slg_using_speedangle"]
    if any(k not in idx for k in need):
        return None

    def f(row, key):
        try:
            return float(row[idx[key]])
        except (ValueError, IndexError):
            return float("nan")

    acc = {"L": {"ab": 0, "xba": 0.0, "xslg": 0.0, "wden": 0.0, "xnum": 0.0, "pa": 0, "k": 0, "bb": 0},
           "R": {"ab": 0, "xba": 0.0, "xslg": 0.0, "wden": 0.0, "xnum": 0.0, "pa": 0, "k": 0, "bb": 0}}
    for row in rows[1:]:
        try:
            ev = row[idx["events"]].strip()
        except IndexError:
            continue
        if ev == "":                                   # not a PA-ending pitch
            continue
        st = (row[idx["stand"]].strip() if idx["stand"] < len(row) else "").upper()
        if st not in ("L", "R"):
            continue
        a = acc[st]
        a["pa"] += 1                                    # every row here is a PA-ending event
        if ev in ("strikeout", "strikeout_double_play"):
            a["k"] += 1
        elif ev in ("walk", "intent_walk"):
            a["bb"] += 1
        typ = row[idx["type"]].strip()
        wd, wv = f(row, "woba_denom"), f(row, "woba_value")
        xw = f(row, "estimated_woba_using_speedangle")
        xb = f(row, "estimated_ba_using_speedangle")
        xs = f(row, "estimated_slg_using_speedangle")
        if wd == wd and wd > 0:                        # (wd == wd) => not NaN
            a["xnum"] += xw if (typ == "X" and xw == xw) else (0.0 if wv != wv else wv)
            a["wden"] += wd
        if ev not in AB_EXCLUDE:                        # at-bat (K included, sacs excluded)
            a["ab"] += 1
            if typ == "X":                              # batted ball
                if xb == xb:
                    a["xba"] += xb
                if xs == xs:
                    a["xslg"] += xs

    out = {}
    for st in ("L", "R"):
        a = acc[st]
        o = {}
        if a["wden"] > 0:
            o["xwoba"] = round(a["xnum"] / a["wden"], 3)
        if a["ab"] > 0:
            o["xba"] = round(a["xba"] / a["ab"], 3)
            o["xslg"] = round(a["xslg"] / a["ab"], 3)
        if a["pa"] > 0:                                 # K%/BB% allowed vs this batter hand
            o["kpct"] = round(100.0 * a["k"] / a["pa"], 1)
            o["bbpct"] = round(100.0 * a["bb"] / a["pa"], 1)
        o["_ab"] = a["ab"]
        o["_pa"] = a["pa"]
        o["_wden"] = round(a["wden"], 1)
        out[st] = o
    return out


def fetch_agg(url, tag):
    for attempt in range(3):
        try:
            return aggregate_by_stand(fetch_csv(url))
        except Exception as e:                          # noqa: BLE001
            if attempt == 2:
                print("  ! %s: %s" % (tag, e), file=sys.stderr)
                return None
            time.sleep(0.8 * (attempt + 1))
    return None


def pitcher_ids():
    teams = get_json(STATSAPI + "/teams?sportId=1&season=%d" % SEASON).get("teams", [])
    ids = set()
    for t in teams:
        tid = t.get("id")
        if tid is None:
            continue
        roster = get_json(STATSAPI + "/teams/%d/roster?rosterType=active&season=%d"
                          % (tid, SEASON)).get("roster", [])
        for p in roster:
            pos = (p.get("position") or {}).get("abbreviation", "")
            if pos == "P":
                ids.add(p["person"]["id"])
        time.sleep(0.15)
    return sorted(ids)


def main():
    ids = pitcher_ids()
    print("pitchers: %d | season %s | L30 window %s..%s" % (len(ids), YEAR, L30_FROM, L30_TO))
    out = {}
    filled = [0]

    def work(pid):
        season = fetch_agg(statcast_url(pid), "%s season" % pid)
        l30 = fetch_agg(statcast_url(pid, L30_FROM, L30_TO), "%s L30" % pid)
        res = {}
        for st in ("L", "R"):
            b = {}
            sv = (season or {}).get(st) or {}
            if sv.get("_ab", 0) >= SEASON_MIN_AB:
                if "xba" in sv:
                    b["xba"] = sv["xba"]
                if "xslg" in sv:
                    b["xslg"] = sv["xslg"]
                if "xwoba" in sv:
                    b["xwoba"] = sv["xwoba"]
                if "kpct" in sv:
                    b["kpct"] = sv["kpct"]
                if "bbpct" in sv:
                    b["bbpct"] = sv["bbpct"]
                b["pa"] = int(round(sv.get("_wden", 0)))
            lv = (l30 or {}).get(st) or {}
            if lv.get("_ab", 0) >= L30_MIN_AB:
                if "xba" in lv:
                    b["xba_l30"] = lv["xba"]
                if "xslg" in lv:
                    b["xslg_l30"] = lv["xslg"]
                if "kpct" in lv:
                    b["kpct_l30"] = lv["kpct"]
                if "bbpct" in lv:
                    b["bbpct_l30"] = lv["bbpct"]
            if lv.get("_wden", 0) >= L30_MIN_WDEN and "xwoba" in lv:
                b["xwoba_l30"] = lv["xwoba"]
            if b:
                res[st] = b
        if res:
            out[str(pid)] = res
            if (res.get("L", {}).get("xba") is not None or
                    res.get("R", {}).get("xba") is not None):
                filled[0] += 1

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(work, ids):
            done += 1
            if done % 40 == 0:
                print("  ...%d/%d (%d pitchers with season xBA)" % (done, len(ids), filled[0]))

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("Done. Wrote %s with %d pitchers (%d with season xBA)." % (OUT_FILE, len(out), filled[0]))


if __name__ == "__main__":
    main()
