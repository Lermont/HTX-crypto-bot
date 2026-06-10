# -*- coding: utf-8 -*-
"""Reconstruct fills, gate failures and entry quality from trades CSVs."""
import csv
import glob
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10_000_000)

def ts(t):
    return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%m-%d %H:%M:%S")

FILL_EVENTS = {"buy_order_filled", "sell_order_filled"}
INTERESTING = {
    "entry_ladder_placed", "exit_ladder_placed", "hard_stop_loss_placed",
    "buy_order_filled", "sell_order_filled", "cycle_closed",
    "exit_runner_activated", "macro_averaging_blocked", "state_exchange_mismatch",
    "entry_order_canceled", "buy_order_canceled", "sell_order_canceled",
}

for side in ["long", "short"]:
    files = sorted(
        glob.glob(rf"D:\HTX-Crypto-bot_1.3\{side}\csv_archive\*trades*.csv")
        + glob.glob(rf"D:\HTX-Crypto-bot_1.3\{side}\bot_futures*trades.csv")
    )
    rows = []
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                if row.get("event") in INTERESTING:
                    rows.append(row)
    rows.sort(key=lambda r: float(r["ts"]))
    print(f"\n=================== {side.upper()} ===================")
    for r in rows:
        ev = r["event"]
        sym = (r.get("symbol") or "").replace("/USDT:USDT", "")
        msg = (r.get("message") or "")[:110]
        price = r.get("price") or ""
        amount = r.get("amount") or ""
        reason = (r.get("reason") or "")
        # keep reason short for fills
        if ev in FILL_EVENTS or ev in {"cycle_closed", "hard_stop_loss_placed", "exit_runner_activated"}:
            print(f"{ts(r['ts'])} {ev:28s} {sym:8s} px={price} amt={amount} | {msg}")
        elif ev == "macro_averaging_blocked":
            pass  # counted below
    cnt = Counter(r["event"] for r in rows)
    print("counts:", dict(cnt))
