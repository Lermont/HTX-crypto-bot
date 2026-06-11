# -*- coding: utf-8 -*-
"""Day-3: why entries dried up; gate-by-gate block analysis + counterfactuals."""
import csv
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\HTX-Crypto-bot_1.3"
RESTART = 1781178884.0  # 11.06 14:54:44 local (process 2876)


def lt(t):
    return datetime.fromtimestamp(float(t) + 10800, tz=timezone.utc).strftime("%d.%m %H:%M")


def jsonl_records(prof):
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
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield d


def rec_ts(d):
    for key in ("ts",):
        v = d.get(key)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    ec = d.get("external_context") or {}
    try:
        return float(ec.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


print("#" * 72)
print(f"# 1. BLOCK REASONS since restart {lt(RESTART)} (signal_analytics)")
print("#" * 72)
for prof in ("long", "short"):
    heads = Counter()
    counter_macro_scores = []   # wscore of signals blocked ONLY by the 0.06 min (0.04<=w<0.06)
    btc_blocks = []             # (ts, symbol, btc_return)
    cap_blocks = []             # (ts, symbol, net, planned, cap)
    notional_blocks = 0
    decisions = Counter()
    for d in jsonl_records(prof):
        t = rec_ts(d)
        if t < RESTART:
            continue
        decisions[d.get("decision") or ""] += 1
        br = d.get("block_reason") or ""
        if not br:
            continue
        head = br.split(";", 1)[0]
        heads[head] += 1
        sym = d.get("symbol") or ""
        if head == "entry_weighted_score_below_min":
            m = re.search(r"weighted_score=([-\d.]+)", br)
            mm = re.search(r";min=([\d.]+)", br)
            if m and mm:
                w, mn = float(m.group(1)), float(mm.group(1))
                if mn > 0.045 and 0.04 <= w < mn:
                    counter_macro_scores.append((t, sym, w))
        elif head == "short_entry_btc_momentum_block":
            m = re.search(r"btc_return_30m=([-\d.]+)", br)
            btc_blocks.append((t, sym, float(m.group(1)) if m else 0.0))
        elif head == "entry_net_exposure_cap_exceeded":
            nums = dict(re.findall(r"(net_side_notional|planned_notional|cap_notional|equity)=([\d.-]+)", br))
            cap_blocks.append((t, sym, nums))
        elif head == "entry_planned_notional_below_min":
            notional_blocks += 1
    print(f"--- {prof}: decisions={dict(decisions)}")
    for k, c in heads.most_common(15):
        print(f"    {k:50s} {c}")
    print(f"    [counter-macro band 0.04<=w<min] uniq-signal count={len(counter_macro_scores)}")
    print(f"    [btc momentum blocks] count={len(btc_blocks)}")
    print(f"    [net exposure cap blocks] count={len(cap_blocks)}")
    if cap_blocks:
        t, sym, nums = cap_blocks[-1]
        print(f"      last: {lt(t)} {sym} {nums}")
    # store for counterfactual section
    if prof == "short":
        SHORT_CM = counter_macro_scores
        SHORT_BTC = btc_blocks
        SHORT_CAP = cap_blocks

print()
print("#" * 72)
print("# 2. ENTRIES / CYCLES / FILLS since restart")
print("#" * 72)
for prof, cyc, cyc_arch, tr, tr_arch in [
    ("long", rf"{ROOT}\long\bot_futures_cycle_stats.csv", rf"{ROOT}\long\csv_archive\bot_futures_cycle_stats.*.csv",
     rf"{ROOT}\long\bot_futures_trades.csv", rf"{ROOT}\long\csv_archive\bot_futures_trades.*.csv"),
    ("short", rf"{ROOT}\short\bot_futures_short_cycle_stats.csv", rf"{ROOT}\short\csv_archive\bot_futures_short_cycle_stats.*.csv",
     rf"{ROOT}\short\bot_futures_short_trades.csv", rf"{ROOT}\short\csv_archive\bot_futures_short_trades.*.csv"),
]:
    n_cyc, pnl = 0, 0.0
    for fp in sorted(glob.glob(cyc_arch)) + [cyc]:
        try:
            for r in csv.DictReader(open(fp, encoding="utf-8", errors="replace")):
                if float(r["closed_at"]) >= RESTART:
                    n_cyc += 1
                    pnl += float(r["realized_pnl_quote"])
                    print(f"  {prof} cycle {lt(float(r['closed_at']))} {r['symbol'].split('/')[0]:8s} "
                          f"pnl={float(r['realized_pnl_quote']):+7.3f} reason={r['close_reason']}")
        except FileNotFoundError:
            pass
    ev = Counter()
    for fp in sorted(glob.glob(tr_arch)) + [tr]:
        try:
            for r in csv.DictReader(open(fp, encoding="utf-8", errors="replace")):
                try:
                    t = float(r["ts"])
                except (ValueError, KeyError):
                    continue
                if t >= RESTART and r["event"] in (
                    "entry_ladder_placed", "buy_order_filled", "sell_order_filled",
                    "cycle_closed", "entry_order_canceled", "exit_ladder_placed",
                    "exit_ladder_rebuilt", "hard_stop_loss_placed",
                ):
                    ev[r["event"]] += 1
        except FileNotFoundError:
            pass
    print(f"  == {prof}: cycles={n_cyc} pnl={pnl:+.2f} events={dict(ev)}")

print()
print("#" * 72)
print("# 3. COUNTERFACTUAL: blocked short candidates, forward return (short side)")
print("#" * 72)
# price series
series = defaultdict(list)
for prof in ("long", "short"):
    for fp in sorted(glob.glob(rf"{ROOT}\{prof}\csv_archive\external_price_feed.*.csv")) + [
            rf"{ROOT}\{prof}\external_price_feed.csv"]:
        try:
            for r in csv.DictReader(open(fp, encoding="utf-8", errors="replace")):
                try:
                    mid, t = float(r["htx_mid"]), float(r["ts"])
                except (ValueError, KeyError):
                    continue
                if mid > 0 and t >= RESTART - 600:
                    series[r["symbol"]].append((t, mid))
        except FileNotFoundError:
            pass
for s in series.values():
    s.sort()


def fwd_return_short(sym, t0, horizon_sec):
    pts = series.get(sym, [])
    base = None
    for t, p in pts:
        if t >= t0:
            base = p
            break
    if base is None:
        return None
    fut = [p for t, p in pts if t0 <= t <= t0 + horizon_sec]
    if len(fut) < 2:
        return None
    last = fut[-1]
    best = min(fut)   # short side: lower price = profit
    return (base - last) / base, (base - best) / base  # (ret_at_horizon, MFE)


def summarize(name, cands, horizon=7200):
    # dedupe: keep first candidate per symbol per 30 min
    seen = {}
    uniq = []
    for t, sym, *rest in sorted(cands):
        k = (sym, int(t // 1800))
        if k in seen:
            continue
        seen[k] = 1
        uniq.append((t, sym))
    rets, mfes, n_data = [], [], 0
    for t, sym in uniq:
        r = fwd_return_short(sym, t, horizon)
        if r is None:
            continue
        n_data += 1
        rets.append(r[0])
        mfes.append(r[1])
    if not rets:
        print(f"  {name}: candidates={len(uniq)} (no price data)")
        return
    avg = sum(rets) / len(rets)
    win = sum(1 for x in rets if x > 0)
    print(f"  {name}: uniq_candidates={len(uniq)} with_data={n_data} "
          f"avg_fwd_2h={avg*100:+.2f}% win_rate={win}/{len(rets)} "
          f"avg_MFE_2h={sum(mfes)/len(mfes)*100:+.2f}%")


summarize("counter_macro band (0.04<=w<0.06)", [(t, s) for t, s, w in SHORT_CM])
summarize("btc_momentum_block", [(t, s) for t, s, b in SHORT_BTC])
summarize("net_exposure_cap", [(t, s) for t, s, n in SHORT_CAP])

print()
print("#" * 72)
print("# 4. HOURLY: blocked-by-gate counts + entries (short)")
print("#" * 72)
hourly = defaultdict(Counter)
for d in jsonl_records("short"):
    t = rec_ts(d)
    if t < RESTART:
        continue
    br = d.get("block_reason") or ""
    h = lt(t)[:11]
    if br:
        hourly[h][br.split(";", 1)[0]] += 1
    elif d.get("decision") == "entry_budget_calculated":
        hourly[h]["ENTRY_OK"] += 1
for h in sorted(hourly):
    c = hourly[h]
    print(f"  {h}: ok={c.get('ENTRY_OK',0):3d} score_min={c.get('entry_weighted_score_below_min',0):5d} "
          f"btc={c.get('short_entry_btc_momentum_block',0):3d} cap={c.get('entry_net_exposure_cap_exceeded',0):3d} "
          f"notional={c.get('entry_planned_notional_below_min',0):3d} other={sum(v for k,v in c.items() if k not in ('ENTRY_OK','entry_weighted_score_below_min','short_entry_btc_momentum_block','entry_net_exposure_cap_exceeded','entry_planned_notional_below_min')):4d}")
