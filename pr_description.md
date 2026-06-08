## 🧪 [testing improvement] Add test for StrategyRiskMixin's finish helper

### 🎯 What:
Addressed a testing gap in `htxbot/strategy_risk.py` where the nested `finish` helper function within `_risk_budget` lacked explicit test coverage.

### 📊 Coverage:
A new test `test_risk_budget_finish_method_updates_context` has been added to `tests/test_unified_bot.py`. It simulates a scenario (`free_margin_below_reserve`) that triggers the `finish` helper. The test verifies that:
- The `budget` and `reason` are correctly returned.
- The `_last_risk_budget_context` is properly assigned and populated with expected runtime context values like `free`, `equity`, `reserve`, `is_new_position`, and `budget_scale`.

### ✨ Result:
The inner behavior of the `_risk_budget` flow is now reliably verified under test, increasing overall test confidence without altering existing behavior.
