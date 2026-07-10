#!/usr/bin/env python3
"""
build_hitter_logs.py  ->  hitter_logs.json

Per-game hitter logs, SPLIT BY THE OPPOSING STARTER'S HANDEDNESS, for L10 hit-rates.

Output shape (keyed by MLB player id), most-recent game first:
{
  "665742": {
    "L": [ {"ab":4,"hrr":2,"tb":3,"hr":0,"fs":9.0}, ... ],   # games vs LHP starters
    "R": [ {"ab":5,"hrr":3,"tb":5,"hr":1,"fs":16.0}, ... ]    # games vs RHP starters
  },
  ...
}

Per game:
  ab  = at-bats
  hrr = hits + runs + RBIs        (the H+R+RBI prop)
  tb  = total bases               (the Total Bases prop)
  hr  = home runs                 (the HR prop)
  fs  = DraftKings-style hitter fantasy score:
        3*1B + 5*2B + 8*3B + 10*HR + 2*RBI + 2*R + 2*BB + 2*HBP + 5*SB

The opposing starter's hand for each game comes from the season schedule
(hydrate=probablePitcher; for completed games MLB reports the actual starter),
joined to each pitcher's throwing hand via the people endpoint.

No API key needed. The MLB Stats API is public.
"""

import json
import time
import urllib.request

SEASON = 2026
BASE = "https://statsapi.mlb.com/api/v1"
SLEEP = 0.03
KEEP = 15                      # games kept per hand (app uses last 10 with 3+ AB)
OUT_FILE = "hitter_logs.json"


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
        return int(x)
    except (TypeError, ValueError):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0


def main():
    # 1) hitter ids from active rosters
    teams = get(f"{BASE}/teams?sportId=1&season={SEASON}").get("teams", [])
    team_ids = [t["id"] for t in teams]
    hitters = set()
    for tid in team_ids:
        roster = get(f"{BASE}/teams/{tid}/roster?rosterType=active&season={SEASON}").get("roster", [])
        for p in roster:
            if ((p.get("position") or {}).get("abbreviation", "")) != "P":
                hitters.add(p["person"]["id"])
        time.sleep(SLEEP)
    print(f"{len(hitters)} hitters")

    # 2) season schedule -> gamePk -> home/away starter pitcher ids
    sched = get(f"{BASE}/schedule?sportId=1&season={SEASON}&gameType=R&hydrate=probablePitcher")
    game_start = {}   # gamePk -> {"home":pid or None, "away":pid or None}
    pids = set()
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            pk = g.get("gamePk")
            tt = g.get("teams", {})
            hp = ((tt.get("home", {}) or {}).get("probablePitcher") or {}).get("id")
            ap = ((tt.get("away", {}) or {}).get("probablePitcher") or {}).get("id")
            game_start[pk] = {"home": hp, "away": ap}
            if hp:
                pids.add(hp)
            if ap:
                pids.add(ap)

    # 3) pitcher throwing hand (batched)
    hand = {}
    plist = list(pids)
    for i in range(0, len(plist), 100):
        chunk = plist[i:i + 100]
        j = get(f"{BASE}/people?personIds={','.join(map(str, chunk))}&fields=people,id,pitchHand,code")
        for p in j.get("people", []):
            c = (p.get("pitchHand") or {}).get("code")
            if c in ("L", "R"):
                hand[p["id"]] = c
        time.sleep(SLEEP)
    print(f"{len(game_start)} games, {len(hand)} pitcher hands")

    def opp_hand(pk, is_home):
        gs = game_start.get(pk)
        if not gs:
            return None
        # the hitter faces the OTHER team's starter
        opp_pid = gs["away"] if is_home else gs["home"]
        return hand.get(opp_pid)

    # 4) per-hitter game logs, bucketed by opposing starter hand
    out = {}
    matched = 0
    for n, pid in enumerate(sorted(hitters), 1):
        j = get(f"{BASE}/people/{pid}/stats?stats=gameLog&group=hitting&season={SEASON}&gameType=R")
        try:
            splits = j["stats"][0]["splits"]
        except (KeyError, IndexError, TypeError):
            splits = []
        recL, recR = [], []
        for sp in reversed(splits):        # gameLog is oldest-first -> reverse to recent-first
            st = sp.get("stat", {})
            pk = (sp.get("game") or {}).get("gamePk")
            oh = opp_hand(pk, bool(sp.get("isHome")))
            if oh not in ("L", "R"):
                continue
            ab = num(st.get("atBats"))
            h = num(st.get("hits")); r = num(st.get("runs")); rbi = num(st.get("rbi"))
            tb = num(st.get("totalBases")); hr = num(st.get("homeRuns"))
            dbl = num(st.get("doubles")); trp = num(st.get("triples"))
            bb = num(st.get("baseOnBalls")); hbp = num(st.get("hitByPitch")); sb = num(st.get("stolenBases"))
            singles = max(0, h - dbl - trp - hr)
            fs = 3 * singles + 5 * dbl + 8 * trp + 10 * hr + 2 * rbi + 2 * r + 2 * bb + 2 * hbp + 5 * sb
            g = {"ab": ab, "hrr": h + r + rbi, "tb": tb, "hr": hr, "fs": round(fs, 1)}
            (recL if oh == "L" else recR).append(g)
        rec = {}
        if recL:
            rec["L"] = recL[:KEEP]
        if recR:
            rec["R"] = recR[:KEEP]
        if rec:
            out[str(pid)] = rec
            matched += 1
        if n % 50 == 0:
            print(f"  {n}/{len(hitters)}")
        time.sleep(SLEEP)

    with open(OUT_FILE, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {OUT_FILE}: {matched} hitters with hand-split logs")


if __name__ == "__main__":
    main()
