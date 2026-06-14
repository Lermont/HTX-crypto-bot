# -*- coding: utf-8 -*-
"""Risk/reward & trailing-stop reality check.

Answers three questions raised in the 2026-06-14 strategy review, from the
*actual* trade logs (no parameter changes):

  A. Expectancy  -- win-rate, avg_win/avg_loss, profit factor, expectancy.
                    A wide stop (-4%) past the average TP is only mathematically
                    sound when win-rate clears the breakeven bar L/(L+W).
  B. Tail path   -- share of cycles that reached the -4% hard stop WITHOUT first
                    touching the +1.2% first take-profit. Those are the only
                    cycles that pay the full stop, because TP1 pulls the stop to
                    breakeven (HARD_STOP_BREAKEVEN_AFTER_FIRST_EXIT=true).
  C. Trailing    -- how often the runner/trailing exit actually activated and
                    closed, vs. how often cycles died on the hard stop / time
                    exits instead.

Part A is exact (net cycle PnL is logged). Parts B/C are best-effort: they
reconstruct the in-position price path from signal_analytics external_context
htx_mid, whose coverage is the entry-gate stream and may be sparse while a
position is open. The script prints the usable sample size so the numbers can be
judged honestly.

Usage:  python analyze_riskreward.py
"""
import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10_000_000)

BASE = os.path.dirname(os.path.abspath(__file__))

# Live intent (kept in sync with .env). The thresholds below are only used to
# classify the reconstructed price path; they do not affect trading.
TP1_MARKUP = 0.012   # first fixed take-profit rung (EMA_EXIT_NORMAL_LADDER_MARKUPS[0])
HARD_STOP = 0.04     # HARD_STOP_LOSS_PCT
RUNNER_ACTIVATION = 0.020  # EMA_EXIT_TRAILING_ACTIVATION_MARKUP

PNL_RE = re.compile(r"pnl=(-?\d+(?:\.\d+)?)")

# event names per side
ENTRY_FILL = {"long": "buy_order_filled", "short": "sell_order_filled"}
EXIT_FILL = {"long": "sell_order_filled", "short": "buy_order_filled"}


def trade_files(side):
    live = "bot_futures_trades.csv" if side == "long" else "bot_futures_short_trades.csv"
    return sorted(
        glob.glob(os.path.join(BASE, side, "csv_archive", "*trades*.csv"))
        + glob.glob(os.path.join(BASE, side, live))
    )


def read_rows(side):
    rows = []
    for fp in trade_files(side):
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                rows.append(r)
    rows.sort(key=lambda r: float(r["ts"]))
    return rows


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Part A: expectancy from cycle_closed
# --------------------------------------------------------------------------- #
def expectancy(rows):
    """Return (overall stats dict, per-reason dict) for one side."""
    pnls = []
    by_reason = defaultdict(list)
    for r in rows:
        if r.get("event") != "cycle_closed":
            continue
        m = PNL_RE.search(r.get("message") or "")
        if not m:
            continue
        pnl = float(m.group(1))
        pnls.append(pnl)
        by_reason[r.get("reason") or "?"].append(pnl)
    return pnls, by_reason


def summarize(pnls):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    sum_win = sum(wins)
    sum_loss = sum(losses)
    wr = len(wins) / n
    avg_win = sum_win / len(wins) if wins else 0.0
    avg_loss = sum_loss / len(losses) if losses else 0.0
    pf = (sum_win / abs(sum_loss)) if sum_loss else float("inf")
    exp = sum(pnls) / n
    # breakeven win-rate implied by the realized avg win/loss magnitudes
    be_wr = abs(avg_loss) / (abs(avg_loss) + avg_win) if (avg_win + abs(avg_loss)) else 0.0
    return dict(
        n=n, wins=len(wins), losses=len(losses), wr=wr,
        avg_win=avg_win, avg_loss=avg_loss, pf=pf, exp=exp,
        total=sum(pnls), be_wr=be_wr,
    )


def print_expectancy(label, pnls, by_reason):
    s = summarize(pnls)
    print(f"\n--- {label}: expectancy (net USDT per cycle) ---")
    if not s:
        print("  no closed cycles with pnl")
        return
    print(f"  cycles={s['n']}  wins={s['wins']}  losses={s['losses']}  win_rate={s['wr']*100:5.1f}%")
    print(f"  avg_win=+{s['avg_win']:.3f}  avg_loss={s['avg_loss']:.3f}  profit_factor={s['pf']:.2f}")
    print(f"  expectancy/trade={s['exp']:+.3f} USDT   total={s['total']:+.2f} USDT")
    print(f"  breakeven win-rate (given avg win/loss)={s['be_wr']*100:5.1f}%  "
          f"-> {'ABOVE bar (edge +)' if s['wr'] > s['be_wr'] else 'BELOW bar (edge -)'}")
    if by_reason:
        print("  by close reason:")
        for reason, ps in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            sub = summarize(ps)
            print(f"    {reason:32s} n={sub['n']:3d}  wr={sub['wr']*100:5.1f}%  "
                  f"exp={sub['exp']:+.3f}  total={sub['total']:+.2f}")


# --------------------------------------------------------------------------- #
# Part B/C: reconstruct cycles + price path
# --------------------------------------------------------------------------- #
def reconstruct_cycles(rows, side):
    """State machine per symbol -> list of cycles.

    cycle = dict(symbol, entry_ts, entry_px, close_ts, reason, pnl)
    entry_px is the FIRST entry fill (initial entry); TP1/stop in the question
    are defined relative to the initial entry.
    """
    entry_ev = ENTRY_FILL[side]
    open_cycle = {}  # symbol -> partial cycle
    cycles = []
    for r in rows:
        ev = r.get("event")
        sym = r.get("symbol") or ""
        if not sym:
            continue
        if ev == entry_ev:
            px = fnum(r.get("price"))
            if px and sym not in open_cycle:
                open_cycle[sym] = dict(
                    symbol=sym, entry_ts=float(r["ts"]), entry_px=px
                )
        elif ev == "cycle_closed":
            cyc = open_cycle.pop(sym, None)
            if cyc is None:
                continue
            m = PNL_RE.search(r.get("message") or "")
            cyc["close_ts"] = float(r["ts"])
            cyc["reason"] = r.get("reason") or "?"
            cyc["pnl"] = float(m.group(1)) if m else None
            cycles.append(cyc)
    return cycles


def price_series(side):
    series = defaultdict(list)
    files = sorted(
        glob.glob(os.path.join(BASE, side, "csv_archive", "signal_analytics*.jsonl"))
        + glob.glob(os.path.join(BASE, side, "signal_analytics.jsonl"))
    )
    for fp in files:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"htx_mid"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ec = d.get("external_context") or {}
                sym = ec.get("symbol")
                mid = fnum(ec.get("htx_mid"))
                t = ec.get("ts") or d.get("ts")
                t = fnum(t)
                if sym and mid and t:
                    series[sym].append((t, mid))
    for sym in series:
        series[sym].sort()
    return series


def path_metrics(cyc, series, side):
    """Return MFE, MAE, touched_tp1, touched_stop, stop_before_tp1, npts."""
    sign = 1 if side == "long" else -1
    pts = [(t, p) for t, p in series.get(cyc["symbol"], [])
           if cyc["entry_ts"] <= t <= cyc["close_ts"]]
    if not pts:
        return None
    e = cyc["entry_px"]
    mfe = -9.9
    mae = 9.9
    tp1_t = None
    stop_t = None
    for t, p in pts:
        ret = sign * (p - e) / e
        mfe = max(mfe, ret)
        mae = min(mae, ret)
        if tp1_t is None and ret >= TP1_MARKUP:
            tp1_t = t
        if stop_t is None and ret <= -HARD_STOP:
            stop_t = t
    stop_before_tp1 = stop_t is not None and (tp1_t is None or stop_t < tp1_t)
    return dict(
        mfe=mfe, mae=mae,
        touched_tp1=tp1_t is not None,
        touched_stop=stop_t is not None,
        stop_before_tp1=stop_before_tp1,
        npts=len(pts),
    )


def print_path(label, cycles, series, side):
    print(f"\n--- {label}: price-path / tail analysis (best-effort, htx_mid) ---")
    with_data = []
    for c in cycles:
        m = path_metrics(c, series, side)
        if m:
            with_data.append((c, m))
    print(f"  reconstructed cycles={len(cycles)}  with usable in-position price path={len(with_data)}")
    if not with_data:
        print("  no in-position price coverage -> tail metric not computable on this data")
        return
    n = len(with_data)
    tp1 = sum(1 for _, m in with_data if m["touched_tp1"])
    stop = sum(1 for _, m in with_data if m["touched_stop"])
    tail = sum(1 for _, m in with_data if m["stop_before_tp1"])
    avg_mfe = sum(m["mfe"] for _, m in with_data) / n
    avg_mae = sum(m["mae"] for _, m in with_data) / n
    print(f"  touched +{TP1_MARKUP*100:.1f}% (TP1, stop->BE): {tp1:3d}/{n}  ({tp1/n*100:4.1f}%)")
    print(f"  touched -{HARD_STOP*100:.1f}% (hard stop):      {stop:3d}/{n}  ({stop/n*100:4.1f}%)")
    print(f"  TAIL: hit -{HARD_STOP*100:.1f}% before +{TP1_MARKUP*100:.1f}%:  "
          f"{tail:3d}/{n}  ({tail/n*100:4.1f}%)  <- only these pay the full stop")
    print(f"  avg MFE={avg_mfe*100:+.2f}%   avg MAE={avg_mae*100:+.2f}%")
    # Of cycles that closed on the hard stop, did they ever offer TP1 / runner activation?
    hs = [(c, m) for c, m in with_data if str(c["reason"]).startswith("hard_stop")]
    if hs:
        offered_tp1 = sum(1 for _, m in hs if m["mfe"] >= TP1_MARKUP)
        offered_run = sum(1 for _, m in hs if m["mfe"] >= RUNNER_ACTIVATION)
        print(f"  among {len(hs)} hard-stop closes: {offered_tp1} had MFE>=+{TP1_MARKUP*100:.1f}% "
              f"(TP1 was reachable), {offered_run} had MFE>=+{RUNNER_ACTIVATION*100:.1f}% (runner-activation reachable)")


def print_trailing(label, rows):
    cnt = defaultdict(int)
    for r in rows:
        cnt[r.get("event")] += 1
    print(f"\n--- {label}: trailing/runner activity ---")
    print(f"  exit_runner_activated={cnt.get('exit_runner_activated',0)}  "
          f"hard_stop_loss_placed={cnt.get('hard_stop_loss_placed',0)}  "
          f"hard_stop_loss_market_close_placed={cnt.get('hard_stop_loss_market_close_placed',0)}")
    print(f"  exit_ladder_placed={cnt.get('exit_ladder_placed',0)}  "
          f"ema_breakeven_activated={cnt.get('ema_breakeven_activated',0)}  "
          f"factor_horizon_activated={cnt.get('factor_horizon_activated',0)}")


# --------------------------------------------------------------------------- #
def main():
    all_pnls = []
    all_by_reason = defaultdict(list)
    for side in ("long", "short"):
        rows = read_rows(side)
        pnls, by_reason = expectancy(rows)
        all_pnls += pnls
        for k, v in by_reason.items():
            all_by_reason[k] += v
        print(f"\n=================== {side.upper()} ===================")
        print_expectancy(side, pnls, by_reason)
        print_trailing(side, rows)
        cycles = reconstruct_cycles(rows, side)
        series = price_series(side)
        print_path(side, cycles, series, side)

    print("\n=================== COMBINED ===================")
    print_expectancy("long+short", all_pnls, all_by_reason)
    print(f"\nthresholds used: TP1=+{TP1_MARKUP*100:.1f}%  hard_stop=-{HARD_STOP*100:.1f}%  "
          f"runner_activation=+{RUNNER_ACTIVATION*100:.1f}%")
    print("note: PnL is net USDT per cycle (position sizes vary); win-rate is per-cycle.")


if __name__ == "__main__":
    main()
