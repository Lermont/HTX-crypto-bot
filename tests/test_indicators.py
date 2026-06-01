# -*- coding: utf-8 -*-

import math
import unittest

from htxbot.indicators import (
    average_true_range,
    calculate_ema,
    calculate_ema_series,
    calculate_rsi,
    clamp,
    compute_log_return,
    realized_volatility,
)
from htxbot.signal_math import (
    btc_risk_context,
    daily_volatility_context,
    ema_pullback_recovery_context,
    ema_signal_direction_metrics,
    gold_btc_ratio_return,
    local_reversion_context,
    relative_strength_context,
    signal_budget_multiplier,
    signal_score,
    volatility_multiplier,
)


class IndicatorMathTests(unittest.TestCase):
    def test_clamp_bounds_value_without_side_effects(self):
        self.assertEqual(clamp(5, 1, 10), 5)
        self.assertEqual(clamp(0, 1, 10), 1)
        self.assertEqual(clamp(15, 1, 10), 10)

    def test_ema_and_series_share_final_value(self):
        prices = [10.0, 20.0, 30.0]
        series = calculate_ema_series(prices, 2)

        self.assertEqual(len(series), len(prices))
        self.assertAlmostEqual(series[0], 10.0)
        self.assertAlmostEqual(series[1], 16.6666666667)
        self.assertAlmostEqual(series[-1], calculate_ema(prices, 2))

    def test_rsi_handles_rising_falling_flat_and_short_history(self):
        rising = [float(index) for index in range(1, 40)]
        falling = list(reversed(rising))
        flat = [10.0] * 40

        self.assertGreater(calculate_rsi(rising, 14), 50.0)
        self.assertLess(calculate_rsi(falling, 14), 50.0)
        self.assertEqual(calculate_rsi(flat, 14), 50.0)
        self.assertEqual(calculate_rsi([1.0, 2.0], 14), 0.0)

    def test_log_return_rejects_non_positive_prices(self):
        self.assertEqual(compute_log_return(0.0, 100.0), 0.0)
        self.assertEqual(compute_log_return(100.0, 0.0), 0.0)
        self.assertEqual(compute_log_return(-5.0, 100.0), 0.0)
        self.assertAlmostEqual(compute_log_return(110.0, 100.0), math.log(1.1))

    def test_realized_volatility_matches_sample_variance(self):
        closes = [100.0, 105.0, 102.0, 108.0]
        returns = [math.log(105.0 / 100.0), math.log(102.0 / 105.0), math.log(108.0 / 102.0)]
        mean = sum(returns) / len(returns)
        expected = math.sqrt(sum((item - mean) ** 2 for item in returns) / (len(returns) - 1))

        self.assertAlmostEqual(realized_volatility(closes, 3), expected)
        self.assertEqual(realized_volatility(closes, 1), 0.0)
        self.assertEqual(realized_volatility([100.0, 0.0, -1.0, 102.0], 3), 0.0)

    def test_average_true_range_from_ohlcv(self):
        candles = [
            [1, 10.0, 11.0, 9.5, 10.5, 1.0],
            [2, 10.5, 12.0, 10.0, 11.0, 1.0],
            [3, 11.0, 11.5, 10.5, 10.75, 1.0],
            [4, 10.75, 13.0, 10.25, 12.5, 1.0],
        ]

        self.assertAlmostEqual(average_true_range(candles, 3), (2.0 + 1.0 + 2.75) / 3.0)
        self.assertEqual(average_true_range(candles[:2], 3), 0.0)


class SignalMathTests(unittest.TestCase):
    def test_signal_score_is_directional_and_clamped_at_zero(self):
        long_score = signal_score(
            rs30=0.01,
            rs60=0.03,
            ema50=98.0,
            ema100=100.0,
            price=100.0,
            position_side="long",
            ema_gap_weight=2.0,
        )
        short_score = signal_score(
            rs30=0.03,
            rs60=0.01,
            ema50=102.0,
            ema100=100.0,
            price=100.0,
            position_side="short",
            ema_gap_weight=2.0,
        )

        self.assertAlmostEqual(long_score, 0.02 + 0.04)
        self.assertAlmostEqual(short_score, long_score)
        self.assertEqual(signal_score(0.0, 0.0, 100.0, 99.0, 100.0, "long", 1.0), 0.0)

    def test_relative_strength_uses_btc_as_benchmark(self):
        closes = [100.0, 110.0, 121.0]
        btc = [100.0, 105.0, 110.25]
        context = relative_strength_context(closes, btc, fast_window=1, slow_window=2)

        self.assertAlmostEqual(context["rs30"], math.log(121.0 / 110.0) - math.log(110.25 / 105.0))
        self.assertAlmostEqual(context["rs60"], math.log(121.0 / 100.0) - math.log(110.25 / 100.0))
        self.assertAlmostEqual(context["btc_return_30m"], math.log(110.25 / 105.0))

    def test_pullback_recovery_context_is_mirrored_for_long_and_short(self):
        long_context = ema_pullback_recovery_context(
            [100.0, 99.0, 98.0, 97.0, 98.0, 100.0, 103.0, 106.0],
            fast_period=2,
            slow_period=4,
            lookback=6,
            max_cross_age=3,
            gap_threshold=0.0,
            position_side="long",
        )
        short_context = ema_pullback_recovery_context(
            [100.0, 101.0, 102.0, 103.0, 102.0, 100.0, 97.0, 94.0],
            fast_period=2,
            slow_period=4,
            lookback=6,
            max_cross_age=3,
            gap_threshold=0.0,
            position_side="short",
        )

        self.assertTrue(long_context["pullback_valid"])
        self.assertTrue(short_context["pullback_valid"])
        self.assertGreater(long_context["pullback_recovery_gap"], 0.0)
        self.assertGreater(short_context["pullback_recovery_gap"], 0.0)

    def test_ema_signal_direction_metrics_preserve_long_short_invariants(self):
        long_metrics = ema_signal_direction_metrics(
            "long",
            current_close=100.0,
            ema_macro_fast=105.0,
            ema_macro_slow=100.0,
            ema_pullback_fast=102.0,
            ema_pullback_slow=101.0,
            ema_trigger_fast=103.0,
            ema_trigger_slow=101.0,
            pullback_valid=True,
            rs60=0.02,
            btc_return_30m=0.01,
            use_rs_confirmation=True,
            long_min_rs60=0.0,
            short_max_rs60=0.0,
            use_btc_risk_filter=True,
            btc_long_min_return_30m=-0.005,
            btc_short_max_return_30m=0.005,
        )
        short_metrics = ema_signal_direction_metrics(
            "short",
            current_close=100.0,
            ema_macro_fast=95.0,
            ema_macro_slow=100.0,
            ema_pullback_fast=98.0,
            ema_pullback_slow=101.0,
            ema_trigger_fast=97.0,
            ema_trigger_slow=101.0,
            pullback_valid=True,
            rs60=-0.02,
            btc_return_30m=-0.01,
            use_rs_confirmation=True,
            long_min_rs60=0.0,
            short_max_rs60=0.0,
            use_btc_risk_filter=True,
            btc_long_min_return_30m=-0.005,
            btc_short_max_return_30m=0.005,
        )

        self.assertTrue(long_metrics["entry_valid"])
        self.assertTrue(short_metrics["entry_valid"])
        self.assertAlmostEqual(long_metrics["score"], 0.05 + 0.02 + 0.01 + 0.02)
        self.assertAlmostEqual(short_metrics["score"], 0.05 + 0.04 + 0.03 + 0.02)
        self.assertTrue(long_metrics["add_valid"])
        self.assertTrue(short_metrics["add_valid"])

    def test_ema_signal_direction_metrics_block_invalid_filters(self):
        metrics = ema_signal_direction_metrics(
            "long",
            current_close=100.0,
            ema_macro_fast=105.0,
            ema_macro_slow=100.0,
            ema_pullback_fast=102.0,
            ema_pullback_slow=101.0,
            ema_trigger_fast=103.0,
            ema_trigger_slow=101.0,
            pullback_valid=True,
            rs60=-0.01,
            btc_return_30m=-0.02,
            use_rs_confirmation=True,
            long_min_rs60=0.0,
            short_max_rs60=0.0,
            use_btc_risk_filter=True,
            btc_long_min_return_30m=-0.005,
            btc_short_max_return_30m=0.005,
        )

        self.assertTrue(metrics["macro_valid"])
        self.assertTrue(metrics["trigger_valid"])
        self.assertFalse(metrics["rs_confirm_valid"])
        self.assertFalse(metrics["btc_entry_valid"])
        self.assertFalse(metrics["entry_valid"])

    def test_budget_and_volatility_multipliers_are_pure_math(self):
        self.assertEqual(signal_budget_multiplier(10.0, False, 1.0, 0.25, 1.0), 1.0)
        self.assertAlmostEqual(signal_budget_multiplier(0.5, True, 1.0, 0.25, 1.0), 0.625)
        self.assertEqual(volatility_multiplier(10.0, False, 1.0, 0.5, 2.0), 1.0)
        self.assertEqual(volatility_multiplier(10.0, True, 1.0, 0.5, 2.0), 2.0)

    def test_daily_volatility_context_targets_budget_without_exchange(self):
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        context = daily_volatility_context(
            closes,
            window=4,
            reference=0.000001,
            enable_targeted_sizing=True,
            min_budget_multiplier=0.25,
            max_budget_multiplier=2.0,
        )

        self.assertGreater(context["daily_volatility"], 0.0)
        self.assertGreater(context["daily_volatility_multiplier"], 1.0)
        self.assertEqual(context["volatility_budget_multiplier"], 0.25)

    def test_btc_risk_context_is_directional(self):
        long_context = btc_risk_context(
            [100.0, 95.0, 90.0],
            position_side="long",
            enabled=True,
            return_window=1,
            volatility_window=2,
            drop_threshold=0.0,
            drop_budget_multiplier=0.5,
            high_vol_threshold=999.0,
            vol_budget_multiplier=0.5,
            min_budget_multiplier=0.25,
            max_ladder_multiplier=2.0,
        )
        short_context = btc_risk_context(
            [100.0, 105.0, 110.0],
            position_side="short",
            enabled=True,
            return_window=1,
            volatility_window=2,
            drop_threshold=0.0,
            drop_budget_multiplier=0.5,
            high_vol_threshold=999.0,
            vol_budget_multiplier=0.5,
            min_budget_multiplier=0.25,
            max_ladder_multiplier=2.0,
        )

        self.assertEqual(long_context["reason"], "btc_drop")
        self.assertEqual(short_context["reason"], "btc_rise")
        self.assertEqual(long_context["budget_multiplier"], 0.5)
        self.assertEqual(short_context["budget_multiplier"], 0.5)

    def test_gold_btc_ratio_return_supports_direct_and_derived_ratios(self):
        gold = [100.0, 105.0, 110.0]
        btc = [50.0, 50.0, 55.0]
        direct = [2.0, 2.1, 2.0]

        self.assertAlmostEqual(gold_btc_ratio_return(gold, btc, 2), math.log((110.0 / 55.0) / (100.0 / 50.0)))
        self.assertAlmostEqual(gold_btc_ratio_return(gold, btc, 2, direct_closes=direct), math.log(2.0 / 2.0))
        self.assertEqual(gold_btc_ratio_return(gold[:1], btc[:1], 2), 0.0)

    def test_local_reversion_context_uses_side_specific_edge(self):
        closes = [100.0, 110.0, 105.0]

        long_context = local_reversion_context(closes, 105.0, "long")
        short_context = local_reversion_context(closes, 105.0, "short")

        self.assertAlmostEqual(long_context["local_reversion"], (110.0 - 105.0) / 110.0)
        self.assertAlmostEqual(short_context["local_reversion"], (105.0 - 100.0) / 100.0)


if __name__ == "__main__":
    unittest.main()
