#!/usr/bin/env python3
"""
enrich_pitcher_form.py  —  cush-data pipeline step

Builds pitcher_form.json: leading-indicator "form/fatigue" signals from Statcast
for each pitcher, computed for the full SEASON and a rolling LAST-30-DAYS window.

Per pitcher (keyed by MLBAM id):
    veloFb   / veloFb_l30    fastball (FF/SI/FT) average release speed, mph
    whiff    / whiff_l30      whiff% = swinging strikes / swings
    csw      / csw_l30        CSW% = (called strikes + swinging strikes) / pitches
    pitches  / pitches_l30    sample sizes

These are the stats that LEAD outcomes (a fading fastball or slipping whiff rate
shows up weeks before ERA does), so the app blends recent-vs-season on THESE for
pitcher form instead of noisy outcome stats. L30 buckets are sample-gated.

Mirrors netlify/functions/savant.js swing/whiff definitions. Stdlib only (urllib).
Idempotent + re-runnable. Reads the pitcher id list from an existing feed file.

Env (all optional):
    YEAR            Statcast season (default: current UTC year)
    WORKERS         parallel requests (default 5)
    IDS_FILE        feed file to read pitcher ids from (default pitcher_ewma.json)
    OUT_FILE        output (default pitcher_form.json)
    L30_DAYS        rolling window in days (default 30)
    L30_MIN_PITCH   min pitches to accept an L30 bucket (default 120)
    FORCE           "1" to recompute pitchers that already have data
"""

import json, os, sys, csv, io, time, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor

YEAR          = os.environ.get("YEAR") or str(time.gmtime().tm_year)
WORKERS       = int(os.environ.get("WORKERS", "5"))
IDS_FILE      = os.environ.get("IDS_FILE", "pitcher_ewma.json")
OUT_FILE      = os.environ.get("OUT_FILE", "pitcher_form.json")
L30_DAYS      = int(os.environ.get("L30_DAYS", "30"))
L30_MIN_PITCH = int(os.environ.get("L30_MIN_PITCH", "120"))
FORCE         = os.environ.get("FORCE") == "1"

SAVANT = "https://baseballsavant.mlb.com"
FASTBALLS = {"FF", "SI", "FT"}
# swing / whiff descriptions — identical to savant.js
WHIFF_DESC = {"swinging_strike", "swinging_strike_blocked", "foul_tip"}
SWING_DESC = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip", "hit_into_play"}
CALLED = "called_strike"

_TODAY = datetime.date.today()
L30_FROM = (_TODAY - datetime.timedelta(days=L30_DAYS)).isoformat()
L30_TO   = _TODAY.isoformat()


def statcast_url(pid, dfrom=None, dto=None):
    u = (SAVANT + "/statcast_search/csv?all=true&type=details&player_type=pitcher"
         "&pitchers_lookup%5B%5D=" + str(pid) +
         "&hfSea=" + YEAR + "%7C&hfGT=R%7C&min_pitches=0")
    if dfrom and dto:
        u += "&game_date_gt=" + dfrom + "&game_date_lt=" + dto
    return u


def fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def aggregate(text):
    """Return {'veloFb','whiff','csw','pitches'} or None."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return None
    idx = {k.strip(): i for i, k in enumerate(rows[0])}
    need = ["pitch_type", "description", "release_speed"]
    if any(k not in idx for k in need):
        return None

    def g(row, key):
        try:
            return row[idx[key]].strip()
        except (IndexError, KeyError):
            return ""

    pitches = 0
    fb_sum = 0.0
    fb_n = 0
    swings = 0
    whiffs = 0
    called = 0
    wstr = 0
    for row in rows[1:]:
        desc = g(row, "description")
        if not desc:
            continue
        pitches += 1
        pt = g(row, "pitch_type")
        try:
            rs = float(g(row, "release_speed"))
        except ValueError:
            rs = float("nan")
        if pt in FASTBALLS and rs == rs:            # rs==rs => not NaN
            fb_sum += rs
            fb_n += 1
        if desc in SWING_DESC:
            swings += 1
        if desc in WHIFF_DESC:
            whiffs += 1
            wstr += 1
        if desc == CALLED:
            called += 1

    if pitches == 0:
        return None
    out = {"pitches": pitches}
    if fb_n >= 20:
        out["veloFb"] = round(fb_sum / fb_n, 1)
    if swings > 0:
        out["whiff"] = round(100.0 * whiffs / swings, 1)
    out["csw"] = round(100.0 * (called + wstr) / pitches, 1)
    return out


def fetch_agg(url, tag):
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
    if not os.path.exists(IDS_FILE):
        print("ERROR: %s not found" % IDS_FILE, file=sys.stderr)
        sys.exit(1)
    with open(IDS_FILE, "r", encoding="utf-8") as fh:
        ids = list(json.load(fh).keys())

    data = {}
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}

    jobs = []
    for pid in ids:
        cur = data.get(pid) or {}
        need_season = FORCE or cur.get("csw") is None
        need_l30 = FORCE or cur.get("csw_l30") is None
        if need_season or need_l30:
            jobs.append(pid)

    print("pitchers: %d | to enrich: %d | season %s | L30 %s..%s"
          % (len(ids), len(jobs), YEAR, L30_FROM, L30_TO))

    filled = [0]
    filled30 = [0]

    def work(pid):
        cur = data.get(pid) or {}
        # season
        if FORCE or cur.get("csw") is None:
            ov = fetch_agg(statcast_url(pid), "%s season" % pid)
            if ov:
                for k in ("veloFb", "whiff", "csw", "pitches"):
                    if k in ov:
                        cur[k] = ov[k]
                filled[0] += 1
        # last-30-days
        if FORCE or cur.get("csw_l30") is None:
            ov = fetch_agg(statcast_url(pid, L30_FROM, L30_TO), "%s L30" % pid)
            if ov and ov.get("pitches", 0) >= L30_MIN_PITCH:
                if "veloFb" in ov:
                    cur["veloFb_l30"] = ov["veloFb"]
                if "whiff" in ov:
                    cur["whiff_l30"] = ov["whiff"]
                if "csw" in ov:
                    cur["csw_l30"] = ov["csw"]
                cur["pitches_l30"] = ov["pitches"]
                filled30[0] += 1
        if cur:
            data[pid] = cur

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(work, jobs):
            done += 1
            if done % 40 == 0:
                print("  ...%d/%d (%d season, %d L30)"
                      % (done, len(jobs), filled[0], filled30[0]))

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print("Done. Season filled %d, L30 filled %d. Wrote %s (%d pitchers)."
          % (filled[0], filled30[0], OUT_FILE, len(data)))


if __name__ == "__main__":
    main()
