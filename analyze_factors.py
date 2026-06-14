# -*- coding: utf-8 -*-
"""Prove each entry filter's predictive power from cross-sectional factor snapshots.

Reads ``<profile>/factor_snapshots.jsonl`` (+ rotated archives) written by the
live bot's factor-scoring engine, joins forward returns from
``external_price_feed.csv`` (htx_mid), and reports, per side and horizon:

  * Fama-MacBeth regression of forward returns on the per-symbol z-scores:
    average slope per filter with Newey-West t-stat (autocorrelation-robust),
    plus a Benjamini-Hochberg FDR pass over the filters.
  * Univariate top-vs-bottom fractile spread per filter (net of round-trip cost).
  * Composite top-K portfolio: realised forward return, win rate, per-trade
    Sharpe and the implied trade count for 80% power (net of round-trip cost).

Estimation uses the WHOLE cross-section every snapshot, so it does not need any
live trades — it works the moment snapshots start accruing. Round-trip cost only
affects the portfolio / fractile P&L (a constant is absorbed by the FM intercept).

Usage:
    python analyze_factors.py [--horizons 3600,7200,14400] [--cost-bps 10]
                              [--since "2026-06-13 20:35"] [--top-k 3]
"""
import argparse
import bisect
import csv
import glob
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\HTX-Crypto-bot_1.3"
TZ = timezone(timedelta(hours=3))


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def snapshot_records(prof):
    files = sorted(glob.glob(rf"{ROOT}\{prof}\csv_archive\factor_snapshots.*.jsonl")) + [
        rf"{ROOT}\{prof}\factor_snapshots.jsonl"
    ]
    for fp in files:
        try:
            fh = open(fp, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def price_series(prof, since):
    series = defaultdict(list)
    files = sorted(glob.glob(rf"{ROOT}\{prof}\csv_archive\external_price_feed.*.csv")) + [
        rf"{ROOT}\{prof}\external_price_feed.csv"
    ]
    for fp in files:
        try:
            reader = csv.DictReader(open(fp, encoding="utf-8", errors="replace"))
        except FileNotFoundError:
            continue
        for r in reader:
            mid, t = f(r.get("htx_mid")), f(r.get("ts"))
            if mid > 0 and t >= since - 600:
                series[r["symbol"]].append((t, mid))
    for s in series.values():
        s.sort()
    return series


def fwd_return(series, sym, t0, horizon, side):
    pts = series.get(sym)
    if not pts:
        return None
    ts = [p[0] for p in pts]
    i = bisect.bisect_left(ts, t0)
    if i >= len(pts):
        return None
    base = pts[i][1]
    j = bisect.bisect_right(ts, t0 + horizon) - 1
    if j <= i:
        return None
    last = pts[j][1]
    if base <= 0:
        return None
    return (base - last) / base if side == "short" else (last - base) / base


def newey_west_tstat(values):
    """Mean and NW autocorrelation-robust t-stat of a per-period series."""
    arr = np.asarray(values, dtype=float)
    n = arr.size
    if n < 3:
        return (float(arr.mean()) if n else 0.0), 0.0, n
    mean = float(arr.mean())
    dev = arr - mean
    lag = max(1, int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0))))
    lag = min(lag, n - 1)
    gamma0 = float((dev * dev).mean())
    s = gamma0
    for k in range(1, lag + 1):
        cov = float((dev[k:] * dev[:-k]).mean())
        s += 2.0 * (1.0 - k / (lag + 1.0)) * cov
    if s <= 0:
        return mean, 0.0, n
    se = math.sqrt(s / n)
    return mean, (mean / se if se > 0 else 0.0), n


def two_sided_p(t):
    return math.erfc(abs(t) / math.sqrt(2.0))


def bh_fdr(pvals, q=0.10):
    """Return set of indices rejected under Benjamini-Hochberg at level q."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    threshold_rank = -1
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= rank / m * q:
            threshold_rank = rank
    if threshold_rank < 0:
        return set()
    return set(order[:threshold_rank])


def analyze(prof, horizons, cost, since, top_k, min_names):
    records = [
        d
        for d in snapshot_records(prof)
        if f(d.get("ts")) >= since and (d.get("rows") or [])
    ]
    print("=" * 84)
    print(f"  {prof.upper()}  — factor snapshots: {len(records)}")
    print("=" * 84)
    if not records:
        print("  (no factor snapshots yet — run the bot with FACTOR_SCORING_ENABLED on)\n")
        return
    metrics = records[-1].get("metrics") or []
    if not metrics:
        print("  (snapshots carry no metric names)\n")
        return
    series = price_series(prof, since)
    span = (
        f"{datetime.fromtimestamp(f(records[0]['ts']), TZ):%d.%m %H:%M}"
        f" … {datetime.fromtimestamp(f(records[-1]['ts']), TZ):%d.%m %H:%M}"
    )
    print(f"  window: {span}   metrics: {', '.join(metrics)}   round-trip cost: {cost*1e4:.1f} bps\n")

    for H in horizons:
        # Per-snapshot cross-sections -> Fama-MacBeth slopes + fractile/composite P&L.
        fm_slopes = defaultdict(list)            # metric -> [beta_t]
        fractile_spread = defaultdict(list)      # metric -> [top-bottom return]
        composite_ret = []                       # top-K composite portfolio per-trade returns
        composite_periods = []                   # per-snapshot mean composite-topK return
        n_obs = 0
        for d in records:
            t0 = f(d.get("ts"))
            rows = d.get("rows") or []
            ys, zmat, comps = [], [], []
            for row in rows:
                sym = row.get("s")
                r = fwd_return(series, sym, t0, H, prof)
                if r is None:
                    continue
                z = row.get("z") or {}
                ys.append(r)
                zmat.append([f(z.get(m)) for m in metrics])
                comps.append(f(row.get("c")))
            n = len(ys)
            if n < min_names:
                continue
            n_obs += n
            y = np.asarray(ys)
            Z = np.asarray(zmat)
            # ---- Fama-MacBeth cross-sectional OLS with intercept ----
            X = np.column_stack([np.ones(n), Z])
            if np.linalg.matrix_rank(X) == X.shape[1]:
                try:
                    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
                    for i, m in enumerate(metrics):
                        fm_slopes[m].append(float(beta[i + 1]))
                except np.linalg.LinAlgError:
                    pass
            # ---- univariate top/bottom fractile spread per metric ----
            frac = max(1, n // 5)
            for i, m in enumerate(metrics):
                order = np.argsort(Z[:, i])
                bottom = y[order[:frac]].mean()
                top = y[order[-frac:]].mean()
                fractile_spread[m].append(float(top - bottom) - 2 * cost)
            # ---- composite top-K portfolio (net of round-trip cost) ----
            comp_order = np.argsort(comps)[::-1]
            picks = comp_order[: max(1, top_k)]
            picked = [float(y[k]) - cost for k in picks]
            composite_ret.extend(picked)
            composite_periods.append(float(np.mean(picked)))

        hours = H / 3600.0
        print(f"  ── horizon {hours:g}h   cross-section obs={n_obs} ──")
        # Fama-MacBeth table with BH-FDR
        names, means, ts_, ps = [], [], [], []
        for m in metrics:
            mean, t, T = newey_west_tstat(fm_slopes[m])
            names.append(m)
            means.append(mean)
            ts_.append(t)
            ps.append(two_sided_p(t))
        rejected = bh_fdr(ps, q=0.10)
        print(f"    {'filter':14s} {'FM_slope':>11} {'NW_t':>7} {'p':>8} {'periods':>7}  BH(10%)")
        for i, m in enumerate(names):
            mark = "  *" if i in rejected else ""
            spread_mean, spread_t, _ = newey_west_tstat(fractile_spread[m])
            print(
                f"    {m:14s} {means[i]:+11.5f} {ts_[i]:+7.2f} {ps[i]:8.3f} "
                f"{len(fm_slopes[m]):7d}{mark}   frac_spread={spread_mean*100:+.3f}% (t={spread_t:+.2f})"
            )
        # Composite portfolio summary
        if composite_ret:
            arr = np.asarray(composite_ret)
            avg = float(arr.mean())
            sd = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
            win = int((arr > 0).sum())
            sr = avg / sd if sd > 0 else 0.0
            need = int(math.ceil(7.85 / (sr * sr))) if sr != 0 else -1
            pmean, pt, _ = newey_west_tstat(composite_periods)
            print(
                f"    composite top-{top_k}: trades={arr.size} avgRet={avg*100:+.3f}% "
                f"win={win}/{arr.size} SR/trade={sr:+.3f} "
                f"n@80%power={'n/a' if need < 0 else need}  (per-period NW t={pt:+.2f})"
            )
        print()
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="3600,7200,14400")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--since", default="")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--min-names", type=int, default=10)
    ap.add_argument("--profiles", default="long,short")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    cost = args.cost_bps / 1e4
    since = 0.0
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()

    for prof in [p.strip() for p in args.profiles.split(",") if p.strip()]:
        analyze(prof, horizons, cost, since, args.top_k, args.min_names)


if __name__ == "__main__":
    main()
