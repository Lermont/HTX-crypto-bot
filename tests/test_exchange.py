# -*- coding: utf-8 -*-
import unittest
from htxbot.exchange import ExchangeMixin

class DummyExchange(ExchangeMixin):
    def __init__(self):
        self.exchange = type('Mock', (object,), {"markets": {"BTC-USDT": {"id": "BTC-USDT"}}})()

    def _market(self, symbol):
        return self.exchange.markets.get(symbol)

    def _safe_float(self, value, fallback=0.0):
        try:
            return float(value) if value is not None else fallback
        except (TypeError, ValueError):
            return fallback

class TestAccountLeverageFromPayload(unittest.TestCase):
    """
    Tests the logic of the `item_leverage` inner function located inside
    ExchangeMixin._account_leverage_from_payload(). Since it's an inner function,
    we test it indirectly via the outer function's return values.
    """
    def setUp(self):
        self.exchange = DummyExchange()

    def test_item_leverage_basic(self):
        payload = {
            "data": [
                {"symbol": "BTC-USDT", "leverage": "10"}
            ]
        }
        self.assertEqual(self.exchange._account_leverage_from_payload("BTC-USDT", payload), 10.0)

    def test_item_leverage_with_info(self):
        payload = {
            "data": [
                {"symbol": "BTC-USDT", "info": {"lever_rate": "20.5"}}
            ]
        }
        self.assertEqual(self.exchange._account_leverage_from_payload("BTC-USDT", payload), 20.5)

    def test_item_leverage_invalid_item(self):
        # Passing list instead of dict, item_leverage should return 0.0
        # The logic falls back to 0.0
        payload = {
            "data": [
                ["invalid", "item"]
            ]
        }
        self.assertEqual(self.exchange._account_leverage_from_payload("BTC-USDT", payload), 0.0)

    def test_item_leverage_multiple_keys(self):
        payload = {
            "data": [
                {"symbol": "BTC-USDT", "leverRate": "15"}
            ]
        }
        self.assertEqual(self.exchange._account_leverage_from_payload("BTC-USDT", payload), 15.0)

    def test_item_leverage_nested_structures(self):
        payload = {
            "data": {
                "positions": [
                    {"symbol": "BTC-USDT", "leverage": "5.5"}
                ]
            }
        }
        self.assertEqual(self.exchange._account_leverage_from_payload("BTC-USDT", payload), 5.5)

    def test_item_leverage_returns_fallback_when_symbol_no_match(self):
        # The outer function logic retains a `fallback` to the first valid leverage
        # it encounters if no specific item_matches() is found.
        # Thus, it correctly returns 10.0 here.
        payload = {
            "data": [
                {"symbol": "ETH-USDT", "leverage": "10"}
            ]
        }
        self.assertEqual(self.exchange._account_leverage_from_payload("BTC-USDT", payload), 10.0)

    def test_item_leverage_zero_leverage(self):
        # if leverage <= 0 it should continue searching
        payload = {
            "data": [
                {"symbol": "BTC-USDT", "leverage": "0"},
                {"symbol": "BTC-USDT", "leverage": "12.5"}
            ]
        }
        self.assertEqual(self.exchange._account_leverage_from_payload("BTC-USDT", payload), 12.5)

if __name__ == '__main__':
    unittest.main()
