#!/usr/bin/env python3
"""
grade_nba_proj.py  —  grade a prior NBA projection snapshot against actual results.

Takes an immutable snapshot written earlier by build_nba_proj.py and the CURRENT
nba_stats.json (whose player logs now contain the game that was projected). For each
row it finds the actual outcome of the projected game, scores the point estimate,
and decomposes the error into a minutes part and a rate part so we can later tell
WHERE misses come from. Append-only: never mutates the snapshot.

Matching rule (unbiased): the projected game is the player's earliest log entry with
date strictly after the snapshot's `last_log_date` for that player. If none exists
yet -> status "pending". If the player did not appear (no such log row) -> "no_result"
(a DNP is NOT graded as zero — per spec).

No sportsbook lines are joined yet, so this grades POINT-ESTIMATE accuracy (MAE / bias /
error attribution). Probability calibration (Brier/log-loss) comes once lines are wired in.
"""

import json, math, os, datetime
import build_nba_proj as E   # reuse config + project_stat


def find_actual(log, last_log_date):
    """Earliest game strictly after last_log_date = the game that was projected."""
    cand = [g for g in (log or []) if g.get("d") and last_log_date and g["d"] > last_log_date
            and g.get("min", 0) and g["min"] > 0]
    if not cand:
        return None
    return sorted(cand, key=lambda g: g["d"])[0]


def grade_row(row, actual):
    """Return a scored, append-only record. Exact attribution:
       statError = minutesError*projRate + actualMinutes*rateError."""
    stat = row["stat_type"]; lk = E.STATS[stat]["log_key"]
    proj_mean = row["projected_stat_mean"]; proj_min = row["projected_minutes"]
    proj_rate = row["final_rate"]
    a_stat = float(actual.get(lk, 0) or 0); a_min = float(actual.get("min", 0) or 0)
    a_rate = (a_stat / a_min) if a_min > 0 else 0.0
    minutes_err = a_min - proj_min
    rate_err = a_rate - proj_rate
    stat_err = a_stat - proj_mean
    g = dict(row)  # carry the full immutable forecast forward
    g.update({
        "graded_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "actual_game_date": actual.get("d"),
        "actual_stat": a_stat, "actual_minutes": a_min, "actual_rate": round(a_rate, 5),
        "stat_error": round(stat_err, 3), "abs_error": round(abs(stat_err), 3),
        "minutes_error": round(minutes_err, 2), "rate_error": round(rate_err, 5),
        "minutes_contribution": round(minutes_err * proj_rate, 3),   # error from minutes miss
        "rate_contribution": round(a_min * rate_err, 3),             # error from rate miss
        "result_status": "graded",
    })
    return g


def grade_snapshot(snapshot, current_stats):
    players = current_stats.get("players", {})
    graded, pending = [], 0
    for row in snapshot.get("rows", []):
        p = players.get(row["player_id"])
        actual = find_actual(p.get("log") if p else None, row.get("last_log_date"))
        if actual is None:
            pending += 1
            continue
        graded.append(grade_row(row, actual))
    return graded, pending


def summarize(graded):
    from collections import defaultdict
    agg = defaultdict(lambda: {"n": 0, "abs": 0.0, "signed": 0.0, "min_c": 0.0, "rate_c": 0.0})
    for g in graded:
        s = g["stat_type"]; a = agg[s]
        a["n"] += 1; a["abs"] += g["abs_error"]; a["signed"] += g["stat_error"]
        a["min_c"] += abs(g["minutes_contribution"]); a["rate_c"] += abs(g["rate_contribution"])
    out = {}
    for s, a in agg.items():
        n = a["n"] or 1
        out[s] = {"n": a["n"], "MAE": round(a["abs"]/n, 3), "bias": round(a["signed"]/n, 3),
                  "minutes_share": round(a["min_c"]/(a["min_c"]+a["rate_c"]+1e-9), 3),
                  "rate_share": round(a["rate_c"]/(a["min_c"]+a["rate_c"]+1e-9), 3)}
    return out


def main():
    snap_path = os.environ.get("NBA_PROJ_SNAPSHOT", "nba_proj_snapshot.json")
    if not os.path.exists(snap_path):
        print("no prior snapshot (%s) — nothing to grade yet (first run)." % snap_path)
        return []
    with open(snap_path) as f:
        snapshot = json.load(f)
    current = E.load_stats()
    graded, pending = grade_snapshot(snapshot, current)
    out_path = os.environ.get("NBA_GRADED_OUT", "nba_proj_graded.jsonl")
    with open(out_path, "a") as f:   # append-only
        for g in graded:
            f.write(json.dumps(g, separators=(",", ":")) + "\n")
    print("GRADED", len(graded), "rows | pending(no game yet):", pending, "-> appended to", out_path)
    print("summary:", json.dumps(summarize(graded), indent=2))
    return graded


if __name__ == "__main__":
    main()
