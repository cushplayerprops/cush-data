#!/usr/bin/env python3
"""
backtest_pitcher_fs.py  ->  prints a validation report (no file written)

Leakage-free walk-forward test of the Pitcher FS (PrizePicks fantasy) model against
this season's actual starts. For every start, it:

  1. Reconstructs the pitcher's talent from his PRIOR starts only (no peeking at the
     game being predicted) -- running IP / SO / BF / ER accumulators.
  2. Projects FP with the SAME backbone the app uses:
         E[FP] = 3*xIP + 3*xK - 3*xER + 6*P(Win) + 4*P(QS)
     (Opponent K / offense adjustments are OMITTED here -- this tests the backbone, the
      part that drives the ranking. Those layers are centered near 1.0 and wash out in
      aggregate; we test them separately once wired.)
  3. Scores the ACTUAL PrizePicks FP for that start:
         FP = 1*outs + 3*K - 3*ER + 4*QS + 6*Win
         outs = IP*3 ; QS = (IP>=6 and ER<=3) ; Win = decision was a win.

Then it reports how well the projection tracked reality:
  * Pearson r(proj FP, actual FP)              -- overall projection quality
  * top-decile LIFT                            -- the bettor metric: do our highest
                                                  projections actually score the most?
  * per-component r (xIP vs IP, xK vs K, xER vs ER) -- which piece is strong/weak
  * mean actual FP, projection bias (proj - actual)

Use this the way we used the hitter/HR weight backtests: change a constant (LG_IP,
FIP_W is N/A here, the regressions, the P(Win)/P(QS) shapes), rerun, watch r and lift.

DEPENDENCIES: standard library only. No API key.
RUN: swap this into the backtest workflow's `python X.py` line (or run locally).
"""

import json, sys, time, math, urllib.request

SEASON     = 2026
MIN_PRIOR  = 3                              # need >=3 prior starts before we trust a projection
LG_IP      = 5.3
IP_REG     = 3.0
LG_ERA     = 4.20
ERA_REG    = 30.0
MIN_OPP_G  = 5                              # need >=5 prior opp games before trusting their R/G
OPP_LO     = 0.80                           # oppRunFac caps (mild, per the design doc)
OPP_HI     = 1.20
MLB_BASE   = "https://statsapi.mlb.com/api/v1"
TIMEOUT    = 30
UA = {"User-Agent": "Mozilla/5.0 (cush-fs-backtest)"}


def _get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa
            last = e
            time.sleep(1.5 * (k + 1))
    print("  ! fetch failed:", last, file=sys.stderr)
    return None


def ip_to_float(s):
    try:
        s = str(s)
        if "." in s:
            whole, frac = s.split(".")
            outs = int(whole) * 3 + int(frac)
        else:
            outs = int(float(s)) * 3
        return outs / 3.0
    except (ValueError, TypeError):
        return None


def clmp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def poi_cdf3(l):
    if l <= 0:
        return 1.0
    e = math.exp(-l)
    return e * (1 + l + l * l / 2 + l * l * l / 6)


def project(prior, opp_run_fac=1.0):
    """Model FP from prior-only accumulators dict {ip,so,bf,er,gs}. Backbone only.
    opp_run_fac scales the run-prevention rate (opponent offense). 1.0 = neutral
    = original behavior, so the baseline arm is unchanged."""
    gs = prior["gs"]
    avg_ip = prior["ip"] / gs if gs else LG_IP
    xIP = clmp((avg_ip * gs + LG_IP * IP_REG) / (gs + IP_REG), 3.0, 7.5)
    k_rate = (prior["so"] / prior["bf"]) if prior["bf"] > 0 else 0.21
    bf_per_ip = clmp(prior["bf"] / prior["ip"], 3.8, 4.9) if prior["ip"] > 0 else 4.25
    xK = clmp(k_rate * xIP * bf_per_ip, 0, 15)
    era = (prior["er"] / prior["ip"] * 9) if prior["ip"] > 0 else LG_ERA
    xERA9 = (era * prior["ip"] + LG_ERA * ERA_REG) / (prior["ip"] + ERA_REG)
    xERA9adj = xERA9 * opp_run_fac                 # opponent-offense adjusted run rate
    xER = clmp(xERA9adj * xIP / 9, 0, 9)
    pIP6 = clmp(0.5 + (xIP - 6) * 0.35, 0.02, 0.95)
    pQS = pIP6 * poi_cdf3(xERA9adj * 6 / 9)        # tougher offense -> fewer quality starts
    pWIN = clmp(0.5 + ((LG_ERA - xERA9) / 1.20) * 0.08, 0.30, 0.70)  # pitcher quality, unadjusted
    fp = 3 * xIP + 3 * xK - 3 * xER + 6 * pWIN + 4 * pQS
    return {"fp": fp, "xIP": xIP, "xK": xK, "xER": xER}


def actual_fp(ipf, so, er, win):
    outs = int(round(ipf * 3))
    qs = 1 if (ipf >= 6.0 and er <= 3) else 0
    return outs + 3 * so - 3 * er + 4 * qs + 6 * (1 if win else 0), outs, qs


def all_team_ids():
    data = _get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}'
    return [t["id"] for t in json.loads(data).get("teams", [])]


def pitcher_ids(team_ids):
    ids = {}
    for tid in team_ids:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, tid))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            if (p.get("position") or {}).get("abbreviation") == "P":
                ids[p["person"]["id"]] = 1
        time.sleep(0.04)
    return list(ids)


def team_runs(team_ids):
    """{teamId: sorted [(date, runsScored)]} from team hitting game logs,
    plus league runs/game (used to center oppRunFac at 1.0)."""
    tr = {}
    allruns = []
    for tid in team_ids:
        data = _get("%s/teams/%d/stats?stats=gameLog&group=hitting&season=%d&gameType=R"
                    % (MLB_BASE, tid, SEASON))
        lst = []
        if data:
            try:
                for sp in json.loads(data)["stats"][0]["splits"]:
                    st = sp.get("stat") or {}
                    try:
                        rn = int(st.get("runs") or 0)
                    except (TypeError, ValueError):
                        rn = 0
                    lst.append((sp.get("date") or "", rn))
                    allruns.append(rn)
            except (KeyError, IndexError, TypeError):
                pass
        lst.sort(key=lambda x: x[0])
        tr[tid] = lst
        time.sleep(0.04)
    lg = (sum(allruns) / len(allruns)) if allruns else 4.40
    return tr, lg


def opp_prior_rg(tr, opp_id, date):
    """Opponent runs/game using ONLY their games before `date` (leakage-free)."""
    games = tr.get(opp_id) or []
    vals = [r for (d, r) in games if d < date]
    if len(vals) < MIN_OPP_G:
        return None
    return sum(vals) / len(vals)


def starts(pid):
    """Chronological list of dicts per START: ip, so, bf, er, win."""
    data = _get("%s/people/%d/stats?stats=gameLog&group=pitching&season=%d&gameType=R"
                % (MLB_BASE, pid, SEASON))
    rows = []
    try:
        for sp in json.loads(data)["stats"][0]["splits"]:
            st = sp.get("stat") or {}
            try:
                if int(st.get("gamesStarted") or 0) < 1:
                    continue
            except (TypeError, ValueError):
                continue
            ipf = ip_to_float(st.get("inningsPitched"))
            if ipf is None:
                continue
            def _i(k):
                try:
                    return int(st.get(k) or 0)
                except (TypeError, ValueError):
                    return 0
            rows.append({"date": sp.get("date") or "", "ip": ipf, "so": _i("strikeOuts"),
                         "bf": _i("battersFaced"), "er": _i("earnedRuns"), "win": _i("wins") >= 1,
                         "opp": (sp.get("opponent") or {}).get("id")})
    except (KeyError, IndexError, TypeError):
        return []
    rows.sort(key=lambda r: r["date"])
    return rows


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def main():
    print("collecting teams + opponent offense ...", file=sys.stderr)
    tids = all_team_ids()
    tr, LG_RG = team_runs(tids)
    print("  %d teams, league R/G = %.2f" % (len(tids), LG_RG), file=sys.stderr)
    print("collecting pitchers ...", file=sys.stderr)
    ids = pitcher_ids(tids)
    print("  %d pitchers" % len(ids), file=sys.stderr)

    P_fp, A_fp = [], []                      # baseline (oppRunFac = 1.0)
    P_ip, A_ip = [], []
    P_k,  A_k  = [], []
    P_er, A_er = [], []
    P_fp2 = []                               # +opponent-offense arm
    P_er2 = []
    opp_used = 0

    for i, pid in enumerate(sorted(ids), 1):
        gs_rows = starts(pid)
        time.sleep(0.05)
        if len(gs_rows) <= MIN_PRIOR:
            continue
        acc = {"ip": 0.0, "so": 0, "bf": 0, "er": 0, "gs": 0}
        for r in gs_rows:
            if acc["gs"] >= MIN_PRIOR and acc["ip"] > 0:
                # opponent-offense factor (leakage-free: opp games before this date)
                orf = 1.0
                prg = opp_prior_rg(tr, r.get("opp"), r["date"]) if r.get("opp") is not None else None
                if prg is not None:
                    orf = clmp(prg / LG_RG, OPP_LO, OPP_HI)
                    opp_used += 1
                pr  = project(acc, 1.0)                 # baseline arm
                pr2 = project(acc, orf)                 # +opponent-offense arm
                af, outs, qs = actual_fp(r["ip"], r["so"], r["er"], r["win"])
                P_fp.append(pr["fp"]);  A_fp.append(af)
                P_ip.append(pr["xIP"]); A_ip.append(r["ip"])
                P_k.append(pr["xK"]);   A_k.append(r["so"])
                P_er.append(pr["xER"]); A_er.append(r["er"])
                P_fp2.append(pr2["fp"]); P_er2.append(pr2["xER"])
            acc["ip"] += r["ip"]; acc["so"] += r["so"]; acc["bf"] += r["bf"]
            acc["er"] += r["er"]; acc["gs"] += 1
        if i % 50 == 0:
            print("  %d/%d (%d test starts)" % (i, len(ids), len(P_fp)), file=sys.stderr)

    n = len(P_fp)
    if n < 10:
        print("not enough test starts (%d) -- run later in the season." % n)
        return

    # top-decile lift (baseline)
    order = sorted(range(n), key=lambda j: P_fp[j], reverse=True)
    top = order[:max(1, n // 10)]
    lift = (sum(A_fp[j] for j in top) / len(top)) / (sum(A_fp) / n)

    print("\n=== PITCHER FS BACKTEST (%d leakage-free starts, %d+ prior) ===" % (n, MIN_PRIOR))
    print("mean ACTUAL FP/start : %.2f" % (sum(A_fp) / n))
    print("mean PROJ   FP/start : %.2f   (bias %+.2f)" % (sum(P_fp) / n, (sum(P_fp) - sum(A_fp)) / n))
    print("r(proj FP, actual FP): %.3f" % pearson(P_fp, A_fp))
    print("top-decile LIFT      : %.2fx   (top 10%% of projections score %.0f%% of league avg)"
          % (lift, lift * 100))
    print("-- per component --")
    print("r(xIP, actual IP)    : %.3f" % pearson(P_ip, A_ip))
    print("r(xK,  actual K)     : %.3f" % pearson(P_k,  A_k))
    print("r(xER, actual ER)    : %.3f" % pearson(P_er, A_er))

    # ---- xER LEVER: opponent offense (baseline vs +opp off, one run) ----
    order2 = sorted(range(n), key=lambda j: P_fp2[j], reverse=True)
    top2 = order2[:max(1, n // 10)]
    lift2 = (sum(A_fp[j] for j in top2) / len(top2)) / (sum(A_fp) / n)
    print("\n=== xER LEVER: opponent offense wired into runs (oppRunFac) ===")
    print("league R/G %.2f | cap [%.2f,%.2f] | min opp games %d | opp data used %d/%d starts"
          % (LG_RG, OPP_LO, OPP_HI, MIN_OPP_G, opp_used, n))
    print("                        BASELINE     +OPP OFF")
    print("r(xER, actual ER)     :   %.3f        %.3f" % (pearson(P_er, A_er), pearson(P_er2, A_er)))
    print("r(proj FP, actual FP) :   %.3f        %.3f" % (pearson(P_fp, A_fp), pearson(P_fp2, A_fp)))
    print("top-decile LIFT       :   %.2fx        %.2fx" % (lift, lift2))
    print("mean PROJ FP          :   %.2f        %.2f" % (sum(P_fp) / n, sum(P_fp2) / n))
    print("bias (proj-actual)    :   %+.2f        %+.2f"
          % ((sum(P_fp) - sum(A_fp)) / n, (sum(P_fp2) - sum(A_fp)) / n))
    print("\nNote: BASELINE column should match the prior run (sanity). P(Win) stays a")
    print("crude proxy by design; opponent offense is applied to xER and P(QS) only.")


if __name__ == "__main__":
    main()
