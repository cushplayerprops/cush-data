#!/usr/bin/env python3
"""
enrich_hand_splits_xstats.py  —  cush-data pipeline step

Adds GENUINE hand-split Statcast expected stats to each batter in
batter_hand_splits.json, split by the OPPOSING PITCHER's hand:

    entry["L"]  += {"xwoba","xba","xslg"}                 # season vs LHP
    entry["R"]  += {"xwoba","xba","xslg"}                 # season vs RHP
    entry["L"]  += {"xwoba_l30","xba_l30","xslg_l30"}     # last-30-days vs LHP
    entry["R"]  += {"xwoba_l30","xba_l30","xslg_l30"}     # last-30-days vs RHP

These are exactly the .L / .R buckets the CUSH front-end reads as
handSplit[id].L / .R.  The app prefers the measured season values over the old
xwOBA-derived "tilt", and now prefers the measured *_l30 values for the recency
blend over actual-result L30 SLG.

The aggregation mirrors netlify/functions/savant.js line-for-line and is
validated to reproduce Baseball Savant's official leaderboard
(Aaron Judge 2025: xBA .299 vs .300, xSLG .707 vs .708, xwOBA .461 vs .460).

The recent window is a rolling last-N-days pull via Statcast's game_date filters,
and is HAND-SPECIFIC: vs RHP uses a 14-day window (PAs vs righties accrue fast
enough for a 2-week read), while vs LHP keeps the 30-day window (lefty starters
are rarer, so a longer pull is needed to clear the min sample). Both are written
into the same `xwoba_l30`/`xba_l30`/`xslg_l30` fields the app already reads.
Small hand-split samples (esp. vs LHP) are noisy, so a bucket is only written
when it clears a minimum AB / wOBA-denominator; otherwise the field is left
absent and the app falls back to season-expected / actual.

Runs in the daily Build-Cush-feeds Action right after build_hand_splits.py.
Stdlib only (urllib) — no pip installs needed. Idempotent + re-runnable.

Env (all optional):
    YEAR          Statcast season (default: current UTC year)
    WORKERS       parallel requests (default 6)
    FORCE         "1" to recompute buckets that already have the stats
    L30_DAYS      rolling recency window in days, vs LHP (default 30)
    L14_DAYS      rolling recency window in days, vs RHP (default 14)
    L30_MIN_AB    min at-bats to accept an L30 xBA/xSLG bucket (default 15)
    L30_MIN_WDEN  min wOBA denominator to accept an L30 xwOBA bucket (default 18)
"""

import json, os, sys, csv, io, time, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor

FILE    = sys.argv[1] if len(sys.argv) > 1 else "batter_hand_splits.json"
YEAR    = os.environ.get("YEAR") or str(time.gmtime().tm_year)
WORKERS = int(os.environ.get("WORKERS", "6"))
FORCE   = os.environ.get("FORCE") == "1"

L30_DAYS     = int(os.environ.get("L30_DAYS", "30"))
L14_DAYS     = int(os.environ.get("L14_DAYS", "14"))   # vs-RHP recent window (larger sample accrues faster)
L30_MIN_AB   = int(os.environ.get("L30_MIN_AB", "15"))
L30_MIN_WDEN = float(os.environ.get("L30_MIN_WDEN", "18"))

SAVANT = "https://baseballsavant.mlb.com"
AB_EXCLUDE = {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_fly_double_play",
              "sac_bunt", "sac_bunt_double_play", "catcher_interf"}

# rolling recency window (UTC; GitHub Actions runs UTC). Savant game_date filters
# are inclusive, so [today-L30_DAYS, today] captures the last ~L30_DAYS of games.
_TODAY = datetime.date.today()
L30_FROM = (_TODAY - datetime.timedelta(days=L30_DAYS)).isoformat()
L14_FROM = (_TODAY - datetime.timedelta(days=L14_DAYS)).isoformat()
L30_TO   = _TODAY.isoformat()

# Recency window is hand-specific: vs RHP uses the shorter L14 window (PAs vs
# righties accrue fast enough for a 2-week sample), vs LHP keeps the L30 window
# (lefty starters are rarer, so a 30-day pull is needed to clear the min sample).
def _recent_from(hand):
    return L14_FROM if hand == "R" else L30_FROM


def statcast_url(pid, hand, dfrom=None, dto=None):
    # identical shape to savant.js: regular season, split by opposing pitcher hand.
    # When dfrom/dto are given, restrict to that game-date window (rolling L30).
    u = (SAVANT + "/statcast_search/csv?all=true&type=details&player_type=batter"
         "&batters_lookup%5B%5D=" + str(pid) +
         "&hfSea=" + YEAR + "%7C&hfGT=R%7C&min_pitches=0&pitcher_throws=" + hand)
    if dfrom and dto:
        u += "&game_date_gt=" + dfrom + "&game_date_lt=" + dto
    return u


def fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def aggregate(text):
    """Return {'xwoba','xba','xslg','_ab','_wden'} for one player/hand, or None."""
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
    if not out:
        return None
    out["_ab"] = ab
    out["_wden"] = round(wden, 1)
    return out


def fetch_agg(url, tag):
    """fetch + aggregate with up to 3 retries; returns dict or None."""
    for attempt in range(3):
        try:
            return aggregate(fetch_csv(url))
        except Exception as e:                          # noqa: BLE001
            if attempt == 2:
                print("  ! %s: %s" % (tag, e), file=sys.stderr)
                return None
            time.sleep(0.8 * (attempt + 1))
    return None


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
            need_season = FORCE or bucket.get("xba") is None or bucket.get("xslg") is None
            need_l30 = FORCE or bucket.get("xba_l30") is None or bucket.get("xslg_l30") is None
            if need_season or need_l30:
                jobs.append((pid, hand))

    print("batters: %d | buckets to enrich: %d | season %s | recent: R %s..%s (L%d) / L %s..%s (L%d)"
          % (len(data), len(jobs), YEAR, L14_FROM, L30_TO, L14_DAYS, L30_FROM, L30_TO, L30_DAYS))

    filled = [0]
    filled30 = [0]

    def work(job):
        pid, hand = job
        b = data[pid][hand]
        # --- season (unchanged behavior) ---
        if FORCE or b.get("xba") is None or b.get("xslg") is None:
            ov = fetch_agg(statcast_url(pid, hand), "%s %s season" % (pid, hand))
            if ov:
                if "xba" in ov:
                    b["xba"] = ov["xba"]; filled[0] += 1
                if "xslg" in ov:
                    b["xslg"] = ov["xslg"]
                if "xwoba" in ov:
                    b["xwoba"] = ov["xwoba"]
        # --- recent window (hand-specific: L14 vs RHP, L30 vs LHP) ---
        if FORCE or b.get("xba_l30") is None or b.get("xslg_l30") is None:
            _rfrom = _recent_from(hand)
            ov = fetch_agg(statcast_url(pid, hand, _rfrom, L30_TO),
                           "%s %s L%d" % (pid, hand, L14_DAYS if hand == "R" else L30_DAYS))
            if ov:
                if ov.get("_ab", 0) >= L30_MIN_AB:
                    if "xba" in ov:
                        b["xba_l30"] = ov["xba"]; filled30[0] += 1
                    if "xslg" in ov:
                        b["xslg_l30"] = ov["xslg"]
                if ov.get("_wden", 0) >= L30_MIN_WDEN and "xwoba" in ov:
                    b["xwoba_l30"] = ov["xwoba"]

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(work, jobs):
            done += 1
            if done % 50 == 0:
                print("  ...%d/%d (%d season xBA, %d L30 xBA)"
                      % (done, len(jobs), filled[0], filled30[0]))

    with open(FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print("Done. Season xBA filled %d, L30 xBA filled %d. Wrote %s."
          % (filled[0], filled30[0], FILE))


if __name__ == "__main__":
    main()
