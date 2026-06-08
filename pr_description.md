⚡ Optimize macro gold coin candidate resolution

💡 **What:**
Extracted the alias resolution loop logic inside `_macro_gold_coin_candidates` into a module-level cached helper function `_compute_gold_coin_candidates` using `@functools.lru_cache`. The instance method now simply delegates to this cached helper by passing a frozen `tuple(config.MACRO.gold_coins)` hashable configuration key. Deduplication, null string handling, and fallback behavior precisely match the original logic. Added dedicated unit tests under `tests/test_exchange_candidates.py` to continuously enforce this behavior.

🎯 **Why:**
The `_macro_gold_coin_candidates` method is repeatedly queried across hot path loops directly inside `exchange.py`. Previously, each invocation would re-evaluate dictionaries, allocate sets, map strings dynamically, and duplicate values dynamically based on `config.MACRO.gold_coins`. By caching this logic against a purely static configuration list, these heavy repetitive loop-level operations are bypassed immediately, accelerating execution paths during candidate evaluations.

📊 **Measured Improvement:**
A quick baseline micro-benchmark was performed simulating 100,000 repetitive resolutions of a single gold coin parameter.
- Baseline `_macro_gold_coin_candidates`: **~0.096 seconds**
- Cached `_compute_gold_coin_candidates`: **~0.016 seconds**
This yields roughly an **~83% reduction** in invocation overhead dynamically scaled per instance queries on hot loops.

⚠️ **Important Notice on Test Failure Baseline**
```bash
python3 -m pytest -q
# Fails on clean main as well:
# TypeError: cannot unpack non-iterable NoneType object
# Location: htxbot/signal_engine.py, _calculate_signal_indicators unpacking
# Affected tests: tests/test_unified_bot.py
```
This PR strictly implements the `exchange.py` optimizations. The full test suite currently fails off the root test command due to a structural pre-existing baseline bug occurring in `htxbot/signal_engine.py::_calculate_signal_indicators` unpacking logic. The same failure happens independently on a clean `main` branch checkout. I've left the `signal_engine` modification scope separate, as this PR focuses narrowly on the gold candidates optimization. The individual optimizations for the gold candidates themselves pass all tests.
