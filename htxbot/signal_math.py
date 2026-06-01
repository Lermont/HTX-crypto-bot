# -*- coding: utf-8 -*-

import math
from typing import Optional, Sequence

from .indicators import calculate_ema_series, clamp, compute_log_return, realized_volatility


def signal_score(
    rs30: float,
    rs60: float,
    ema50: float,
    ema100: float,
    price: float,
    position_side: str,
    ema_gap_weight: float,
) -> float:
    if str(position_side).lower() == "short":
        rs_edge = max(0.0, rs30 - rs60)
    else:
        rs_edge = max(0.0, rs60 - rs30)

    ema_edge = 0.0
    if price > 0:
        if str(position_side).lower() == "short":
            ema_gap = (ema50 - ema100) / price
        else:
            ema_gap = (ema100 - ema50) / price
        ema_edge = max(0.0, ema_gap) * ema_gap_weight
    return rs_edge + ema_edge


def local_reversion_context(closes: Sequence[float], current_close: float, position_side: str, window: int = 15) -> dict:
    recent = []
    for price in closes[-window - 1:]:
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if value > 0:
            recent.append(value)
    if not recent or current_close <= 0:
        return {
            "pullback_from_high": 0.0,
            "bounce_from_low": 0.0,
            "local_reversion": 0.0,
        }

    recent_high = max(recent)
    recent_low = min(recent)
    pullback_from_high = (recent_high - current_close) / recent_high if recent_high > 0 else 0.0
    bounce_from_low = (current_close - recent_low) / recent_low if recent_low > 0 else 0.0
    directional_reversion = bounce_from_low if str(position_side).lower() == "short" else pullback_from_high
    return {
        "pullback_from_high": max(0.0, pullback_from_high),
        "bounce_from_low": max(0.0, bounce_from_low),
        "local_reversion": max(0.0, directional_reversion),
    }


def signal_budget_multiplier(
    score: float,
    enabled: bool,
    reference: float,
    min_multiplier: float,
    max_multiplier: float,
) -> float:
    if not enabled:
        return 1.0
    ratio = clamp(score / max(reference, 1e-12), 0.0, 1.0)
    return min_multiplier + (max_multiplier - min_multiplier) * ratio


def volatility_multiplier(
    volatility: float,
    enabled: bool,
    reference: float,
    min_multiplier: float,
    max_multiplier: float,
) -> float:
    if not enabled:
        return 1.0
    return clamp(volatility / max(reference, 1e-12), min_multiplier, max_multiplier)


def daily_volatility_context(
    closes: Sequence[float],
    window: int,
    reference: float,
    enable_targeted_sizing: bool,
    min_budget_multiplier: float,
    max_budget_multiplier: float,
) -> dict:
    window = int(window)
    if window <= 1 or len(closes) < window + 1:
        return {
            "daily_volatility": 0.0,
            "daily_volatility_multiplier": 1.0,
            "volatility_budget_multiplier": 1.0,
        }

    daily_volatility = realized_volatility(closes, window) * math.sqrt(window)
    daily_volatility_multiplier = daily_volatility / max(reference, 1e-12)
    volatility_budget = 1.0
    if enable_targeted_sizing:
        if daily_volatility_multiplier > 0:
            raw_budget = 1.0 / daily_volatility_multiplier
        else:
            raw_budget = max_budget_multiplier
        volatility_budget = clamp(raw_budget, min_budget_multiplier, max_budget_multiplier)

    return {
        "daily_volatility": daily_volatility,
        "daily_volatility_multiplier": daily_volatility_multiplier,
        "volatility_budget_multiplier": volatility_budget,
    }


def btc_risk_context(
    benchmark_closes: Sequence[float],
    position_side: str,
    enabled: bool,
    return_window: int,
    volatility_window: int,
    drop_threshold: float,
    drop_budget_multiplier: float,
    high_vol_threshold: float,
    vol_budget_multiplier: float,
    min_budget_multiplier: float,
    max_ladder_multiplier: float,
) -> dict:
    if not enabled:
        return {
            "return": 0.0,
            "volatility": 0.0,
            "budget_multiplier": 1.0,
            "ladder_multiplier": 1.0,
            "reason": "disabled",
        }

    return_window = int(return_window)
    volatility_window = int(volatility_window)
    btc_return = 0.0
    if return_window > 0 and len(benchmark_closes) > return_window:
        btc_return = compute_log_return(benchmark_closes[-1], benchmark_closes[-return_window - 1])
    btc_volatility = realized_volatility(benchmark_closes, volatility_window)

    budget_multiplier = 1.0
    reasons = []
    if str(position_side).lower() == "short":
        btc_risk_move = btc_return >= -drop_threshold
        btc_risk_reason = "btc_rise"
    else:
        btc_risk_move = btc_return <= drop_threshold
        btc_risk_reason = "btc_drop"

    if btc_risk_move:
        budget_multiplier *= drop_budget_multiplier
        reasons.append(btc_risk_reason)
    if btc_volatility >= high_vol_threshold:
        budget_multiplier *= vol_budget_multiplier
        reasons.append("btc_high_vol")

    budget_multiplier = clamp(budget_multiplier, min_budget_multiplier, 1.0)
    ladder_multiplier = clamp(1.0 + (1.0 - budget_multiplier), 1.0, max_ladder_multiplier)
    return {
        "return": btc_return,
        "volatility": btc_volatility,
        "budget_multiplier": budget_multiplier,
        "ladder_multiplier": ladder_multiplier,
        "reason": "+".join(reasons) if reasons else "neutral",
    }


def gold_btc_ratio_return(
    gold_closes: Sequence[float],
    btc_closes: Sequence[float],
    window: int,
    direct_closes: Optional[Sequence[float]] = None,
) -> float:
    window = max(1, int(window))
    if direct_closes:
        values = [float(price) for price in direct_closes if float(price) > 0]
        if len(values) > window:
            return compute_log_return(values[-1], values[-window - 1])
        return 0.0

    count = min(len(gold_closes), len(btc_closes))
    if count <= window:
        return 0.0
    ratios = []
    for gold, btc in zip(gold_closes[-count:], btc_closes[-count:]):
        if gold > 0 and btc > 0:
            ratios.append(gold / btc)
    if len(ratios) <= window:
        return 0.0
    return compute_log_return(ratios[-1], ratios[-window - 1])


def ema_pullback_recovery_context(
    closes: Sequence[float],
    fast_period: int,
    slow_period: int,
    lookback: int,
    max_cross_age: int,
    gap_threshold: float,
    position_side: str,
) -> dict:
    lookback = max(1, int(lookback))
    max_cross_age = max(1, int(max_cross_age))
    gap_threshold = max(0.0, float(gap_threshold))
    fast_series = calculate_ema_series(closes, fast_period)
    slow_series = calculate_ema_series(closes, slow_period)

    signed_gaps = []
    for fast, slow in zip(fast_series, slow_series):
        if slow <= 0:
            signed_gaps.append(0.0)
            continue
        if str(position_side).lower() == "short":
            signed_gaps.append((slow - fast) / slow)
        else:
            signed_gaps.append((fast - slow) / slow)

    current_gap = signed_gaps[-1] if signed_gaps else 0.0
    if gap_threshold > 0:
        recovered = current_gap + 1e-12 >= gap_threshold
    else:
        recovered = current_gap > 0

    history_start = max(0, len(signed_gaps) - lookback - 1)
    recent_gaps = signed_gaps[history_start:]
    had_pullback = any(gap <= 0 for gap in recent_gaps)

    last_cross_index = None
    for index in range(max(1, history_start), len(signed_gaps)):
        if signed_gaps[index] > 0 and signed_gaps[index - 1] <= 0:
            last_cross_index = index

    cross_age = len(signed_gaps) - 1 - last_cross_index if last_cross_index is not None else -1
    fresh_cross = cross_age >= 0 and cross_age <= max_cross_age
    valid = bool(recovered and had_pullback and fresh_cross)

    return {
        "pullback_valid": valid,
        "pullback_recovered": recovered,
        "pullback_had_pullback": had_pullback,
        "pullback_cross_age_candles": cross_age,
        "pullback_recovery_lookback_candles": lookback,
        "pullback_recovery_max_cross_age_candles": max_cross_age,
        "pullback_recovery_gap": current_gap,
        "pullback_recovery_min_gap": gap_threshold,
    }


def relative_strength_context(
    closes: Sequence[float],
    benchmark_closes: Sequence[float],
    fast_window: int,
    slow_window: int,
) -> dict:
    if not closes or not benchmark_closes:
        return {"rs30": 0.0, "rs60": 0.0, "btc_return_30m": 0.0}
    current_close = closes[-1]
    current_btc = benchmark_closes[-1]
    if current_close <= 0 or current_btc <= 0:
        return {"rs30": 0.0, "rs60": 0.0, "btc_return_30m": 0.0}

    fast_window = max(1, int(fast_window))
    slow_window = max(1, int(slow_window))
    required_window = max(fast_window, slow_window)
    if len(closes) <= required_window or len(benchmark_closes) <= required_window:
        return {"rs30": 0.0, "rs60": 0.0, "btc_return_30m": 0.0}

    rs30 = compute_log_return(current_close, closes[-fast_window - 1]) - compute_log_return(
        current_btc,
        benchmark_closes[-fast_window - 1],
    )
    rs60 = compute_log_return(current_close, closes[-slow_window - 1]) - compute_log_return(
        current_btc,
        benchmark_closes[-slow_window - 1],
    )
    btc_return = compute_log_return(current_btc, benchmark_closes[-fast_window - 1])
    return {"rs30": rs30, "rs60": rs60, "btc_return_30m": btc_return}


def ema_signal_direction_metrics(
    position_side: str,
    current_close: float,
    ema_macro_fast: float,
    ema_macro_slow: float,
    ema_pullback_fast: float,
    ema_pullback_slow: float,
    ema_trigger_fast: float,
    ema_trigger_slow: float,
    pullback_valid: bool,
    rs60: float,
    btc_return_30m: float,
    use_rs_confirmation: bool,
    long_min_rs60: float,
    short_max_rs60: float,
    use_btc_risk_filter: bool,
    btc_long_min_return_30m: float,
    btc_short_max_return_30m: float,
) -> dict:
    if current_close <= 0:
        return {
            "macro_valid": False,
            "pullback_valid": bool(pullback_valid),
            "trigger_valid": False,
            "rs_confirm_valid": False,
            "btc_entry_valid": False,
            "macro_gap": 0.0,
            "trigger_gap": 0.0,
            "pullback_depth": 0.0,
            "rs_edge": 0.0,
            "score": 0.0,
            "entry_valid": False,
            "add_valid": False,
        }

    if str(position_side).lower() == "short":
        macro_valid = ema_macro_fast < ema_macro_slow
        trigger_valid = ema_trigger_fast < ema_trigger_slow
        rs_confirm_valid = (not use_rs_confirmation) or rs60 <= short_max_rs60
        btc_entry_valid = (not use_btc_risk_filter) or btc_return_30m <= btc_short_max_return_30m
        macro_gap = (ema_macro_slow - ema_macro_fast) / current_close
        trigger_gap = (ema_trigger_slow - ema_trigger_fast) / current_close
        pullback_depth = (ema_pullback_slow - ema_pullback_fast) / current_close
        rs_edge = max(0.0, -rs60)
    else:
        macro_valid = ema_macro_fast > ema_macro_slow
        trigger_valid = ema_trigger_fast > ema_trigger_slow
        rs_confirm_valid = (not use_rs_confirmation) or rs60 >= long_min_rs60
        btc_entry_valid = (not use_btc_risk_filter) or btc_return_30m >= btc_long_min_return_30m
        macro_gap = (ema_macro_fast - ema_macro_slow) / current_close
        trigger_gap = (ema_trigger_fast - ema_trigger_slow) / current_close
        pullback_depth = (ema_pullback_fast - ema_pullback_slow) / current_close
        rs_edge = max(0.0, rs60)

    score = macro_gap + trigger_gap + pullback_depth + rs_edge
    entry_valid = bool(macro_valid and pullback_valid and trigger_valid and rs_confirm_valid and btc_entry_valid)
    add_valid = bool(macro_valid and (trigger_valid or pullback_valid))
    return {
        "macro_valid": macro_valid,
        "pullback_valid": bool(pullback_valid),
        "trigger_valid": trigger_valid,
        "rs_confirm_valid": rs_confirm_valid,
        "btc_entry_valid": btc_entry_valid,
        "macro_gap": macro_gap,
        "trigger_gap": trigger_gap,
        "pullback_depth": pullback_depth,
        "rs_edge": rs_edge,
        "score": score,
        "entry_valid": entry_valid,
        "add_valid": add_valid,
    }


__all__ = [
    "btc_risk_context",
    "daily_volatility_context",
    "ema_pullback_recovery_context",
    "ema_signal_direction_metrics",
    "gold_btc_ratio_return",
    "local_reversion_context",
    "relative_strength_context",
    "signal_budget_multiplier",
    "signal_score",
    "volatility_multiplier",
]
