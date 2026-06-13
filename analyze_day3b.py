# -*- coding: utf-8 -*-
"""Why every signal fails the weighted-score min: score distribution + penalty anatomy."""
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\HTX-Crypto-bot_1.3"
RESTART = 1781200000.0  # 11.06 20:46 local


def lt(t):
    return datetime.fromtimestamp(float(t) + 10800, tz=timezone.utc).strftime("%d.%m %H:%M")


def pct(vals, q):
    if not vals:
        return float("nan")
    s = sorted(vals)
    i = min(len(s) - 1, int(q * (len(s) - 1)))
    return s[i]


for prof in ("long", "short"):
    files = sorted(glob.glob(rf"{ROOT}\{prof}\csv_archive\signal_analytics.*.jsonl")) + [
        rf"{ROOT}\{prof}\signal_analytics.jsonl"]
    n = 0
    valid_ema = []          # weighted scores when ema_entry_valid=1
    invalid_ema = 0
    flag_fail_in_validema = Counter()
    penalties_in_validema = Counter()
    raw_scores_validema = []
    near_miss = []          # (w, ts, sym, penalties-string)
    side_flags = Counter()  # entry_side_valid / ema flags overall
    macro_side_hours = defaultdict(Counter)
    for fp in files:
        try:
            fh = open(fp, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with fh:
            for line in fh:
                if '"entry_weighted_score_below_min' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = float(d.get("ts") or 0)
                if t < RESTART:
                    continue
                br = d.get("block_reason") or ""
                sig = d.get("signal") or {}
                n += 1
                m = re.search(r"weighted_score=([-\d.]+)", br)
                raw = re.search(r"raw_score=([-\d.]+)", br)
                w = float(m.group(1)) if m else float("nan")
                ema_ok = "ema_entry_valid=1" in br
                h = lt(t)[:11]
                macro_side_hours[h][sig.get("ema_macro_side") or "?"] += 1
                for fl in ("entry_side_valid", "macro_valid", "pullback_valid",
                           "trigger_valid", "market_structure_valid", "volume_valid",
                           "rs_confirm_valid", "btc_entry_valid", "chop_valid"):
                    if f"{fl}=1" in br:
                        side_flags[fl] += 1
                if not ema_ok:
                    invalid_ema += 1
                    continue
                valid_ema.append(w)
                if raw:
                    raw_scores_validema.append(float(raw.group(1)))
                for fl in ("macro_valid", "pullback_valid", "trigger_valid",
                           "market_structure_valid", "volume_valid",
                           "rs_confirm_valid", "btc_entry_valid", "chop_valid",
                           "entry_side_valid"):
                    if f"{fl}=0" in br:
                        flag_fail_in_validema[fl] += 1
                for pm in re.finditer(r"penalty_(\w+)=([\d.]+)", br):
                    if float(pm.group(2)) > 1e-9:
                        penalties_in_validema[pm.group(1)] += 1
                if w >= 0.02:
                    pens = ";".join(f"{pm.group(1)}={pm.group(2)}" for pm in re.finditer(r"penalty_(\w+)=([\d.]+)", br) if float(pm.group(2)) > 1e-9)
                    near_miss.append((w, t, d.get("symbol") or "", pens))
    print("#" * 70)
    print(f"# {prof}: {n} score-blocked checks since {lt(RESTART)}")
    print(f"  ema_entry_valid=0 (pullback/structure kills it): {invalid_ema} ({invalid_ema / max(n,1) * 100:.1f}%)")
    print(f"  ema_entry_valid=1 but score below min:           {len(valid_ema)} ({len(valid_ema) / max(n,1) * 100:.1f}%)")
    if valid_ema:
        print(f"  weighted_score distribution (ema ok): p50={pct(valid_ema, .5):+.4f} "
              f"p75={pct(valid_ema, .75):+.4f} p90={pct(valid_ema, .9):+.4f} "
              f"p99={pct(valid_ema, .99):+.4f} max={max(valid_ema):+.4f}")
        print(f"  raw score (ema ok): p50={pct(raw_scores_validema, .5):+.4f} p90={pct(raw_scores_validema, .9):+.4f} max={max(raw_scores_validema):+.4f}")
        print(f"  failed flags within ema-ok blocks: {dict(flag_fail_in_validema.most_common())}")
        print(f"  nonzero penalties within ema-ok blocks: {dict(penalties_in_validema.most_common())}")
    print(f"  near-misses (w>=0.02): {len(near_miss)}")
    for w, t, sym, pens in sorted(near_miss, reverse=True)[:10]:
        print(f"    {lt(t)} {sym.split('/')[0]:8s} w={w:+.4f} pens: {pens[:120]}")
    print("  macro EMA side by hour (sample):")
    hours = sorted(macro_side_hours)
    for h in hours[:: max(1, len(hours) // 8)]:
        print(f"    {h}: {dict(macro_side_hours[h])}")
