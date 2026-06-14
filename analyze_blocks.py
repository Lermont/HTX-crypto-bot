# -*- coding: utf-8 -*-
"""Per-symbol entry-block statistics for the current bot session (pid 9988, restart 13.06 20:35 local)."""
import glob
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\HTX-Crypto-bot_1.3"
TZ = timezone(timedelta(hours=3))  # local display tz (UTC+3), matches lt() offset in analyze_day3
# current session start: watchdog log "2026-06-13 20:35:44 | started: pid=9988"
RESTART = datetime(2026, 6, 13, 20, 35, 44, tzinfo=TZ).timestamp()


def lt(t):
    return datetime.fromtimestamp(float(t), tz=TZ).strftime("%d.%m %H:%M")


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
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def rec_ts(d):
    v = d.get("ts")
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


for prof in ("long", "short"):
    print("#" * 72)
    print(f"# {prof.upper()} — entry blocks since session start {lt(RESTART)}")
    print("#" * 72)
    total = 0
    ok = 0
    heads = Counter()                       # block-reason category
    sym_total = Counter()                   # checks per symbol
    sym_head = defaultdict(Counter)         # per symbol -> reason histogram
    first_seen, last_seen = None, None
    for d in jsonl_records(prof):
        t = rec_ts(d)
        if t < RESTART:
            continue
        if d.get("decision") != "entry_gate_checked":
            continue
        total += 1
        first_seen = t if first_seen is None else min(first_seen, t)
        last_seen = t if last_seen is None else max(last_seen, t)
        sym = (d.get("symbol") or "?").split("/")[0]
        sym_total[sym] += 1
        br = d.get("block_reason") or ""
        if not br:
            ok += 1
            sym_head[sym]["ENTRY_OK"] += 1
            continue
        head = br.split(";", 1)[0]
        heads[head] += 1
        sym_head[sym][head] += 1

    if total == 0:
        print("  (no entry_gate_checked records in this window)\n")
        continue

    span = f"{lt(first_seen)} … {lt(last_seen)}" if first_seen else "-"
    print(f"  window data: {span}   checks={total}   passed_gate(ENTRY_OK)={ok}")
    print(f"  --- block reasons (category : count) ---")
    for k, c in heads.most_common():
        print(f"    {k:42s} {c:5d}  ({c*100//total}%)")
    print(f"  --- per-symbol (checks; dominant reasons) ---")
    for sym, c in sym_total.most_common():
        parts = ", ".join(f"{r}={n}" for r, n in sym_head[sym].most_common(3))
        flag = "  <== passes gate" if sym_head[sym].get("ENTRY_OK") else ""
        print(f"    {sym:10s} checks={c:4d}  | {parts}{flag}")
    print()
