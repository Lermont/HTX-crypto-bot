# -*- coding: utf-8 -*-
"""Show score/budget details for entries that were actually placed."""
import glob
import json
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

def ts_fmt(t):
    try:
        return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%H:%M")
    except (TypeError, ValueError):
        return "?"

files = []
for folder in ("long", "short"):
    files += sorted(
        glob.glob(rf"D:\HTX-Crypto-bot_1.3\{folder}\csv_archive\signal_analytics*.jsonl")
        + glob.glob(rf"D:\HTX-Crypto-bot_1.3\{folder}\signal_analytics.jsonl")
    )

for fp in files:
    for line in open(fp, encoding="utf-8", errors="replace"):
        if '"entry_ladder_placed"' not in line and '"entry_budget_calculated"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        dec = d.get("decision")
        if dec not in ("entry_ladder_placed", "entry_budget_calculated"):
            continue
        sym = d.get("symbol") or "?"
        out = {"ts": ts_fmt(d.get("ts")), "decision": dec, "symbol": str(sym).split("/")[0]}
        flat = {}
        def walk(obj, prefix=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    walk(v, f"{prefix}{k}.")
            else:
                flat[prefix[:-1]] = obj
        walk(d)
        for k, v in flat.items():
            lk = k.lower()
            if any(s in lk for s in ("score", "budget", "stage", "quality")) and "block" not in lk:
                out[k] = v
        print(json.dumps(out, ensure_ascii=False))
