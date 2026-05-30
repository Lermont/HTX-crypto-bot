import pytest
from htxbot.indicators import calculate_ema

def test_calculate_ema_empty():
    assert calculate_ema([], 10) == 0.0

def test_calculate_ema_single_element():
    assert calculate_ema([42.5], 10) == 42.5

def test_calculate_ema_constant_prices():
    prices = [10.0, 10.0, 10.0, 10.0, 10.0]
    assert calculate_ema(prices, 5) == pytest.approx(10.0)

def test_calculate_ema_known_calculation():
    # Let prices = [10.0, 20.0, 30.0], period = 3
    # alpha = 2 / (3 + 1) = 0.5
    # ema_0 = 10.0
    # ema_1 = 20.0 * 0.5 + 10.0 * 0.5 = 15.0
    # ema_2 = 30.0 * 0.5 + 15.0 * 0.5 = 22.5
    prices = [10.0, 20.0, 30.0]
    assert calculate_ema(prices, 3) == pytest.approx(22.5)

def test_calculate_ema_period_one():
    # If period = 1, alpha = 2 / 2 = 1.0. The EMA should exactly track the latest price.
    prices = [10.0, 20.0, 30.0]
    assert calculate_ema(prices, 1) == pytest.approx(30.0)

def test_calculate_ema_negative_numbers():
    prices = [-10.0, -20.0, -30.0]
    assert calculate_ema(prices, 3) == pytest.approx(-22.5)

def test_calculate_ema_handles_integers():
    prices = [10, 20, 30]
    assert calculate_ema(prices, 3) == pytest.approx(22.5)
