#!/usr/bin/env python3
# build_batter_discipline.py
# Pulls MLB batter plate-discipline (chase / whiff / contact / swing / K%) from
# Baseball Savant's custom leaderboard CSV and writes batter_discipline.json for
# the Cush Player Props Strikeout model (opponent side of the matchup).
#
# Runs in a GitHub Action (Savant is reachable there). Keyed by MLBAM player_id,
# which matches the ids the app already uses for lineups / hand splits.
#
# In addition to the SEASON figures, this now emits a rolling LAST-30-DAYS
# discipline split with the suffix "_l30":
#     chase_l30, whiff_l30, izCon_l30, ozCon_l30
# The Strikeout model reads these ONLY for hitters facing a RHP (its _bdv reader
# uses *_l30 when present, else falls back to the season value), so a wrong or
# ignored date filter can never break the model -- it simply stays on season.
# To keep that guarantee, the L30 pass is SELF-VALIDATING: it is only written if
# the date-ranged fetch is provably a smaller window than the season pull.
import csv, io, json, sys, datetime, urllib.request

YEAR = datetime.date.today().year

# rolling recent window (days) used for the *_l30 discipline split
L30_DAYS      = 30
L30_MIN_PA    = 20     # per-batter min PA in the window to accept an _l30 value
# If the "date-ranged" fetch comes back with per-batter PA this close to the
# season pull, Savant ignored the date filter -> we DROP the whole L30 pass
# rather than write season numbers mislabeled as recent form.
L30_MAX_RATIO = 0.60

_TODAY   = datetime.date.today()
L30_FROM = (_TODAY - datetime.timedelta(days=L30_DAYS)).isoformat()
L30_TO   = _TODAY.isoformat()


def savant_url(year, min_pa=25, dfrom=None, dto=None):
    sels = "pa,k_percent,swing_percent,whiff_percent,oz_swing_percent,iz_contact_percent,oz_contact_percent"
    u = ("https://baseballsavant.mlb.com/leaderboard/custom"
         "?year=%d&type=batter&filter=&min=%d"
         "&selections=%s&sort=pa&sortDir=desc&csv=true" % (year, min_pa, sels))
    # Rolling-window split. Savant's custom leaderboard honors an explicit
    # start/end date range; if a given deployment's Savant ignores it, the
    # self-validation below (L30_MAX_RATIO) catches the no-op and we skip L30.
    if dfrom and dto:
        u += "&startdt=%s&enddt=%s" % (dfrom, dto)
    return u

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

def parse_rows(text):
    """CSV text -> {pid: {chase,whiff,izCon,ozCon,pa}} (raw, unrounded)."""
    rows = list(csv.DictReader(io.StringIO(text)))
    out = {}
    for row in rows:
        pid = col(row, "player_id", "playerid", "mlbam_id", "id")
        if pid is None:
            continue
        pid = str(pid).strip()
        if not pid.isdigit():
            continue
        out[pid] = {
            "chase": num(col(row, "oz_swing_percent", "o_swing_percent", "chase_percent")),
            "whiff": num(col(row, "whiff_percent")),
            "swing": num(col(row, "swing_percent")),
            "kPct":  num(col(row, "k_percent", "strikeout_percent")),
            "izCon": num(col(row, "iz_contact_percent", "in_zone_contact_percent")),
            "ozCon": num(col(row, "oz_contact_percent", "out_zone_contact_percent")),
            "pa":    num(col(row, "pa", "b_total_pa", "plate_appearances")),
        }
    return out, len(rows)

def build(year):
    url = savant_url(year)
    print("GET(season)", url)
    raw, nrows = parse_rows(fetch_csv(url))
    print("rows:", nrows)
    out = {}
    for pid, v in raw.items():
        chase, whiff, kpct = v["chase"], v["whiff"], v["kPct"]
        pa = v["pa"]
        contact = (round(100.0 - whiff, 1)) if whiff is not None else None
        if chase is None and whiff is None and kpct is None:
            continue
        out[pid] = {
            "chase":  round(chase, 1) if chase is not None else None,
            "whiff":  round(whiff, 1) if whiff is not None else None,
            "contact": contact,
            "swing":  round(v["swing"], 1) if v["swing"] is not None else None,
            "kPct":   round(kpct, 1)  if kpct  is not None else None,
            "izCon":  round(v["izCon"], 1) if v["izCon"] is not None else None,
            "ozCon":  round(v["ozCon"], 1) if v["ozCon"] is not None else None,
            "pa":     int(pa)         if pa    is not None else None,
        }
    return out

def enrich_l30(data, year):
    """Add chase_l30/whiff_l30/izCon_l30/ozCon_l30 to `data` in place.

    Self-validating: only writes _l30 fields if the date-ranged pull is
    demonstrably a *smaller* window than the season pull (proving Savant honored
    the start/end dates). On any doubt it writes nothing, so the model keeps
    using season values for RHP matchups -- never season numbers mislabeled as
    recent form.
    """
    url = savant_url(year, min_pa=10, dfrom=L30_FROM, dto=L30_TO)
    print("GET(L30)", url)
    try:
        raw, nrows = parse_rows(fetch_csv(url))
    except Exception as e:
        print("L30 fetch failed (%s); leaving season-only" % e)
        return 0
    print("L30 rows:", nrows)
    if not raw:
        print("L30 empty; leaving season-only")
        return 0

    # Validate the window actually shrank vs season. Compare per-batter PA for
    # batters present in both pulls; the median recent/season ratio should be
    # well under L30_MAX_RATIO for a true ~30-day window mid/late season.
    ratios = []
    for pid, v in raw.items():
        p30 = v["pa"]
        pse = data.get(pid, {}).get("pa")
        if p30 and pse and pse > 0:
            ratios.append(p30 / float(pse))
    if not ratios:
        print("L30 has no PA overlap with season; leaving season-only")
        return 0
    ratios.sort()
    med = ratios[len(ratios) // 2]
    print("L30 median PA ratio vs season: %.2f (need < %.2f)" % (med, L30_MAX_RATIO))
    if med >= L30_MAX_RATIO:
        print("L30 window ~= season -> Savant ignored the date filter; SKIPPING L30")
        return 0

    written = 0
    for pid, v in raw.items():
        if pid not in data:
            continue
        p30 = v["pa"]
        if p30 is None or p30 < L30_MIN_PA:
            continue
        wrote_any = False
        for src, dst in (("chase", "chase_l30"), ("whiff", "whiff_l30"),
                         ("izCon", "izCon_l30"), ("ozCon", "ozCon_l30")):
            val = v[src]
            if val is not None:
                data[pid][dst] = round(val, 1)
                wrote_any = True
        if wrote_any:
            data[pid]["pa_l30"] = int(p30)
            written += 1
    print("L30 written for %d batters (>= %d PA in window)" % (written, L30_MIN_PA))
    return written

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
                year = year - 1
        except Exception as e:
            print("prev-year fetch failed:", e)

    if not data:
        print("ERROR: no batters parsed; leaving existing file untouched")
        sys.exit(1)

    # rolling last-30-days discipline split (season stays the backbone)
    try:
        enrich_l30(data, year)
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
        "lg_contact": avg("contact"),
        "lg_kPct": avg("kPct"),
    })

if __name__ == "__main__":
    main()
