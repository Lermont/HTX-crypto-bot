1.  **Define `SignalAnalyticsEvent` Dataclass in `htxbot/models.py`:**
    *   Create a dataclass to hold the arguments currently passed to `_record_signal_analytics`.
    *   Include fields: `decision` (str), `symbol` (str), `signal` (Optional[dict]), `block_reason` (str), `external_context` (Optional[dict]), `planned_budget` (float), `planned_orders` (int), `planned_notional` (float), `placed_orders` (int), `filled_notional` (float), `realized_pnl_quote` (float), `operation_id` (str), `order_id` (str), `cycle_id` (str), `context` (Optional[dict]).
    *   Set appropriate defaults for all fields to match the current function signature.
    *   Add it to `__all__`.
2.  **Update `htxbot/monitoring.py`:**
    *   Import `SignalAnalyticsEvent` from `.models`.
    *   Change the signature of `_record_signal_analytics` to accept `self` and a single `event: SignalAnalyticsEvent` object. Alternatively, keep `decision` as a positional argument and move the rest into the dataclass, or put everything into the dataclass. Putting everything but `self` into the dataclass makes the call sites cleaner. Wait, there are many call sites. Updating all of them is required.
    *   Actually, let's keep the existing signature but accept kwargs or just update all call sites to instantiate the dataclass. Let's see how many call sites there are. Around 29 call sites based on my previous grep. This is quite manageable to refactor.
    *   Wait, is there an intermediate approach? The problem description says: "Groupings arguments into a dataclass is a moderate effort but manageable." This confirms I should update the call sites.
3.  **Refactor Call Sites:**
    *   Find all instances of `_record_signal_analytics`.
    *   Replace `bot._record_signal_analytics(...)` or `self._record_signal_analytics(...)` with `self._record_signal_analytics(SignalAnalyticsEvent(...))`.
    *   Need to make sure positional arguments are properly mapped. Most calls use keyword arguments for everything after the first `decision` argument. Let's look at `tests/test_unified_bot.py` and `htxbot/strategy_entry.py` etc. to confirm.
4.  **Run Tests and Linter:**
    *   Run `ruff format` and `ruff check --fix` on modified files.
    *   Run the test suite `PYTHONPATH=. python3 -m pytest tests/`.
5.  **Pre-commit Steps:**
    *   Run `pre_commit_instructions` tool to get required checks.
    *   Complete pre-commit steps to ensure proper testing, verification, review, and reflection are done.
