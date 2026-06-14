# -*- coding: utf-8 -*-
"""Counterfactual: how many extra entries the two pullback levers unlock, and their forward 2h return.

Lever 1 (cross-age): EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES 360 -> 480/600  (6 -> 8/10 candles @1h)
Lever 2 (gap):       EMA_PULLBACK_RECOVERY_GAP                 0.001 -> 0.0007

Recompute pullback_valid from logged signal fields, flip ema_entry_valid, drop the ema_entry+pullback
penalties, and re-test weighted_score >= min. Then forward-return the unlocked candidates from htx_mid.
"""
import glob, json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ROOT = r"D:\HTX-Crypto-bot_1.3"
TZ = timezone(timedelta(hours=3))
RESTART = datetime(2026, 6, 13, 20, 35, 44, tzinfo=TZ).timestamp()
HORIZON = 7200  # 2h


def jrecs(prof):
    files = sorted(glob.glob(rf"{ROOT}\{prof}\csv_archive\signal_analytics.*.jsonl")) + [
        rf"{ROOT}\{prof}\signal_analytics.jsonl"]
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


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ---- price series for forward return (htx_mid, both feeds merged) ----
series = defaultdict(list)
import csv
for prof in ("long", "short"):
    for fp in sorted(glob.glob(rf"{ROOT}\{prof}\csv_archive\external_price_feed.*.csv")) + [
            rf"{ROOT}\{prof}\external_price_feed.csv"]:
        try:
            for r in csv.DictReader(open(fp, encoding="utf-8", errors="replace")):
                mid, t = f(r.get("htx_mid")), f(r.get("ts"))
                if mid > 0 and t >= RESTART - 600:
                    series[r["symbol"]].append((t, mid))
        except FileNotFoundError:
            pass
for s in series.values():
    s.sort()


def fwd(sym, t0, side):
    pts = series.get(sym, [])
    base = next((p for t, p in pts if t >= t0), None)
    if base is None:
        return None
    fut = [p for t, p in pts if t0 <= t <= t0 + HORIZON]
    if len(fut) < 2:
        return None
    last = fut[-1]
    if side == "short":
        return (base - last) / base, (base - min(fut)) / base
    return (last - base) / base, (max(fut) - base) / base


SCEN = {
    "BASE         (gap .0010, age 6)": (0.0010, 6),
    "L1 age->8    (gap .0010, age 8)": (0.0010, 8),
    "L1 age->10   (gap .0010, age 10)": (0.0010, 10),
    "L2 gap->.0007(gap .0007, age 6)": (0.0007, 6),
    "BOTH age8    (gap .0007, age 8)": (0.0007, 8),
    "BOTH age10   (gap .0007, age 10)": (0.0007, 10),
}


def qualifies(sig, gap_thr, age_max):
    """Re-test pullback gate + weighted score under scenario params. Returns (passed, new_weighted)."""
    if not bool(sig.get("macro_valid")):
        return False, None
    had = bool(sig.get("pullback_had_pullback"))
    gap = f(sig.get("pullback_recovery_gap"))
    ca = f(sig.get("pullback_cross_age_candles"), -1)
    new_pb = had and (gap + 1e-12 >= gap_thr) and (0 <= ca <= age_max)
    if not new_pb:
        return False, None
    pens = sig.get("entry_weighted_penalties") or {}
    drop = f(pens.get("ema_entry")) + f(pens.get("pullback"))
    new_w = f(sig.get("entry_weighted_score")) + drop
    mn = f(sig.get("entry_weighted_score_min"), 0.025)
    return (new_w + 1e-12 >= mn), new_w


for prof in ("long", "short"):
    side = prof
    # collect per-scenario unlocked candidate instances (dedupe sym per 30min)
    rows = list(jrecs(prof))
    print("=" * 78)
    print(f"  {prof.upper()}  — counterfactual entries since {datetime.fromtimestamp(RESTART,TZ):%d.%m %H:%M}")
    print("=" * 78)
    print(f"  {'scenario':30s} {'entries':>7} {'uniq':>5} {'wData':>5} {'avgRet2h':>9} {'win':>7} {'avgMFE':>8}")
    for name, (gap_thr, age_max) in SCEN.items():
        seen = {}
        cands = []
        for d in rows:
            if d.get("decision") != "entry_gate_checked":
                continue
            t = f(d.get("ts"))
            if t < RESTART:
                continue
            sig = d.get("signal") or {}
            if not sig:
                continue
            ok, _ = qualifies(sig, gap_thr, age_max)
            if not ok:
                continue
            sym = (d.get("symbol") or "?").split("/")[0]
            full = d.get("symbol") or ""
            key = (sym, int(t // 1800))
            if key in seen:
                continue
            seen[key] = 1
            cands.append((t, sym, full))
        n = len(cands)
        rets, mfes = [], []
        for t, sym, full in cands:
            r = fwd(full, t, side)
            if r:
                rets.append(r[0]); mfes.append(r[1])
        if rets:
            avg = sum(rets) / len(rets) * 100
            win = sum(1 for x in rets if x > 0)
            mfe = sum(mfes) / len(mfes) * 100
            print(f"  {name:30s} {n:7d} {n:5d} {len(rets):5d} {avg:+8.2f}% {win:3d}/{len(rets):<3d} {mfe:+7.2f}%")
        else:
            print(f"  {name:30s} {n:7d} {n:5d} {0:5d} {'  n/a':>9} {'   -':>7} {'   -':>8}")
    print()
