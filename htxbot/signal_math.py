# -*- coding: utf-8 -*-

import math
from typing import Optional, Sequence

from .indicators import calculate_ema_series, choppiness_index, clamp, compute_log_return, realized_volatility


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
        rs_direction = rs30 - rs60
        rs_edge = max(0.0, rs_direction)
    else:
        rs_direction = rs60 - rs30
        rs_edge = max(0.0, rs_direction)

    ema_edge = 0.0
    if price > 0:
        if str(position_side).lower() == "short":
            ema_gap = (ema50 - ema100) / price
        else:
            ema_gap = (ema100 - ema50) / price
        ema_edge = max(0.0, ema_gap) * ema_gap_weight

    rs_multiplier = max(0.01, 1.0 + rs_direction)
    return ema_edge * rs_multiplier


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
        rs_direction = -rs60
        rs_edge = max(0.0, rs_direction)
    else:
        macro_valid = ema_macro_fast > ema_macro_slow
        trigger_valid = ema_trigger_fast > ema_trigger_slow
        rs_confirm_valid = (not use_rs_confirmation) or rs60 >= long_min_rs60
        btc_entry_valid = (not use_btc_risk_filter) or btc_return_30m >= btc_long_min_return_30m
        macro_gap = (ema_macro_fast - ema_macro_slow) / current_close
        trigger_gap = (ema_trigger_fast - ema_trigger_slow) / current_close
        pullback_depth = (ema_pullback_fast - ema_pullback_slow) / current_close
        rs_direction = rs60
        rs_edge = max(0.0, rs_direction)

    rs_multiplier = max(0.01, 1.0 + rs_direction)
    pullback_multiplier = max(0.01, 1.0 + pullback_depth)
    base_trend = macro_gap + trigger_gap
    score = base_trend * pullback_multiplier * rs_multiplier
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


def _volume_direction(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "long"
    if close_price < open_price:
        return "short"
    return "neutral"


def _volume_profile_context(
    rows: Sequence[tuple],
    profile_window: int,
    bins: int,
    value_area_fraction: float,
    position_side: str,
) -> dict:
    profile_window = max(1, int(profile_window))
    bins = max(2, int(bins))
    value_area_fraction = clamp(float(value_area_fraction), 0.10, 1.0)
    if len(rows) < profile_window:
        return {
            "volume_profile_valid": False,
            "volume_profile_poc": 0.0,
            "volume_profile_value_area_low": 0.0,
            "volume_profile_value_area_high": 0.0,
            "volume_profile_break": False,
            "volume_profile_reason": f"volume_profile_history_short;candles={len(rows)};required={profile_window}",
        }

    profile_rows = list(rows[-profile_window:])
    low_price = min(row[2] for row in profile_rows)
    high_price = max(row[1] for row in profile_rows)
    current_close = profile_rows[-1][3]
    if low_price <= 0 or high_price <= low_price or current_close <= 0:
        return {
            "volume_profile_valid": True,
            "volume_profile_poc": current_close,
            "volume_profile_value_area_low": low_price if low_price > 0 else 0.0,
            "volume_profile_value_area_high": high_price if high_price > 0 else 0.0,
            "volume_profile_break": False,
            "volume_profile_reason": "volume_profile_flat_range",
        }

    bin_size = (high_price - low_price) / bins
    bucket_volume = [0.0 for _ in range(bins)]
    for open_price, high, low, close_price, volume in profile_rows:
        typical_price = (high + low + close_price) / 3.0
        raw_index = int((typical_price - low_price) / bin_size)
        index = max(0, min(bins - 1, raw_index))
        bucket_volume[index] += volume

    total_volume = sum(bucket_volume)
    if total_volume <= 0:
        return {
            "volume_profile_valid": True,
            "volume_profile_poc": current_close,
            "volume_profile_value_area_low": low_price,
            "volume_profile_value_area_high": high_price,
            "volume_profile_break": False,
            "volume_profile_reason": "volume_profile_empty",
        }

    poc_index = max(range(bins), key=lambda index: bucket_volume[index])
    low_index = high_index = poc_index
    covered = bucket_volume[poc_index]
    target = total_volume * value_area_fraction
    while covered < target and (low_index > 0 or high_index < bins - 1):
        left_volume = bucket_volume[low_index - 1] if low_index > 0 else -1.0
        right_volume = bucket_volume[high_index + 1] if high_index < bins - 1 else -1.0
        if right_volume >= left_volume:
            high_index += 1
            covered += max(0.0, right_volume)
        else:
            low_index -= 1
            covered += max(0.0, left_volume)

    poc = low_price + (poc_index + 0.5) * bin_size
    value_area_low = low_price + low_index * bin_size
    value_area_high = low_price + (high_index + 1) * bin_size
    if str(position_side).lower() == "short":
        profile_break = current_close > value_area_high
    else:
        profile_break = current_close < value_area_low

    return {
        "volume_profile_valid": True,
        "volume_profile_poc": poc,
        "volume_profile_value_area_low": value_area_low,
        "volume_profile_value_area_high": value_area_high,
        "volume_profile_break": bool(profile_break),
        "volume_profile_reason": "volume_profile_break" if profile_break else "volume_profile_ok",
    }


def volume_confirmation_context(
    candles: Sequence[Sequence[float]],
    short_window: int,
    long_window: int,
    min_ratio: float,
    min_directional_fraction: float,
    position_side: str,
    spike_window: int = 0,
    spike_min_ratio: float = 0.0,
    adverse_spike_min_ratio: float = 0.0,
    profile_enabled: bool = False,
    profile_window: int = 0,
    profile_bins: int = 12,
    profile_value_area: float = 0.70,
) -> dict:
    short_window = max(1, int(short_window))
    long_window = max(short_window, int(long_window))
    min_ratio = max(0.0, float(min_ratio))
    min_directional_fraction = clamp(float(min_directional_fraction), 0.0, 1.0)

    spike_window = max(1, int(spike_window or short_window))
    spike_min_ratio = max(0.0, float(spike_min_ratio))
    adverse_spike_min_ratio = max(0.0, float(adverse_spike_min_ratio))
    profile_window = max(1, int(profile_window or long_window))

    rows = []
    for row in candles or []:
        if len(row) < 6:
            continue
        try:
            open_price = float(row[1])
            high_price = float(row[2])
            low_price = float(row[3])
            close_price = float(row[4])
            volume = float(row[5])
        except (TypeError, ValueError):
            continue
        if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0 or volume <= 0:
            continue
        rows.append((open_price, high_price, low_price, close_price, volume))

    required = max(short_window, long_window, profile_window if profile_enabled else 0)
    if len(rows) < required:
        return {
            "volume_valid": False,
            "volume_average_valid": False,
            "volume_ratio": 0.0,
            "volume_recent": 0.0,
            "volume_baseline": 0.0,
            "volume_directional_fraction": 0.0,
            "volume_spike_valid": False,
            "volume_spike_ratio": 0.0,
            "volume_spike_volume": 0.0,
            "volume_spike_baseline": 0.0,
            "volume_spike_direction": "unknown",
            "volume_spike_reason": "volume_history_short",
            "volume_profile_valid": False,
            "volume_profile_poc": 0.0,
            "volume_profile_value_area_low": 0.0,
            "volume_profile_value_area_high": 0.0,
            "volume_profile_break": False,
            "volume_profile_reason": "volume_history_short",
            "volume_required_candles": required,
            "volume_reason": f"volume_history_short;candles={len(rows)};required={required}",
        }

    recent = rows[-short_window:]
    baseline = rows[-long_window:]
    recent_average = sum(row[4] for row in recent) / short_window
    baseline_average = sum(row[4] for row in baseline) / long_window
    ratio = recent_average / baseline_average if baseline_average > 0 else 0.0

    total_recent_volume = sum(row[4] for row in recent)
    if str(position_side).lower() == "short":
        directional_volume = sum(volume for open_price, _high, _low, close_price, volume in recent if close_price < open_price)
    else:
        directional_volume = sum(volume for open_price, _high, _low, close_price, volume in recent if close_price > open_price)
    directional_fraction = directional_volume / total_recent_volume if total_recent_volume > 0 else 0.0

    ratio_valid = ratio + 1e-12 >= min_ratio
    directional_valid = directional_fraction + 1e-12 >= min_directional_fraction
    average_valid = bool(ratio_valid and directional_valid)

    spike_recent = rows[-spike_window:]
    previous_rows = rows[-(long_window + spike_window):-spike_window] if len(rows) > spike_window else []
    if not previous_rows:
        previous_rows = rows[-long_window:]
    spike_baseline = sum(row[4] for row in previous_rows) / len(previous_rows) if previous_rows else baseline_average
    spike_row = max(spike_recent, key=lambda row: row[4])
    spike_direction = _volume_direction(spike_row[0], spike_row[3])
    spike_ratio = spike_row[4] / spike_baseline if spike_baseline > 0 else 0.0
    aligned_spike = spike_min_ratio > 0 and spike_ratio + 1e-12 >= spike_min_ratio and spike_direction == str(position_side).lower()
    adverse_spike = (
        adverse_spike_min_ratio > 0
        and spike_ratio + 1e-12 >= adverse_spike_min_ratio
        and spike_direction not in {"neutral", str(position_side).lower()}
    )

    if profile_enabled:
        profile = _volume_profile_context(
            rows,
            profile_window,
            profile_bins,
            profile_value_area,
            position_side,
        )
    else:
        profile = {
            "volume_profile_valid": True,
            "volume_profile_poc": 0.0,
            "volume_profile_value_area_low": 0.0,
            "volume_profile_value_area_high": 0.0,
            "volume_profile_break": False,
            "volume_profile_reason": "disabled",
        }

    profile_break = bool(profile.get("volume_profile_break", False))
    spike_valid = not bool(adverse_spike and (profile_break or not profile_enabled))
    profile_valid = bool(profile.get("volume_profile_valid", True)) and not bool(adverse_spike and profile_break)
    if aligned_spike and spike_valid:
        spike_reason = "volume_aligned_spike"
    elif adverse_spike and not spike_valid:
        spike_reason = "volume_adverse_spike"
    elif spike_min_ratio > 0 or adverse_spike_min_ratio > 0:
        spike_reason = "volume_spike_neutral"
    else:
        spike_reason = "disabled"

    confirmed = bool(average_valid or (aligned_spike and spike_valid))
    volume_valid = bool(confirmed and spike_valid and profile_valid)

    if volume_valid and average_valid:
        reason = "volume_confirmed"
    elif volume_valid:
        reason = "volume_spike_confirmed"
    elif not profile_valid:
        reason = "volume_profile_adverse_break" if adverse_spike and profile_break else profile.get("volume_profile_reason", "volume_profile_invalid")
    elif not spike_valid:
        reason = spike_reason
    elif not ratio_valid and not aligned_spike:
        reason = "volume_ratio_below_min"
    elif not directional_valid:
        reason = "volume_directional_fraction_below_min"
    else:
        reason = "volume_confirmation_missing"

    return {
        "volume_valid": volume_valid,
        "volume_average_valid": average_valid,
        "volume_ratio": ratio,
        "volume_recent": recent_average,
        "volume_baseline": baseline_average,
        "volume_directional_fraction": directional_fraction,
        "volume_spike_valid": bool(spike_valid),
        "volume_spike_ratio": spike_ratio,
        "volume_spike_volume": spike_row[4],
        "volume_spike_baseline": spike_baseline,
        "volume_spike_direction": spike_direction,
        "volume_spike_reason": spike_reason,
        **profile,
        "volume_profile_valid": bool(profile_valid),
        "volume_required_candles": required,
        "volume_reason": reason,
    }


def choppiness_context(candles: Sequence[Sequence[float]], period: int, max_chop: float) -> dict:
    period = max(2, int(period))
    max_chop = max(0.0, float(max_chop))
    if len(candles or []) < period + 1:
        return {
            "chop_valid": False,
            "chop": 0.0,
            "chop_max": max_chop,
            "chop_period": period,
            "chop_reason": f"chop_history_short;candles={len(candles or [])};required={period + 1}",
        }

    value = choppiness_index(candles, period)
    valid = value <= max_chop + 1e-12
    return {
        "chop_valid": bool(valid),
        "chop": value,
        "chop_max": max_chop,
        "chop_period": period,
        "chop_reason": "chop_ok" if valid else "chop_above_max",
    }


__all__ = [
    "btc_risk_context",
    "choppiness_context",
    "daily_volatility_context",
    "ema_pullback_recovery_context",
    "ema_signal_direction_metrics",
    "gold_btc_ratio_return",
    "local_reversion_context",
    "relative_strength_context",
    "signal_budget_multiplier",
    "signal_score",
    "volume_confirmation_context",
    "volatility_multiplier",
]
