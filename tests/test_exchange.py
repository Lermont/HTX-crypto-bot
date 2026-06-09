# -*- coding: utf-8 -*-
import unittest
from htxbot.exchange import ExchangeMixin


class DummyExchange(ExchangeMixin):
    def __init__(self):
        self.exchange = type(
            "Mock", (object,), {"markets": {"BTC-USDT": {"id": "BTC-USDT"}}}
        )()

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
        payload = {"data": [{"symbol": "BTC-USDT", "leverage": "10"}]}
        self.assertEqual(
            self.exchange._account_leverage_from_payload("BTC-USDT", payload), 10.0
        )

    def test_item_leverage_with_info(self):
        payload = {"data": [{"symbol": "BTC-USDT", "info": {"lever_rate": "20.5"}}]}
        self.assertEqual(
            self.exchange._account_leverage_from_payload("BTC-USDT", payload), 20.5
        )

    def test_item_leverage_invalid_item(self):
        # Passing list instead of dict, item_leverage should return 0.0
        # The logic falls back to 0.0
        payload = {"data": [["invalid", "item"]]}
        self.assertEqual(
            self.exchange._account_leverage_from_payload("BTC-USDT", payload), 0.0
        )

    def test_item_leverage_multiple_keys(self):
        payload = {"data": [{"symbol": "BTC-USDT", "leverRate": "15"}]}
        self.assertEqual(
            self.exchange._account_leverage_from_payload("BTC-USDT", payload), 15.0
        )

    def test_item_leverage_nested_structures(self):
        payload = {"data": {"positions": [{"symbol": "BTC-USDT", "leverage": "5.5"}]}}
        self.assertEqual(
            self.exchange._account_leverage_from_payload("BTC-USDT", payload), 5.5
        )

    def test_item_leverage_returns_fallback_when_symbol_no_match(self):
        # The outer function logic retains a `fallback` to the first valid leverage
        # it encounters if no specific item_matches() is found.
        # Thus, it correctly returns 10.0 here.
        payload = {"data": [{"symbol": "ETH-USDT", "leverage": "10"}]}
        self.assertEqual(
            self.exchange._account_leverage_from_payload("BTC-USDT", payload), 10.0
        )

    def test_item_leverage_zero_leverage(self):
        # if leverage <= 0 it should continue searching
        payload = {
            "data": [
                {"symbol": "BTC-USDT", "leverage": "0"},
                {"symbol": "BTC-USDT", "leverage": "12.5"},
            ]
        }
        self.assertEqual(
            self.exchange._account_leverage_from_payload("BTC-USDT", payload), 12.5
        )


if __name__ == "__main__":
    unittest.main()


class TestFetchOrderBookSafe(unittest.TestCase):
    """
    Tests the logic of the `fetch_order_book_safe` inner function located inside
    ExchangeMixin._prefetch_market_data_snapshots(). Since it's an inner function,
    we test it indirectly via the outer function's execution and observing the side effects
    (i.e., cached calls and log events) as recommended by testing guidelines.
    """

    def setUp(self):
        self.exchange = DummyExchange()
        self.exchange.symbols = ["BTC-USDT", "ETH-USDT"]
        self.exchange._cached_calls = []
        self.exchange._log_events = []

        # Add required methods/attributes to Mock exchange for the method to run
        self.exchange.exchange.has = {"fetchTickers": False}
        self.exchange.exchange.fetch_order_book = lambda symbol, limit: None

        # Override _cached_order_book to track calls and simulate failure
        def mock_cached_order_book(symbol, limit):
            self.exchange._cached_calls.append((symbol, limit))
            if symbol == "ETH-USDT":
                raise ValueError("Simulated fetch error")
            return {"bids": [], "asks": []}

        self.exchange._cached_order_book = mock_cached_order_book

        # Override _log_event to capture logged exceptions
        def mock_log_event(level, msg, *args, **kwargs):
            self.exchange._log_events.append((level, msg, kwargs))

        self.exchange._log_event = mock_log_event

        # Mock prefetch tickers to avoid unrelated code running
        self.exchange._prefetch_ticker_snapshots = lambda symbols: None

    def test_fetch_order_book_safe_indirect(self):
        # We need to use override_frozen_config_fields to bypass frozen dataclass restriction
        from tests.config_overrides import override_frozen_config_fields
        from htxbot import config

        # Enable spread filter to allow execution to reach fetch_order_book_safe
        with override_frozen_config_fields(
            config.STRATEGY,
            entry_spread_filter_enabled=True,
            entry_spread_filter_max_bps=10.0,
        ):
            self.exchange._prefetch_market_data_snapshots()

        # Verify success path: BTC-USDT should be processed successfully
        self.assertIn(("BTC-USDT", 5), self.exchange._cached_calls)

        # Verify error path: ETH-USDT throws error but it's handled safely
        self.assertIn(("ETH-USDT", 5), self.exchange._cached_calls)

        # The exception for ETH-USDT should be caught and logged
        error_logs = [
            event
            for event in self.exchange._log_events
            if event[2].get("reason") == "order_book_prefetch_failed"
        ]
        self.assertEqual(len(error_logs), 1)

        level, msg, kwargs = error_logs[0]
        self.assertEqual(level, "DEBUG")
        self.assertEqual(kwargs.get("symbol"), "ETH-USDT")
        self.assertIsInstance(kwargs.get("exception"), ValueError)
        self.assertEqual(str(kwargs.get("exception")), "Simulated fetch error")
