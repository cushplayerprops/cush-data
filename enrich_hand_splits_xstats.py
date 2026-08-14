#!/usr/bin/env python3
"""
enrich_hand_splits_xstats.py  —  cush-data pipeline step

Adds GENUINE hand-split Statcast expected stats (xwOBA, xBA, xSLG) to each
batter in batter_hand_splits.json, split by the OPPOSING PITCHER's hand:

    entry["L"]  += {"xwoba","xba","xslg"}   # the hitter vs LHP  (pitcher_throws=L)
    entry["R"]  += {"xwoba","xba","xslg"}   # the hitter vs RHP  (pitcher_throws=R)

These are exactly the .L / .R buckets the CUSH front-end reads as
handSplit[id].L / .R.  The app now prefers these measured values over the old
xwOBA-derived "tilt" approximation.

The aggregation mirrors netlify/functions/savant.js line-for-line and is
validated to reproduce Baseball Savant's official leaderboard
(Aaron Judge 2025: xBA .299 vs .300, xSLG .707 vs .708, xwOBA .461 vs .460).

Runs in the daily Build-Cush-feeds Action right after build_hand_splits.py.
Stdlib only (urllib) — no pip installs needed. Idempotent + re-runnable.

Env (all optional):
    YEAR         Statcast season (default: current UTC year)
    WORKERS      parallel requests (default 6)
    FORCE        "1" to recompute buckets that already have xba/xslg
"""

import json, os, sys, csv, io, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

FILE    = sys.argv[1] if len(sys.argv) > 1 else "batter_hand_splits.json"
YEAR    = os.environ.get("YEAR") or str(time.gmtime().tm_year)
WORKERS = int(os.environ.get("WORKERS", "6"))
FORCE   = os.environ.get("FORCE") == "1"

SAVANT = "https://baseballsavant.mlb.com"
AB_EXCLUDE = {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_fly_double_play",
              "sac_bunt", "sac_bunt_double_play", "catcher_interf"}


def statcast_url(pid, hand):
    # identical shape to savant.js: regular season, split by opposing pitcher hand
    return (SAVANT + "/statcast_search/csv?all=true&type=details&player_type=batter"
            "&batters_lookup%5B%5D=" + str(pid) +
            "&hfSea=" + YEAR + "%7C&hfGT=R%7C&min_pitches=0&pitcher_throws=" + hand)


def fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def aggregate(text):
    """Return {'xwoba','xba','xslg'} for one player/hand, or None if no data."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return None
    idx = {k.strip(): i for i, k in enumerate(rows[0])}
    need = ["events", "type", "woba_denom", "woba_value",
            "estimated_woba_using_speedangle", "estimated_ba_using_speedangle",
            "estimated_slg_using_speedangle"]
    if any(k not in idx for k in need):
        return None

    def f(row, key):
        try:
            return float(row[idx[key]])
        except (ValueError, IndexError):
            return float("nan")

    ab = 0
    xba_sum = xslg_sum = wden = xnum = 0.0
    for row in rows[1:]:
        try:
            ev = row[idx["events"]].strip()
        except IndexError:
            continue
        if ev == "":                                   # not a PA-ending pitch
            continue
        typ = row[idx["type"]].strip()
        wd, wv = f(row, "woba_denom"), f(row, "woba_value")
        xw = f(row, "estimated_woba_using_speedangle")
        xb = f(row, "estimated_ba_using_speedangle")
        xs = f(row, "estimated_slg_using_speedangle")
        if wd == wd and wd > 0:                        # (wd == wd) => not NaN
            xnum += xw if (typ == "X" and xw == xw) else (0.0 if wv != wv else wv)
            wden += wd
        if ev not in AB_EXCLUDE:                        # at-bat (K included, sacs excluded)
            ab += 1
            if typ == "X":                              # batted ball
                if xb == xb:
                    xba_sum += xb
                if xs == xs:
                    xslg_sum += xs

    out = {}
    if wden > 0:
        out["xwoba"] = round(xnum / wden, 3)
    if ab > 0:
        out["xba"] = round(xba_sum / ab, 3)
        out["xslg"] = round(xslg_sum / ab, 3)
    return out or None


def main():
    if not os.path.exists(FILE):
        print("ERROR: %s not found" % FILE, file=sys.stderr)
        sys.exit(1)
    with open(FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    jobs = []
    for pid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        for hand in ("L", "R"):
            bucket = entry.get(hand)
            if not isinstance(bucket, dict):
                continue                                # only fill buckets that already exist
            if not FORCE and bucket.get("xba") is not None and bucket.get("xslg") is not None:
                continue
            jobs.append((pid, hand))

    print("batters: %d | buckets to enrich: %d | season %s"
          % (len(data), len(jobs), YEAR))

    filled = [0]

    def work(job):
        pid, hand = job
        for attempt in range(3):
            try:
                ov = aggregate(fetch_csv(statcast_url(pid, hand)))
                if ov:
                    b = data[pid][hand]
                    if "xba" in ov:
                        b["xba"] = ov["xba"]
                        filled[0] += 1
                    if "xslg" in ov:
                        b["xslg"] = ov["xslg"]
                    if "xwoba" in ov:
                        b["xwoba"] = ov["xwoba"]
                return
            except Exception as e:                      # noqa: BLE001
                if attempt == 2:
                    print("  ! %s %s: %s" % (pid, hand, e), file=sys.stderr)
                else:
                    time.sleep(0.8 * (attempt + 1))

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(work, jobs):
            done += 1
            if done % 50 == 0:
                print("  ...%d/%d (%d xBA filled)" % (done, len(jobs), filled[0]))

    with open(FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print("Done. Enriched %d buckets with real xwOBA/xBA/xSLG. Wrote %s." % (filled[0], FILE))


if __name__ == "__main__":
    main()
