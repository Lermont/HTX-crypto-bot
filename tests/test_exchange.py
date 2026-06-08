# -*- coding: utf-8 -*-
import unittest
from htxbot.exchange import ExchangeMixin


class TestExchangeMixinLeverage(unittest.TestCase):
    class DummyExchange(ExchangeMixin):
        def __init__(self):
            self._market_returns = {}

        def _market(self, symbol):
            return self._market_returns.get(symbol, {})

        def _safe_float(self, v, default=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

    def setUp(self):
        self.ex = self.DummyExchange()

    def test_item_matches_empty_market_id(self):
        # When _market returns empty dict, market_id becomes empty string
        self.ex._market_returns["BTC/USDT"] = {}
        # item_matches returns True immediately if market_id is falsy
        payload = {"data": [{"contract_code": "ANYTHING", "lever_rate": 15}]}
        self.assertEqual(
            self.ex._account_leverage_from_payload("BTC/USDT", payload), 15.0
        )

    def test_item_matches_contract_code(self):
        self.ex._market_returns["BTC/USDT"] = {"id": "BTC-USDT"}

        # Test exact match on contract_code finding the right leverage
        payload = {
            "data": [
                {"contract_code": "ETH-USDT", "lever_rate": 20},
                {"contract_code": "BTC-USDT", "lever_rate": 30},
            ]
        }
        self.assertEqual(
            self.ex._account_leverage_from_payload("BTC/USDT", payload), 30.0
        )

    def test_item_matches_other_keys(self):
        self.ex._market_returns["BTC/USDT"] = {"id": "BTC-USDT"}

        # Test exact match on 'pair'
        payload_pair = {
            "data": [
                {"pair": "ETH-USDT", "lever_rate": 20},
                {"pair": "BTC-USDT", "lever_rate": 40},
            ]
        }
        self.assertEqual(
            self.ex._account_leverage_from_payload("BTC/USDT", payload_pair), 40.0
        )

        # Test exact match on 'symbol'
        payload_symbol = {
            "data": [
                {"symbol": "ETH-USDT", "lever_rate": 20},
                {"symbol": "BTC-USDT", "lever_rate": 50},
            ]
        }
        self.assertEqual(
            self.ex._account_leverage_from_payload("BTC/USDT", payload_symbol), 50.0
        )

        # Test exact match on 'contractCode'
        payload_cc = {
            "data": [
                {"contractCode": "ETH-USDT", "lever_rate": 20},
                {"contractCode": "BTC-USDT", "lever_rate": 60},
            ]
        }
        self.assertEqual(
            self.ex._account_leverage_from_payload("BTC/USDT", payload_cc), 60.0
        )

    def test_item_matches_no_match_returns_fallback(self):
        self.ex._market_returns["BTC/USDT"] = {"id": "BTC-USDT"}

        # When there is no match for the symbol, the loop finishes and returns the fallback.
        # Fallback is the first valid leverage seen.
        payload = {
            "data": [
                {"symbol": "ETH-USDT", "lever_rate": 20},
                {"symbol": "LTC-USDT", "lever_rate": 30},
            ]
        }
        self.assertEqual(
            self.ex._account_leverage_from_payload("BTC/USDT", payload), 20.0
        )

    def test_item_leverage_from_info(self):
        # Make sure that item_leverage behavior is also covered where it fetches from 'info'
        self.ex._market_returns["BTC/USDT"] = {"id": "BTC-USDT"}
        payload = {"data": [{"symbol": "BTC-USDT", "info": {"leverage": 25}}]}
        self.assertEqual(
            self.ex._account_leverage_from_payload("BTC/USDT", payload), 25.0
        )


if __name__ == "__main__":
    unittest.main()
