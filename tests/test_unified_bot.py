# -*- coding: utf-8 -*-

import csv
import logging
import math
import os
import tempfile
import time
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path

import config
import ccxt
from htxbot.app import HtxFuturesBot
from htxbot.combined import CombinedHtxFuturesBot
from htxbot.external_price import BookTicker, ExternalPriceFeed, MexcBookTickerClient
from htxbot.indicators import calculate_rsi

replace = config.replace_settings


SYMBOL = "TEST/USDT:USDT"
BTC_SYMBOL = "BTC/USDT:USDT"
XAUT_SYMBOL = "XAUT/USDT:USDT"
MARKET = {
    "symbol": SYMBOL,
    "id": "TEST-USDT",
    "base": "TEST",
    "quote": "USDT",
    "settle": "USDT",
    "linear": True,
    "swap": True,
    "contractSize": 1.0,
    "limits": {"amount": {"min": 1.0}},
    "precision": {"price": 0.01},
}
BTC_MARKET = {
    **MARKET,
    "symbol": BTC_SYMBOL,
    "id": "BTC-USDT",
    "base": "BTC",
}
XAUT_MARKET = {
    **MARKET,
    "symbol": XAUT_SYMBOL,
    "id": "XAUT-USDT",
    "base": "XAUT",
}


def ohlcv_series(closes, timeframe_sec=60, start_ts=1_700_000_000_000):
    return [
        [start_ts + index * timeframe_sec * 1000, close, close, close, close, 1.0]
        for index, close in enumerate(closes)
    ]


class StaticExternalPriceFeed:
    def __init__(self, context):
        self.context = dict(context)
        self.calls = []

    def get_context(self, symbol, htx_ticker, market=None):
        self.calls.append((symbol, dict(htx_ticker), market))
        context = dict(self.context)
        context.setdefault("symbol", symbol)
        context.setdefault("mexc_symbol", "TESTUSDT")
        context.setdefault("ts", time.time())
        return context


class InvalidExternalPriceFeed:
    def get_context(self, symbol, htx_ticker, market=None):
        return None


class FakeMexcClient:
    def __init__(self, books):
        self.books = list(books)
        self.calls = []

    def fetch(self, symbol):
        self.calls.append(symbol)
        if not self.books:
            raise RuntimeError("no fake books left")
        return self.books.pop(0)


class FakeRequestsResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error
        self.raise_for_status_calls = 0

    def raise_for_status(self):
        self.raise_for_status_calls += 1
        if self.error is not None:
            raise self.error

    def json(self):
        return dict(self.payload)


class FakeRequestsSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self.responses:
            raise RuntimeError("no fake responses left")
        return self.responses.pop(0)


class FakeExchange:
    def __init__(self):
        self.markets = {SYMBOL: MARKET}
        self.urls = {"hostnames": {}}
        self.has = {
            "fetchFundingRate": False,
            "fetchOpenOrders": True,
            "fetchOrder": False,
            "fetchPositions": True,
            "fetchMyTrades": False,
        }
        self.precisionMode = ccxt.TICK_SIZE
        self.canceled_orders = []
        self.created_orders = []
        self.cancel_fail_ids = set()
        self.open_orders = []
        self.positions = []
        self.fetch_open_orders_failures = []
        self.fetch_positions_failures = []
        self.fetch_open_orders_calls = 0
        self.fetch_positions_calls = 0
        self.ohlcv = {}
        self.ohlcv_calls = []
        self.reject_leverage_above = None
        self.reject_leverage_not_equal = None
        self.account_leverage = 50
        self.reject_reduce_only_closeable_amount = False
        self.create_order_calls = 0
        self.set_position_mode_calls = []
        self.set_position_mode_error = None
        self.ticker = {"bid": 9.9, "ask": 10.1, "last": 10.0}

    def market(self, symbol):
        return self.markets[symbol]

    def load_markets(self, reload=False):
        return self.markets

    def set_markets(self, markets):
        self.markets = markets

    def amount_to_precision(self, symbol, amount):
        return str(math.floor(float(amount)))

    def price_to_precision(self, symbol, price):
        return f"{round(float(price), 2):.2f}"

    def fetch_ticker(self, symbol):
        return dict(self.ticker)

    def cancel_order(self, order_id, symbol, params=None):
        if str(order_id) in self.cancel_fail_ids:
            raise RuntimeError("cancel failed")
        self.canceled_orders.append((str(order_id), symbol, params or {}))

    def fetch_open_orders(self, symbol=None, params=None):
        self.fetch_open_orders_calls += 1
        if self.fetch_open_orders_failures:
            raise self.fetch_open_orders_failures.pop(0)
        if symbol is None:
            return list(self.open_orders)
        return [order for order in self.open_orders if order.get("symbol", SYMBOL) == symbol]

    def fetch_positions(self, symbols=None, params=None):
        self.fetch_positions_calls += 1
        if self.fetch_positions_failures:
            raise self.fetch_positions_failures.pop(0)
        wanted = set(symbols or [])
        if not wanted:
            return list(self.positions)
        return [position for position in self.positions if position.get("symbol", SYMBOL) in wanted]

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        self.ohlcv_calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "since": since,
                "limit": limit,
                "params": params or {},
            }
        )
        rows = list(self.ohlcv.get((symbol, timeframe), []))
        if limit:
            return rows[-int(limit):]
        return rows

    def create_order(self, symbol, type, side, amount, price, params=None):
        params = params or {}
        self.create_order_calls += 1
        if self.reject_leverage_not_equal is not None:
            leverage = float(params.get("leverRate") or 0.0)
            if leverage != float(self.reject_leverage_not_equal):
                raise RuntimeError(
                    'htx {"status":"error","err_code":1045,'
                    '"err_msg":"Unable to change leverage due to open orders."}'
                )
        if self.reject_reduce_only_closeable_amount and params.get("reduceOnly"):
            raise RuntimeError(
                'htx {"status":"error","err_code":1492,'
                '"err_msg":"Amount of Reduce Only order exceeds the amount available to close."}'
            )
        if self.reject_leverage_above is not None:
            leverage = float(params.get("leverRate") or 0.0)
            if leverage > self.reject_leverage_above:
                raise RuntimeError(
                    'htx {"status":"error","err_code":1206,'
                    '"err_msg":"To protect you from high risk exposure, high leverage is not supported."}'
                )
        order = {
            "id": f"created_{len(self.created_orders) + 1}",
            "symbol": symbol,
            "type": type,
            "side": side,
            "amount": amount,
            "price": price,
            "params": params,
        }
        self.created_orders.append(order)
        return order

    def set_position_mode(self, hedged, symbol=None, params=None):
        self.set_position_mode_calls.append((hedged, symbol, params or {}))
        if self.set_position_mode_error is not None:
            raise self.set_position_mode_error
        return {"status": "ok"}

    def contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo(self, request):
        return {
            "status": "ok",
            "data": {
                "positions": [],
                "contract_detail": [
                    {
                        "contract_code": request.get("contract_code", "TEST-USDT"),
                        "lever_rate": str(self.account_leverage),
                    }
                ],
            },
        }


@contextmanager
def override_config(**values):
    sentinel = object()
    previous = {name: config.__dict__.get(name, sentinel) for name in values}
    for name, value in values.items():
        setattr(config, name, value)
    try:
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is sentinel:
                delattr(config, name)
            else:
                setattr(config, name, old_value)


@contextmanager
def patched_env(**values):
    sentinel = object()
    previous = {name: os.environ.get(name, sentinel) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is sentinel:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value


class UnifiedBotTests(unittest.TestCase):
    def make_bot(self, tmp_path: Path) -> HtxFuturesBot:
        instance = object.__new__(HtxFuturesBot)
        logger = logging.getLogger(f"test_unified_bot_{id(instance)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        instance.log = logger
        instance.exchange = FakeExchange()
        instance.state_path = tmp_path / "state.json"
        instance.lock_path = tmp_path / "state.lock"
        instance.markets_cache_path = tmp_path / "markets.json"
        instance.csv_path = tmp_path / "trades.csv"
        instance.cycle_stats_path = tmp_path / "cycles.csv"
        instance.macro_csv_path = tmp_path / "macro.csv"
        instance.external_price_csv_path = tmp_path / "external_price.csv"
        instance.timeframe_sec = 60
        instance.states = {}
        instance.symbols = [SYMBOL]
        instance.market_by_symbol = {SYMBOL: MARKET}
        instance.disabled_symbols = set()
        instance.benchmark_symbol = SYMBOL
        instance.macro_gold_symbol = None
        instance.macro_gold_is_spot = False
        instance._macro_gold_lookup_done = True
        instance.macro_direct_gold_btc_symbol = None
        instance.macro_direct_gold_btc_is_spot = False
        instance._macro_direct_gold_btc_lookup_done = True
        instance.macro_spot_exchange = None
        instance.funding_cache = {}
        instance.order_leverage_cache = {}
        instance.signal_cache = {
            "benchmark_ok": True,
            "macro": {
                "gold_btc_rsi": {
                    "ok": False,
                    "ts": int(time.time()),
                    "regime": "macro_unavailable",
                    "reason": "test_neutral",
                    "long_budget_multiplier": 1.0,
                    "short_budget_multiplier": 1.0,
                    "ladder_multiplier": 1.0,
                    "disable_new_entries": False,
                    "disable_averaging": False,
                    "disable_recovery": False,
                    "time_exit_multiplier": 1.0,
                },
            },
            "symbols": {},
        }
        instance.entry_symbols = {SYMBOL}
        instance.one_way_mode_checked = False
        instance._ensure_csv_file()
        instance._ensure_cycle_stats_file()
        instance._ensure_macro_csv_file()
        instance._ensure_external_price_csv_file()
        return instance

    def ema_test_strategy(self, **overrides):
        defaults = {
            "ema_macro_fast_minutes": 10,
            "ema_macro_slow_minutes": 20,
            "ema_pullback_fast_minutes": 3,
            "ema_pullback_slow_minutes": 8,
            "ema_pullback_recovery_lookback_minutes": 12,
            "ema_pullback_recovery_max_cross_age_minutes": 6,
            "ema_pullback_recovery_gap": 0.001,
            "ema_trigger_fast_minutes": 1,
            "ema_trigger_slow_minutes": 2,
            "ema_use_rs_confirmation": True,
            "ema_use_btc_risk_filter": True,
            "daily_volatility_window": 10,
        }
        defaults.update(overrides)
        return replace(config.STRATEGY, **defaults)

    def entry_signal(self, *, score=0.05, rs30=0.002, rs60=0.003, ts=1000):
        return {
            "valid": True,
            "entry_valid": True,
            "add_valid": True,
            "macro_valid": True,
            "pullback_valid": True,
            "trigger_valid": True,
            "btc_entry_valid": True,
            "benchmark_ok": True,
            "score": score,
            "rs30": rs30,
            "rs60": rs60,
            "trend_ema_gap": score / 2.0,
            "ema_gap": score / 4.0,
            "ts": ts,
        }

    def macro_context(self, **overrides):
        context = {
            "ok": True,
            "ts": int(time.time()),
            "regime": "neutral",
            "gold_symbol": XAUT_SYMBOL,
            "btc_symbol": BTC_SYMBOL,
            "gold_rsi": 50.0,
            "btc_rsi": 50.0,
            "rsi_spread": 0.0,
            "gold_btc_ratio_return": 0.0,
            "long_budget_multiplier": 1.0,
            "short_budget_multiplier": 1.0,
            "ladder_multiplier": 1.0,
            "disable_new_entries": False,
            "disable_averaging": False,
            "disable_recovery": False,
            "time_exit_multiplier": 1.0,
            "reason": "test",
        }
        context.update(overrides)
        return context

    def set_macro_context(self, bot, **overrides):
        context = self.macro_context(**overrides)
        bot.signal_cache.setdefault("macro", {})["gold_btc_rsi"] = context
        return context

    def external_context(self, **overrides):
        context = {
            "valid": True,
            "stale": False,
            "reason": "ok",
            "symbol": SYMBOL,
            "mexc_symbol": "TESTUSDT",
            "ts": time.time(),
            "htx_bid": 101.9,
            "htx_ask": 102.1,
            "htx_mid": 102.0,
            "mexc_bid": 99.9,
            "mexc_ask": 100.1,
            "mexc_mid": 100.0,
            "spread_bps": 200.0,
            "spread_bps_30s_avg": 200.0,
            "spread_bps_2m_avg": 200.0,
            "spread_bps_10m_avg": 200.0,
            "spread_bps_zscore": 0.0,
            "htx_change_30s_bps": 0.0,
            "mexc_change_30s_bps": 0.0,
            "htx_change_1m_bps": 0.0,
            "mexc_change_1m_bps": 0.0,
            "age_ms": 0,
        }
        context.update(overrides)
        return context

    def test_mexc_client_uses_requests_session_params_and_timeout(self):
        response = FakeRequestsResponse(
            {
                "bidPrice": "99.9",
                "askPrice": "100.1",
                "bidQty": "7",
                "askQty": "8",
            }
        )
        session = FakeRequestsSession([response])
        client = MexcBookTickerClient(timeout_sec=2.5, session=session)

        book = client.fetch("TESTUSDT")

        self.assertEqual(book.bid, 99.9)
        self.assertEqual(book.ask, 100.1)
        self.assertEqual(book.bid_qty, 7.0)
        self.assertEqual(book.ask_qty, 8.0)
        self.assertEqual(response.raise_for_status_calls, 1)
        self.assertEqual(
            session.calls,
            [
                {
                    "url": "https://api.mexc.com/api/v3/ticker/bookTicker",
                    "params": {"symbol": "TESTUSDT"},
                    "timeout": 2.5,
                }
            ],
        )

    def test_external_price_requests_error_uses_cached_fallback(self):
        now = [2000.0]
        settings = replace(
            config.EXTERNAL_PRICE_FEED,
            rest_poll_interval_sec=0.0,
            stale_after_ms=10000,
            max_price_age_ms=10000,
            max_internal_spread_bps=50.0,
            min_valid_bid_qty_usdt=1.0,
            min_valid_ask_qty_usdt=1.0,
        )
        session = FakeRequestsSession(
            [
                FakeRequestsResponse(
                    {
                        "bidPrice": "99.9",
                        "askPrice": "100.1",
                        "bidQty": "10",
                        "askQty": "10",
                    }
                ),
                FakeRequestsResponse(error=RuntimeError("http failed")),
            ]
        )
        client = MexcBookTickerClient(timeout_sec=3.0, session=session)
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: now[0])

        first = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)
        now[0] = 2001.0
        second = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertTrue(first["valid"])
        self.assertTrue(second["valid"])
        self.assertEqual(second["reason"], "mexc_fetch_failed_cached")

    def test_external_price_requests_error_without_cache_returns_failed_context(self):
        settings = replace(
            config.EXTERNAL_PRICE_FEED,
            rest_poll_interval_sec=0.0,
            max_internal_spread_bps=50.0,
            min_valid_bid_qty_usdt=1.0,
            min_valid_ask_qty_usdt=1.0,
        )
        session = FakeRequestsSession([FakeRequestsResponse(error=RuntimeError("timeout"))])
        client = MexcBookTickerClient(timeout_sec=3.0, session=session)
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: 2000.0)

        context = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertFalse(context["valid"])
        self.assertTrue(context["stale"])
        self.assertEqual(context["reason"], "mexc_fetch_failed")

    def test_external_price_invalid_mexc_book_reports_invalid_reason(self):
        settings = replace(
            config.EXTERNAL_PRICE_FEED,
            rest_poll_interval_sec=0.0,
            max_internal_spread_bps=50.0,
            min_valid_bid_qty_usdt=1000.0,
            min_valid_ask_qty_usdt=1000.0,
        )
        client = FakeMexcClient([BookTicker(99.9, 100.1, 1.0, 1.0, ts=2000.0)])
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: 2000.0)

        context = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertFalse(context["valid"])
        self.assertTrue(context["stale"])
        self.assertEqual(context["reason"], "mexc_book_invalid")

    def test_external_price_feed_computes_spread_rollups_and_changes(self):
        now = [1000.0]
        settings = replace(
            config.EXTERNAL_PRICE_FEED,
            rest_poll_interval_sec=0.0,
            max_internal_spread_bps=50.0,
            min_valid_bid_qty_usdt=1.0,
            min_valid_ask_qty_usdt=1.0,
        )
        client = FakeMexcClient([
            BookTicker(99.9, 100.1, 10.0, 10.0, ts=1000.0),
            BookTicker(100.9, 101.1, 10.0, 10.0, ts=1030.0),
            BookTicker(101.9, 102.1, 10.0, 10.0, ts=1060.0),
        ])
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: now[0])

        first = feed.get_context(SYMBOL, {"bid": 100.9, "ask": 101.1}, market=MARKET)
        now[0] = 1030.0
        second = feed.get_context(SYMBOL, {"bid": 101.9, "ask": 102.1}, market=MARKET)
        now[0] = 1060.0
        third = feed.get_context(SYMBOL, {"bid": 103.9, "ask": 104.1}, market=MARKET)

        self.assertTrue(first["valid"])
        self.assertAlmostEqual(first["spread_bps"], 100.0)
        self.assertAlmostEqual(second["spread_bps_30s_avg"], 100.0)
        self.assertGreater(third["htx_change_1m_bps"], third["mexc_change_1m_bps"])
        self.assertEqual(client.calls, ["TESTUSDT", "TESTUSDT", "TESTUSDT"])

    def test_external_price_stale_context_is_invalid(self):
        now = [2000.0]
        settings = replace(config.EXTERNAL_PRICE_FEED, stale_after_ms=3000, max_price_age_ms=3000)
        client = FakeMexcClient([BookTicker(99.9, 100.1, 10.0, 10.0, ts=1990.0)])
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: now[0])

        context = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertFalse(context["valid"])
        self.assertTrue(context["stale"])
        self.assertGreater(context["age_ms"], 3000)

    def test_external_price_uses_strictest_stale_age_limit(self):
        now = [2000.0]
        settings = replace(
            config.EXTERNAL_PRICE_FEED,
            stale_after_ms=1000,
            max_price_age_ms=10000,
            max_internal_spread_bps=50.0,
            min_valid_bid_qty_usdt=1.0,
            min_valid_ask_qty_usdt=1.0,
        )
        client = FakeMexcClient([BookTicker(99.9, 100.1, 10.0, 10.0, ts=1998.0)])
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: now[0])

        context = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertFalse(context["valid"])
        self.assertTrue(context["stale"])
        self.assertGreater(context["age_ms"], 1000)

    def test_external_price_long_premium_blocks_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(self.external_context(spread_bps=25.0))
                bot.signal_cache["symbols"] = {SYMBOL: self.entry_signal()}

                bot._maybe_place_initial_buy(SYMBOL, bot.signal_cache["symbols"][SYMBOL])

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("external_premium_blocked", rows[-1]["reason"])

    def test_external_price_short_discount_blocks_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, dry_run=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(self.external_context(spread_bps=-25.0))
                bot.signal_cache["symbols"] = {SYMBOL: self.entry_signal(rs30=-0.002, rs60=-0.003)}

                bot._maybe_place_initial_buy(SYMBOL, bot.signal_cache["symbols"][SYMBOL])

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("external_discount_blocked", rows[-1]["reason"])

    def test_external_price_stale_is_ignored_by_default_for_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(self.external_context(valid=False, stale=True, reason="stale"))
                signal = self.entry_signal()

                bot._maybe_place_initial_buy(SYMBOL, signal)

                self.assertTrue(bot._get_state(SYMBOL).entry_orders)

    def test_external_price_disable_stale_reference_blocks_entry_even_when_ignore_is_true(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            settings = replace(
                config.EXTERNAL_PRICE_FEED,
                disable_trading_if_reference_stale=True,
                ignore_reference_if_stale=True,
            )
            with override_config(RUNTIME=runtime, EXTERNAL_PRICE_FEED=settings):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(valid=False, stale=True, reason="stale")
                )

                bot._maybe_place_initial_buy(SYMBOL, self.entry_signal())

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("external_reference_stale", rows[-1]["reason"])

    def test_external_price_invalid_context_blocks_entry_when_stale_is_fatal(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            settings = replace(config.EXTERNAL_PRICE_FEED, disable_trading_if_reference_stale=True)
            with override_config(RUNTIME=runtime, EXTERNAL_PRICE_FEED=settings):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = InvalidExternalPriceFeed()

                bot._maybe_place_initial_buy(SYMBOL, self.entry_signal())

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("external_price_context_invalid", rows[-1]["reason"])

    def test_pending_entry_keeps_external_impulse_bonus_during_signal_revalidation(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            strategy = replace(config.STRATEGY, entry_min_score=0.03)
            settings = replace(
                config.EXTERNAL_PRICE_FEED,
                impulse_confirmation_enabled=True,
                mexc_lead_threshold_bps_30s=5.0,
                impulse_score_bonus=0.02,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy, EXTERNAL_PRICE_FEED=settings):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        spread_bps=0.0,
                        htx_change_30s_bps=0.0,
                        mexc_change_30s_bps=10.0,
                    )
                )
                state = bot._get_state(SYMBOL)
                state.entry_orders = [
                    {
                        "id": "pending_entry",
                        "side": config.ENTRY_SIDE,
                        "price": 100.0,
                        "amount": 1.0,
                        "created_at": time.time(),
                    }
                ]
                signal = self.entry_signal(score=0.015)

                bot._manage_entry_orders(SYMBOL, signal, open_orders=[])

                self.assertEqual([order["id"] for order in state.entry_orders], ["pending_entry"])

    def test_external_price_context_is_cached_within_entry_decision(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            strategy = replace(config.STRATEGY, entry_min_score=0.03)
            settings = replace(
                config.EXTERNAL_PRICE_FEED,
                impulse_confirmation_enabled=True,
                mexc_lead_threshold_bps_30s=5.0,
                impulse_score_bonus=0.02,
                block_if_exchange_divergence_1m_bps=0.0,
                max_htx_premium_for_long_bps=50.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy, EXTERNAL_PRICE_FEED=settings):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        spread_bps=0.0,
                        htx_change_30s_bps=0.0,
                        mexc_change_30s_bps=10.0,
                    )
                )

                bot._maybe_place_initial_buy(SYMBOL, self.entry_signal(score=0.015))

                self.assertTrue(bot._get_state(SYMBOL).entry_orders)
                self.assertEqual(len(bot.external_price_feed.calls), 1)

                bot._reset_private_caches()
                bot._external_entry_block_reason(SYMBOL)

                self.assertEqual(len(bot.external_price_feed.calls), 2)

    def test_external_price_divergence_sets_entry_cooldown(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(spread_bps=0.0, htx_change_1m_bps=80.0, mexc_change_1m_bps=10.0)
                )
                signal = self.entry_signal()

                bot._maybe_place_initial_buy(SYMBOL, signal)

                state = bot._get_state(SYMBOL)
                self.assertEqual(state.entry_orders, [])
                self.assertGreater(state.cooldown_until, time.time())

    def test_external_price_favorable_premium_tightens_long_exit_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(self.external_context(spread_bps=25.0))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                bot._place_sell_ladder(SYMBOL, 100.0, 100.0, rebuild=False, closeable_contracts=100.0, mode="normal")

                self.assertEqual([order["amount"] for order in bot.exchange.created_orders], [40.0, 30.0, 20.0])
                self.assertEqual([order["price"] for order in bot.exchange.created_orders], [100.5, 101.0, 102.0])
                self.assertEqual(state.exit_runner_contracts, 10.0)
                self.assertEqual(state.sell_ladder_orders[0]["ladder_name"], "external_tightened")

    def test_external_price_stale_keeps_normal_exit_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(self.external_context(valid=False, stale=True, spread_bps=25.0))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                bot._place_sell_ladder(SYMBOL, 100.0, 100.0, rebuild=False, closeable_contracts=100.0, mode="normal")

                self.assertEqual([order["amount"] for order in bot.exchange.created_orders], [35.0, 25.0, 25.0])
                self.assertEqual([order["price"] for order in bot.exchange.created_orders], [100.8, 101.6, 103.0])

    def test_calculate_rsi_basic_shapes(self):
        rising = [float(index) for index in range(1, 40)]
        falling = list(reversed(rising))
        flat = [10.0] * 40

        self.assertGreater(calculate_rsi(rising, 14), 50.0)
        self.assertLess(calculate_rsi(falling, 14), 50.0)
        self.assertEqual(calculate_rsi(flat, 14), 50.0)
        self.assertEqual(calculate_rsi([1.0, 2.0], 14), 0.0)

    def macro_regime_bot(self, tmp_path: Path, gold_rsi: float, btc_rsi: float) -> HtxFuturesBot:
        bot = self.make_bot(tmp_path)
        bot.benchmark_symbol = BTC_SYMBOL
        bot.macro_gold_symbol = XAUT_SYMBOL
        bot._macro_gold_lookup_done = True
        bot.exchange.ohlcv[(XAUT_SYMBOL, "4h")] = ohlcv_series([100.0] * 40, 4 * 60 * 60)
        bot.exchange.ohlcv[(BTC_SYMBOL, "4h")] = ohlcv_series([100.0] * 40, 4 * 60 * 60)
        values = iter([gold_rsi, btc_rsi])
        bot._calculate_rsi = lambda closes, period: next(values)
        return bot

    def test_gold_btc_rsi_context_regimes(self):
        cases = [
            (65.0, 42.0, "crypto_underperforms_gold"),
            (48.0, 65.0, "crypto_risk_on"),
            (35.0, 35.0, "deleveraging"),
            (65.0, 65.0, "broad_liquidity_risk_on"),
        ]
        macro = replace(config.MACRO, gold_cache_ttl_sec=0, gold_min_candles=20)
        with override_config(MACRO=macro):
            for gold_rsi, btc_rsi, regime in cases:
                with tempfile.TemporaryDirectory() as raw_tmp, self.subTest(regime=regime):
                    bot = self.macro_regime_bot(Path(raw_tmp), gold_rsi, btc_rsi)

                    context = bot._gold_btc_rsi_context()

                    self.assertTrue(context["ok"])
                    self.assertEqual(context["regime"], regime)

    def test_gold_btc_rsi_context_unavailable_without_xaut(self):
        macro = replace(config.MACRO, gold_cache_ttl_sec=0, gold_min_candles=20)
        with tempfile.TemporaryDirectory() as raw_tmp, override_config(MACRO=macro):
            bot = self.make_bot(Path(raw_tmp))
            bot.macro_gold_symbol = None
            bot._macro_gold_lookup_done = True

            context = bot._gold_btc_rsi_context()

            self.assertFalse(context["ok"])
            self.assertEqual(context["regime"], "macro_unavailable")
            self.assertEqual(context["long_budget_multiplier"], 1.0)

    def test_macro_disable_new_entries_blocks_initial_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            self.set_macro_context(
                bot,
                regime="deleveraging",
                disable_new_entries=True,
                reason="btc_weak_gold_weak",
            )

            bot._maybe_place_initial_buy(SYMBOL, self.entry_signal())

            self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
            self.assertEqual(bot.exchange.created_orders, [])

    def test_macro_disable_averaging_blocks_average_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True, dry_run_equity=1000.0)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [{"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}]
                self.set_macro_context(
                    bot,
                    regime="crypto_underperforms_gold",
                    disable_averaging=True,
                    reason="gold_strong_btc_weak",
                )

                bot._maybe_place_average_buy(
                    SYMBOL,
                    {
                        "valid": True,
                        "add_valid": True,
                        "macro_valid": True,
                        "trigger_valid": True,
                        "pullback_valid": False,
                        "ts": 1000,
                        "ladder_multiplier": 1.0,
                        "budget_multiplier": 1.0,
                    },
                )

                self.assertEqual(state.entry_orders, [])

    def test_macro_gold_symbol_is_not_added_to_entry_universe(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            macro = replace(config.MACRO, gold_cache_ttl_sec=0, gold_min_candles=20)
            with override_config(RUNTIME=runtime, MACRO=macro, COINS=("test",)):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.markets = {
                    SYMBOL: MARKET,
                    BTC_SYMBOL: BTC_MARKET,
                    XAUT_SYMBOL: XAUT_MARKET,
                }
                bot.symbols = []
                bot.entry_symbols = set()
                bot.market_by_symbol = {}
                bot.states = {}
                bot._macro_gold_lookup_done = False

                bot.setup()

                self.assertIn(SYMBOL, bot.entry_symbols)
                self.assertNotIn(XAUT_SYMBOL, bot.entry_symbols)
                self.assertNotIn(XAUT_SYMBOL, bot.symbols)
                self.assertEqual(bot.exchange.created_orders, [])

    def test_macro_gold_lookup_accepts_xault_alias_for_xaut_market(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            macro = replace(config.MACRO, gold_coins=("xault",))
            with override_config(MACRO=macro):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.markets = {
                    SYMBOL: MARKET,
                    BTC_SYMBOL: BTC_MARKET,
                    XAUT_SYMBOL: XAUT_MARKET,
                }

                self.assertEqual(bot._find_macro_gold_symbol(), XAUT_SYMBOL)

    def test_cancel_all_orders_keeps_state_when_one_cancel_fails(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                buy_ref = {"id": "buy_1", "side": "buy", "price": 99.0, "amount": 2.0}
                sell_ref = {"id": "sell_1", "side": "sell", "price": 101.0, "amount": 2.0}
                state.entry_orders = [buy_ref]
                state.sell_ladder_orders = [sell_ref]
                bot.exchange.cancel_fail_ids.add("sell_1")

                bot._cancel_all_orders(SYMBOL, reason="test_cancel_failure")

                self.assertEqual(state.entry_orders, [buy_ref])
                self.assertEqual(state.sell_ladder_orders, [sell_ref])

    def test_frozen_recovery_is_disabled_for_ema_strategy(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True)
            strategy = replace(
                config.STRATEGY,
                frozen_recovery_allow_drawdown_trigger=True,
                frozen_recovery_min_drawdown=0.008,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 1.0
                state.entry_price = 10.4
                state.frozen_no_more_buys = True
                state.sell_ladder_orders = [{"id": "sell", "side": "sell", "price": 10.1, "amount": 1.0}]

                bot._maybe_place_frozen_recovery_buy(
                    SYMBOL,
                    {
                        "valid": True,
                        "entry_valid": False,
                        "add_valid": False,
                        "score": 0.0,
                        "rs_edge": 0.0,
                        "volatility_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "budget_multiplier": 3.0,
                        "frozen_recovery_confirmed": False,
                        "frozen_recovery_confirmed_candles": 0,
                        "ts": 1000,
                    },
                )

                self.assertEqual(state.entry_orders, [])
                self.assertEqual(state.frozen_recovery_buys, 0)
                self.assertTrue(state.frozen_no_more_buys)

    def test_entry_ladder_prices_round_away_from_crossing_book(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=True)):
                bot = self.make_bot(Path(raw_tmp))
                bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=100.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="test_rounding",
                )
                self.assertTrue(bot._get_state(SYMBOL).entry_orders)
                raw_long_price = 10.0 * (1 - config.BUYING.ladder_offsets[0])
                self.assertLessEqual(bot._get_state(SYMBOL).entry_orders[0]["price"], raw_long_price)

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=True)):
                bot = self.make_bot(Path(raw_tmp))
                bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=100.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="test_rounding",
                )
                self.assertTrue(bot._get_state(SYMBOL).entry_orders)
                raw_short_price = 10.0 * (1 + config.BUYING.ladder_offsets[0])
                self.assertGreaterEqual(bot._get_state(SYMBOL).entry_orders[0]["price"], raw_short_price)

    def test_ema_entry_ladder_uses_two_one_percent_levels(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=True)):
                bot = self.make_bot(Path(raw_tmp))
                bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=100.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="ema_initial_signal",
                )

                orders = bot._get_state(SYMBOL).entry_orders
                self.assertEqual(len(orders), 2)
                self.assertEqual([order["price"] for order in orders], [10.0, 9.9])

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=True)):
                bot = self.make_bot(Path(raw_tmp))
                bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=100.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="ema_initial_signal",
                )

                orders = bot._get_state(SYMBOL).entry_orders
                self.assertEqual(len(orders), 2)
                self.assertEqual([order["price"] for order in orders], [10.0, 10.1])

    def test_profile_validation_rejects_mismatched_ema_ladder_lengths(self):
        profile = config.resolve_profile("long")
        invalid = replace(
            profile,
            buying=replace(profile.buying, ladder_fractions=(0.5, 0.5), ladder_offsets=(0.0,)),
            strategy=replace(profile.strategy, ema_entry_ladder_fractions=(0.5, 0.5)),
        )

        with self.assertRaisesRegex(ValueError, "ladder_fractions and ladder_offsets"):
            config._validate_profile(invalid)

    def test_entry_ladder_uses_manual_account_leverage_not_sizing_leverage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, post_only_enabled=False)
            risk = replace(config.RISK, leverage=30, account_leverage=0)
            buying = replace(config.BUYING, ladder_fractions=(1.0,), ladder_offsets=(0.0,))
            with override_config(RUNTIME=runtime, RISK=risk, BUYING=buying):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.account_leverage = 50
                bot.exchange.reject_leverage_not_equal = 50

                bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=10.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="ema_initial_signal",
                )

                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["params"]["leverRate"], 50)
                self.assertEqual(order["amount"], 30.0)
                state_order = bot._get_state(SYMBOL).entry_orders[0]
                self.assertEqual(state_order["leverage"], 50.0)
                self.assertEqual(state_order["sizing_leverage"], 30.0)
                self.assertEqual(state_order["amount"], 30.0)

    def test_entry_ladder_caps_sizing_to_lower_manual_account_leverage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, dry_run=False, post_only_enabled=False)
            risk = replace(config.RISK, leverage=30, account_leverage=0)
            buying = replace(config.BUYING, ladder_fractions=(1.0,), ladder_offsets=(0.0,))
            with override_config(RUNTIME=runtime, RISK=risk, BUYING=buying):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.account_leverage = 5

                bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=10.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="ema_initial_signal",
                )

                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["params"]["leverRate"], 5)
                self.assertEqual(order["amount"], 5.0)
                state_order = bot._get_state(SYMBOL).entry_orders[0]
                self.assertEqual(state_order["leverage"], 5.0)
                self.assertEqual(state_order["sizing_leverage"], 5.0)
                self.assertEqual(state_order["amount"], 5.0)

    def test_profile_reads_legacy_risk_setting_aliases(self):
        env = {
            "ALIAS_POSITION_BUDGET_FRACTION": "0.04",
            "ALIAS_MIN_QUOTE_RESERVE": "10",
            "ALIAS_MAX_ACTIVE_POSITIONS": "10",
            "ALIAS_MAX_POSITION_NOTIONAL_FRACTION": "0.08",
            "ALIAS_MAX_TOTAL_NOTIONAL_FRACTION": "0.85",
            "ALIAS_DUST_POSITION_NOTIONAL": "12",
        }
        previous = {key: os.environ.get(key) for key in env}
        try:
            os.environ.update(env)
            profile = config._make_profile("alias", "long", ("test",))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(profile.buying.position_budget_fraction, 0.04)
        self.assertEqual(profile.risk.min_quote_reserve, 10.0)
        self.assertEqual(profile.risk.max_active_positions, 10)
        self.assertEqual(profile.risk.max_position_notional_fraction, 0.08)
        self.assertEqual(profile.risk.max_total_notional_fraction, 0.85)
        self.assertEqual(profile.risk.dust_position_notional, 12.0)
        self.assertEqual(profile.risk.tiny_entry_max_notional, 12.0)

    def test_profile_specific_env_overrides_global_fallback(self):
        with patched_env(DRY_RUN="true", LONG_DRY_RUN="false"):
            profile = config._make_profile("long", "long", ("test",))

        self.assertFalse(profile.runtime.dry_run)

    def test_profile_dotenv_unprefixed_keys_are_profile_scoped(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            (tmp_path / "long").mkdir()
            (tmp_path / "short").mkdir()
            (tmp_path / ".env").write_text("DRY_RUN=true\n", encoding="utf-8")
            (tmp_path / "long" / ".env").write_text("DRY_RUN=false\n", encoding="utf-8")

            with patched_env(DRY_RUN=None, LONG_DRY_RUN=None, HTXBOT_LONG_DRY_RUN=None):
                with override_config(BASE_DIR=tmp_path):
                    profile = config._make_profile("long", "long", ("test",))

        self.assertFalse(profile.runtime.dry_run)

    def test_invalid_env_values_warn_and_use_defaults(self):
        config.CONFIG_WARNINGS.clear()
        env = {
            "ALIAS_POST_ONLY_ENABLED": "definitely",
            "ALIAS_POLL_INTERVAL_SEC": "slow",
            "ALIAS_DRY_RUN_EQUITY": "heavy",
            "ALIAS_EMA_ENTRY_LADDER_FRACTIONS": "0.5,nope",
        }
        with patched_env(**env):
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                profile = config._make_profile("alias", "long", ("test",))

        self.assertTrue(profile.runtime.post_only_enabled)
        self.assertEqual(profile.runtime.poll_interval_sec, 3)
        self.assertEqual(profile.runtime.dry_run_equity, 1000.0)
        self.assertEqual(profile.buying.ladder_fractions, (0.5, 0.5))
        self.assertGreaterEqual(len(config.CONFIG_WARNINGS), 4)
        self.assertGreaterEqual(len(captured), 4)

    def test_pydantic_settings_are_frozen_and_hashable(self):
        mapping = {config.EXTERNAL_PRICE_FEED: "shared"}

        self.assertEqual(mapping[config.EXTERNAL_PRICE_FEED], "shared")
        with self.assertRaises(Exception):
            config.RUNTIME.dry_run = not config.RUNTIME.dry_run

    def test_entry_ladder_does_not_retry_with_lower_leverage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, post_only_enabled=False)
            risk = replace(config.RISK, leverage=30, account_leverage=50)
            buying = replace(config.BUYING, ladder_fractions=(1.0,), ladder_offsets=(0.0,))
            with override_config(RUNTIME=runtime, RISK=risk, BUYING=buying):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.reject_leverage_above = 20

                bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=10.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="ema_initial_signal",
                )

                self.assertEqual(bot.exchange.create_order_calls, 1)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])

    def test_normal_exit_ladder_uses_three_reduce_only_limits_and_runner(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                bot._place_sell_ladder(
                    SYMBOL,
                    total_contracts=100.0,
                    avg_entry_price=100.0,
                    rebuild=False,
                    closeable_contracts=100.0,
                    mode="normal",
                )

                self.assertEqual(len(bot.exchange.created_orders), 3)
                self.assertEqual([order["type"] for order in bot.exchange.created_orders], ["limit", "limit", "limit"])
                self.assertEqual([order["side"] for order in bot.exchange.created_orders], ["sell", "sell", "sell"])
                self.assertEqual([order["amount"] for order in bot.exchange.created_orders], [35.0, 25.0, 25.0])
                self.assertEqual([order["price"] for order in bot.exchange.created_orders], [100.8, 101.6, 103.0])
                self.assertTrue(all(order["params"].get("reduceOnly") for order in bot.exchange.created_orders))
                self.assertEqual(state.exit_runner_contracts, 15.0)
                self.assertFalse(state.exit_runner_active)

    def test_exit_ladder_switches_to_medium_and_heavy_by_position_ratio(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                state.position_size = 150.0
                state.position_available = 150.0
                bot._place_sell_ladder(
                    SYMBOL,
                    total_contracts=150.0,
                    avg_entry_price=100.0,
                    rebuild=False,
                    closeable_contracts=150.0,
                    mode="normal",
                )
                self.assertEqual([order["price"] for order in bot.exchange.created_orders], [100.4, 101.0, 102.0, 103.5])
                self.assertEqual(state.exit_runner_contracts, 0.0)

                bot.exchange.created_orders.clear()
                state.sell_ladder_orders = []
                state.sell_ladder_signature = ""
                state.position_size = 200.0
                state.position_available = 200.0
                bot._place_sell_ladder(
                    SYMBOL,
                    total_contracts=200.0,
                    avg_entry_price=100.0,
                    rebuild=True,
                    closeable_contracts=200.0,
                    mode="normal",
                )
                self.assertEqual([order["price"] for order in bot.exchange.created_orders], [100.3, 100.8, 101.5])

    def test_exit_ladder_time_decay_removes_runner_after_six_hours(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                state.cycle_opened_at = time.time() - 6.1 * 60.0 * 60.0

                bot._place_sell_ladder(
                    SYMBOL,
                    total_contracts=100.0,
                    avg_entry_price=100.0,
                    rebuild=False,
                    closeable_contracts=100.0,
                    mode="normal",
                )

                self.assertEqual([order["amount"] for order in bot.exchange.created_orders], [35.0, 25.0, 40.0])
                self.assertEqual([order["price"] for order in bot.exchange.created_orders], [100.8, 101.6, 103.0])
                self.assertEqual(state.exit_runner_contracts, 0.0)

    def test_normal_runner_closes_on_trailing_pullback(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                bot._place_sell_ladder(
                    SYMBOL,
                    total_contracts=100.0,
                    avg_entry_price=100.0,
                    rebuild=False,
                    closeable_contracts=100.0,
                    mode="normal",
                )
                self.assertEqual(state.exit_runner_contracts, 15.0)

                bot.exchange.ticker = {"bid": 102.1, "ask": 102.2, "last": 102.1}
                managed = bot._maybe_manage_exit_runner(SYMBOL, {"trigger_valid": True})
                self.assertFalse(managed)
                self.assertTrue(state.exit_runner_active)
                self.assertEqual(len(bot.exchange.created_orders), 3)

                state.position_available = 15.0
                bot.exchange.ticker = {"bid": 101.0, "ask": 101.1, "last": 101.0}
                managed = bot._maybe_manage_exit_runner(SYMBOL, {"trigger_valid": True})

                self.assertTrue(managed)
                self.assertEqual(len(bot.exchange.created_orders), 4)
                runner_order = bot.exchange.created_orders[-1]
                self.assertEqual(runner_order["amount"], 15.0)
                self.assertEqual(runner_order["price"], 101.0)
                self.assertTrue(runner_order["params"].get("reduceOnly"))
                self.assertTrue(state.sell_ladder_orders[-1]["runner"])

    def test_entry_expansion_orders_are_canceled_by_ema_entry_check(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=True)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.entry_orders = [
                    {
                        "id": "entry_1",
                        "side": "buy",
                        "price": 10.0,
                        "amount": 1.0,
                        "created_at": time.time(),
                    }
                ]
                signal = {
                    "valid": True,
                    "entry_valid": False,
                    "trend_valid": True,
                    "btc_entry_valid": True,
                    "rs_overheated": False,
                    "score": config.STRATEGY.entry_min_score * 0.80,
                    "rs_edge": config.STRATEGY.entry_min_rs_edge * 0.80,
                    "rs30": 0.0,
                    "rs60": config.STRATEGY.entry_min_rs60_abs * 0.80,
                    "ema_gap": config.STRATEGY.entry_min_ema_gap * 0.80,
                    "recent_return_5m": config.STRATEGY.entry_min_recent_return_5m * 0.80,
                    "recent_return_15m": 0.0,
                    "local_reversion": config.STRATEGY.entry_min_pullback * 0.80,
                }

                bot._manage_entry_orders(SYMBOL, signal, [])

                self.assertEqual(state.entry_orders, [])
                bot.signal_cache["benchmark_ok"] = False
                self.assertFalse(bot._is_entry_expansion_signal_valid(signal))

    def test_tiny_partial_entry_timeout_closes_market_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True, order_timeout_sec=1)
            risk = replace(
                config.RISK,
                dust_position_notional=1.0,
                tiny_entry_close_enabled=True,
                tiny_entry_max_notional=10.0,
                tiny_entry_max_planned_fraction=0.10,
            )
            with override_config(RUNTIME=runtime, RISK=risk):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 2.0
                state.position_available = 2.0
                state.entry_price = 2.0
                state.planned_quote_budget = 20.0
                state.initial_entry_notional = 600.0
                state.entry_orders = [
                    {
                        "id": "entry_1",
                        "side": "buy",
                        "price": 2.0,
                        "amount": 300.0,
                        "created_at": time.time() - 10.0,
                    }
                ]
                state.sell_ladder_orders = [
                    {
                        "id": "exit_1",
                        "side": "sell",
                        "price": 2.1,
                        "amount": 2.0,
                    }
                ]

                bot._manage_entry_orders(SYMBOL, self.entry_signal(ts=2000), [])

                self.assertEqual(state.entry_orders, [])
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 2.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertTrue(state.zombie_position)
                self.assertTrue(state.frozen_no_more_buys)

    def test_entry_quality_gate_requires_score_and_directional_rs(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                entry_min_score=0.03,
                entry_min_rs60_abs=0.002,
                entry_min_rs30_abs=0.001,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))

                self.assertTrue(bot._is_entry_signal_valid(self.entry_signal(score=0.04, rs30=0.002, rs60=0.003)))
                self.assertFalse(bot._is_entry_signal_valid(self.entry_signal(score=0.02, rs30=0.002, rs60=0.003)))
                self.assertFalse(bot._is_entry_signal_valid(self.entry_signal(score=0.04, rs30=0.002, rs60=0.001)))
                self.assertFalse(bot._is_entry_signal_valid(self.entry_signal(score=0.04, rs30=0.0005, rs60=0.003)))

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            strategy = replace(
                config.STRATEGY,
                entry_min_score=0.03,
                entry_min_rs60_abs=0.002,
                entry_min_rs30_abs=0.001,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))

                self.assertTrue(bot._is_entry_signal_valid(self.entry_signal(score=0.04, rs30=-0.002, rs60=-0.003)))
                self.assertFalse(bot._is_entry_signal_valid(self.entry_signal(score=0.04, rs30=0.002, rs60=-0.003)))

    def test_entry_gate_limits_to_top_ranked_symbols(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                entry_min_score=0.0,
                entry_min_rs60_abs=0.0,
                entry_min_rs30_abs=0.0,
                entry_max_new_ladders_per_signal=2,
                entry_rate_limit_ladders=0,
                entry_crowded_min_signals=0,
                entry_crowded_signal_fraction=0.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                symbols = ("AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT")
                bot.entry_symbols = set(symbols)
                bot.signal_cache = {
                    "benchmark_ok": True,
                    "closed_candle_ts": 1000,
                    "symbols": {
                        symbols[0]: self.entry_signal(score=0.03, rs30=0.003, rs60=0.003),
                        symbols[1]: self.entry_signal(score=0.08, rs30=0.002, rs60=0.002),
                        symbols[2]: self.entry_signal(score=0.05, rs30=0.004, rs60=0.004),
                    },
                }

                gate = bot._prepare_new_entry_gate()

                self.assertEqual(gate["ranked_symbols"], [symbols[1], symbols[2], symbols[0]])
                self.assertEqual(gate["allowed_symbols"], {symbols[1], symbols[2]})
                self.assertIn("entry_top_n_blocked", gate["blocked_reasons"][symbols[0]])

    def test_entry_gate_rate_limit_counts_recent_positions(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                entry_min_score=0.0,
                entry_min_rs60_abs=0.0,
                entry_min_rs30_abs=0.0,
                entry_max_new_ladders_per_signal=5,
                entry_rate_limit_ladders=1,
                entry_rate_limit_window_minutes=60.0,
                entry_crowded_min_signals=0,
                entry_crowded_signal_fraction=0.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                recent = bot._get_state("RECENT/USDT:USDT")
                recent.position_size = 1.0
                recent.entry_price = 10.0
                recent.cycle_opened_at = time.time()
                symbols = ("AAA/USDT:USDT", "BBB/USDT:USDT")
                bot.entry_symbols = set(symbols)
                bot.signal_cache = {
                    "benchmark_ok": True,
                    "closed_candle_ts": 1000,
                    "symbols": {symbol: self.entry_signal(score=0.05) for symbol in symbols},
                }

                gate = bot._prepare_new_entry_gate()

                self.assertEqual(gate["allowed_symbols"], set())
                self.assertEqual(gate["rate_remaining"], 0)
                self.assertTrue(all("entry_rate_limited" in reason for reason in gate["blocked_reasons"].values()))

    def test_entry_gate_crowded_mode_tightens_thresholds_and_top_n(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                entry_min_score=0.0,
                entry_min_rs60_abs=0.0,
                entry_min_rs30_abs=0.0,
                entry_max_new_ladders_per_signal=5,
                entry_rate_limit_ladders=0,
                entry_crowded_min_signals=3,
                entry_crowded_signal_fraction=0.0,
                entry_crowded_max_new_ladders_per_signal=1,
                entry_crowded_min_score=0.05,
                entry_crowded_min_rs60_abs=0.003,
                entry_crowded_min_rs30_abs=0.0015,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                symbols = ("AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT")
                bot.entry_symbols = set(symbols)
                bot.signal_cache = {
                    "benchmark_ok": True,
                    "closed_candle_ts": 1000,
                    "symbols": {
                        symbols[0]: self.entry_signal(score=0.06, rs30=0.002, rs60=0.004),
                        symbols[1]: self.entry_signal(score=0.04, rs30=0.004, rs60=0.004),
                        symbols[2]: self.entry_signal(score=0.07, rs30=0.001, rs60=0.004),
                    },
                }

                gate = bot._prepare_new_entry_gate()

                self.assertTrue(gate["crowded"])
                self.assertEqual(gate["allowed_symbols"], {symbols[0]})
                self.assertIn("entry_score_below_min", gate["blocked_reasons"][symbols[1]])
                self.assertIn("entry_rs30_below_min", gate["blocked_reasons"][symbols[2]])

    def test_ema_long_entry_signal_is_valid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [198, 195, 192, 189, 186, 183, 180, 184, 188, 192, 196]
                benchmark_closes = [100.0] * config.SIGNALS.min_signal_candles
                while len(benchmark_closes) < len(closes):
                    benchmark_closes.append(100.0)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {"budget_multiplier": 1.0, "ladder_multiplier": 1.0, "reason": "test"},
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertTrue(signal["macro_valid"])
                self.assertTrue(signal["pullback_valid"])
                self.assertTrue(signal["pullback_had_pullback"])
                self.assertLessEqual(signal["pullback_cross_age_candles"], 6)
                self.assertTrue(signal["trigger_valid"])
                self.assertTrue(signal["entry_valid"])
                self.assertEqual(strategy.ema_pullback_slow_minutes, 8)

    def test_ema_pullback_recovery_requires_fresh_cross(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(ema_pullback_recovery_max_cross_age_minutes=2)
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = (
                    list(range(100, 201, 2))
                    + [198, 195, 192, 189, 186, 183, 180, 184, 188, 192, 196, 198, 200]
                )
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {"budget_multiplier": 1.0, "ladder_multiplier": 1.0, "reason": "test"},
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertTrue(signal["pullback_recovered"])
                self.assertTrue(signal["pullback_had_pullback"])
                self.assertGreater(signal["pullback_cross_age_candles"], 2)
                self.assertFalse(signal["pullback_valid"])
                self.assertFalse(signal["entry_valid"])

    def test_signal_build_applies_macro_budget_and_ladder_multipliers(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [198, 195, 192, 189, 186, 183, 180, 184, 188, 192, 196]
                benchmark_closes = [100.0] * len(closes)
                macro_context = self.macro_context(
                    regime="crypto_underperforms_gold",
                    long_budget_multiplier=0.55,
                    ladder_multiplier=1.25,
                    disable_averaging=True,
                )

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {"budget_multiplier": 0.80, "ladder_multiplier": 1.20, "reason": "btc_drop"},
                    latest_ts=1000,
                    macro_context=macro_context,
                )

                self.assertIsNotNone(signal)
                self.assertAlmostEqual(signal["budget_multiplier"], 0.44)
                self.assertAlmostEqual(signal["ladder_multiplier"], 1.50)
                self.assertEqual(signal["macro_regime"], "crypto_underperforms_gold")
                self.assertTrue(signal["macro_disable_averaging"])

    def test_ema_short_entry_signal_is_valid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(200, 99, -2)) + [102, 105, 108, 111, 114, 117, 120, 116, 112, 108, 104]
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {"budget_multiplier": 1.0, "ladder_multiplier": 1.0, "reason": "test"},
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertTrue(signal["macro_valid"])
                self.assertTrue(signal["pullback_valid"])
                self.assertTrue(signal["pullback_had_pullback"])
                self.assertLessEqual(signal["pullback_cross_age_candles"], 6)
                self.assertTrue(signal["trigger_valid"])
                self.assertTrue(signal["entry_valid"])

    def test_ema_macro_false_blocks_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(150, 99, -2)) + [102, 105, 108, 111, 114, 117, 120, 118, 116, 114]
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {"budget_multiplier": 1.0, "ladder_multiplier": 1.0, "reason": "test"},
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertFalse(signal["macro_valid"])
                self.assertFalse(signal["entry_valid"])

    def test_daily_volatility_falls_back_to_neutral_without_history(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                daily_volatility_window=config.SIGNALS.min_signal_candles + 100,
                daily_volatility_reference=0.020,
                enable_volatility_targeted_sizing=True,
                min_volatility_budget_multiplier=0.65,
                max_volatility_budget_multiplier=1.50,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = [100.0] * config.SIGNALS.min_signal_candles
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {"budget_multiplier": 1.0, "ladder_multiplier": 1.0, "reason": "test"},
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertEqual(signal["daily_volatility"], 0.0)
                self.assertEqual(signal["daily_volatility_multiplier"], 1.0)
                self.assertEqual(signal["volatility_budget_multiplier"], 1.0)

    def test_risk_budget_applies_volatility_budget_multiplier(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            buying = replace(config.BUYING, position_budget_fraction=0.02)
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=True, dry_run_equity=1000.0), BUYING=buying):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)

                reduced_budget, reduced_reason = bot._risk_budget(
                    SYMBOL,
                    state,
                    reference_price=10.0,
                    is_new_position=True,
                    signal={"budget_multiplier": 1.0, "volatility_budget_multiplier": 0.50},
                )
                expanded_budget, expanded_reason = bot._risk_budget(
                    SYMBOL,
                    state,
                    reference_price=10.0,
                    is_new_position=True,
                    signal={"budget_multiplier": 1.0, "volatility_budget_multiplier": 1.50},
                )

                self.assertAlmostEqual(reduced_budget, 10.0)
                self.assertAlmostEqual(expanded_budget, 30.0)
                self.assertIn("effective_budget_multiplier=0.500", reduced_reason)
                self.assertIn("effective_budget_multiplier=1.500", expanded_reason)

    def test_ema_averaging_places_half_position_margin_after_drawdown(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=True, dry_run_equity=1000.0)
            strategy = replace(config.STRATEGY, ema_averaging_interval_hours=8.0, ema_max_averaging_stages=2)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [{"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}]

                bot._maybe_place_average_buy(
                    SYMBOL,
                    {
                        "valid": True,
                        "add_valid": True,
                        "macro_valid": True,
                        "trigger_valid": True,
                        "pullback_valid": False,
                        "ts": 1000,
                        "ladder_multiplier": 1.0,
                        "budget_multiplier": 1.0,
                    },
                )

                self.assertTrue(state.entry_orders)
                self.assertEqual(state.average_stage, 1)
                self.assertIsNotNone(state.last_average_at)

    def test_ema_averaging_is_blocked_after_breakeven(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=True, dry_run_equity=1000.0)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_mode = "breakeven"
                state.breakeven_activated_at = time.time()
                state.sell_ladder_orders = [{"id": "be", "side": "sell", "price": 10.21, "amount": 20.0}]

                bot._maybe_place_average_buy(
                    SYMBOL,
                    {"valid": True, "add_valid": True, "macro_valid": True, "ts": 1000},
                )

                self.assertEqual(state.entry_orders, [])
                self.assertEqual(state.average_stage, 0)

    def test_ema_breakeven_activates_without_market_or_stop_order(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            strategy = replace(config.STRATEGY, ema_breakeven_after_hours=48.0, ema_breakeven_fee_buffer=0.0002)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 48.1 * 60.0 * 60.0
                state.sell_ladder_orders = [{"id": "tp", "side": "sell", "price": 101.0, "amount": 10.0}]

                applied = bot._maybe_apply_time_based_exit(SYMBOL, signal={"valid": True, "add_valid": True})

                self.assertTrue(applied)
                self.assertEqual(state.sell_ladder_mode, "breakeven")
                self.assertTrue(state.frozen_no_more_buys)
                self.assertIsNotNone(state.breakeven_activated_at)
                self.assertIn(("tp", SYMBOL, {"marginMode": config.RISK.margin_mode}), bot.exchange.canceled_orders)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "limit")
                self.assertEqual(order["side"], "sell")
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertFalse(any(item["type"].startswith("stop") or item["type"] == "market" for item in bot.exchange.created_orders))

    def test_ema_2d_default_is_2880_minutes(self):
        with config.use_profile("long"):
            self.assertEqual(config.STRATEGY.ema_pullback_slow_minutes, 2880)

    def test_ema_large_periods_convert_to_configured_timeframes(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            periods = bot._ema_periods(converted=True)

            self.assertEqual(config.STRATEGY.ema_macro_timeframe, "1d")
            self.assertEqual(config.STRATEGY.ema_pullback_timeframe, "4h")
            self.assertEqual(periods["ema_macro_fast"], 25)
            self.assertEqual(periods["ema_macro_slow"], 50)
            self.assertEqual(periods["ema_pullback_fast"], 6)
            self.assertEqual(periods["ema_pullback_slow"], 12)
            self.assertEqual(periods["ema_trigger_fast"], 50)
            self.assertEqual(periods["ema_trigger_slow"], 100)
            self.assertEqual(bot._ema_pullback_recovery_windows(converted=True), (12, 6))
            self.assertEqual(bot._ema_required_history("pullback", converted=True), 24)

    def test_rs_windows_convert_to_trigger_timeframe(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_macro_timeframe="5m",
                ema_pullback_timeframe="5m",
                ema_trigger_timeframe="5m",
                ema_macro_fast_minutes=5,
                ema_macro_slow_minutes=10,
                ema_pullback_fast_minutes=5,
                ema_pullback_slow_minutes=10,
                ema_trigger_fast_minutes=5,
                ema_trigger_slow_minutes=10,
            )
            signals = replace(config.SIGNALS, timeframe="5m")
            with override_config(STRATEGY=strategy, SIGNALS=signals):
                bot = self.make_bot(Path(raw_tmp))
                closes = [
                    80.0, 82.0, 84.0, 86.0, 88.0,
                    90.0, 89.0, 90.0, 91.0, 92.0,
                    93.0, 94.0, 95.0, 100.0, 102.0,
                    104.0, 106.0, 108.0, 110.0, 112.0,
                ]
                benchmark = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark,
                    {"reason": "neutral"},
                    latest_ts=1000,
                    cache_key=SYMBOL,
                    macro_closes=closes,
                    macro_latest_ts=1000,
                    pullback_closes=list(reversed(closes)),
                    pullback_latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertTrue(math.isclose(signal["rs30"], math.log(112.0 / 100.0), rel_tol=1e-12))
                self.assertTrue(math.isclose(signal["rs60"], math.log(112.0 / 90.0), rel_tol=1e-12))
                self.assertEqual(signal["btc_return_30m"], 0.0)

    def test_signal_update_fetches_higher_timeframe_ema_history(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            trigger_closes = [100.0 + index * 0.1 for index in range(120)]
            macro_closes = [100.0 + index for index in range(60)]
            pullback_closes = [100.0 + index for index in range(18)] + [116.0, 113.0, 110.0, 107.0, 104.0, 101.0, 98.0, 102.0, 106.0, 110.0, 114.0, 118.0]

            trigger_start = 1_760_000_000_000
            trigger_latest = trigger_start + (len(trigger_closes) - 1) * 60 * 1000
            macro_start = trigger_latest - (len(macro_closes) - 1) * 24 * 60 * 60 * 1000
            pullback_start = trigger_latest - (len(pullback_closes) - 1) * 4 * 60 * 60 * 1000

            bot.exchange.ohlcv[(SYMBOL, "1m")] = ohlcv_series(trigger_closes, 60, trigger_start)
            bot.exchange.ohlcv[(SYMBOL, "1d")] = ohlcv_series(macro_closes, 24 * 60 * 60, macro_start)
            bot.exchange.ohlcv[(SYMBOL, "4h")] = ohlcv_series(pullback_closes, 4 * 60 * 60, pullback_start)

            updated = bot._update_signal_cache_if_needed()

            self.assertTrue(updated)
            signal = bot.signal_cache["symbols"][SYMBOL]
            self.assertTrue(signal["entry_valid"])
            self.assertTrue(signal["pullback_valid"])
            self.assertLessEqual(signal["pullback_cross_age_candles"], 6)
            self.assertEqual(signal["ema_macro_timeframe"], "1d")
            self.assertEqual(signal["ema_pullback_timeframe"], "4h")
            limits = {}
            for call in bot.exchange.ohlcv_calls:
                timeframe = call["timeframe"]
                limits[timeframe] = max(limits.get(timeframe, 0), int(call["limit"] or 0))
            self.assertLess(limits["1d"], 100)
            self.assertLess(limits["4h"], 30)
            self.assertLess(limits["1m"], 2000)

    def test_hard_time_exit_close_fraction_ramps_by_age(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                hard_time_exit_after_minutes=480.0,
                hard_time_exit_close_fraction=0.25,
                hard_time_exit_step_minutes=60.0,
                hard_time_exit_fraction_step=0.25,
                hard_time_exit_bypass_profit_bank=True,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 10.0

                now = time.time()
                for held_minutes, expected_contracts in ((480.0, 25.0), (540.0, 50.0), (660.0, 100.0)):
                    state.cycle_opened_at = now - held_minutes * 60.0
                    contracts = bot._controlled_loss_contracts(
                        SYMBOL,
                        state,
                        reference_price=9.0,
                        had_sell_ladder=False,
                    )
                    self.assertEqual(contracts, expected_contracts)

    def test_hard_time_exit_uses_wider_loss_cap(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                hard_time_exit_after_minutes=480.0,
                hard_time_exit_max_loss_on_notional=0.012,
                controlled_loss_max_loss_on_notional=0.006,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 481.0 * 60.0

                price = bot._controlled_loss_exit_price(
                    SYMBOL,
                    avg_entry_price=100.0,
                    move_fraction=1.0,
                    context={"controlled_reference_price": 80.0},
                )

                self.assertAlmostEqual(price, 98.80)

    def test_absolute_force_exit_ignores_stale_frozen_state(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=True,
                absolute_force_exit_after_minutes=10.0,
            )
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 0.0
                state.position_frozen = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 11.0 * 60.0

                applied = bot._maybe_apply_absolute_force_exit(SYMBOL, reason="test_force_exit")

                self.assertTrue(applied)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["type"], "market")
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 10.0)
                self.assertTrue(bot.exchange.created_orders[0]["params"].get("reduceOnly"))
                self.assertTrue(state.zombie_position)
                self.assertEqual(state.sell_ladder_mode, "absolute_force_exit")
                self.assertEqual(state.sell_ladder_signature, "")

    def test_absolute_force_exit_market_order_is_capped_to_available_amount(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=True,
                absolute_force_exit_after_minutes=10.0,
            )
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 4.0
                state.position_frozen = 6.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 11.0 * 60.0

                applied = bot._maybe_apply_absolute_force_exit(SYMBOL, reason="test_force_exit")

                self.assertTrue(applied)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["type"], "market")
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 4.0)
                self.assertTrue(bot.exchange.created_orders[0]["params"].get("reduceOnly"))

    def test_absolute_force_exit_waits_after_canceling_exit_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=True,
                absolute_force_exit_after_minutes=10.0,
            )
            runtime = replace(config.RUNTIME, dry_run=False, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 11.0 * 60.0
                state.sell_ladder_orders = [
                    {"id": "sell_1", "side": "sell", "price": 101.0, "amount": 10.0}
                ]

                applied = bot._maybe_apply_absolute_force_exit(SYMBOL, reason="test_force_exit")

                self.assertTrue(applied)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(state.sell_ladder_mode, "absolute_force_exit")
                self.assertIn(("sell_1", SYMBOL, {"marginMode": config.RISK.margin_mode}), bot.exchange.canceled_orders)

                applied = bot._maybe_apply_absolute_force_exit(SYMBOL, reason="test_force_exit")

                self.assertTrue(applied)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["type"], "market")
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 10.0)
                self.assertTrue(bot.exchange.created_orders[0]["params"].get("reduceOnly"))

    def test_unknown_short_exit_orders_over_position_are_canceled(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                open_orders = [
                    {
                        "id": "unknown_buy",
                        "symbol": SYMBOL,
                        "side": "buy",
                        "price": 99.0,
                        "amount": 8.0,
                        "remaining": 8.0,
                    }
                ]

                valid = bot._validate_sell_orders(SYMBOL, open_orders)

                self.assertFalse(valid)
                self.assertIn(("unknown_buy", SYMBOL, {"marginMode": config.RISK.margin_mode}), bot.exchange.canceled_orders)

    def test_tracked_exit_order_without_reduce_only_is_canceled(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [{"id": "sell_1", "side": "sell", "price": 101.0, "amount": 5.0}]

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "sell_1",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 5.0,
                            "remaining": 5.0,
                            "reduceOnly": False,
                        }
                    ],
                )

                self.assertFalse(valid)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertIn(("sell_1", SYMBOL, {"marginMode": config.RISK.margin_mode}), bot.exchange.canceled_orders)

    def test_unknown_reduce_only_exit_orders_are_adopted(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.entry_price = 100.0

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "orphan_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 5.0,
                            "remaining": 5.0,
                            "reduceOnly": True,
                        }
                    ],
                )

                self.assertTrue(valid)
                self.assertEqual([order["id"] for order in state.sell_ladder_orders], ["orphan_sell"])
                self.assertEqual(state.sell_ladder_signature, bot._sell_ladder_signature("normal"))
                bot._ensure_sell_ladder(SYMBOL)
                self.assertEqual(bot.exchange.created_orders, [])

    def test_tracked_exit_orders_are_preserved_when_temporarily_invisible(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.sell_ladder_signature = bot._sell_ladder_signature("normal")
                state.sell_ladder_orders = [
                    {"id": "sell_1", "side": "sell", "price": 101.0, "amount": 5.0}
                ]

                valid = bot._validate_sell_orders(SYMBOL, [])

                self.assertTrue(valid)
                self.assertEqual([order["id"] for order in state.sell_ladder_orders], ["sell_1"])
                self.assertFalse(state.frozen_no_more_buys)
                self.assertEqual(bot.exchange.canceled_orders, [])
                bot._ensure_sell_ladder(SYMBOL)
                self.assertEqual(bot.exchange.created_orders, [])

    def test_tracked_exit_order_id_rotation_adopts_visible_reduce_only_exit(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0
                state.sell_ladder_signature = bot._sell_ladder_signature("normal")
                state.sell_ladder_orders = [
                    {"id": "old_sell", "side": "sell", "price": 101.0, "amount": 5.0}
                ]

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "rotated_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 5.0,
                            "remaining": 5.0,
                            "reduceOnly": True,
                        }
                    ],
                )

                self.assertTrue(valid)
                self.assertEqual([order["id"] for order in state.sell_ladder_orders], ["rotated_sell"])
                self.assertEqual(state.sell_ladder_signature, bot._sell_ladder_signature("normal"))
                self.assertEqual(bot.exchange.canceled_orders, [])

    def test_exit_ladder_rebuild_ignores_stale_frozen_state_without_tracked_orders(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0

                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(len(bot.exchange.created_orders), 3)
                self.assertEqual([order["side"] for order in bot.exchange.created_orders], ["sell", "sell", "sell"])
                self.assertEqual(sum(order["amount"] for order in bot.exchange.created_orders), 4.0)
                self.assertTrue(all(order["params"].get("reduceOnly") for order in bot.exchange.created_orders))
                self.assertEqual(len(state.sell_ladder_orders), 3)
                self.assertEqual(state.exit_runner_contracts, 1.0)

    def test_exit_ladder_waits_when_exchange_reports_closeable_reserved(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.reject_reduce_only_closeable_amount = True
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0

                bot._ensure_sell_ladder(SYMBOL)
                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 1)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("normal"),
                )

    def test_breakeven_waits_on_pending_closeable_without_retrying_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.reject_reduce_only_closeable_amount = True
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0
                state.sell_ladder_mode = "breakeven"
                state.frozen_no_more_buys = True

                self.assertTrue(bot._maybe_apply_time_based_exit(SYMBOL, None))
                self.assertTrue(bot._maybe_apply_time_based_exit(SYMBOL, None))

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 1)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("breakeven"),
                )

    def test_unknown_non_reduce_only_exit_order_blocks_duplicate_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "manual_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 3.0,
                            "remaining": 3.0,
                            "reduceOnly": False,
                        }
                    ],
                )

                self.assertFalse(valid)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(bot.exchange.canceled_orders, [])

    def test_unknown_exit_orders_cancel_tracked_ladder_when_combined_amount_exceeds_position(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [{"id": "sell_1", "side": "sell", "price": 101.0, "amount": 4.0}]

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "sell_1",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 4.0,
                            "remaining": 4.0,
                            "reduceOnly": True,
                        },
                        {
                            "id": "manual_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 102.0,
                            "amount": 2.0,
                            "remaining": 2.0,
                            "reduceOnly": True,
                        },
                    ],
                )

                self.assertFalse(valid)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertIn(("sell_1", SYMBOL, {"marginMode": config.RISK.margin_mode}), bot.exchange.canceled_orders)

    def test_private_snapshots_are_bulk_cached_per_cycle(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 5.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": 3,
                    }
                ]
                bot.exchange.open_orders = [
                    {"id": "sell_1", "symbol": SYMBOL, "side": "sell", "price": 101.0, "amount": 5.0}
                ]

                bot._reset_private_caches()
                first_snapshot = bot._fetch_position_snapshot(SYMBOL)
                second_snapshot = bot._fetch_position_snapshot(SYMBOL)
                first_orders = bot._fetch_open_orders(SYMBOL)
                second_orders = bot._fetch_open_orders(SYMBOL)

                self.assertEqual(first_snapshot["long_size"], 5.0)
                self.assertEqual(second_snapshot["long_size"], 5.0)
                self.assertEqual(first_orders, second_orders)
                self.assertEqual(bot.exchange.fetch_positions_calls, 1)
                self.assertEqual(bot.exchange.fetch_open_orders_calls, 1)

    def test_private_position_fetch_retries_transient_network_failure(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False), EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_positions_failures = [ccxt.RequestTimeout("timeout")]
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 5.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": 3,
                    }
                ]

                bot._reset_private_caches()
                snapshot = bot._fetch_position_snapshot(SYMBOL)

                self.assertTrue(snapshot["ok"])
                self.assertEqual(snapshot["long_size"], 5.0)
                self.assertEqual(bot.exchange.fetch_positions_calls, 2)
                self.assertEqual(bot.exchange.urls["hostnames"]["contract"], "api.two.test")

    def test_private_position_fetch_honors_configured_retry_count(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=4,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False), EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_positions_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                    ccxt.RequestTimeout("timeout-3"),
                ]
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 5.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": 3,
                    }
                ]

                bot._reset_private_caches()
                snapshot = bot._fetch_position_snapshot(SYMBOL)

                self.assertTrue(snapshot["ok"])
                self.assertEqual(snapshot["long_size"], 5.0)
                self.assertEqual(bot.exchange.fetch_positions_calls, 4)
                self.assertEqual(bot.exchange.urls["hostnames"]["contract"], "api.two.test")

    def test_exhausted_private_network_fetch_logs_warning_not_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False), EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.has["fetchPositions"] = False
                bot.exchange.fetch_positions_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                ]

                bot._reset_private_caches()
                snapshot = bot._fetch_position_snapshot(SYMBOL)

                self.assertFalse(snapshot["ok"])
                self.assertEqual(bot.exchange.fetch_positions_calls, 2)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "WARNING")
                self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
                self.assertEqual(rows[-1]["reason"], "position_fetch_failed")

    def test_exhausted_open_orders_network_fetch_logs_warning_not_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False), EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.has["fetchOpenOrders"] = False
                bot.exchange.fetch_open_orders_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                ]

                bot._reset_private_caches()
                orders = bot._fetch_open_orders(SYMBOL)

                self.assertIsNone(orders)
                self.assertEqual(bot.exchange.fetch_open_orders_calls, 2)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "WARNING")
                self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
                self.assertEqual(rows[-1]["reason"], "open_orders_fetch_failed")

    def test_position_mode_locked_by_existing_positions_logs_info(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.set_position_mode_error = RuntimeError(
                    'htx {"status":"error","err_code":1494,'
                    '"err_msg":"Position mode cannot be adjusted for existing positions."}'
                )

                self.assertTrue(bot._ensure_one_way_position_mode(force=True))
                self.assertTrue(bot.one_way_mode_checked)
                self.assertEqual(len(bot.exchange.set_position_mode_calls), 1)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "INFO")
                self.assertEqual(rows[-1]["event"], "futures_setup")
                self.assertEqual(rows[-1]["reason"], "position_mode_existing_positions")

    def test_step_network_exception_logs_warning_not_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            bot._log_step_exception(SYMBOL, ccxt.RequestTimeout("timeout"))

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["level"], "WARNING")
            self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
            self.assertEqual(rows[-1]["reason"], "step_network_error")

    def test_step_non_network_exception_stays_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            bot._log_step_exception(SYMBOL, RuntimeError("logic failed"))

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["level"], "ERROR")
            self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
            self.assertEqual(rows[-1]["reason"], "step_error")

    def test_bulk_private_snapshots_fall_back_when_some_payload_symbols_are_missing(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 2.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": 3,
                    },
                    {
                        "side": "long",
                        "contracts": 3.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": 3,
                    },
                ]
                bot.exchange.open_orders = [
                    {"id": "with_symbol", "symbol": SYMBOL, "side": "sell", "price": 101.0, "amount": 2.0},
                    {"id": "without_symbol", "side": "sell", "price": 102.0, "amount": 3.0},
                ]

                bot._reset_private_caches()
                snapshot = bot._fetch_position_snapshot(SYMBOL)
                orders = bot._fetch_open_orders(SYMBOL)

                self.assertEqual(snapshot["long_size"], 5.0)
                self.assertEqual({order["id"] for order in orders}, {"with_symbol", "without_symbol"})
                self.assertEqual(bot.exchange.fetch_positions_calls, 2)
                self.assertEqual(bot.exchange.fetch_open_orders_calls, 2)

    def test_enabled_profile_names_rejects_unknown_env_profile(self):
        previous = os.environ.get("BOT_PROFILES")
        os.environ["BOT_PROFILES"] = "long,typo"
        try:
            with self.assertRaisesRegex(KeyError, "typo"):
                config.enabled_profile_names()
        finally:
            if previous is None:
                os.environ.pop("BOT_PROFILES", None)
            else:
                os.environ["BOT_PROFILES"] = previous

    def test_combined_run_once_rechecks_disabled_symbols(self):
        class FakeBot:
            def __init__(self):
                self.profile = config.resolve_profile("long")
                self.symbols = [SYMBOL]
                self.disabled_symbols = {SYMBOL}
                self.states = {}
                self.calls = []

            def _update_signal_cache_if_needed(self):
                pass

            def step_symbol(self, symbol):
                self.calls.append(symbol)

            def _save_state(self):
                pass

        combined = object.__new__(CombinedHtxFuturesBot)
        fake_bot = FakeBot()
        combined.bots = [fake_bot]

        CombinedHtxFuturesBot.run_once(combined)

        self.assertEqual(fake_bot.calls, [SYMBOL])

    def test_reserved_opposite_position_does_not_disable_combined_profile(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            with override_config(RUNTIME=replace(config.RUNTIME, dry_run=False)):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_reserved_symbols = {SYMBOL}
                state = bot._get_state(SYMBOL)
                state.entry_orders = [{"id": "short_entry", "side": "sell", "price": 10.1, "amount": 1.0}]

                status = bot._sync_state_with_position(
                    SYMBOL,
                    {
                        "short_size": 0.0,
                        "short_available": 0.0,
                        "long_size": 5.0,
                        "long_available": 5.0,
                        "long_entry_price": 10.0,
                    },
                    open_orders=[],
                )

                self.assertEqual(status, "reserved")
                self.assertNotIn(SYMBOL, bot.disabled_symbols)
                self.assertEqual(state.entry_orders, [])
                self.assertIn(("short_entry", SYMBOL, {"marginMode": config.RISK.margin_mode}), bot.exchange.canceled_orders)

    def test_combined_rejects_mixed_dry_run_modes(self):
        long_profile = replace(
            config.resolve_profile("long"),
            runtime=replace(config.resolve_profile("long").runtime, dry_run=False),
        )
        short_profile = replace(
            config.resolve_profile("short"),
            runtime=replace(config.resolve_profile("short").runtime, dry_run=True),
        )
        combined = object.__new__(CombinedHtxFuturesBot)
        combined.profiles = [long_profile, short_profile]

        with self.assertRaisesRegex(RuntimeError, "DRY_RUN"):
            CombinedHtxFuturesBot._validate_shared_exchange_profiles(combined)

    def test_combined_uses_separate_external_feeds_for_different_profile_settings(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)

            def isolated_profile(name, timeout):
                profile = config.resolve_profile(name)
                return replace(
                    profile,
                    runtime=replace(
                        profile.runtime,
                        state_file=str(tmp_path / f"{name}_state.json"),
                        markets_cache_file=str(tmp_path / f"{name}_markets.json"),
                    ),
                    monitoring=replace(
                        profile.monitoring,
                        csv_log_file=str(tmp_path / f"{name}_trades.csv"),
                        cycle_stats_csv_file=str(tmp_path / f"{name}_cycles.csv"),
                        macro_csv_file=str(tmp_path / f"{name}_macro.csv"),
                        external_price_csv_file=str(tmp_path / f"{name}_external.csv"),
                        csv_archive_dir=str(tmp_path / f"{name}_archive"),
                    ),
                    external_price_feed=replace(profile.external_price_feed, rest_timeout_sec=timeout),
                )

            combined = CombinedHtxFuturesBot(
                profiles=(
                    isolated_profile("long", 1.0),
                    isolated_profile("short", 9.0),
                )
            )

            self.assertIsNot(combined.bots[0].external_price_feed, combined.bots[1].external_price_feed)
            self.assertEqual(combined.bots[0].external_price_feed.settings.rest_timeout_sec, 1.0)
            self.assertEqual(combined.bots[1].external_price_feed.settings.rest_timeout_sec, 9.0)

    def test_short_profile_excludes_filtered_illiquid_and_unstable_coins(self):
        short_coins = {coin.lower() for coin in config.resolve_profile("short").coins}

        self.assertIn("doge", short_coins)
        self.assertIn("fil", short_coins)
        self.assertNotIn("fartcoin", short_coins)
        self.assertNotIn("space", short_coins)
        self.assertNotIn("enj", short_coins)


if __name__ == "__main__":
    unittest.main()
