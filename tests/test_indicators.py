import pytest
from htxbot.indicators import calculate_ema, calculate_rsi, compute_log_return, clamp, realized_volatility

def test_clamp():
    assert clamp(5, 1, 10) == 5
    assert clamp(0, 1, 10) == 1
    assert clamp(15, 1, 10) == 10
