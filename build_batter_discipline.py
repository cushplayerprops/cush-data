#!/usr/bin/env python3
# build_batter_discipline.py
# Pulls MLB batter plate-discipline (chase / whiff / contact / swing / K%) from
# Baseball Savant and writes batter_discipline.json for the Cush Player Props
# Strikeout model (opponent side of the matchup). Keyed by MLBAM player_id.
#
# SEASON figures come from the custom leaderboard (one light CSV for everyone).
# The rolling LAST-30-DAYS split (suffix "_l30") is computed PER BATTER from the
# pitch-by-pitch statcast_search endpoint -- the same endpoint enrich_pitcher_form
# uses -- because that endpoint honors an explicit date range (game_date_gt/lt),
# whereas the custom leaderboard silently ignores start/end dates.
#     chase_l30, whiff_l30, izCon_l30, ozCon_l30
# The Strikeout model reads these ONLY for hitters facing a RHP (its _bdv reader
# uses *_l30 when present, else falls back to the season value), so if the L30
# pass yields nothing the model simply stays on season -- never broken.
import csv, io, json, sys, os, time, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor

YEAR = datetime.date.today().year

SAVANT = "https://baseballsavant.mlb.com"

# ---- rolling recent window (pitch-level, per batter) -----------------------
L30_DAYS      = int(os.environ.get("L30_DAYS", "30"))
L30_MIN_PITCH = int(os.environ.get("L30_MIN_PITCH", "50"))   # gate a batter's L30 bucket
WORKERS       = int(os.environ.get("WORKERS", "5"))
_TODAY   = datetime.date.today()
L30_FROM = (_TODAY - datetime.timedelta(days=L30_DAYS)).isoformat()
L30_TO   = _TODAY.isoformat()

# swing / whiff pitch descriptions -- identical to enrich_pitcher_form / savant.js
WHIFF_DESC = {"swinging_strike", "swinging_strike_blocked", "foul_tip"}
SWING_DESC = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip", "hit_into_play"}


def savant_url(year, min_pa=25):
    sels = "pa,k_percent,swing_percent,whiff_percent,oz_swing_percent,iz_contact_percent,oz_contact_percent"
    return ("https://baseballsavant.mlb.com/leaderboard/custom"
            "?year=%d&type=batter&filter=&min=%d"
            "&selections=%s&sort=pa&sortDir=desc&csv=true" % (year, min_pa, sels))

def fetch_csv(url, ua_win=True):
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36") if ua_win else "Mozilla/5.0"
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Accept": "text/csv,application/csv,*/*"})
    with urllib.request.urlopen(req, timeout=90) as r:
        # utf-8-sig strips the BOM Savant prepends on the leaderboard CSV.
        return r.read().decode("utf-8-sig", "replace")

def num(x):
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(str(x).replace("%", "").strip())
    except Exception:
        return None

def col(row, *names):
    low = {(k or "").strip().lower(): v for k, v in row.items()}
    for n in names:
        if n in low:
            return low[n]
    return None

def build(year):
    """Season plate-discipline for every qualified batter (custom leaderboard)."""
    url = savant_url(year)
    print("GET(season)", url)
    rows = list(csv.DictReader(io.StringIO(fetch_csv(url))))
    print("rows:", len(rows))
    out = {}
    for row in rows:
        pid = col(row, "player_id", "playerid", "mlbam_id", "id")
        if pid is None:
            continue
        pid = str(pid).strip()
        if not pid.isdigit():
            continue
        chase = num(col(row, "oz_swing_percent", "o_swing_percent", "chase_percent"))
        whiff = num(col(row, "whiff_percent"))
        swing = num(col(row, "swing_percent"))
        kpct  = num(col(row, "k_percent", "strikeout_percent"))
        izc   = num(col(row, "iz_contact_percent", "in_zone_contact_percent"))
        ozc   = num(col(row, "oz_contact_percent", "out_zone_contact_percent"))
        pa    = num(col(row, "pa", "b_total_pa", "plate_appearances"))
        contact = (round(100.0 - whiff, 1)) if whiff is not None else None
        if chase is None and whiff is None and kpct is None:
            continue
        out[pid] = {
            "chase":  round(chase, 1) if chase is not None else None,
            "whiff":  round(whiff, 1) if whiff is not None else None,
            "contact": contact,
            "swing":  round(swing, 1) if swing is not None else None,
            "kPct":   round(kpct, 1)  if kpct  is not None else None,
            "izCon":  round(izc, 1)   if izc   is not None else None,
            "ozCon":  round(ozc, 1)   if ozc   is not None else None,
            "pa":     int(pa)         if pa    is not None else None,
        }
    return out

# ---- pitch-level LAST-30-DAYS discipline, per batter -----------------------

def batter_l30_url(pid, dfrom, dto):
    # Mirrors enrich_pitcher_form's proven param set: group_by=name + the three
    # min_* pinned to 0 are what make statcast_search actually return rows, and
    # game_date_gt/lt gives a REAL date window (unlike the custom leaderboard).
    return (SAVANT + "/statcast_search/csv?all=true&type=details&player_type=batter"
            "&hfSea=" + str(YEAR) + "%7C&group_by=name&min_pitches=0&min_results=0&min_pas=0"
            "&batters_lookup%5B%5D=" + str(pid)
            + "&game_date_gt=" + dfrom + "&game_date_lt=" + dto)

def agg_l30(text):
    """Per-pitch CSV -> {chase,whiff,izCon,ozCon,pitches} for one batter, or None.

    zone 1-9 = in the strike zone, 11-14 = out of zone (Savant's `zone` field).
    chase = swings at out-of-zone pitches / out-of-zone pitches
    whiff = whiffs / swings
    izCon = contact on in-zone swings / in-zone swings
    ozCon = contact on out-of-zone swings / out-of-zone swings
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return None
    idx = {k.strip(): i for i, k in enumerate(rows[0])}
    if "description" not in idx or "zone" not in idx:
        return None

    def g(row, key):
        try:
            return row[idx[key]].strip()
        except (IndexError, KeyError):
            return ""

    inz_p = inz_sw = inz_con = 0
    ooz_p = ooz_sw = ooz_con = 0
    swings = whiffs = 0
    for row in rows[1:]:
        desc = g(row, "description")
        if not desc:
            continue
        try:
            zi = int(float(g(row, "zone")))
        except ValueError:
            continue
        inzone = 1 <= zi <= 9
        is_swing = desc in SWING_DESC
        is_whiff = desc in WHIFF_DESC
        is_contact = is_swing and not is_whiff
        if inzone:
            inz_p += 1
            if is_swing: inz_sw += 1
            if is_contact: inz_con += 1
        else:
            ooz_p += 1
            if is_swing: ooz_sw += 1
            if is_contact: ooz_con += 1
        if is_swing: swings += 1
        if is_whiff: whiffs += 1

    tot = inz_p + ooz_p
    if tot == 0:
        return None
    out = {"pitches": tot}
    if ooz_p > 0:  out["chase"] = round(100.0 * ooz_sw / ooz_p, 1)
    if swings > 0: out["whiff"] = round(100.0 * whiffs / swings, 1)
    if inz_sw > 0: out["izCon"] = round(100.0 * inz_con / inz_sw, 1)
    if ooz_sw > 0: out["ozCon"] = round(100.0 * ooz_con / ooz_sw, 1)
    return out

def fetch_l30(pid):
    url = batter_l30_url(pid, L30_FROM, L30_TO)
    for attempt in range(3):
        try:
            return agg_l30(fetch_csv(url, ua_win=False))
        except Exception as e:                       # noqa: BLE001
            if attempt == 2:
                print("  ! %s L30: %s" % (pid, e), file=sys.stderr)
                return None
            time.sleep(0.8 * (attempt + 1))
    return None

def enrich_l30(data):
    """Add chase_l30/whiff_l30/izCon_l30/ozCon_l30 in place, pitch-level per batter."""
    ids = list(data.keys())
    print("L30 pitch-level enrich: %d batters | %s..%s | workers=%d min_pitch=%d"
          % (len(ids), L30_FROM, L30_TO, WORKERS, L30_MIN_PITCH))
    written = [0]

    def work(pid):
        ov = fetch_l30(pid)
        if not ov or ov.get("pitches", 0) < L30_MIN_PITCH:
            return
        wrote = False
        for src, dst in (("chase", "chase_l30"), ("whiff", "whiff_l30"),
                         ("izCon", "izCon_l30"), ("ozCon", "ozCon_l30")):
            if ov.get(src) is not None:
                data[pid][dst] = ov[src]
                wrote = True
        if wrote:
            data[pid]["pitches_l30"] = ov["pitches"]
            written[0] += 1

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(work, ids):
            done += 1
            if done % 80 == 0:
                print("  ...%d/%d (%d filled)" % (done, len(ids), written[0]))
    print("L30 written for %d batters (>= %d pitches in window)" % (written[0], L30_MIN_PITCH))
    return written[0]

def main():
    year = YEAR
    try:
        data = build(year)
    except Exception as e:
        print("primary fetch failed:", e)
        data = {}
    if len(data) < 50 and year > 2015:
        print("sparse (%d) for %d; trying %d" % (len(data), year, year - 1))
        try:
            prev = build(year - 1)
            if len(prev) > len(data):
                data = prev
        except Exception as e:
            print("prev-year fetch failed:", e)

    if not data:
        print("ERROR: no batters parsed; leaving existing file untouched")
        sys.exit(1)

    # rolling last-30-days discipline split (season stays the backbone)
    try:
        enrich_l30(data)
    except Exception as e:
        print("L30 enrich errored (%s); season-only output" % e)

    with open("batter_discipline.json", "w") as fp:
        json.dump(data, fp, separators=(",", ":"))

    def avg(k):
        vs = [v[k] for v in data.values() if v.get(k) is not None]
        return round(sum(vs) / len(vs), 1) if vs else None
    n_l30 = sum(1 for v in data.values() if v.get("chase_l30") is not None)
    print("WROTE batter_discipline.json", {
        "batters": len(data),
        "with_l30": n_l30,
        "lg_chase": avg("chase"),
        "lg_whiff": avg("whiff"),
        "lg_kPct": avg("kPct"),
    })

if __name__ == "__main__":
    main()
