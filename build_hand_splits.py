#!/usr/bin/env python3
"""
build_hand_splits.py
Generates batter_hand_splits.json for the cushplayerprops board.

Output shape (keyed by MLB player id):
{
  "665742": {
    "L": { "wrcPlus": 141, "iso": "0.255", "kPct": "18.9", "bbPct": "12.4" },   # vs LHP
    "R": { "wrcPlus": 168, "iso": "0.322", "kPct": "16.2", "bbPct": "14.1" }    # vs RHP
  },
  ...
}

Formulas match the app exactly (parseHitterStat):
  wrcPlus = round((OPS / 0.720) * 100)
  iso     = SLG - AVG            (3 decimals, e.g. "0.255")
  kPct    = SO  / PA * 100       (1 decimal)
  bbPct   = BB  / PA * 100       (1 decimal)

No API key needed. The MLB Stats API is public.
"""

import json
import time
import urllib.request

SEASON = 2026
BASE = "https://statsapi.mlb.com/api/v1"
MIN_PA = 1          # raise this (e.g. 25) if you want to ignore tiny vs-hand samples
SLEEP = 0.04        # be gentle on the API between calls
OUT_FILE = "batter_hand_splits.json"


def get(url, tries=3):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1)
    return {}


def parse(stat):
    """Replicates the app's parseHitterStat for the fields the columns use."""
    if not stat:
        return None
    try:
        pa = int(stat.get("plateAppearances") or 0)
    except (TypeError, ValueError):
        pa = 0
    if pa < MIN_PA:
        return None
    avg = float(stat.get("avg") or 0)
    slg = float(stat.get("slg") or 0)
    ops = float(stat.get("ops") or 0)
    so = int(stat.get("strikeOuts") or 0)
    bb = int(stat.get("baseOnBalls") or 0)
    return {
        "wrcPlus": round((ops / 0.720) * 100) if ops > 0 else None,
        "iso": f"{slg - avg:.3f}",
        "kPct": f"{so / pa * 100:.1f}",
        "bbPct": f"{bb / pa * 100:.1f}",
    }


def fetch_split(pid, code):
    url = (f"{BASE}/people/{pid}/stats?stats=statSplits&group=hitting"
           f"&season={SEASON}&gameType=R&sitCodes={code}")
    j = get(url)
    try:
        stat = j["stats"][0]["splits"][0]["stat"]
    except (KeyError, IndexError, TypeError):
        return None
    return parse(stat)


def main():
    # 1) every MLB team id
    teams = get(f"{BASE}/teams?sportId=1&season={SEASON}").get("teams", [])
    team_ids = [t["id"] for t in teams]
    print(f"{len(team_ids)} teams")

    # 2) collect hitter ids from active rosters (skip pitchers)
    hitter_ids = set()
    for tid in team_ids:
        roster = get(f"{BASE}/teams/{tid}/roster?rosterType=active&season={SEASON}").get("roster", [])
        for p in roster:
            pos = (p.get("position") or {}).get("abbreviation", "")
            if pos != "P":
                hitter_ids.add(p["person"]["id"])
        time.sleep(SLEEP)
    print(f"{len(hitter_ids)} hitters to fetch")

    # 3) per hitter: vs LHP (vl) and vs RHP (vr)
    out = {}
    for n, pid in enumerate(sorted(hitter_ids), 1):
        L = fetch_split(pid, "vl")   # vs left-handed pitchers
        R = fetch_split(pid, "vr")   # vs right-handed pitchers
        if L or R:
            out[str(pid)] = {"L": L, "R": R}
        if n % 50 == 0:
            print(f"  {n}/{len(hitter_ids)}")
        time.sleep(SLEEP)

    # 4) write the feed
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {OUT_FILE} with {len(out)} hitters")


if __name__ == "__main__":
    main()
