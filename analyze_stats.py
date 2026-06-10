# -*- coding: utf-8 -*-
"""Ad-hoc analysis of long/short bot statistics for strategy review."""
import json
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

def ts(t):
    if not t:
        return "-"
    return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%m-%d %H:%M")

for side, path in [
    ("long", r"D:\HTX-Crypto-bot_1.3\long\bot_futures_state.json"),
    ("short", r"D:\HTX-Crypto-bot_1.3\short\bot_futures_short_state.json"),
]:
    data = json.load(open(path, encoding="utf-8"))
    print(f"\n========== {side.upper()} ==========")
    n_open = 0
    tot_unreal = 0.0
    tot_realized = 0.0
    for sym, st in sorted(data.items()):
        realized = float(st.get("realized_pnl") or 0)
        tot_realized += realized
        size = float(st.get("position_size") or 0)
        if not size:
            continue
        n_open += 1
        unreal = float(st.get("unrealized_pnl") or 0)
        tot_unreal += unreal
        entry = st.get("entry_price")
        opened = st.get("cycle_opened_at")
        stage = st.get("buy_stage")
        avg_stage = st.get("average_stage")
        frozen = st.get("frozen_no_more_buys")
        runner = st.get("exit_runner_active")
        zombie = st.get("zombie_position")
        notional = float(st.get("total_bought_quote") or 0) - float(st.get("total_sold_quote") or 0)
        print(f"{sym:22s} size={size:>14.4f} entry={entry} opened={ts(opened)} "
              f"buy_stage={stage} avg_stage={avg_stage} frozen={frozen} runner={runner} "
              f"zombie={zombie} unreal={unreal:>9.4f} realized_cum={realized:>9.4f}")
    print(f"-- {side}: open={n_open}  sum_unrealized={tot_unreal:.2f}  sum_realized_all_symbols={tot_realized:.2f}")
