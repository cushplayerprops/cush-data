#!/usr/bin/env python3
"""
build_team_fs_allowed.py  --  build team_fs_allowed.json for Cush Player Props

For every MLB team, the QUALITY-ADJUSTED (residual) PrizePicks-style fantasy points
that opposing STARTING pitchers put up against them, split by the starter's hand.
resid = actual FS - that pitcher's own season FS/start.  Positive = soft spot.
Scoring matches the app's pitFS:  FS = outs + 3*K - 3*ER + QS(4 if outs>=18 & ER<=3).
Wins are omitted (team/bullpen driven, not a lineup property).
Stdlib only. Run: python build_team_fs_allowed.py   (or pass a year: ... 2026)
"""
import json, sys, time, datetime, urllib.request

SEASON = int(sys.argv[1]) if len(sys.argv) > 1 else datetime.date.today().year
OUT_PATH, L30_DAYS, MIN_N_IDX = "team_fs_allowed.json", 30, 6
API, TIMEOUT, SLEEP, RETRIES = "https://statsapi.mlb.com/api/v1", 25, 0.05, 4

def get(url):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cush-fs-allowed/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e; time.sleep(0.6 * (attempt + 1))
    print(f"  ! giving up on {url}  ({last})", file=sys.stderr); return None

def ip_to_outs(ip):
    if ip is None: return None
    try:
        s = str(ip)
        if "." in s:
            whole, frac = s.split("."); return int(whole) * 3 + int(frac)
        return int(float(s)) * 3
    except Exception:
        return None

def pp_fs(outs, k, er):
    qs = 4 if (outs is not None and outs >= 18 and er is not None and er <= 3) else 0
    return outs + 3 * (k or 0) - 3 * (er or 0) + qs

def load_teams():
    j = get(f"{API}/teams?sportId=1&season={SEASON}") or {}
    out = {}
    for t in j.get("teams", []):
        tid = t.get("id")
        if tid is not None:
            out[int(tid)] = {"abbr": t.get("abbreviation") or "", "name": t.get("name") or ""}
    return out

def load_starter_ids():
    url = f"{API}/stats?stats=season&group=pitching&season={SEASON}&gameType=R&playerPool=All&limit=3000"
    j = get(url) or {}; ids = []
    for blk in j.get("stats", []):
        for sp in blk.get("splits", []):
            st = sp.get("stat", {}) or {}
            try: gs = int(st.get("gamesStarted"))
            except (TypeError, ValueError): gs = 0
            pid = (sp.get("player") or {}).get("id")
            if gs >= 1 and pid is not None: ids.append(int(pid))
    return sorted(set(ids))

def load_hands(pitcher_ids):
    hands = {}
    for i in range(0, len(pitcher_ids), 100):
        chunk = pitcher_ids[i:i + 100]
        j = get(f"{API}/people?personIds={','.join(map(str, chunk))}") or {}
        for p in j.get("people", []):
            pid = p.get("id"); code = ((p.get("pitchHand") or {}).get("code") or "").upper()
            if pid is not None and code in ("L", "R"): hands[int(pid)] = code
        time.sleep(SLEEP)
    return hands

def collect_starts(pitcher_ids, hands):
    starts = []; total = len(pitcher_ids)
    for idx, pid in enumerate(pitcher_ids, 1):
        hand = hands.get(pid)
        if hand not in ("L", "R"): continue
        j = get(f"{API}/people/{pid}/stats?stats=gameLog&group=pitching&season={SEASON}&gameType=R")
        time.sleep(SLEEP)
        if not j: continue
        raw = []
        for blk in j.get("stats", []):
            for sp in blk.get("splits", []):
                st = sp.get("stat", {}) or {}
                try:
                    if int(st.get("gamesStarted") or 0) < 1: continue
                except (TypeError, ValueError): continue
                outs = ip_to_outs(st.get("inningsPitched"))
                if outs is None: continue
                opp = (sp.get("opponent") or {}).get("id")
                if opp is None: continue
                raw.append({"opp": int(opp),
                            "fs": float(pp_fs(outs, st.get("strikeOuts"), st.get("earnedRuns"))),
                            "date": sp.get("date") or ""})
        if not raw: continue
        mean_fs = sum(r["fs"] for r in raw) / len(raw)
        for r in raw:
            starts.append({"opp": r["opp"], "hand": hand, "fs": r["fs"],
                           "resid": r["fs"] - mean_fs, "date": r["date"]})
        if idx % 25 == 0 or idx == total:
            print(f"  ... {idx}/{total} pitchers, {len(starts)} starts", file=sys.stderr)
    return starts

def _agg(rows):
    n = len(rows)
    if n == 0: return None
    return {"res": round(sum(r["resid"] for r in rows) / n, 2),
            "raw": round(sum(r["fs"] for r in rows) / n, 2), "n": n}

def _std(vals):
    if len(vals) < 2: return 1.0
    m = sum(vals) / len(vals)
    return ((sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5) or 1.0

def build(starts, teams):
    cutoff = (datetime.date.today() - datetime.timedelta(days=L30_DAYS)).isoformat()
    out = {}
    for tid, meta in teams.items():
        entry = {"abbr": meta["abbr"], "name": meta["name"], "vsL": {}, "vsR": {}}
        for hand, key in (("L", "vsL"), ("R", "vsR")):
            allrows = [s for s in starts if s["opp"] == tid and s["hand"] == hand]
            recent = [s for s in allrows if s["date"] and s["date"] >= cutoff]
            season = _agg(allrows); l30 = _agg(recent)
            if season: entry[key]["season"] = season
            if l30: entry[key]["l30"] = l30
        out[str(tid)] = entry
    for hk in ("vsL", "vsR"):
        for win in ("season", "l30"):
            vals = [out[t][hk][win]["res"] for t in out if win in out[t].get(hk, {})]
            if not vals: continue
            mean = sum(vals) / len(vals); sd = _std(vals)
            for t in out:
                cell = out[t].get(hk, {}).get(win)
                if not cell: continue
                shrink = min(1.0, cell["n"] / MIN_N_IDX)
                z = ((mean + (cell["res"] - mean) * shrink) - mean) / sd
                cell["idx"] = int(round(max(70, min(140, 100 + z * 10))))
    return out

def main():
    print(f"[team_fs_allowed] season={SEASON}", file=sys.stderr)
    teams = load_teams(); print(f"  teams: {len(teams)}", file=sys.stderr)
    sids = load_starter_ids(); print(f"  starters: {len(sids)}", file=sys.stderr)
    hands = load_hands(sids); print(f"  hands: {len(hands)}", file=sys.stderr)
    starts = collect_starts(sids, hands); print(f"  starts: {len(starts)}", file=sys.stderr)
    data = build(starts, teams)
    data["_meta"] = {"season": SEASON, "generated": datetime.date.today().isoformat(),
                     "formula": "FS = outs + 3*K - 3*ER + QS(4 if outs>=18 & ER<=3); wins omitted",
                     "note": "res = FS above pitcher's own norm allowed to that hand; idx 100-centered (>100 = softer, better for pitcher)",
                     "l30_days": L30_DAYS}
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"  wrote {OUT_PATH} ({len(data)-1} teams)", file=sys.stderr)

if __name__ == "__main__":
    main()
