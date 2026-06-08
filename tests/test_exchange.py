# -*- coding: utf-8 -*-
import unittest
from htxbot.exchange import ExchangeMixin
from tests.config_overrides import override_frozen_config_fields
import config

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



class TestAvailableAndFrozen(unittest.TestCase):
    """
    Tests the logic of the `available_and_frozen` inner function located inside
    ExchangeMixin._fetch_position_snapshot(). Since it's an inner function,
    we test it indirectly via the outer function's resulting snapshot values.
    """
    def setUp(self):
        self.exchange = DummyExchange()
        self.exchange._contract_size = lambda s: 1.0
        self.exchange._contracts_to_notional = lambda s, c, p: c * p
        self.exchange._average_price_from_notional = lambda s, size, notional: notional / size if size > 0 else 0.0

    def test_available_and_frozen_basic(self):
        self.exchange._bulk_positions_by_symbol = lambda: {"BTC-USDT": [{
            "contracts": 10,
            "side": "long",
            "available": 6,
            "frozen": 4,
            "entryPrice": 100
        }]}

        with override_frozen_config_fields(config.RISK, margin_mode="cross"):
            snap = self.exchange._fetch_position_snapshot("BTC-USDT")

        self.assertEqual(snap["long_available"], 6.0)
        self.assertEqual(snap["long_frozen"], 4.0)

    def test_available_and_frozen_from_info(self):
        self.exchange._bulk_positions_by_symbol = lambda: {"BTC-USDT": [{
            "contracts": 10,
            "side": "long",
            "info": {
                "available": 3,
                "frozen": 7
            },
            "entryPrice": 100
        }]}

        with override_frozen_config_fields(config.RISK, margin_mode="cross"):
            snap = self.exchange._fetch_position_snapshot("BTC-USDT")

        self.assertEqual(snap["long_available"], 3.0)
        self.assertEqual(snap["long_frozen"], 7.0)

    def test_available_and_frozen_alternate_keys(self):
        self.exchange._bulk_positions_by_symbol = lambda: {"BTC-USDT": [{
            "contracts": 15,
            "side": "short",
            "canCloseVolume": 8,
            "frozen_volume": 7,
            "entryPrice": 100
        }]}

        with override_frozen_config_fields(config.RISK, margin_mode="cross"):
            snap = self.exchange._fetch_position_snapshot("BTC-USDT")

        self.assertEqual(snap["short_available"], 8.0)
        self.assertEqual(snap["short_frozen"], 7.0)

    def test_available_and_frozen_fallback(self):
        # When neither available nor frozen is provided
        self.exchange._bulk_positions_by_symbol = lambda: {"BTC-USDT": [{
            "contracts": 12,
            "side": "long",
            "entryPrice": 100
        }]}

        with override_frozen_config_fields(config.RISK, margin_mode="cross"):
            snap = self.exchange._fetch_position_snapshot("BTC-USDT")

        self.assertEqual(snap["long_available"], 12.0)
        self.assertEqual(snap["long_frozen"], 0.0)

    def test_available_and_frozen_partial_fallback(self):
        # When only available is provided, frozen should be contracts - available
        self.exchange._bulk_positions_by_symbol = lambda: {"BTC-USDT": [{
            "contracts": 20,
            "side": "short",
            "available": 15,
            "entryPrice": 100
        }]}

        with override_frozen_config_fields(config.RISK, margin_mode="cross"):
            snap = self.exchange._fetch_position_snapshot("BTC-USDT")

        self.assertEqual(snap["short_available"], 15.0)
        self.assertEqual(snap["short_frozen"], 5.0)

    def test_available_and_frozen_bounds(self):
        # Available shouldn't exceed contracts, frozen shouldn't be negative
        self.exchange._bulk_positions_by_symbol = lambda: {"BTC-USDT": [{
            "contracts": 10,
            "side": "long",
            "available": 15,
            "frozen": -5,
            "entryPrice": 100
        }]}

        with override_frozen_config_fields(config.RISK, margin_mode="cross"):
            snap = self.exchange._fetch_position_snapshot("BTC-USDT")

        self.assertEqual(snap["long_available"], 10.0)
        self.assertEqual(snap["long_frozen"], 0.0)

if __name__ == '__main__':
    unittest.main()
