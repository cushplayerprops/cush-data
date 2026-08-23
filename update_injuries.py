#!/usr/bin/env python3
"""
update_injuries.py - lightweight injury-only refresher for wnba_stats.json.

Pulls ONLY the free ESPN injury feed (no stats.wnba.com / no ScrapeOps) and patches
each player's injStatus/injDetail in the existing wnba_stats.json, then stamps
injUpdated. Designed to run frequently on game days (every ~10 min) without the cost
of a full feed rebuild. Reuses build_wnba.py's own injury parsing so classification
stays identical to the full build.

Players no longer on the ESPN report have their injStatus/injDetail cleared, so
recovered players don't stay flagged between full builds.
"""
import json, datetime, os
from build_wnba import fetch_injuries_raw, parse_injuries, pbnorm, OUT


def main():
    if not os.path.exists(OUT):
        print("update_injuries: no", OUT, "present -- skipping (full build must run first)")
        return
    with open(OUT) as f:
        data = json.load(f)
    players = data.get("players") or {}
    if not players:
        print("update_injuries: no players in feed -- skipping")
        return

    try:
        inj_map = parse_injuries(fetch_injuries_raw())
    except Exception as e:
        print("update_injuries: injury fetch/parse failed:", e, "-- leaving feed unchanged")
        return
    if not inj_map:
        print("update_injuries: ESPN returned no injuries -- leaving feed unchanged")
        return

    matched = 0
    for p in players.values():
        nm = p.get("name")
        if not nm:
            continue
        st = inj_map.get(pbnorm(nm))
        if st:
            p["injStatus"] = st.get("status")
            if st.get("detail"):
                p["injDetail"] = st["detail"]
            else:
                p.pop("injDetail", None)
            matched += 1
        else:
            # no longer on the injury report -> clear any stale flag
            p.pop("injStatus", None)
            p.pop("injDetail", None)

    data["injUpdated"] = datetime.datetime.utcnow().isoformat() + "Z"
    if isinstance(data.get("counts"), dict):
        data["counts"]["injListed"] = len(inj_map)
        data["counts"]["injMatched"] = matched

    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print("update_injuries: matched %d of %d listed injuries" % (matched, len(inj_map)))


if __name__ == "__main__":
    main()
