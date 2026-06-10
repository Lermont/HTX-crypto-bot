# -*- coding: utf-8 -*-
"""Per-entry MFE/MAE analysis using price series recovered from signal_analytics."""
import glob
import json
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

def ts_fmt(t):
    return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%H:%M")

# entries: (folder, symbol, side, entry_ts_utc_epoch, entry_price)
ENTRIES = [
    ("long", "WLD/USDT:USDT", "long", 1781091679, 0.5096),
    ("long", "NEAR/USDT:USDT", "long", 1781097134, 2.138),
    ("short", "AVAX/USDT:USDT", "short", 1781090742, 6.5209),
    ("short", "HYPE/USDT:USDT", "short", 1781090766, 55.694),
    ("short", "LINK/USDT:USDT", "short", 1781090772, 7.745835),
    ("short", "PEPE/USDT:USDT", "short", 1781090783, 2.7439e-06),
    ("short", "SHIB/USDT:USDT", "short", 1781090790, 4.656e-06),
    ("short", "SUI/USDT:USDT", "short", 1781090794, 0.7385),
    ("short", "BONK/USDT:USDT", "short", 1781090979, 4.231e-06),
    ("short", "ADA/USDT:USDT", "short", 1781091411, 0.159293),
    ("short", "ETC/USDT:USDT", "short", 1781094922, 6.9419),
    ("short", "DOGE/USDT:USDT", "short", 1781098574, 0.084422),
    ("short", "ETH/USDT:USDT", "short", 1781099747, 1644.59),
    ("short", "FIL/USDT:USDT", "short", 1781101163, 0.751),
    ("short", "XRP/USDT:USDT", "short", 1781101208, 1.1239),
    ("short", "LTC/USDT:USDT", "short", 1781101417, 42.62),
]

# build price series per (folder, symbol) from signal analytics external_context.htx_mid
series = {}
for folder in ("long", "short"):
    files = sorted(
        glob.glob(rf"D:\HTX-Crypto-bot_1.3\{folder}\csv_archive\signal_analytics*.jsonl")
        + glob.glob(rf"D:\HTX-Crypto-bot_1.3\{folder}\signal_analytics.jsonl")
    )
    for fp in files:
        for line in open(fp, encoding="utf-8", errors="replace"):
            if '"htx_mid"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ec = d.get("external_context") or {}
            sym = ec.get("symbol")
            mid = ec.get("htx_mid")
            t = ec.get("ts")
            if sym and mid and t:
                series.setdefault((folder, sym), []).append((float(t), float(mid)))

print(f"{'sym':8s} {'side':5s} {'entry':>12s} {'t_entry':>6s} | {'MFE%':>7s} {'t_mfe':>6s} | {'MAE%':>7s} {'t_mae':>6s} | {'last%':>7s} pts")
for folder, sym, side, t0, px0 in ENTRIES:
    pts = sorted(series.get((folder, sym), []))
    after = [(t, p) for t, p in pts if t >= t0]
    if not after:
        print(f"{sym.split('/')[0]:8s} {side:5s} {px0:>12.6g} {ts_fmt(t0):>6s} | no price data")
        continue
    sign = 1 if side == "long" else -1
    rets = [(sign * (p - px0) / px0, t, p) for t, p in after]
    best = max(rets)
    worst = min(rets)
    last = rets[-1]
    print(f"{sym.split('/')[0]:8s} {side:5s} {px0:>12.6g} {ts_fmt(t0):>6s} | "
          f"{best[0]*100:>6.2f}% {ts_fmt(best[1]):>6s} | "
          f"{worst[0]*100:>6.2f}% {ts_fmt(worst[1]):>6s} | "
          f"{last[0]*100:>6.2f}% n={len(after)}")
