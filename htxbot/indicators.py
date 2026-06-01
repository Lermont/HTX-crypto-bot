# -*- coding: utf-8 -*-

import math
from typing import Sequence

try:
    import numpy as np
except Exception:  # pragma: no cover - dependency is installed via requirements.
    np = None


HAS_NUMPY = np is not None


def calculate_ema(prices: Sequence[float], period: int) -> float:
    if len(prices) == 0:
        return 0.0

    alpha = 2 / (period + 1)
    ema = float(prices[0])
    for price in prices[1:]:
        ema = float(price) * alpha + ema * (1 - alpha)
    return ema


def calculate_ema_series(prices: Sequence[float], period: int) -> list:
    if len(prices) == 0:
        return []

    alpha = 2.0 / (max(1, int(period)) + 1.0)
    ema = float(prices[0])
    values = [ema]
    for price in prices[1:]:
        ema = float(price) * alpha + ema * (1.0 - alpha)
        values.append(ema)
    return values


def calculate_rsi(closes: Sequence[float], period: int) -> float:
    period = int(period)
    if period <= 0 or len(closes) <= period:
        return 0.0

    try:
        values = [float(price) for price in closes if float(price) > 0]
    except (TypeError, ValueError):
        return 0.0
    if len(values) <= period:
        return 0.0

    gains = []
    losses = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss <= 0:
        if avg_gain <= 0:
            return 50.0
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_log_return(price_now: float, price_then: float) -> float:
    if price_now <= 0 or price_then <= 0:
        return 0.0
    return math.log(price_now / price_then)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def realized_volatility(closes: Sequence[float], window: int) -> float:
    if window <= 1 or len(closes) < window + 1:
        return 0.0

    if HAS_NUMPY:
        sample = np.asarray(closes[-window - 1:], dtype=float)
        previous = sample[:-1]
        current = sample[1:]
        valid = (current > 0) & (previous > 0)
        if int(valid.sum()) < 2:
            return 0.0
        returns = np.log(current[valid] / previous[valid])
        variance = returns.var(ddof=1)
        return float(np.sqrt(max(0.0, float(variance))))

    sample = closes[-window - 1:]
    returns = [
        compute_log_return(sample[index], sample[index - 1])
        for index in range(1, len(sample))
        if sample[index] > 0 and sample[index - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    return math.sqrt(max(0.0, variance))


def average_true_range(candles: Sequence[Sequence[float]], period: int) -> float:
    period = int(period)
    if period <= 0 or len(candles) < period + 1:
        return 0.0

    rows = []
    for row in candles:
        if len(row) < 5:
            continue
        try:
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        if high <= 0 or low <= 0 or close <= 0 or high < low:
            continue
        rows.append((high, low, close))

    if len(rows) < period + 1:
        return 0.0

    true_ranges = []
    for index in range(1, len(rows)):
        high, low, _close = rows[index]
        previous_close = rows[index - 1][2]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))

    if len(true_ranges) < period:
        return 0.0
    return sum(true_ranges[-period:]) / period


__all__ = [
    "HAS_NUMPY",
    "average_true_range",
    "calculate_ema",
    "calculate_ema_series",
    "calculate_rsi",
    "clamp",
    "compute_log_return",
    "realized_volatility",
]
