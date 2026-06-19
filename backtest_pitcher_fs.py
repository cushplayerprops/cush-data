#!/usr/bin/env python3
"""
backtest_pitcher_fs.py  ->  prints a validation report (no file written)

Leakage-free walk-forward test of the Pitcher FS (PrizePicks fantasy) model.
For every start, talent is reconstructed from PRIOR starts only, FP is projected
with the app backbone E[FP]=3*xIP+3*xK-3*xER+6*P(Win)+4*P(QS), and compared to the
ACTUAL PrizePicks FP (1*outs+3*K-3*ER+4*QS+6*Win).

THIS VERSION -- recency-weighting experiment.
The prior starts are combined with an exponential half-life (in starts): a start H
starts back counts half as much as the most recent. We sweep several half-lives in
ONE run and report each, so you can read the best directly:
    HALF_LIVES = [0, 3, 5, 8, 12]   (0 = flat / equal weights = original baseline)
The 'flat' column MUST match the prior run -- that's the built-in sanity check.

DEPENDENCIES: standard library only. No API key.
"""

import json, sys, time, math, urllib.request

SEASON     = 2026
MIN_PRIOR  = 3                              # need >=3 prior starts before we trust a projection
LG_IP      = 5.3
IP_REG     = 3.0
LG_ERA     = 4.20
ERA_REG    = 30.0
HALF_LIVES = [0, 3, 5, 8, 12]              # 0 = flat (baseline); others = recency half-life in starts
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


def weighted_acc(prior, halflife):
    """Combine prior starts into accumulators. halflife=0 -> equal weights (baseline);
    else exponential recency: the most recent prior start gets weight 1, a start H
    back gets 0.5. 'gs' becomes the effective (weighted) sample size."""
    n = len(prior)
    if halflife and halflife > 0:
        w = [0.5 ** ((n - 1 - idx) / halflife) for idx in range(n)]
    else:
        w = [1.0] * n
    acc = {"ip": 0.0, "so": 0.0, "bf": 0.0, "er": 0.0, "gs": 0.0}
    for wi, r in zip(w, prior):
        acc["ip"] += wi * r["ip"]; acc["so"] += wi * r["so"]; acc["bf"] += wi * r["bf"]
        acc["er"] += wi * r["er"]; acc["gs"] += wi
    return acc


def project(prior):
    """Model FP from accumulators dict {ip,so,bf,er,gs}. Backbone only (unchanged)."""
    gs = prior["gs"]
    avg_ip = prior["ip"] / gs if gs else LG_IP
    xIP = clmp((avg_ip * gs + LG_IP * IP_REG) / (gs + IP_REG), 3.0, 7.5)
    k_rate = (prior["so"] / prior["bf"]) if prior["bf"] > 0 else 0.21
    bf_per_ip = clmp(prior["bf"] / prior["ip"], 3.8, 4.9) if prior["ip"] > 0 else 4.25
    xK = clmp(k_rate * xIP * bf_per_ip, 0, 15)
    era = (prior["er"] / prior["ip"] * 9) if prior["ip"] > 0 else LG_ERA
    xERA9 = (era * prior["ip"] + LG_ERA * ERA_REG) / (prior["ip"] + ERA_REG)
    xER = clmp(xERA9 * xIP / 9, 0, 9)
    pIP6 = clmp(0.5 + (xIP - 6) * 0.35, 0.02, 0.95)
    pQS = pIP6 * poi_cdf3(xERA9 * 6 / 9)
    pWIN = clmp(0.5 + ((LG_ERA - xERA9) / 1.20) * 0.08, 0.30, 0.70)
    fp = 3 * xIP + 3 * xK - 3 * xER + 6 * pWIN + 4 * pQS
    return {"fp": fp, "xIP": xIP, "xK": xK, "xER": xER}


def actual_fp(ipf, so, er, win):
    outs = int(round(ipf * 3))
    qs = 1 if (ipf >= 6.0 and er <= 3) else 0
    return outs + 3 * so - 3 * er + 4 * qs + 6 * (1 if win else 0), outs, qs


def pitcher_ids():
    ids = {}
    teams = json.loads(_get(MLB_BASE + "/teams?sportId=1") or '{"teams":[]}')["teams"]
    for t in teams:
        data = _get("%s/teams/%d/roster?rosterType=active" % (MLB_BASE, t["id"]))
        if not data:
            continue
        for p in json.loads(data).get("roster", []):
            if (p.get("position") or {}).get("abbreviation") == "P":
                ids[p["person"]["id"]] = 1
        time.sleep(0.04)
    return list(ids)


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
                         "bf": _i("battersFaced"), "er": _i("earnedRuns"), "win": _i("wins") >= 1})
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


def decile_lift(proj, act):
    n = len(proj)
    order = sorted(range(n), key=lambda j: proj[j], reverse=True)
    top = order[:max(1, n // 10)]
    return (sum(act[j] for j in top) / len(top)) / (sum(act) / n)


def hl_label(h):
    return "flat" if h == 0 else "HL=%d" % h


def main():
    print("collecting pitchers ...", file=sys.stderr)
    ids = pitcher_ids()
    print("  %d pitchers" % len(ids), file=sys.stderr)

    A_fp, A_ip, A_k, A_er = [], [], [], []
    P_fp = {h: [] for h in HALF_LIVES}       # projected FP per half-life
    P_ip = {h: [] for h in HALF_LIVES}
    P_k  = {h: [] for h in HALF_LIVES}
    P_er = {h: [] for h in HALF_LIVES}

    for i, pid in enumerate(sorted(ids), 1):
        gs_rows = starts(pid)
        time.sleep(0.05)
        if len(gs_rows) <= MIN_PRIOR:
            continue
        prior = []
        for r in gs_rows:
            if len(prior) >= MIN_PRIOR:
                accf = weighted_acc(prior, 0)
                if accf["ip"] > 0:
                    af, outs, qs = actual_fp(r["ip"], r["so"], r["er"], r["win"])
                    A_fp.append(af); A_ip.append(r["ip"]); A_k.append(r["so"]); A_er.append(r["er"])
                    for h in HALF_LIVES:
                        acc = accf if h == 0 else weighted_acc(prior, h)
                        pr = project(acc)
                        P_fp[h].append(pr["fp"]); P_ip[h].append(pr["xIP"])
                        P_k[h].append(pr["xK"]); P_er[h].append(pr["xER"])
            prior.append(r)
        if i % 50 == 0:
            print("  %d/%d (%d test starts)" % (i, len(ids), len(A_fp)), file=sys.stderr)

    n = len(A_fp)
    if n < 10:
        print("not enough test starts (%d) -- run later in the season." % n)
        return

    # familiar baseline block (flat weights = h=0), so output matches the prior format
    h0 = 0
    print("\n=== PITCHER FS BACKTEST (%d leakage-free starts, %d+ prior) ===" % (n, MIN_PRIOR))
    print("mean ACTUAL FP/start : %.2f" % (sum(A_fp) / n))
    print("mean PROJ   FP/start : %.2f   (bias %+.2f)"
          % (sum(P_fp[h0]) / n, (sum(P_fp[h0]) - sum(A_fp)) / n))
    print("r(proj FP, actual FP): %.3f" % pearson(P_fp[h0], A_fp))
    print("top-decile LIFT      : %.2fx" % decile_lift(P_fp[h0], A_fp))
    print("-- per component --")
    print("r(xIP, actual IP)    : %.3f" % pearson(P_ip[h0], A_ip))
    print("r(xK,  actual K)     : %.3f" % pearson(P_k[h0], A_k))
    print("r(xER, actual ER)    : %.3f" % pearson(P_er[h0], A_er))

    # recency-weighting sweep
    print("\n=== RECENCY-WEIGHTING SWEEP (half-life in starts; 'flat' = baseline) ===")
    print("  weight    r(projFP)   lift    r(xIP)   r(xK)    bias")
    for h in HALF_LIVES:
        print("  %-7s   %.3f      %.2fx    %.3f    %.3f    %+.2f"
              % (hl_label(h),
                 pearson(P_fp[h], A_fp),
                 decile_lift(P_fp[h], A_fp),
                 pearson(P_ip[h], A_ip),
                 pearson(P_k[h], A_k),
                 (sum(P_fp[h]) - sum(A_fp)) / n))
    print("\nRead: pick the half-life with the best r(projFP)/lift. 'flat' must match")
    print("the prior run. A shorter half-life weights recent form more heavily.")


if __name__ == "__main__":
    main()
