#!/usr/bin/env python3
"""
build_batter_pitch_iso.py
=========================
Generates batter_pitch_iso.json for cushplayerprops.win.

This precomputes, ONCE on the server, the per-hitter pitch-family splits that the
HR / Hitter models currently crawl from Baseball Savant inside every user's browser
(~40 heavy CSV downloads, the "CALCULATING PITCH-MIX DATA" wait). With this file
committed to the cush-data repo, the site loads it in one request and the 2-minute
wait disappears.

Output path (commit this next to hr_matrix.json):
    batter_pitch_iso.json

Output shape (matches the model exactly):
    {
      "665742": {                      # MLBAM batter id
        "L": { "fb": {"iso":0.281,"pa":143,"woba":0.402,"xwoba":0.388},
               "br": {...} | null,
               "off": {...} | null },
        "R": { "fb": {...}, "br": {...}, "off": {...} }
      },
      ...
    }
  - "L"/"R" = pitcher handedness the hitter faced.
  - fb / br / off = fastball / breaking / offspeed families.
  - A family is null when the hitter has no PA-ending pitches of that family.

Requires: pip install pybaseball pandas
Run:      python build_batter_pitch_iso.py
Env (optional):
    SEASON      default = current year
    START_DT    default = "<SEASON>-03-01"
    END_DT      default = today (UTC)
    OUT         default = "batter_pitch_iso.json"
    ROUND       default = 4   (decimal places; smaller = smaller file)
"""

import os, json, datetime, sys

# ---- config ----------------------------------------------------------------
TODAY   = datetime.datetime.utcnow().date()
SEASON  = int(os.environ.get("SEASON", TODAY.year))
START   = os.environ.get("START_DT", f"{SEASON}-03-01")
END     = os.environ.get("END_DT", TODAY.isoformat())
OUT     = os.environ.get("OUT", "batter_pitch_iso.json")
ROUND   = int(os.environ.get("ROUND", "4"))

# ---- mappings (identical to the browser's pitchCat + NOAB) -----------------
FB  = {"FF", "FA", "SI", "FT", "FC"}
BR  = {"SL", "CU", "KC", "ST", "SV", "CS"}
OFF = {"CH", "FS", "FO", "SC"}

def pitch_cat(pt):
    pt = (str(pt) if pt is not None else "").upper()
    if pt in FB:  return "fb"
    if pt in BR:  return "br"
    if pt in OFF: return "off"
    return None

# events that are NOT at-bats (excluded from ISO denominator) -- matches NOAB
NOAB = {
    "walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt",
    "sac_fly_double_play", "sac_bunt_double_play", "catcher_interf",
}

def num(x):
    try:
        v = float(x)
        return None if v != v else v   # NaN check
    except (TypeError, ValueError):
        return None

# ---- pull season statcast --------------------------------------------------
def load_statcast():
    from pybaseball import statcast
    print(f"[bpi] pulling statcast {START} -> {END} (season {SEASON})", flush=True)
    df = statcast(start_dt=START, end_dt=END)
    if df is None or len(df) == 0:
        raise SystemExit("[bpi] no statcast rows returned")
    # regular season only (browser query uses hfGT=R), PA-ending rows only
    df = df[(df.get("game_type") == "R") & (df["events"].notna())]
    print(f"[bpi] {len(df):,} PA-ending regular-season rows", flush=True)
    return df

# ---- aggregate -------------------------------------------------------------
def build(df):
    # agg[bid][hand][cat] = dict of running totals
    agg = {}
    cols = ["batter", "p_throws", "pitch_type", "events",
            "woba_value", "woba_denom", "estimated_woba_using_speedangle"]
    # itertuples is plenty fast for a season (~120k rows)
    sub = df[cols].itertuples(index=False, name=None)
    for batter, p_throws, pitch_type, events, wval, wden, est in sub:
        if batter is None:
            continue
        hand = (str(p_throws) or "").upper()
        if hand not in ("L", "R"):
            continue
        cat = pitch_cat(pitch_type)
        if cat is None:
            continue
        ev = (str(events) or "").strip()
        if not ev or ev == "null" or ev == "nan":
            continue
        bid = str(int(batter)) if str(batter).replace(".0", "").isdigit() else str(batter)
        b = agg.setdefault(bid, {}).setdefault(hand, {}).setdefault(
            cat, {"pa": 0, "ab": 0, "d": 0, "t": 0, "hr": 0, "wn": 0.0, "xn": 0.0, "wd": 0.0})
        b["pa"] += 1
        wd = num(wden);  wv = num(wval);  es = num(est)
        if wd is not None: b["wd"] += wd
        if wv is not None: b["wn"] += wv
        b["xn"] += (es if es is not None else (wv if wv is not None else 0.0))
        if ev in NOAB:
            continue
        b["ab"] += 1
        if   ev == "double":   b["d"]  += 1
        elif ev == "triple":   b["t"]  += 1
        elif ev == "home_run": b["hr"] += 1
    return agg

def leaf(b):
    if not b:
        return None
    ab = b["ab"]
    iso = round((b["d"] + 2 * b["t"] + 3 * b["hr"]) / ab, ROUND) if ab > 0 else None
    woba  = round(b["wn"] / b["wd"], ROUND) if b["wd"] > 0 else None
    xwoba = round(b["xn"] / b["wd"], ROUND) if b["wd"] > 0 else None
    return {"iso": iso, "pa": b["pa"], "woba": woba, "xwoba": xwoba}

def to_output(agg):
    out = {}
    for bid, hands in agg.items():
        side = {}
        for hand in ("L", "R"):
            fam = hands.get(hand, {})
            side[hand] = {
                "fb":  leaf(fam.get("fb")),
                "br":  leaf(fam.get("br")),
                "off": leaf(fam.get("off")),
            }
        out[bid] = side
    return out

# ---- main ------------------------------------------------------------------
def main():
    df = load_statcast()
    agg = build(df)
    out = to_output(agg)
    payload = json.dumps(out, separators=(",", ":"))
    with open(OUT, "w") as f:
        f.write(payload)
    print(f"[bpi] wrote {OUT}: {len(out):,} hitters, {len(payload):,} bytes", flush=True)

if __name__ == "__main__":
    main()
