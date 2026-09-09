#!/usr/bin/env python3
"""
enrich_pitcher_form.py  —  cush-data pipeline step (LIGHT, self-refreshing)

Builds pitcher_form.json: leading-indicator "form/fatigue" signals from Statcast.
For each pitcher we make ONE bounded pitch-level pull (the last BASE_DAYS days)
and split it, in memory, into two non-overlapping windows:

    baseline  = pitches 31..BASE_DAYS days ago   -> veloFb / whiff / csw
    recent    = pitches in the last L30_DAYS days -> veloFb_l30 / whiff_l30 / csw_l30

The app's strikeout model reads the *dip* (veloFb_l30 - veloFb, whiff_l30 - whiff),
so a fading fastball or slipping whiff over the last month shows up as recent-vs-prior.

Why this shape: the previous version pulled every pitch of the FULL SEASON per
pitcher, which Savant times out on -- ~400 pitchers x a season-sized CSV made the
job run for an hour and still write {}. A bounded window is small enough to fetch
reliably (this is the same per-entity, date-ranged mechanism the batter L30 feed
uses successfully) and every run REFRESHES (no skip-if-present), so the recent
signal actually stays current day to day.

Stdlib only. Reads the pitcher id list from an existing feed file (pitcher_ewma.json).

Env (all optional):
    YEAR            Statcast season (default: current UTC year)
    WORKERS         parallel requests (default 8)
    IDS_FILE        feed to read pitcher ids from (default pitcher_ewma.json)
    OUT_FILE        output (default pitcher_form.json)
    BASE_DAYS       total lookback window in days (default 90)
    L30_DAYS        recent window in days (default 30)
    MIN_BASE_PITCH  min pitches to accept a baseline bucket (default 100)
    MIN_L30_PITCH   min pitches to accept a recent bucket (default 60)
"""

import json, os, sys, csv, io, time, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor

YEAR           = os.environ.get("YEAR") or str(time.gmtime().tm_year)
WORKERS        = int(os.environ.get("WORKERS", "8"))
IDS_FILE       = os.environ.get("IDS_FILE", "pitcher_ewma.json")
OUT_FILE       = os.environ.get("OUT_FILE", "pitcher_form.json")
BASE_DAYS      = int(os.environ.get("BASE_DAYS", "90"))
L30_DAYS       = int(os.environ.get("L30_DAYS", "30"))
MIN_BASE_PITCH = int(os.environ.get("MIN_BASE_PITCH", "100"))
MIN_L30_PITCH  = int(os.environ.get("MIN_L30_PITCH", "60"))

SAVANT = "https://baseballsavant.mlb.com"
FASTBALLS = {"FF", "SI", "FT"}
# swing / whiff descriptions -- identical to savant.js / the batter feed
WHIFF_DESC = {"swinging_strike", "swinging_strike_blocked", "foul_tip"}
SWING_DESC = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip", "hit_into_play"}
CALLED = "called_strike"

_TODAY    = datetime.date.today()
BASE_FROM = (_TODAY - datetime.timedelta(days=BASE_DAYS)).isoformat()
L30_CUT   = (_TODAY - datetime.timedelta(days=L30_DAYS)).isoformat()
TO        = _TODAY.isoformat()


def statcast_url(pid):
    # group_by=name + the three min_* pinned to 0 are what make statcast_search
    # actually return rows; game_date_gt/lt bounds it to the last BASE_DAYS days.
    return (SAVANT + "/statcast_search/csv?all=true&type=details&player_type=pitcher"
            "&hfSea=" + YEAR + "%7C&group_by=name&min_pitches=0&min_results=0&min_pas=0"
            "&pitchers_lookup%5B%5D=" + str(pid)
            + "&game_date_gt=" + BASE_FROM + "&game_date_lt=" + TO)


def fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def agg(rows, idx):
    """Aggregate a list of per-pitch rows -> {veloFb,whiff,csw,pitches} or None."""
    def g(row, key):
        try:
            return row[idx[key]].strip()
        except (IndexError, KeyError):
            return ""
    pitches = fb_n = swings = whiffs = called = 0
    fb_sum = 0.0
    for row in rows:
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
        if desc == CALLED:
            called += 1
    if pitches == 0:
        return None
    out = {"pitches": pitches}
    if fb_n >= 20:
        out["veloFb"] = round(fb_sum / fb_n, 1)
    if swings > 0:
        out["whiff"] = round(100.0 * whiffs / swings, 1)
    out["csw"] = round(100.0 * (called + whiffs) / pitches, 1)
    return out


def process(pid):
    """One bounded fetch -> baseline (prior) + recent (last L30) buckets."""
    for attempt in range(3):
        try:
            rows = list(csv.reader(io.StringIO(fetch_csv(statcast_url(pid)))))
            if not rows:
                return None
            idx = {k.strip(): i for i, k in enumerate(rows[0])}
            if "description" not in idx or "release_speed" not in idx or "game_date" not in idx:
                return None
            gd = idx["game_date"]
            body = rows[1:]
            recent_rows = [r for r in body if len(r) > gd and r[gd].strip() >= L30_CUT]
            base_rows   = [r for r in body if len(r) > gd and r[gd].strip() <  L30_CUT]
            base = agg(base_rows, idx)
            rec  = agg(recent_rows, idx)
            out = {}
            if base and base.get("pitches", 0) >= MIN_BASE_PITCH:
                for k in ("veloFb", "whiff", "csw"):
                    if k in base:
                        out[k] = base[k]
                out["pitches"] = base["pitches"]
            if rec and rec.get("pitches", 0) >= MIN_L30_PITCH:
                if "veloFb" in rec: out["veloFb_l30"] = rec["veloFb"]
                if "whiff" in rec:  out["whiff_l30"]  = rec["whiff"]
                if "csw" in rec:    out["csw_l30"]    = rec["csw"]
                out["pitches_l30"] = rec["pitches"]
            return out or None
        except Exception as e:                          # noqa: BLE001
            if attempt == 2:
                print("  ! %s: %s" % (pid, e), file=sys.stderr)
                return None
            time.sleep(1.0 * (attempt + 1))
    return None


def main():
    if not os.path.exists(IDS_FILE):
        print("ERROR: %s not found" % IDS_FILE, file=sys.stderr)
        sys.exit(1)
    with open(IDS_FILE, "r", encoding="utf-8") as fh:
        ids = list(json.load(fh).keys())

    print("pitchers: %d | baseline %s..%s | recent last %dd | workers=%d"
          % (len(ids), BASE_FROM, L30_CUT, L30_DAYS, WORKERS))

    data = {}
    filled = [0]
    filled_l30 = [0]

    def work(pid):
        ov = process(pid)
        if ov:
            data[pid] = ov
            if ov.get("veloFb") is not None or ov.get("whiff") is not None:
                filled[0] += 1
            if ov.get("veloFb_l30") is not None or ov.get("whiff_l30") is not None:
                filled_l30[0] += 1

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(work, ids):
            done += 1
            if done % 60 == 0:
                print("  ...%d/%d (%d base, %d recent)"
                      % (done, len(ids), filled[0], filled_l30[0]))

    if not data:
        print("ERROR: no pitchers parsed; leaving existing file untouched")
        sys.exit(1)

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print("WROTE %s: %d pitchers | %d with baseline velo/whiff | %d with recent(L30)"
          % (OUT_FILE, len(data), filled[0], filled_l30[0]))


if __name__ == "__main__":
    main()
