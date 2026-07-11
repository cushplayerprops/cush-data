#!/usr/bin/env python3
"""
build_pitcher_logs.py  ->  pitcher_logs.json

Per-game pitcher logs for L10 hit-rates on the Strikeout / Pitcher-FS models.

Output shape (keyed by MLB player id), most-recent game first:
{
  "543037": [ {"k":7,"outs":18,"er":2,"fs":18.85}, ... ],
  ...
}

Per game:
  k    = strikeouts
  outs = outs recorded (innings pitched x 3)   -> pitcher-outs prop
  er   = earned runs                            -> earned-runs prop
  fs   = DraftKings-style pitcher fantasy:
         outs*0.75 + k*2 + win*4 - er*2 - (hits+walks+HBP)*0.6

Only appearances with at least one out are kept (drops did-not-pitch rows).
No API key needed. The MLB Stats API is public.
"""

import json
import time
import urllib.request

SEASON = 2026
BASE = "https://statsapi.mlb.com/api/v1"
SLEEP = 0.03
KEEP = 15
OUT_FILE = "pitcher_logs.json"


def get(url, tries=3):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1)
    return {}


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def ip_to_outs(ip):
    # innings pitched like "6.2" = 6 innings + 2 outs = 20 outs
    try:
        s = str(ip)
        whole = int(float(s))
        frac = int(round((float(s) - whole) * 10))
        return whole * 3 + max(0, min(2, frac))
    except (TypeError, ValueError):
        return 0


def main():
    teams = get(f"{BASE}/teams?sportId=1&season={SEASON}").get("teams", [])
    team_ids = [t["id"] for t in teams]
    pitchers = set()
    for tid in team_ids:
        roster = get(f"{BASE}/teams/{tid}/roster?rosterType=active&season={SEASON}").get("roster", [])
        for p in roster:
            if ((p.get("position") or {}).get("abbreviation", "")) == "P":
                pitchers.add(p["person"]["id"])
        time.sleep(SLEEP)
    print(f"{len(pitchers)} pitchers")

    out = {}
    for n, pid in enumerate(sorted(pitchers), 1):
        j = get(f"{BASE}/people/{pid}/stats?stats=gameLog&group=pitching&season={SEASON}&gameType=R")
        try:
            splits = j["stats"][0]["splits"]
        except (KeyError, IndexError, TypeError):
            splits = []
        rec = []
        for sp in reversed(splits):        # oldest-first -> recent-first
            st = sp.get("stat", {})
            outs = ip_to_outs(st.get("inningsPitched", "0"))
            if outs <= 0:
                continue
            k = int(num(st.get("strikeOuts")))
            er = int(num(st.get("earnedRuns")))
            h = num(st.get("hits")); bb = num(st.get("baseOnBalls")); hbp = num(st.get("hitByPitch"))
            w = num(st.get("wins"))
            fs = outs * 0.75 + k * 2 + w * 4 - er * 2 - (h + bb + hbp) * 0.6
            rec.append({"k": k, "outs": outs, "er": er, "hits": int(h), "fs": round(fs, 1)})
        if rec:
            out[str(pid)] = rec[:KEEP]
        if n % 50 == 0:
            print(f"  {n}/{len(pitchers)}")
        time.sleep(SLEEP)

    with open(OUT_FILE, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {OUT_FILE}: {len(out)} pitchers")


if __name__ == "__main__":
    main()
