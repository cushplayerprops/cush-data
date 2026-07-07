#!/usr/bin/env python3
# build_batter_discipline.py
# Pulls MLB batter plate-discipline (chase / whiff / contact / swing / K%) from
# Baseball Savant's custom leaderboard CSV and writes batter_discipline.json for
# the Cush Player Props Strikeout model (opponent side of the matchup).
#
# Runs in a GitHub Action (Savant is reachable there). Keyed by MLBAM player_id,
# which matches the ids the app already uses for lineups / hand splits.
import csv, io, json, sys, datetime, urllib.request

YEAR = datetime.date.today().year

def savant_url(year, min_pa=25):
    sels = "pa,k_percent,swing_percent,whiff_percent,oz_swing_percent,iz_contact_percent,oz_contact_percent"
    return ("https://baseballsavant.mlb.com/leaderboard/custom"
            "?year=%d&type=batter&filter=&min=%d"
            "&selections=%s&sort=pa&sortDir=desc&csv=true" % (year, min_pa, sels))

def fetch_csv(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/csv,application/csv,*/*",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        # utf-8-sig strips the BOM Savant prepends, which otherwise mis-splits
        # the first ("last_name, first_name") column and shifts every field over.
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
    url = savant_url(year)
    print("GET", url)
    text = fetch_csv(url)
    rows = list(csv.DictReader(io.StringIO(text)))
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

    with open("batter_discipline.json", "w") as fp:
        json.dump(data, fp, separators=(",", ":"))

    def avg(k):
        vs = [v[k] for v in data.values() if v.get(k) is not None]
        return round(sum(vs) / len(vs), 1) if vs else None
    print("WROTE batter_discipline.json", {
        "batters": len(data),
        "lg_chase": avg("chase"),
        "lg_whiff": avg("whiff"),
        "lg_contact": avg("contact"),
        "lg_kPct": avg("kPct"),
    })

if __name__ == "__main__":
    main()
