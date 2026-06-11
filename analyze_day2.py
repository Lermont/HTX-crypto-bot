# -*- coding: utf-8 -*-
"""Day-2 review: stats after the 2026-06-10 strategy changes (items 1,4,7,9,11)."""
import csv
import glob
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"D:\HTX-Crypto-bot_1.3"
PROFILES = {
    "long": {
        "state": rf"{ROOT}\long\bot_futures_state.json",
        "cycles": rf"{ROOT}\long\bot_futures_cycle_stats.csv",
        "trades": rf"{ROOT}\long\bot_futures_trades.csv",
        "trades_arch": rf"{ROOT}\long\csv_archive\bot_futures_trades.*.csv",
        "cycles_arch": rf"{ROOT}\long\csv_archive\bot_futures_cycle_stats.*.csv",
        "pnl": rf"{ROOT}\long\account_pnl.csv",
        "pnl_arch": rf"{ROOT}\long\csv_archive\account_pnl.*.csv",
        "diag": rf"{ROOT}\long\diagnostics.csv",
        "diag_arch": rf"{ROOT}\long\csv_archive\diagnostics.*.csv",
        "signals": rf"{ROOT}\long\signal_analytics.jsonl",
        "signals_arch": rf"{ROOT}\long\csv_archive\signal_analytics.*.jsonl",
    },
    "short": {
        "state": rf"{ROOT}\short\bot_futures_short_state.json",
        "cycles": rf"{ROOT}\short\bot_futures_short_cycle_stats.csv",
        "trades": rf"{ROOT}\short\bot_futures_short_trades.csv",
        "trades_arch": rf"{ROOT}\short\csv_archive\bot_futures_short_trades.*.csv",
        "cycles_arch": rf"{ROOT}\short\csv_archive\bot_futures_short_cycle_stats.*.csv",
        "pnl": rf"{ROOT}\short\account_pnl.csv",
        "pnl_arch": rf"{ROOT}\short\csv_archive\account_pnl.*.csv",
        "diag": rf"{ROOT}\short\diagnostics.csv",
        "diag_arch": rf"{ROOT}\short\csv_archive\diagnostics.*.csv",
        "signals": rf"{ROOT}\short\signal_analytics.jsonl",
        "signals_arch": rf"{ROOT}\short\csv_archive\signal_analytics.*.jsonl",
    },
}

# new settings went live around 19:41 local 10.06 = ~16:41 UTC
CUTOFF = 1781109600.0


def lt(t):
    """epoch -> local-ish (UTC+3) string"""
    return datetime.fromtimestamp(float(t) + 3 * 3600, tz=timezone.utc).strftime("%d.%m %H:%M")


def read_csv_rows(pattern_current, pattern_arch):
    files = sorted(glob.glob(pattern_arch)) + [pattern_current]
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    yield row
        except FileNotFoundError:
            pass


print("#" * 70)
print("# 1. CLOSED CYCLES (all time, split before/after cutoff %s)" % lt(CUTOFF))
print("#" * 70)
for prof, p in PROFILES.items():
    rows = list(read_csv_rows(p["cycles"], p["cycles_arch"]))
    for label, sel in [
        ("before", [r for r in rows if float(r["closed_at"]) < CUTOFF]),
        ("after ", [r for r in rows if float(r["closed_at"]) >= CUTOFF]),
    ]:
        if not sel:
            print(f"{prof:5s} {label}: no closed cycles")
            continue
        pnl = [float(r["realized_pnl_quote"]) for r in sel]
        hold = [float(r["holding_minutes"]) for r in sel]
        wins = sum(1 for x in pnl if x > 0)
        reasons = Counter(r["close_reason"] for r in sel)
        print(f"{prof:5s} {label}: n={len(sel):3d} pnl_sum={sum(pnl):+8.2f} "
              f"win={wins}/{len(sel)} avg_hold_min={sum(hold)/len(hold):6.0f} "
              f"reasons={dict(reasons)}")
    # per-cycle detail for "after"
    after = [r for r in rows if float(r["closed_at"]) >= CUTOFF]
    for r in sorted(after, key=lambda x: float(x["closed_at"])):
        print(f"   {lt(r['closed_at'])} {r['symbol']:18s} pnl={float(r['realized_pnl_quote']):+7.3f} "
              f"({float(r['realized_pnl_percent_on_notional']):+5.2f}% notion) hold={float(r['holding_minutes']):5.0f}m "
              f"reason={r['close_reason']} be={r.get('breakeven_activated','?')} avg_stage={r.get('max_averaging_stage','?')}")

print()
print("#" * 70)
print("# 2. OPEN POSITIONS NOW")
print("#" * 70)
tot = {}
for prof, p in PROFILES.items():
    st = json.load(open(p["state"], encoding="utf-8"))
    n, unreal, notion = 0, 0.0, 0.0
    print(f"--- {prof}")
    for sym, s in sorted(st.items()):
        size = float(s.get("position_size") or 0)
        if not size:
            continue
        n += 1
        u = float(s.get("unrealized_pnl") or 0)
        unreal += u
        nt = float(s.get("total_bought_quote") or 0) - float(s.get("total_sold_quote") or 0)
        notion += abs(nt)
        opened = s.get("cycle_opened_at")
        age_h = (datetime.now(timezone.utc).timestamp() - float(opened)) / 3600 if opened else -1
        print(f"  {sym:20s} entry={s.get('entry_price'):>12} unreal={u:+8.3f} "
              f"open_notional={abs(nt):8.2f} age_h={age_h:5.1f} "
              f"frozen={int(bool(s.get('frozen_no_more_buys')))} be_at={s.get('breakeven_activated_at')} "
              f"runner={int(bool(s.get('exit_runner_active')))}")
    tot[prof] = (n, unreal, notion)
    print(f"  == {prof}: open={n} sum_unreal={unreal:+.2f} sum_open_notional={notion:.2f}")
print(f"NET: long_notional={tot.get('long',(0,0,0))[2]:.0f} short_notional={tot.get('short',(0,0,0))[2]:.0f} "
      f"sum_unreal={tot.get('long',(0,0,0))[1]+tot.get('short',(0,0,0))[1]:+.2f}")

print()
print("#" * 70)
print("# 3. ACCOUNT PNL SNAPSHOTS (hourly-ish samples)")
print("#" * 70)
for prof, p in PROFILES.items():
    rows = list(read_csv_rows(p["pnl"], p["pnl_arch"]))
    if not rows:
        continue
    print(f"--- {prof}: {len(rows)} rows, {lt(rows[0]['ts'])} .. {lt(rows[-1]['ts'])}")
    last_h = None
    for r in rows:
        h = lt(r["ts"])[:11]  # dd.mm hh
        if h != last_h:
            last_h = h
            print(f"  {lt(r['ts'])} open_pnl={float(r['open_pnl']):+8.2f} "
                  f"unreal={float(r['unrealized_pnl']):+8.2f} positions={r['position_count']} "
                  f"open_notional={float(r['open_notional']):8.1f} reason={r['reason']}")

print()
print("#" * 70)
print("# 4. TRADES EVENT LOG: event counts before/after cutoff")
print("#" * 70)
for prof, p in PROFILES.items():
    before, after = Counter(), Counter()
    fees_b, fees_a = 0.0, 0.0
    for r in read_csv_rows(p["trades"], p["trades_arch"]):
        try:
            t = float(r["ts"])
        except (ValueError, KeyError):
            continue
        tgt = after if t >= CUTOFF else before
        tgt[r["event"]] += 1
        try:
            fee = float(r["fee_quote"] or 0)
        except ValueError:
            fee = 0.0
        if t >= CUTOFF:
            fees_a += fee
        else:
            fees_b += fee
    print(f"--- {prof} BEFORE: fees={fees_b:+.2f}")
    for ev, c in before.most_common(15):
        print(f"    {ev:45s} {c}")
    print(f"--- {prof} AFTER:  fees={fees_a:+.2f}")
    for ev, c in after.most_common(25):
        print(f"    {ev:45s} {c}")

print()
print("#" * 70)
print("# 5. DIAGNOSTICS: category/event counts before/after")
print("#" * 70)
for prof, p in PROFILES.items():
    before, after = Counter(), Counter()
    for r in read_csv_rows(p["diag"], p["diag_arch"]):
        try:
            t = float(r["ts"])
        except (ValueError, KeyError):
            continue
        key = f"{r['severity']}/{r['category']}/{r['event']}/{r.get('reason','')}"
        (after if t >= CUTOFF else before)[key] += 1
    print(f"--- {prof} BEFORE")
    for k, c in before.most_common(12):
        print(f"    {k:70s} {c}")
    print(f"--- {prof} AFTER")
    for k, c in after.most_common(20):
        print(f"    {k:70s} {c}")
