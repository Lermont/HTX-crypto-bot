from unittest import mock
# -*- coding: utf-8 -*-

import math
import unittest
import pytest

from htxbot.indicators import (
    average_true_range,
    calculate_ema,
    calculate_ema_series,
    choppiness_index,
    calculate_rsi,
    clamp,
    compute_log_return,
    realized_volatility,
)
from htxbot.signal_math import (
    btc_risk_context,
    choppiness_context,
    daily_volatility_context,
    ema_pullback_recovery_context,
    ema_signal_direction_metrics,
    gold_btc_ratio_return,
    local_reversion_context,
    relative_strength_context,
    signal_budget_multiplier,
    signal_score,
    ema_pair_side,
    volume_confirmation_context,
    volatility_multiplier,
)



def _sample_standard_deviation(values):
    mean = sum(values) / len(values)
    variance = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
    return math.sqrt(variance)


def _expected_realized_volatility(closes, window):
    sample = closes[-window - 1:]
    returns = [
        math.log(sample[index] / sample[index - 1])
        for index in range(1, len(sample))
        if sample[index] > 0 and sample[index - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    return _sample_standard_deviation(returns)


class IndicatorMathTests(unittest.TestCase):

    def test_ema_pair_side_determines_trend_direction(self):
        self.assertEqual(ema_pair_side(10.0, 5.0), "long")
        self.assertEqual(ema_pair_side(5.0, 10.0), "short")
        self.assertEqual(ema_pair_side(10.0, 10.0), "neutral")

    def test_clamp_bounds_value_without_side_effects(self):
        self.assertEqual(clamp(5, 1, 10), 5)
        self.assertEqual(clamp(0, 1, 10), 1)
        self.assertEqual(clamp(15, 1, 10), 10)

    def test_calculate_ema(self):
        # empty prices -> 0.0
        self.assertEqual(calculate_ema([], 5), 0.0)

        # period <= 0 -> 0.0
        self.assertEqual(calculate_ema([10.0, 20.0], 0), 0.0)
        self.assertEqual(calculate_ema([10.0, 20.0], -1), 0.0)

        # one price -> that price
        self.assertEqual(calculate_ema([15.0], 5), 15.0)

        # period == 1 -> last price
        self.assertEqual(calculate_ema([10.0, 20.0, 30.0], 1), 30.0)

        # constant prices -> same constant
        self.assertEqual(calculate_ema([10.0, 10.0, 10.0], 2), 10.0)

        # normal EMA calculation with a known expected value
        prices = [10.0, 20.0, 30.0]
        # alpha = 2 / (2 + 1) = 2/3
        # ema1 = 10.0
        # ema2 = 20 * 2/3 + 10 * 1/3 = 13.3333333333 + 3.3333333333 = 16.6666666667
        # ema3 = 30 * 2/3 + 16.6666666667 * 1/3 = 20 + 5.5555555556 = 25.5555555556
        self.assertAlmostEqual(calculate_ema(prices, 2), 25.5555555556)

        # period > len(prices) -> same result as period == len(prices)
        prices = [10.0, 20.0, 30.0]
        self.assertEqual(calculate_ema(prices, 10), calculate_ema(prices, 3))

        # zero prices can be included as numeric input
        self.assertAlmostEqual(calculate_ema([0.0, 0.0], 2), 0.0)

    def test_ema_and_series_share_final_value(self):
        prices = [10.0, 20.0, 30.0]
        series = calculate_ema_series(prices, 2)

        self.assertEqual(len(series), len(prices))
        self.assertAlmostEqual(series[0], 10.0)
        self.assertAlmostEqual(series[1], 16.6666666667)
        self.assertAlmostEqual(series[-1], calculate_ema(prices, 2))

    def test_calculate_ema_returns_zero_for_empty_prices(self):
        self.assertEqual(calculate_ema([], 10), 0.0)

    def test_calculate_ema_series_returns_empty_list_for_empty_prices(self):
        self.assertEqual(calculate_ema_series([], 10), [])

    def test_rsi_handles_rising_falling_flat_and_short_history(self):
        rising = [float(index) for index in range(1, 40)]
        falling = list(reversed(rising))
        flat = [10.0] * 40

        self.assertGreater(calculate_rsi(rising, 14), 50.0)
        self.assertLess(calculate_rsi(falling, 14), 50.0)
        self.assertEqual(calculate_rsi(flat, 14), 50.0)
        self.assertEqual(calculate_rsi([1.0, 2.0], 14), 0.0)

    def test_log_return_valid_positive_prices(self):
        self.assertAlmostEqual(compute_log_return(110.0, 100.0), math.log(1.1))

    def test_realized_volatility_uses_recent_window_log_returns_and_sample_variance(self):
        closes = [100.0, 110.0, 121.0, 133.1, 120.0, 108.0]
        window = 2

        expected = _expected_realized_volatility(closes, window)

        self.assertAlmostEqual(realized_volatility(closes, window), expected, places=12)

    def test_realized_volatility_ignores_non_positive_price_pairs(self):
        closes = [100.0, 105.0, 0.0, 110.0, 121.0]
        window = 4

        expected = _expected_realized_volatility(closes, window)

        self.assertGreater(expected, 0.0)
        self.assertAlmostEqual(realized_volatility(closes, window), expected, places=12)

    def test_realized_volatility_requires_at_least_two_valid_returns(self):
        self.assertEqual(realized_volatility([100.0, 101.0], 1), 0.0)
        self.assertEqual(realized_volatility([100.0, 0.0, 101.0], 2), 0.0)
        self.assertEqual(realized_volatility([100.0, 101.0], 2), 0.0)

    def test_realized_volatility_numpy_and_fallback_paths_match(self):
        import htxbot.indicators as indicators
        closes = [95.0, 101.0, 97.5, 106.0, 104.0, 111.0]
        window = 5

        numpy_result = indicators.realized_volatility(closes, window)
        with mock.patch.object(indicators, "HAS_NUMPY", False):
            fallback_result = indicators.realized_volatility(closes, window)

        self.assertAlmostEqual(fallback_result, numpy_result, places=12)

    def test_average_true_range_from_ohlcv(self):
        candles = [
            [1, 10.0, 11.0, 9.5, 10.5, 1.0],
            [2, 10.5, 12.0, 10.0, 11.0, 1.0],
            [3, 11.0, 11.5, 10.5, 10.75, 1.0],
            [4, 10.75, 13.0, 10.25, 12.5, 1.0],
        ]

        self.assertAlmostEqual(average_true_range(candles, 3), (2.0 + 1.0 + 2.75) / 3.0)
        self.assertEqual(average_true_range(candles[:2], 3), 0.0)

    def test_choppiness_index_separates_trend_from_noise(self):
        trend = [
            [index, close, close + 0.1, close - 0.1, close, 1.0]
            for index, close in enumerate(range(100, 121))
        ]
        chop = [
            [index, close, close + 0.2, close - 0.2, close, 1.0]
            for index, close in enumerate([100.0, 102.0] * 11)
        ]

        self.assertLess(choppiness_index(trend, 14), 30.0)
        self.assertGreater(choppiness_index(chop, 14), 60.0)


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

        # Multiplicative logic: ema_edge * (1.0 + rs_direction)
        # Long: rs_direction = 0.03 - 0.01 = 0.02. ema_edge = (100 - 98) / 100 * 2 = 0.04. Result = 0.04 * 1.02 = 0.0408
        self.assertAlmostEqual(long_score, 0.0408)
        # Short: rs_direction = 0.03 - 0.01 = 0.02. ema_edge = (102 - 100) / 100 * 2 = 0.04. Result = 0.04 * 1.02 = 0.0408
        self.assertAlmostEqual(short_score, 0.0408)
        # 0.0 * 1.0 = 0.0
        self.assertEqual(signal_score(0.0, 0.0, 100.0, 99.0, 100.0, "long", 1.0), 0.0)

    def test_relative_strength_uses_btc_as_benchmark(self):
        closes = [100.0, 110.0, 121.0]
        btc = [100.0, 105.0, 110.25]
        context = relative_strength_context(closes, btc, fast_window=1, slow_window=2)

        self.assertAlmostEqual(
            context["rs30"], math.log(121.0 / 110.0) - math.log(110.25 / 105.0)
        )
        self.assertAlmostEqual(
            context["rs60"], math.log(121.0 / 100.0) - math.log(110.25 / 100.0)
        )
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


    def test_pullback_recovery_context_handles_gap_threshold_and_boundaries(self):
        closes = [100.0, 99.0, 98.0, 97.0, 98.0, 100.0, 103.0, 106.0]

        # Valid recovery
        valid_ctx = ema_pullback_recovery_context(
            closes=closes,
            fast_period=2,
            slow_period=4,
            lookback=6,
            max_cross_age=3,
            gap_threshold=0.0,
            position_side="long",
        )
        self.assertTrue(valid_ctx["pullback_valid"])
        self.assertTrue(valid_ctx["pullback_recovered"])
        self.assertTrue(valid_ctx["pullback_had_pullback"])

        # Invalid due to gap_threshold
        high_threshold_ctx = ema_pullback_recovery_context(
            closes=closes,
            fast_period=2,
            slow_period=4,
            lookback=6,
            max_cross_age=3,
            gap_threshold=0.05,
            position_side="long",
        )
        self.assertFalse(high_threshold_ctx["pullback_valid"])
        self.assertFalse(high_threshold_ctx["pullback_recovered"])

        # Invalid due to max_cross_age
        strict_age_ctx = ema_pullback_recovery_context(
            closes=closes,
            fast_period=2,
            slow_period=4,
            lookback=6,
            max_cross_age=1,
            gap_threshold=0.0,
            position_side="long",
        )
        self.assertFalse(strict_age_ctx["pullback_valid"])
        self.assertEqual(strict_age_ctx["pullback_cross_age_candles"], 2)

        # Invalid due to lookback
        short_lookback_ctx = ema_pullback_recovery_context(
            closes=closes,
            fast_period=2,
            slow_period=4,
            lookback=1,
            max_cross_age=3,
            gap_threshold=0.0,
            position_side="long",
        )
        self.assertFalse(short_lookback_ctx["pullback_valid"])
        self.assertFalse(short_lookback_ctx["pullback_had_pullback"])

    def test_pullback_recovery_context_empty_or_invalid_data(self):
        # Empty closes
        empty_ctx = ema_pullback_recovery_context(
            closes=[],
            fast_period=2,
            slow_period=4,
            lookback=6,
            max_cross_age=3,
            gap_threshold=0.0,
            position_side="long",
        )
        self.assertFalse(empty_ctx["pullback_valid"])
        self.assertFalse(empty_ctx["pullback_recovered"])
        self.assertFalse(empty_ctx["pullback_had_pullback"])
        self.assertEqual(empty_ctx["pullback_cross_age_candles"], -1)

        # Flat zeros
        zeros_ctx = ema_pullback_recovery_context(
            closes=[0.0, 0.0, 0.0, 0.0],
            fast_period=2,
            slow_period=4,
            lookback=6,
            max_cross_age=3,
            gap_threshold=0.0,
            position_side="long",
        )
        self.assertFalse(zeros_ctx["pullback_valid"])
        self.assertTrue(zeros_ctx["pullback_had_pullback"])
        self.assertEqual(zeros_ctx["pullback_recovery_gap"], 0.0)


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
        self.assertEqual(long_metrics["ema_side"], "long")
        self.assertEqual(short_metrics["ema_side"], "short")
        self.assertTrue(long_metrics["ema_side_valid"])
        self.assertTrue(short_metrics["ema_side_valid"])

        # Multiplicative score logic:
        # Long base_trend = 0.05 (macro gap) + 0.02 (trigger gap) = 0.07
        # Long pullback_depth = (102.0 - 101.0) / 100.0 = 0.01
        # Long pullback multiplier = 1.0 + 0.01 = 1.01
        # Long rs multiplier = 1.0 + 0.02 = 1.02
        # Score = 0.07 * 1.01 * 1.02 = 0.072114
        self.assertAlmostEqual(long_metrics["score"], 0.072114)

        # Short base_trend = 0.05 (macro gap) + 0.04 (trigger gap) = 0.09
        # Short pullback_depth = (101.0 - 98.0) / 100.0 = 0.03
        # Short pullback multiplier = 1.0 + 0.03 = 1.03
        # Short rs multiplier = 1.0 + 0.02 (rs60 is -0.02 -> short direction is 0.02) = 1.02
        # Score = 0.09 * 1.03 * 1.02 = 0.094554
        self.assertAlmostEqual(short_metrics["score"], 0.094554)

        self.assertTrue(long_metrics["add_valid"])
        self.assertTrue(short_metrics["add_valid"])

    def test_ema_signal_direction_metrics_allow_pullback_or_trend_entry(self):
        metrics = ema_signal_direction_metrics(
            "long",
            current_close=100.0,
            ema_macro_fast=105.0,
            ema_macro_slow=100.0,
            ema_pullback_fast=102.0,
            ema_pullback_slow=101.0,
            ema_trigger_fast=97.0,
            ema_trigger_slow=101.0,
            pullback_valid=True,
            rs60=0.02,
            btc_return_30m=0.0,
            use_rs_confirmation=False,
            long_min_rs60=0.0,
            short_max_rs60=0.0,
            use_btc_risk_filter=False,
            btc_long_min_return_30m=0.0,
            btc_short_max_return_30m=0.0,
        )

        self.assertEqual(metrics["ema_macro_side"], "long")
        self.assertEqual(metrics["ema_trigger_side"], "short")
        self.assertEqual(metrics["ema_side"], "neutral")
        self.assertFalse(metrics["ema_side_valid"])
        self.assertTrue(metrics["entry_setup_valid"])
        self.assertTrue(metrics["entry_side_valid"])
        self.assertEqual(metrics["entry_signal_source"], "pullback")
        self.assertTrue(metrics["entry_valid"])

        short_metrics = ema_signal_direction_metrics(
            "short",
            current_close=100.0,
            ema_macro_fast=95.0,
            ema_macro_slow=100.0,
            ema_pullback_fast=98.0,
            ema_pullback_slow=101.0,
            ema_trigger_fast=103.0,
            ema_trigger_slow=101.0,
            pullback_valid=True,
            rs60=-0.02,
            btc_return_30m=0.0,
            use_rs_confirmation=True,
            long_min_rs60=0.0,
            short_max_rs60=0.0,
            use_btc_risk_filter=False,
            btc_long_min_return_30m=0.0,
            btc_short_max_return_30m=0.0,
        )

        self.assertEqual(short_metrics["ema_macro_side"], "short")
        self.assertEqual(short_metrics["ema_trigger_side"], "long")
        self.assertEqual(short_metrics["ema_side"], "neutral")
        self.assertFalse(short_metrics["ema_side_valid"])
        self.assertTrue(short_metrics["entry_setup_valid"])
        self.assertTrue(short_metrics["entry_side_valid"])
        self.assertEqual(short_metrics["entry_signal_source"], "pullback")
        self.assertTrue(short_metrics["entry_valid"])

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
        self.assertAlmostEqual(
            signal_budget_multiplier(0.5, True, 1.0, 0.25, 1.0), 0.625
        )
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

        self.assertAlmostEqual(
            gold_btc_ratio_return(gold, btc, 2),
            math.log((110.0 / 55.0) / (100.0 / 50.0)),
        )
        self.assertAlmostEqual(
            gold_btc_ratio_return(gold, btc, 2, direct_closes=direct),
            math.log(2.0 / 2.0),
        )
        self.assertEqual(gold_btc_ratio_return(gold[:1], btc[:1], 2), 0.0)

    def test_local_reversion_context_uses_side_specific_edge(self):
        closes = [100.0, 110.0, 105.0]

        long_context = local_reversion_context(closes, 105.0, "long")
        short_context = local_reversion_context(closes, 105.0, "short")

        self.assertAlmostEqual(long_context["local_reversion"], (110.0 - 105.0) / 110.0)
        self.assertAlmostEqual(
            short_context["local_reversion"], (105.0 - 100.0) / 100.0
        )

    def test_volume_confirmation_context_requires_recent_volume_expansion(self):
        quiet = [[index, 100.0, 101.0, 99.0, 101.0, 1.0] for index in range(20)]
        confirmed = [
            [index, 100.0, 101.0, 99.0, 101.0, 1.0 if index < 15 else 3.0]
            for index in range(20)
        ]

        quiet_context = volume_confirmation_context(
            quiet,
            short_window=5,
            long_window=20,
            min_ratio=1.05,
            min_directional_fraction=0.0,
            position_side="long",
        )
        confirmed_context = volume_confirmation_context(
            confirmed,
            short_window=5,
            long_window=20,
            min_ratio=1.05,
            min_directional_fraction=0.0,
            position_side="long",
        )

        self.assertFalse(quiet_context["volume_valid"])
        self.assertEqual(quiet_context["volume_reason"], "volume_ratio_below_min")
        self.assertTrue(confirmed_context["volume_valid"])
        self.assertGreater(confirmed_context["volume_ratio"], 1.05)

    def test_volume_spike_can_confirm_pullback_recovery(self):
        candles = [[index, 100.0, 101.0, 99.0, 101.0, 10.0] for index in range(24)]
        candles.append([24, 101.0, 104.0, 100.0, 103.0, 80.0])

        context = volume_confirmation_context(
            candles,
            short_window=5,
            long_window=20,
            min_ratio=3.0,
            min_directional_fraction=0.0,
            position_side="long",
            spike_window=5,
            spike_min_ratio=1.80,
            adverse_spike_min_ratio=2.00,
        )

        self.assertTrue(context["volume_valid"])
        self.assertFalse(context["volume_average_valid"])
        self.assertGreater(context["volume_spike_ratio"], 1.80)
        self.assertEqual(context["volume_spike_direction"], "long")
        self.assertEqual(context["volume_reason"], "volume_spike_confirmed")

    def test_volume_profile_blocks_adverse_spike_breaks_symmetrically(self):
        long_candles = [[index, 100.0, 101.0, 99.0, 100.0, 10.0] for index in range(59)]
        long_candles.append([59, 100.0, 101.0, 89.0, 90.0, 30.0])
        short_candles = [
            [index, 100.0, 101.0, 99.0, 100.0, 10.0] for index in range(59)
        ]
        short_candles.append([59, 100.0, 111.0, 99.0, 110.0, 30.0])

        long_context = volume_confirmation_context(
            long_candles,
            short_window=5,
            long_window=20,
            min_ratio=1.0,
            min_directional_fraction=0.0,
            position_side="long",
            spike_window=5,
            spike_min_ratio=1.80,
            adverse_spike_min_ratio=2.00,
            profile_enabled=True,
            profile_window=60,
            profile_bins=12,
            profile_value_area=0.70,
        )
        short_context = volume_confirmation_context(
            short_candles,
            short_window=5,
            long_window=20,
            min_ratio=1.0,
            min_directional_fraction=0.0,
            position_side="short",
            spike_window=5,
            spike_min_ratio=1.80,
            adverse_spike_min_ratio=2.00,
            profile_enabled=True,
            profile_window=60,
            profile_bins=12,
            profile_value_area=0.70,
        )

        self.assertFalse(long_context["volume_valid"])
        self.assertFalse(short_context["volume_valid"])
        self.assertFalse(long_context["volume_profile_valid"])
        self.assertFalse(short_context["volume_profile_valid"])
        self.assertTrue(long_context["volume_profile_break"])
        self.assertTrue(short_context["volume_profile_break"])
        self.assertEqual(long_context["volume_spike_direction"], "short")
        self.assertEqual(short_context["volume_spike_direction"], "long")

    def test_market_structure_math_is_direction_symmetric(self):
        long_candles = [[index, 100.0, 101.0, 99.0, 101.0, 2.0] for index in range(20)]
        short_candles = [
            [index, 101.0, 102.0, 100.0, 100.0, 2.0] for index in range(20)
        ]

        long_volume = volume_confirmation_context(
            long_candles, 5, 20, 1.0, 0.60, "long"
        )
        short_volume = volume_confirmation_context(
            short_candles, 5, 20, 1.0, 0.60, "short"
        )
        long_chop = choppiness_context(long_candles, 14, 61.8)
        short_chop = choppiness_context(short_candles, 14, 61.8)

        self.assertTrue(long_volume["volume_valid"])
        self.assertTrue(short_volume["volume_valid"])
        self.assertEqual(long_volume["volume_directional_fraction"], 1.0)
        self.assertEqual(short_volume["volume_directional_fraction"], 1.0)
        self.assertEqual(long_chop["chop_valid"], short_chop["chop_valid"])


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize(
    ("price_now", "price_then"),
    [
        (-5.0, 100.0),
        (0.0, 100.0),
        (100.0, -5.0),
        (100.0, 0.0),
        (-5.0, -5.0),
        (0.0, 0.0),
    ],
)
def test_log_return_rejects_non_positive_prices(price_now, price_then):
    assert compute_log_return(price_now, price_then) == 0.0
