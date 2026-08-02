#!/usr/bin/env python3
"""
build_umpires.py  ->  umpires.json

Home-plate umpire tendencies for the Cush strikeout + walk models. Umpires vary in how
big a strike zone they call: a wide-zone ump adds strikeouts and cuts walks, a tight-zone
ump does the opposite. For every ump, this walks every game he's worked the plate this
season and compares that game's COMBINED (both teams) strikeout and walk totals to the
league average per game:

    kIdx  = ump's K/game  / league K/game    (>1 = more Ks, wider/pitcher-friendly zone)
    bbIdx = ump's BB/game / league BB/game    (>1 = more walks, tighter zone)

Both are lightly regressed toward league average so a thin sample can't produce a wild
number. It also maps TODAY's slate to each game's assigned home-plate ump (once MLB posts
it) so the app can nudge each pitcher's K / walk projection by who's behind the plate.

Output shape:
    {
      "umps": { "Pat Hoberg": {"kIdx":1.05,"bbIdx":0.94,"n":22,"kpg":17.1,"bbpg":6.0}, ... },
      "today": { "147": {"ump":"Pat Hoberg","kIdx":1.05,"bbIdx":0.94},   # keyed by BOTH team ids
                 "133": {"ump":"Pat Hoberg","kIdx":1.05,"bbIdx":0.94}, ... },
      "lgKpg": 16.5, "lgBBpg": 6.4, "date": "2026-08-02"
    }
  today is keyed by BOTH teams' MLB team id (home and away) pointing at the same game ump,
  so the app can look up a pitcher's ump by his own team id.

METHOD: game K/BB come from the boxscore batting totals (home batters' Ks + away batters'
Ks = every strikeout in the game). The plate ump comes from the boxscore officials list.
Assignments for upcoming games only appear once MLB posts them (usually game-day), so
'today' fills in when available and is simply empty before then.

DEPENDENCIES: standard library only. No API key.
DEPLOY: run -> push umpires.json to root of cushplayerprops/cush-data main -> cron
(run it a couple times through the day so it picks up today's assignments as they post).
"""

import json, sys, time, urllib.request
from datetime import date

SEASON   = 2026
MLB      = "https://statsapi.mlb.com/api/v1"
OUT      = "umpires.json"
TIMEOUT  = 30
MIN_N    = 5      # min plate games before an ump gets a rating
REG      = 8.0    # regression-toward-league strength (games)
UA = {"User-Agent": "Mozilla/5.0 (cush-ump-build)"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            last = e
            time.sleep(1.2 * (k + 1))
    print("  ! fetch failed:", last, file=sys.stderr)
    return None


def plate_ump(box):
    for o in (box.get("officials") or []):
        if (o.get("officialType") or "") == "Home Plate":
            return (o.get("official") or {}).get("fullName")
    return None


def game_kbb(box):
    """Combined K and BB in the game (both teams' batting)."""
    tot_k = tot_bb = 0.0
    seen = False
    for sd in ("home", "away"):
        tm = (box.get("teams") or {}).get(sd) or {}
        bat = ((tm.get("teamStats") or {}).get("batting")) or {}
        k = bat.get("strikeOuts")
        bb = bat.get("baseOnBalls")
        if k is not None:
            tot_k += k
            seen = True
        if bb is not None:
            tot_bb += bb
    return (tot_k, tot_bb) if seen else (None, None)


def team_ids(g):
    t = g.get("teams") or {}
    h = ((t.get("home") or {}).get("team") or {}).get("id")
    a = ((t.get("away") or {}).get("team") or {}).get("id")
    return h, a


def main():
    today = date.today().isoformat()

    # ---- season: aggregate ump tendencies from every completed game ----
    sched = _get("%s/schedule?sportId=1&startDate=%d-03-01&endDate=%s&gameType=R"
                 % (MLB, SEASON, today))
    dates = (json.loads(sched).get("dates") if sched else []) or []
    pks = []
    for d in dates:
        for g in (d.get("games") or []):
            st = ((g.get("status") or {}).get("abstractGameState") or "")
            if st == "Final" and g.get("gamePk") is not None:
                pks.append(g["gamePk"])
    print("season final games:", len(pks), file=sys.stderr)

    acc = {}                 # ump -> {"k":[...], "bb":[...]}
    lgK = []
    lgBB = []
    for i, pk in enumerate(pks, 1):
        raw = _get("%s/game/%s/boxscore" % (MLB, pk))
        if not raw:
            continue
        try:
            box = json.loads(raw)
        except (ValueError, TypeError):
            continue
        k, bb = game_kbb(box)
        if k is None:
            continue
        lgK.append(k)
        lgBB.append(bb)
        ump = plate_ump(box)
        if ump:
            a = acc.setdefault(ump, {"k": [], "bb": []})
            a["k"].append(k)
            a["bb"].append(bb)
        if i % 100 == 0:
            print("  %d/%d games" % (i, len(pks)), file=sys.stderr)
        time.sleep(0.05)

    lgKpg = (sum(lgK) / len(lgK)) if lgK else 16.5
    lgBBpg = (sum(lgBB) / len(lgBB)) if lgBB else 6.4

    umps = {}
    for nm, a in acc.items():
        n = len(a["k"])
        if n < MIN_N:
            continue
        kpg = sum(a["k"]) / n
        bbpg = sum(a["bb"]) / n
        kIdx = ((kpg * n + lgKpg * REG) / (n + REG)) / lgKpg
        bbIdx = ((bbpg * n + lgBBpg * REG) / (n + REG)) / lgBBpg
        umps[nm] = {"kIdx": round(kIdx, 3), "bbIdx": round(bbIdx, 3),
                    "n": n, "kpg": round(kpg, 1), "bbpg": round(bbpg, 1)}

    # ---- today: map each game to its assigned plate ump (when posted) ----
    tod = {}
    traw = _get("%s/schedule?sportId=1&date=%s&gameType=R" % (MLB, today))
    tdates = (json.loads(traw).get("dates") if traw else []) or []
    for d in tdates:
        for g in (d.get("games") or []):
            pk = g.get("gamePk")
            hid, aid = team_ids(g)
            raw = _get("%s/game/%s/boxscore" % (MLB, pk))
            if not raw:
                continue
            try:
                box = json.loads(raw)
            except (ValueError, TypeError):
                continue
            ump = plate_ump(box)
            if not ump:
                continue
            u = umps.get(ump)
            entry = {"ump": ump,
                     "kIdx": (u["kIdx"] if u else 1.0),
                     "bbIdx": (u["bbIdx"] if u else 1.0)}
            if hid is not None:
                tod[str(hid)] = entry
            if aid is not None:
                tod[str(aid)] = entry
            time.sleep(0.05)

    out = {"umps": umps, "today": tod,
           "lgKpg": round(lgKpg, 2), "lgBBpg": round(lgBBpg, 2), "date": today}
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s : %d rated umps, %d team-slots today (lgK/g %.1f, lgBB/g %.1f)"
          % (OUT, len(umps), len(tod), lgKpg, lgBBpg), file=sys.stderr)


if __name__ == "__main__":
    main()
