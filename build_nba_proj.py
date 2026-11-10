#!/usr/bin/env python3
"""
build_nba_proj.py  —  NBA player-prop projection engine (SHADOW MODE, compute + snapshot only)

Reads the per-game logs already inside nba_stats.json and produces, for every
player x stat, a matchup-FREE projection with mean, predictive SD, and the
parameters of an analytic P(over) distribution. Writes ONE immutable snapshot
row per (player, stat). No grading yet (that's the next script).

Design source: projection_build_spec_v1.md (consolidated from the 4-round AI review).
HARD RULE: baseline is matchup-free. No opponent defensive adjustment here.

Pure-Python, no third-party deps (math only), so the special functions port
straight to the client JS later and the GitHub Action needs nothing extra.
"""

import json, math, os, sys, time, datetime, urllib.request

MODEL_VERSION     = "nba_proj_v1"
PARAMETER_VERSION = "p1_2026-11-10"

# ---------------------------------------------------------------------------
# PER-STAT CONFIG  (all values are STARTING guesses — tune against shadow data)
#   log_key     : field in players[].log
#   half_life   : EWMA half-life in games for the rate
#   k_eff       : shrinkage strength in EFFECTIVE GAMES  (z = nEff/(nEff+k_eff))
#   infl_rate   : day-1 variance inflation on the rate component
#   infl_min    : day-1 variance inflation on the minutes component
#   sd_floor    : base predictive-SD floor (minutes-scaled at use)
#   dist        : "gamma" (points) or "nbinom" (counts, with poisson/dnorm fallback)
# ---------------------------------------------------------------------------
STATS = {
    "pts":  {"log_key": "pts",  "half_life": 6, "k_eff": 7,  "infl_rate": 1.50, "infl_min": 1.75, "sd_floor": 4.0, "dist": "gamma"},
    "reb":  {"log_key": "reb",  "half_life": 8, "k_eff": 7,  "infl_rate": 1.60, "infl_min": 1.75, "sd_floor": 2.0, "dist": "nbinom"},
    "ast":  {"log_key": "ast",  "half_life": 6, "k_eff": 7,  "infl_rate": 1.75, "infl_min": 1.75, "sd_floor": 1.8, "dist": "nbinom"},
    "fg3m": {"log_key": "fg3m", "half_life": 7, "k_eff": 9,  "infl_rate": 1.75, "infl_min": 1.75, "sd_floor": 1.0, "dist": "nbinom"},
    "stl":  {"log_key": "stl",  "half_life": 10,"k_eff": 12, "infl_rate": 2.00, "infl_min": 1.75, "sd_floor": 0.7, "dist": "nbinom"},
    "blk":  {"log_key": "blk",  "half_life": 10,"k_eff": 12, "infl_rate": 2.00, "infl_min": 1.75, "sd_floor": 0.7, "dist": "nbinom"},
    "tov":  {"log_key": "tov",  "half_life": 7, "k_eff": 9,  "infl_rate": 1.75, "infl_min": 1.75, "sd_floor": 0.9, "dist": "nbinom"},
}

RATE_WINDOW      = 12      # games used for the recent rate
WINSOR_PCT       = 0.20    # clamp per-game rates to [p20, p80] of the window
MIN_HALF_LIFE    = 3       # EWMA half-life (games) for recent minutes
MINUTES_CAP      = 44.0
BASELINE_SEASON_MIN   = 400.0   # >= this -> blend 0.7 season + 0.3 recent-25
POSITIONAL_SEASON_MIN = 150.0   # < this  -> use positional baseline
BREAKOUT_SD_MULT = 1.5    # |rateL5 - baseline| > 1.5*sigma_recent  (plus minutes up)
BREAKOUT_Z_FLOOR = 0.85

# ---------------------------------------------------------------------------
# Small math helpers
# ---------------------------------------------------------------------------
def _percentile(sorted_vals, q):
    if not sorted_vals: return 0.0
    if len(sorted_vals) == 1: return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos)); frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac

def winsorize(vals, pct):
    """Clamp each value to the [pct, 1-pct] percentile band of the list."""
    if len(vals) < 3: return vals[:], (min(vals) if vals else 0.0), (max(vals) if vals else 0.0)
    s = sorted(vals)
    lo = _percentile(s, pct); hi = _percentile(s, 1 - pct)
    return [min(max(v, lo), hi) for v in vals], lo, hi

def weighted_mean(vals, wts):
    sw = sum(wts)
    return (sum(v * w for v, w in zip(vals, wts)) / sw) if sw > 0 else 0.0

def weighted_var(vals, wts, mu):
    """Weighted predictive (population) variance — game-to-game spread, not SE of mean."""
    sw = sum(wts)
    if sw <= 0: return 0.0
    return sum(w * (v - mu) ** 2 for v, w in zip(vals, wts)) / sw

def kish_neff(wts):
    """Effective sample size (Kish) for a weight vector."""
    s1 = sum(wts); s2 = sum(w * w for w in wts)
    return (s1 * s1 / s2) if s2 > 0 else 0.0

def small_sample_factor(n_eff):
    return 1.0 + 0.5 * max(0.0, (10.0 - min(n_eff, 10.0)) / 10.0)

# --- regularized lower incomplete gamma  P(a,x) = gamma(a,x)/Gamma(a) --------
def gammainc_lower_reg(a, x):
    if x <= 0 or a <= 0: return 0.0
    if x < a + 1.0:                      # series expansion
        ap = a; s = 1.0 / a; d = s
        for _ in range(500):
            ap += 1.0; d *= x / ap; s += d
            if abs(d) < abs(s) * 1e-12: break
        return s * math.exp(-x + a * math.log(x) - math.lgamma(a))
    else:                                # continued fraction (upper), return 1-Q
        tiny = 1e-300
        b = x + 1.0 - a; c = 1.0 / tiny; d = 1.0 / b; h = d
        for i in range(1, 500):
            an = -i * (i - a); b += 2.0
            d = an * d + b;  d = tiny if abs(d) < tiny else d
            c = b + an / c;  c = tiny if abs(c) < tiny else c
            d = 1.0 / d; delt = d * c; h *= delt
            if abs(delt - 1.0) < 1e-12: break
        q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
        return 1.0 - q

def gamma_cdf(x, shape, scale):
    if x <= 0: return 0.0
    return gammainc_lower_reg(shape, x / scale)

def prob_over_gamma(line, mean, var):
    """P(stat > line) for points via moment-matched gamma, continuity-corrected."""
    if mean <= 0 or var <= 0: return 0.0
    shape = mean * mean / var; scale = var / mean
    return 1.0 - gamma_cdf(line + 0.5, shape, scale)

def prob_over_nbinom(line, mean, var):
    """P(stat > line) for counts via method-of-moments negative binomial.
       Fallbacks: var<=mean -> poisson-ish via discretized normal; keeps it analytic."""
    if mean <= 0: return 0.0
    if var <= mean * 1.02:               # under/near-dispersed -> discretized normal
        sd = math.sqrt(max(var, 1e-9))
        from_z = (line + 0.5 - mean) / sd
        return 1.0 - 0.5 * (1.0 + math.erf(from_z / math.sqrt(2)))
    r = mean * mean / (var - mean); p = r / (r + mean)
    # cumulative P(X <= floor(line)) via PMF recurrence
    k_target = int(math.floor(line + 1e-9))
    pmf = p ** r; cdf = pmf
    for k in range(0, k_target):
        pmf *= (k + r) / (k + 1) * (1 - p); cdf += pmf
    return max(0.0, min(1.0, 1.0 - cdf))

# ---------------------------------------------------------------------------
# Core per-(player, stat) projection
# ---------------------------------------------------------------------------
def project_stat(player, stat, cfg, pos_baseline_rate):
    log = player.get("log") or []
    # sort oldest -> newest; keep games with real minutes
    games = sorted([g for g in log if g.get("min", 0) and g["min"] > 0],
                   key=lambda g: g.get("d", ""))
    if len(games) < 2:
        return None
    lk = cfg["log_key"]
    mins  = [float(g["min"]) for g in games]
    stats = [float(g.get(lk, 0) or 0) for g in games]
    rates = [s / m for s, m in zip(stats, mins)]

    # --- winsorize per-game rates, then EWMA minute-weighted ----------------
    rwindow = min(RATE_WINDOW, len(games))
    r_w = rates[-rwindow:]; m_w = mins[-rwindow:]
    r_wins, w_lo, w_hi = winsorize(r_w, WINSOR_PCT)
    hl = cfg["half_life"]; n = len(r_wins)
    ew = [2.0 ** (-(n - 1 - i) / hl) for i in range(n)]       # newest heaviest
    a  = [e * mm for e, mm in zip(ew, m_w)]                   # minute+recency weight
    rate_raw = weighted_mean(r_wins, a)
    var_rate = weighted_var(r_wins, a, rate_raw)
    n_eff_rate = kish_neff(a)

    # --- season + baseline --------------------------------------------------
    p_min = float(player.get("min", 0) or 0); p_gp = float(player.get("gp", 0) or 0)
    # season rate: use the season aggregate when present; else fall back to the full
    # game log (e.g. fg3m has no season aggregate — only fg3a does), so the baseline
    # isn't silently zero and over-shrinking the projection.
    agg = player.get(lk) if lk in player else (player.get(stat) if stat in player else None)
    if agg is not None and p_min > 0:
        season_rate = float(agg or 0) / p_min
    elif sum(mins) > 0:
        season_rate = sum(stats) / sum(mins)
    else:
        season_rate = rate_raw
    season_minutes = p_min * p_gp
    recent25 = rates[-25:]
    recent25_rate = sum(recent25) / len(recent25) if recent25 else season_rate
    if season_minutes < POSITIONAL_SEASON_MIN and pos_baseline_rate is not None:
        baseline = pos_baseline_rate
    elif season_minutes >= BASELINE_SEASON_MIN:
        baseline = 0.7 * season_rate + 0.3 * recent25_rate
    else:
        baseline = season_rate

    # --- shrinkage with breakout override -----------------------------------
    z = n_eff_rate / (n_eff_rate + cfg["k_eff"]) if (n_eff_rate + cfg["k_eff"]) > 0 else 0.0
    l5 = rates[-5:]; rate_l5 = sum(l5) / len(l5) if l5 else rate_raw
    sigma_recent = (weighted_var(r_wins, [1.0] * n, sum(r_wins) / n)) ** 0.5 if n else 0.0
    l3m = mins[-3:]; prior = mins[-8:-3] or mins[:-3] or mins
    minutes_up = (sum(l3m) / len(l3m) - sum(prior) / len(prior)) >= 4.0 if prior else False
    rate_change_flag = bool(sigma_recent > 0 and abs(rate_l5 - baseline) > BREAKOUT_SD_MULT * sigma_recent and minutes_up)
    if rate_change_flag:
        z = max(z, BREAKOUT_Z_FLOOR)
    final_rate = z * rate_raw + (1 - z) * baseline

    # --- projected minutes (log-based v1; OUT handling = TODO when status in feed)
    mm = mins[-8:]
    hlm = MIN_HALF_LIFE; nm = len(mm)
    ewm = [2.0 ** (-(nm - 1 - i) / hlm) for i in range(nm)]
    rec_min_ewma = weighted_mean(mm, ewm)
    last3 = sorted(mins[-3:]); med3 = last3[len(last3)//2] if last3 else rec_min_ewma
    proj_min = 0.70 * rec_min_ewma + 0.30 * med3
    proj_min = max(0.0, min(MINUTES_CAP, proj_min))
    last10 = mins[-10:]
    mu_m = sum(last10) / len(last10)
    var_minutes = sum((x - mu_m) ** 2 for x in last10) / len(last10)
    minutes_sd = max(3.0, math.sqrt(var_minutes) * 0.8)
    n_eff_minutes = kish_neff(ewm)

    # --- variance of product, per-component inflation + small-sample + floor -
    vr = var_rate * cfg["infl_rate"] * small_sample_factor(n_eff_rate)
    vm = var_minutes * cfg["infl_min"] * small_sample_factor(n_eff_minutes)
    var_stat = proj_min ** 2 * vr + final_rate ** 2 * vm      # cov = 0 in v1
    sd_floor = cfg["sd_floor"] * math.sqrt(max(0.25, min(1.75, proj_min / 28.0)))
    sd = max(math.sqrt(max(var_stat, 0.0)), sd_floor)
    mean = final_rate * proj_min

    # --- distribution params (P(over) computed later, when a line is attached)
    if cfg["dist"] == "gamma":
        dist_var = sd * sd
        dp1 = mean * mean / dist_var if dist_var > 0 else 0.0   # shape
        dp2 = dist_var / mean if mean > 0 else 0.0              # scale
        dist_family = "gamma"
    else:
        dist_var = sd * sd
        if dist_var > mean * 1.02 and mean > 0:
            dp1 = mean * mean / (dist_var - mean)               # r
            dp2 = dp1 / (dp1 + mean)                            # p
            dist_family = "nbinom"
        else:
            dp1 = mean; dp2 = sd                                # discretized-normal fallback
            dist_family = "dnorm"

    return {
        "stat_type": stat,
        "model_version": MODEL_VERSION, "parameter_version": PARAMETER_VERSION,
        "last_log_date": games[-1].get("d"),   # newest game seen at snapshot time -> grade the next game after this
        "eligible_game_count": len(games), "recent_window_game_count": rwindow,
        "season_minutes_sample": round(season_minutes, 1),
        "n_eff_rate": round(n_eff_rate, 3), "n_eff_minutes": round(n_eff_minutes, 3),
        "small_sample_flag": bool(min(n_eff_rate, n_eff_minutes) < 5),
        "rate_raw": round(rate_raw, 5), "winsor_lower": round(w_lo, 5), "winsor_upper": round(w_hi, 5),
        "season_baseline_rate": round(season_rate, 5), "recent25_baseline_rate": round(recent25_rate, 5),
        "shrink_weight_z": round(z, 4), "rate_change_flag": rate_change_flag, "final_rate": round(final_rate, 5),
        "raw_var_rate": round(var_rate, 6), "inflated_var_rate": round(vr, 6), "rate_variance_multiplier": cfg["infl_rate"],
        "team_projected_minutes": None, "recent_minutes_ewma": round(rec_min_ewma, 2),
        "projected_minutes": round(proj_min, 2), "minutes_sd": round(minutes_sd, 2),
        "raw_var_minutes": round(var_minutes, 4), "inflated_var_minutes": round(vm, 4), "minutes_variance_multiplier": cfg["infl_min"],
        "pace_factor": 1.0,
        "variance_floor_value": round(sd_floor, 3), "variance_floor_applied": bool(sd <= sd_floor + 1e-9),
        "projected_stat_mean": round(mean, 3), "projected_stat_sd": round(sd, 3),
        "distribution_family": dist_family, "distribution_parameter_1": round(dp1, 5), "distribution_parameter_2": round(dp2, 5),
    }

def prob_over(row, line):
    """Analytic P(over) for a row + a sportsbook line. Used at grading/join time."""
    mean = row["projected_stat_mean"]; sd = row["projected_stat_sd"]; var = sd * sd
    if row["distribution_family"] == "gamma":
        return prob_over_gamma(line, mean, var)
    if row["distribution_family"] == "nbinom":
        return prob_over_nbinom(line, mean, var)
    z = (line + 0.5 - mean) / sd if sd > 0 else 0.0
    return 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2)))

# ---------------------------------------------------------------------------
# Positional baselines (fallback for tiny-sample players)
# ---------------------------------------------------------------------------
def positional_baselines(players):
    buckets = {}
    for p in players.values():
        pos = (p.get("pos") or "U").split("-")[0]
        mn = float(p.get("min", 0) or 0)
        if mn <= 0: continue
        log = p.get("log") or []
        for stat, cfg in STATS.items():
            key = cfg["log_key"]
            if key in p or stat in p:
                val = float(p.get(key if key in p else stat, 0) or 0); rate = val / mn
            else:  # no season aggregate (e.g. fg3m) -> derive from the game log
                sm = sum(float(g.get("min", 0) or 0) for g in log)
                sv = sum(float(g.get(key, 0) or 0) for g in log)
                rate = (sv / sm) if sm > 0 else 0.0
            buckets.setdefault((pos, stat), []).append(rate)
    out = {}
    for (pos, stat), arr in buckets.items():
        arr.sort(); out[(pos, stat)] = arr[len(arr) // 2]     # median rate
    return out

# ---------------------------------------------------------------------------
def load_stats():
    path = os.environ.get("NBA_STATS", "nba_stats.json")
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    url = "https://raw.githubusercontent.com/cushplayerprops/cush-data/main/nba_stats.json?t=%d" % int(time.time()*1000)
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "c"}), timeout=90).read().decode())

def main():
    d = load_stats()
    players = d.get("players", {})
    game_date = d.get("gameDate") or datetime.date.today().isoformat()
    pos_base = positional_baselines(players)
    snapshot = {
        "schema_version": 1, "model_version": MODEL_VERSION, "parameter_version": PARAMETER_VERSION,
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_stats_updated": d.get("updated"), "game_date": game_date, "rows": [],
    }
    for pid, p in players.items():
        pos = (p.get("pos") or "U").split("-")[0]
        for stat, cfg in STATS.items():
            row = project_stat(p, stat, cfg, pos_base.get((pos, stat)))
            if row is None: continue
            row.update({"player_id": pid, "player_name": p.get("name"),
                        "team_id": p.get("teamId"), "team_abbr": p.get("teamAbbr"),
                        "game_date": game_date})
            snapshot["rows"].append(row)
    out_path = os.environ.get("NBA_PROJ_OUT", "nba_proj_snapshot.json")
    with open(out_path, "w") as f: json.dump(snapshot, f, separators=(",", ":"))
    print("WROTE", out_path, "rows:", len(snapshot["rows"]))
    return snapshot

if __name__ == "__main__":
    main()
