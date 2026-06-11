# -*- coding: utf-8 -*-
"""Day-2 deep dive: entry gate blocks, MFE/MAE per cycle, churn, time exits, macro."""
import csv
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\HTX-Crypto-bot_1.3"
CUTOFF = 1781109600.0


def lt(t):
    return datetime.fromtimestamp(float(t) + 3 * 3600, tz=timezone.utc).strftime("%d.%m %H:%M")


def rows(pattern_arch, current):
    for fp in sorted(glob.glob(pattern_arch)) + [current]:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    yield row
        except FileNotFoundError:
            pass


print("#" * 70)
print("# A. ENTRY BLOCK REASONS after cutoff (signal_analytics jsonl, decision=entry_gate_checked)")
print("#" * 70)
for prof in ("long", "short"):
    files = sorted(glob.glob(rf"{ROOT}\{prof}\csv_archive\signal_analytics.*.jsonl")) + [
        rf"{ROOT}\{prof}\signal_analytics.jsonl"]
    blocks = Counter()
    gate_fail = Counter()
    n = 0
    for fp in files:
        try:
            fh = open(fp, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("ts") or (d.get("external_context") or {}).get("ts") or 0
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = 0
            if ts and ts < CUTOFF:
                continue
            br = d.get("block_reason") or ""
            if not br:
                continue
            n += 1
            head = br.split(";", 1)[0]
            blocks[head] += 1
            # which boolean gates failed
            for m in re.finditer(r"(\w+_valid)=0", br):
                gate_fail[m.group(1)] += 1
        fh.close()
    print(f"--- {prof}: {n} blocked checks after cutoff")
    for k, c in blocks.most_common(12):
        print(f"    {k:55s} {c}")
    print(f"    failed gates within blocks:")
    for k, c in gate_fail.most_common(12):
        print(f"      {k:50s} {c}")

print()
print("#" * 70)
print("# B. PRICE SERIES MFE/MAE for short cycles closed after cutoff + open positions")
print("#" * 70)
# price series per symbol from external_price_feed (htx_mid)
series = defaultdict(list)
for prof in ("long", "short"):
    for r in rows(rf"{ROOT}\{prof}\csv_archive\external_price_feed.*.csv",
                  rf"{ROOT}\{prof}\external_price_feed.csv"):
        try:
            mid = float(r["htx_mid"])
            t = float(r["ts"])
        except (ValueError, KeyError):
            continue
        if mid > 0:
            series[r["symbol"]].append((t, mid))
for s in series.values():
    s.sort()
print("symbols in feed:", len(series), "| sample sizes:",
      {k.split('/')[0]: len(v) for k, v in list(series.items())[:8]})

def mfe_mae(sym, side, t0, t1, px0):
    pts = [(t, p) for t, p in series.get(sym, []) if t0 <= t <= t1]
    if not pts or not px0:
        return None
    sign = 1 if side == "long" else -1
    rets = [(sign * (p - px0) / px0, t) for t, p in pts]
    return max(rets), min(rets), len(pts)

cyc = list(rows(rf"{ROOT}\short\csv_archive\bot_futures_short_cycle_stats.*.csv",
                rf"{ROOT}\short\bot_futures_short_cycle_stats.csv"))
print(f"\n{'sym':8s} {'opened':11s} {'pnl':>7s} {'MFE%':>7s} {'MAE%':>7s} {'pts':>4s}  close_reason")
for r in cyc:
    if float(r["closed_at"]) < CUTOFF:
        continue
    sym = r["symbol"]
    t0, t1 = float(r["opened_at"]), float(r["closed_at"])
    px0 = float(r["average_entry_price"])
    res = mfe_mae(sym, "short", t0, t1, px0)
    if res:
        (mfe, _), (mae, _), npts = res
        print(f"{sym.split('/')[0]:8s} {lt(t0):11s} {float(r['realized_pnl_quote']):+7.2f} "
              f"{mfe*100:+7.2f} {mae*100:+7.2f} {npts:4d}  {r['close_reason']}")
    else:
        print(f"{sym.split('/')[0]:8s} {lt(t0):11s} {float(r['realized_pnl_quote']):+7.2f}    no price data  {r['close_reason']}")

# open positions
st = json.load(open(rf"{ROOT}\short\bot_futures_short_state.json", encoding="utf-8"))
now = datetime.now(timezone.utc).timestamp()
print("\nOPEN (short):")
for sym, s in sorted(st.items()):
    if not float(s.get("position_size") or 0):
        continue
    t0 = float(s.get("cycle_opened_at") or 0)
    px0 = float(s.get("entry_price") or 0)
    res = mfe_mae(sym, "short", t0, now, px0)
    if res:
        (mfe, tmfe), (mae, tmae), npts = res
        print(f"{sym.split('/')[0]:8s} {lt(t0):11s} unreal={float(s.get('unrealized_pnl') or 0):+7.2f} "
              f"MFE={mfe*100:+6.2f}% @{lt(tmfe)} MAE={mae*100:+6.2f}% @{lt(tmae)} pts={npts}")

print()
print("#" * 70)
print("# C. EXIT ORDER REJECT CHURN: symbols/hours (short diagnostics after cutoff)")
print("#" * 70)
sym_h = Counter()
for r in rows(rf"{ROOT}\short\csv_archive\diagnostics.*.csv", rf"{ROOT}\short\diagnostics.csv"):
    try:
        t = float(r["ts"])
    except (ValueError, KeyError):
        continue
    if t < CUTOFF:
        continue
    if r["event"] == "reduce_only_violation_prevented" and r["severity"] == "error":
        sym_h[(r["symbol"], lt(t)[:11])] += 1
for (sym, h), c in sym_h.most_common(25):
    print(f"   {sym:18s} {h}  x{c}")

print()
print("#" * 70)
print("# D. TIME-EXIT / BREAKEVEN / RATE-LIMIT EVENTS after cutoff (trades event log)")
print("#" * 70)
pat = re.compile(r"time_exit|breakeven|rate_limit|notional_below_min|clustering|pullback_required", re.I)
for prof, cur, arch in [
    ("long", rf"{ROOT}\long\bot_futures_trades.csv", rf"{ROOT}\long\csv_archive\bot_futures_trades.*.csv"),
    ("short", rf"{ROOT}\short\bot_futures_short_trades.csv", rf"{ROOT}\short\csv_archive\bot_futures_short_trades.*.csv"),
]:
    cnt = Counter()
    for r in rows(arch, cur):
        try:
            t = float(r["ts"])
        except (ValueError, KeyError):
            continue
        if t < CUTOFF:
            continue
        blob = (r.get("event") or "") + " " + (r.get("reason") or "")
        if pat.search(blob):
            key = r["event"] + " | " + (re.search(pat, blob).group(0).lower())
            cnt[key] += 1
    print(f"--- {prof}")
    for k, c in cnt.most_common(15):
        print(f"    {k:60s} {c}")

print()
print("#" * 70)
print("# E. MACRO REGIME over time (short macro.csv, hourly samples)")
print("#" * 70)
last_h = None
for r in rows(rf"{ROOT}\short\csv_archive\bot_futures_macro.*.csv", rf"{ROOT}\short\bot_futures_macro.csv"):
    h = lt(r["ts"])[:11]
    if h == last_h:
        continue
    last_h = h
    print(f"  {lt(r['ts'])} regime={r['regime']:24s} dir_score={float(r['macro_direction_score']):+5.2f} "
          f"longX={float(r['long_budget_multiplier']):4.2f} shortX={float(r['short_budget_multiplier']):4.2f} "
          f"btc_rsi={float(r['btc_rsi']):5.1f} no_entries={r['disable_new_entries']}")
