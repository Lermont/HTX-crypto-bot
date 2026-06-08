# -*- coding: utf-8 -*-

import csv
import concurrent.futures
import importlib
import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import config
import ccxt
from htxbot.app import HtxFuturesBot
from htxbot.combined import CombinedHtxFuturesBot
from htxbot.exchange import UnexpectedExchangeResponse
from unittest.mock import patch
from htxbot.external_price import BookTicker, ExternalPriceFeed
from htxbot.indicators import (
    average_true_range,
    calculate_rsi,
    compute_log_return,
    realized_volatility,
)
from htxbot.models import (
    OrderRequest,
    PositionLifecycle,
    SellLadderParams,
    SignalContext,
)
from htxbot.shared_exchange import (
    CachedMarketDataExchange,
    MultiAccountExchange,
    ThreadSafeExchange,
)
from tests.config_overrides import override_config


SYMBOL = "TEST/USDT:USDT"
SECOND_SYMBOL = "ALT2/USDT:USDT"
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
SECOND_MARKET = {
    **MARKET,
    "symbol": SECOND_SYMBOL,
    "id": "ALT2-USDT",
    "base": "ALT2",
}
XAUT_MARKET = {
    **MARKET,
    "symbol": XAUT_SYMBOL,
    "id": "XAUT-USDT",
    "base": "XAUT",
}


def ohlcv_series(
    closes, timeframe_sec=60, start_ts=1_700_000_000_000, volumes=None, range_width=0.0
):
    volumes = list(volumes) if volumes is not None else None
    return [
        [
            start_ts + index * timeframe_sec * 1000,
            close,
            close + range_width,
            max(0.00000001, close - range_width),
            close,
            float(volumes[index]) if volumes is not None else 1.0,
        ]
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


class PerSymbolExternalPriceFeed:
    def __init__(self, contexts):
        self.contexts = {symbol: dict(context) for symbol, context in contexts.items()}
        self.calls = []

    def get_context(self, symbol, htx_ticker, market=None):
        self.calls.append((symbol, dict(htx_ticker), market))
        context = dict(self.contexts.get(symbol, {}))
        context.setdefault("valid", True)
        context.setdefault("stale", False)
        context.setdefault("reason", "ok")
        context["symbol"] = symbol
        context.setdefault(
            "mexc_symbol", symbol.split("/", 1)[0].replace(":", "") + "USDT"
        )
        context.setdefault("ts", time.time())
        return context


class FakeMexcClient:
    def __init__(self, books):
        self.books = list(books)
        self.calls = []

    def fetch(self, symbol):
        self.calls.append(symbol)
        if not self.books:
            raise RuntimeError("no fake books left")
        return self.books.pop(0)


class FakeExchange:
    def __init__(self, name="primary"):
        self.name = name
        self.markets = {
            SYMBOL: MARKET,
            SECOND_SYMBOL: SECOND_MARKET,
            BTC_SYMBOL: BTC_MARKET,
        }
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
        self.fetch_open_orders_response_override = None
        self.fetch_positions_response_override = None
        self.fetch_ohlcv_response_override = None
        self.fetch_open_orders_type_error_on_params = False
        self.fetch_open_orders_failures = []
        self.fetch_positions_failures = []
        self.fetch_ohlcv_failures = []
        self.fetch_order_failures = []
        self.fetch_open_orders_calls = 0
        self.fetch_positions_calls = 0
        self.fetch_order_calls = []
        self.fetch_positions_delay = 0.0
        self.fetch_funding_rate_calls = 0
        self.funding_rate_response = {"fundingRate": "0"}
        self.fetch_order_responses = {}
        self.fetch_my_trades_responses = {}
        self.fetch_my_trades_calls = []
        self.ohlcv = {}
        self.ohlcv_calls = []
        self.reject_leverage_above = None
        self.reject_leverage_not_equal = None
        self.account_leverage = 50
        self.account_position_mode = "single_side"
        self.reject_reduce_only_closeable_amount = False
        self.reject_stop_loss_trigger_crossed = False
        self.create_order_failures = []
        self.create_order_calls = 0
        self.set_leverage_calls = []
        self.set_leverage_error = None
        self.set_leverage_errors_by_symbol = {}
        self.set_position_mode_calls = []
        self.set_position_mode_error = None
        self.ticker = {"bid": 9.9, "ask": 10.1, "last": 10.0}
        self.tickers = {}
        self.fetch_ticker_calls = 0
        self.ticker_calls = []
        self.fetch_ticker_delay = 0.0
        self.order_book = {"bids": [[9.99, 100.0]], "asks": [[10.01, 100.0]]}
        self.order_books = {}
        self.fetch_order_book_calls = 0
        self.order_book_calls = []
        self.fetch_order_book_delay = 0.0
        self.fetch_order_book_failures = []
        self.balance_free = 1000.0
        self.balance_total = 1000.0
        self.fetch_balance_calls = 0
        self.fetch_balance_delay = 0.0

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
        self.fetch_ticker_calls += 1
        self.ticker_calls.append(symbol)
        if self.fetch_ticker_delay > 0:
            time.sleep(self.fetch_ticker_delay)
        ticker = self.tickers.get(symbol, self.ticker)
        return dict(ticker)

    def fetch_order_book(self, symbol, limit=None):
        self.fetch_order_book_calls += 1
        self.order_book_calls.append(symbol)
        if self.fetch_order_book_delay > 0:
            time.sleep(self.fetch_order_book_delay)
        if self.fetch_order_book_failures:
            raise self.fetch_order_book_failures.pop(0)
        book = self.order_books.get(symbol, self.order_book)
        return {
            "bids": [list(item) for item in book.get("bids", [])],
            "asks": [list(item) for item in book.get("asks", [])],
        }

    def fetch_balance(self, params=None):
        self.fetch_balance_calls += 1
        if self.fetch_balance_delay > 0:
            time.sleep(self.fetch_balance_delay)
        quote = config.EXCHANGE.quote_currency
        return {
            "free": {quote: self.balance_free},
            "total": {quote: self.balance_total},
            "info": {
                "data": [
                    {
                        "margin_asset": quote,
                        "margin_mode": config.RISK.margin_mode,
                        "margin_available": self.balance_free,
                        "margin_balance": self.balance_total,
                    }
                ]
            },
        }

    def cancel_order(self, order_id, symbol, params=None):
        if str(order_id) in self.cancel_fail_ids:
            raise RuntimeError("cancel failed")
        self.canceled_orders.append((str(order_id), symbol, params or {}))

    def fetch_open_orders(self, symbol=None, params=None):
        self.fetch_open_orders_calls += 1
        if params is not None and self.fetch_open_orders_type_error_on_params:
            raise TypeError(
                "fetch_open_orders() got an unexpected keyword argument 'params'"
            )
        if self.fetch_open_orders_failures:
            raise self.fetch_open_orders_failures.pop(0)
        if self.fetch_open_orders_response_override is not None:
            return self.fetch_open_orders_response_override
        if symbol is None:
            return list(self.open_orders)
        return [
            order for order in self.open_orders if order.get("symbol", SYMBOL) == symbol
        ]

    def fetch_positions(self, symbols=None, params=None):
        self.fetch_positions_calls += 1
        if self.fetch_positions_delay > 0:
            time.sleep(self.fetch_positions_delay)
        if self.fetch_positions_failures:
            raise self.fetch_positions_failures.pop(0)
        if self.fetch_positions_response_override is not None:
            return self.fetch_positions_response_override
        wanted = set(symbols or [])
        if not wanted:
            return list(self.positions)
        return [
            position
            for position in self.positions
            if position.get("symbol", SYMBOL) in wanted
        ]

    def fetch_order(self, order_id, symbol=None, params=None):
        self.fetch_order_calls.append((str(order_id), symbol, params or {}))
        if self.fetch_order_failures:
            raise self.fetch_order_failures.pop(0)
        if str(order_id) in self.fetch_order_responses:
            return dict(self.fetch_order_responses[str(order_id)])
        for order in self.created_orders:
            if str(order.get("id")) == str(order_id):
                return dict(order)
        raise ccxt.OrderNotFound(str(order_id))

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self.fetch_my_trades_calls.append((symbol, since, limit, params or {}))
        rows = self.fetch_my_trades_responses.get(symbol, [])
        return [dict(row) for row in rows]

    def fetch_funding_rate(self, symbol, params=None):
        self.fetch_funding_rate_calls += 1
        if isinstance(self.funding_rate_response, Exception):
            raise self.funding_rate_response
        return dict(self.funding_rate_response)

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
        if self.fetch_ohlcv_failures:
            raise self.fetch_ohlcv_failures.pop(0)
        if self.fetch_ohlcv_response_override is not None:
            return self.fetch_ohlcv_response_override
        rows = list(self.ohlcv.get((symbol, timeframe), []))
        if limit:
            return rows[-int(limit) :]
        return rows

    def create_order(self, symbol, type, side, amount, price, params=None):
        params = params or {}
        self.create_order_calls += 1
        if self.create_order_failures:
            raise self.create_order_failures.pop(0)
        if self.reject_leverage_not_equal is not None:
            leverage = float(params.get("leverRate") or 0.0)
            if leverage != float(self.reject_leverage_not_equal):
                raise RuntimeError(
                    'htx {"status":"error","err_code":1045,'
                    '"err_msg":"Unable to change leverage due to open orders."}'
                )
        if self.reject_stop_loss_trigger_crossed and "stopLossPrice" in params:
            raise RuntimeError(
                'htx {"status":"error","err_code":1407,'
                '"err_msg":"The stop-loss price shall not be greater than or equal to current price."}'
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

    def set_leverage(self, leverage, symbol=None, params=None):
        self.set_leverage_calls.append((leverage, symbol, params or {}))
        if symbol in self.set_leverage_errors_by_symbol:
            raise self.set_leverage_errors_by_symbol[symbol]
        if self.set_leverage_error is not None:
            raise self.set_leverage_error
        self.account_leverage = leverage
        return {"status": "ok"}

    def contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo(self, request):
        data = {
            "positions": [],
            "contract_detail": [
                {
                    "contract_code": request.get("contract_code", "TEST-USDT"),
                    "lever_rate": str(self.account_leverage),
                }
            ],
        }
        if self.account_position_mode:
            data["position_mode"] = self.account_position_mode
        return {
            "status": "ok",
            "data": data,
        }


class UnifiedBotTests(unittest.TestCase):
    def test_bot_modules_import_without_side_effects(self):
        modules = (
            "bot",
            "config",
            "htxbot.app",
            "htxbot.combined",
            "htxbot.exchange",
            "htxbot.monitoring",
            "htxbot.runner",
            "htxbot.signal_engine",
            "htxbot.state",
            "htxbot.strategy",
            "htxbot.strategy_entry",
            "htxbot.strategy_exit",
            "htxbot.strategy_filters",
            "htxbot.strategy_risk",
            "htxbot.models",
        )

        for module in modules:
            with self.subTest(module=module):
                importlib.import_module(module)

    def test_apply_configured_leverage_on_start_optimizations(self):
        from htxbot.app import HtxFuturesBot
        from unittest.mock import MagicMock
        from dataclasses import replace

        bot = HtxFuturesBot(
            profile="long", exchange=MagicMock(), external_price_feed=MagicMock()
        )
        bot.symbols = ["BTC-USDT", "ETH-USDT", "LTC-USDT", "XRP-USDT", "ADA-USDT"]
        bot.exchange.set_leverage = MagicMock()
        bot.exchange.set_leverage.side_effect = lambda leverage, symbol, params: None

        # set 2 max workers
        bot.profile = replace(
            bot.profile, runtime=replace(bot.profile.runtime, market_data_max_workers=2)
        )

        self.assertTrue(bot._apply_configured_leverage_on_start())
        self.assertEqual(bot.exchange.set_leverage.call_count, 5)

        def failing_leverage(leverage, symbol, params):
            if symbol in ["ETH-USDT", "XRP-USDT"]:
                raise Exception("Network error")
            return None

        bot.exchange.set_leverage.side_effect = failing_leverage
        bot.exchange.set_leverage.call_count = 0

        bot._log_event = MagicMock()
        self.assertFalse(bot._apply_configured_leverage_on_start())

        # It might be called multiple times due to the individual failures.
        # Check the last call which should be the summary warning.
        args, kwargs = bot._log_event.call_args
        self.assertEqual(kwargs.get("reason"), "set_leverage_partial_failure")
        self.assertEqual(
            kwargs.get("diagnostic_context").get("failed_symbols"),
            ["ETH-USDT", "XRP-USDT"],
        )

    def test_strategy_components_are_separately_testable_mixins(self):
        from htxbot.strategy import StrategyMixin
        from htxbot.strategy_entry import EntryStrategy
        from htxbot.strategy_exit import ExitStrategy
        from htxbot.strategy_filters import SignalFilters
        from htxbot.strategy_risk import RiskManager

        self.assertTrue(issubclass(StrategyMixin, EntryStrategy))
        self.assertTrue(issubclass(StrategyMixin, ExitStrategy))
        self.assertTrue(issubclass(StrategyMixin, RiskManager))
        self.assertTrue(issubclass(StrategyMixin, SignalFilters))
        self.assertIn("_maybe_place_initial_buy", EntryStrategy.__dict__)
        self.assertIn("_ensure_sell_ladder", ExitStrategy.__dict__)
        self.assertIn("_risk_budget", RiskManager.__dict__)
        self.assertIn("_entry_gate_block_reason", SignalFilters.__dict__)

    def test_bot_init_wraps_injected_exchange_with_thread_safe_proxy(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            tmp_path = Path(raw_tmp)
            runtime = replace(
                config.RUNTIME,
                state_file=str(tmp_path / "state.json"),
                markets_cache_file=str(tmp_path / "markets.json"),
            )
            monitoring = replace(
                config.MONITORING,
                cycle_stats_csv_file=str(tmp_path / "cycles.csv"),
                csv_log_file=str(tmp_path / "trades.csv"),
                macro_csv_file=str(tmp_path / "macro.csv"),
                external_price_csv_file=str(tmp_path / "external_price.csv"),
                account_pnl_csv_file=str(tmp_path / "account_pnl.csv"),
                signal_analytics_csv_file=str(tmp_path / "signal_analytics.csv"),
                signal_analytics_jsonl_file=str(tmp_path / "signal_analytics.jsonl"),
                diagnostics_csv_file=str(tmp_path / "diagnostics.csv"),
                diagnostics_jsonl_file=str(tmp_path / "diagnostics.jsonl"),
                csv_archive_dir=str(tmp_path / "archive"),
            )
            raw_exchange = FakeExchange()

            with override_config(RUNTIME=runtime, MONITORING=monitoring):
                bot = HtxFuturesBot(
                    profile="long",
                    exchange=raw_exchange,
                    external_price_feed=StaticExternalPriceFeed(
                        self.external_context()
                    ),
                )

            self.assertIs(bot.exchange.unsafe_exchange(), raw_exchange)
            self.assertIsNotNone(bot.exchange.thread_safe_lock())

    def test_compute_log_return_cases(self):
        # price_now <= 0
        self.assertEqual(compute_log_return(0.0, 100.0), 0.0)
        self.assertEqual(compute_log_return(-5.0, 100.0), 0.0)

        # price_then <= 0
        self.assertEqual(compute_log_return(100.0, 0.0), 0.0)
        self.assertEqual(compute_log_return(100.0, -5.0), 0.0)

        # Equal positive prices
        self.assertEqual(compute_log_return(100.0, 100.0), 0.0)

        # Normal positive prices
        self.assertAlmostEqual(
            compute_log_return(110.5, 100.0), math.log(110.5 / 100.0)
        )
        self.assertAlmostEqual(compute_log_return(90.5, 100.0), math.log(90.5 / 100.0))

    def test_runtime_diagnostics_artifacts_are_not_git_tracked(self):
        repo_root = Path(__file__).resolve().parents[1]
        if not (repo_root / ".git").exists():
            self.skipTest("git metadata unavailable")

        result = subprocess.run(
            ["git", "ls-files", "long", "short"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        tracked_paths = set(result.stdout.splitlines())
        runtime_artifacts = {
            "long/bot_futures_macro.csv",
            "long/diagnostics.csv",
            "long/diagnostics.jsonl",
            "long/signal_analytics.csv",
            "long/signal_analytics.jsonl",
            "short/bot_futures_macro.csv",
            "short/diagnostics.csv",
            "short/diagnostics.jsonl",
            "short/signal_analytics.csv",
            "short/signal_analytics.jsonl",
        }

        self.assertTrue(runtime_artifacts.isdisjoint(tracked_paths))

    def make_bot(self, tmp_path: Path) -> HtxFuturesBot:
        instance = object.__new__(HtxFuturesBot)
        logger = logging.getLogger(f"test_unified_bot_{id(instance)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        instance.profile = config.current_profile()
        instance.profile_name = config.BOT_NAME
        instance.log = logger
        instance.exchange = ThreadSafeExchange(FakeExchange())
        instance.state_path = tmp_path / "state.json"
        instance.lock_path = tmp_path / "state.lock"
        instance.markets_cache_path = tmp_path / "markets.json"
        instance.csv_path = tmp_path / "trades.csv"
        instance.cycle_stats_path = tmp_path / "cycles.csv"
        instance.macro_csv_path = tmp_path / "macro.csv"
        instance.external_price_csv_path = tmp_path / "external_price.csv"
        instance.account_pnl_csv_path = tmp_path / "account_pnl.csv"
        instance.signal_analytics_csv_path = tmp_path / "signal_analytics.csv"
        instance.signal_analytics_jsonl_path = tmp_path / "signal_analytics.jsonl"
        instance.diagnostics_csv_path = tmp_path / "diagnostics.csv"
        instance.diagnostics_jsonl_path = tmp_path / "diagnostics.jsonl"
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
        instance._account_pnl_lock = threading.RLock()
        instance._funding_cache_lock = threading.RLock()
        instance._private_cache_lock = threading.RLock()
        instance.signal_cache = {
            "benchmark_ok": True,
            "macro": {
                "gold_btc_rsi": {
                    "ok": False,
                    "ts": int(time.time()),
                    "regime": "macro_unavailable",
                    "reason": "test_neutral",
                    "gold_return": 0.0,
                    "btc_return": 0.0,
                    "macro_direction_score": 0.0,
                    "long_budget_multiplier": 1.0,
                    "short_budget_multiplier": 1.0,
                    "directional_long_multiplier": 1.0,
                    "directional_short_multiplier": 1.0,
                    "ladder_multiplier": 1.0,
                    "disable_new_entries": False,
                    "disable_averaging": False,
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
        instance._ensure_account_pnl_csv_file()
        instance.account_pnl_runtime = {"history": [], "last_sample_at": 0.0}
        instance.account_pnl_bots = [instance]
        instance._ensure_signal_analytics_files()
        instance._ensure_diagnostics_files()
        return instance

    @contextmanager
    def guard_path_against_unbounded_reads(
        self, target_path: Path, max_read_size: int = 1024 * 1024
    ):
        target_path = target_path.resolve()
        real_open = Path.open
        read_sizes = []

        class GuardedReader:
            def __init__(self, handle):
                self._handle = handle

            def _record_bounded_read(self, text):
                size = len(text or "")
                if size <= 0:
                    return
                if size > max_read_size:
                    raise AssertionError("CSV migration read chunk is too large")
                read_sizes.append(size)

            def __enter__(self):
                self._handle.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                return self._handle.__exit__(exc_type, exc, tb)

            def __iter__(self):
                return self

            def __next__(self):
                line = next(self._handle)
                self._record_bounded_read(line)
                return line

            def read(self, size=-1):
                if size is None or int(size) < 0:
                    raise AssertionError("CSV migration attempted an unbounded read")
                if int(size) > max_read_size:
                    raise AssertionError("CSV migration read chunk is too large")
                chunk = self._handle.read(size)
                self._record_bounded_read(chunk)
                return chunk

            def readline(self, size=-1):
                line = self._handle.readline(size)
                self._record_bounded_read(line)
                return line

            def readlines(self, hint=-1):
                raise AssertionError("CSV migration attempted to materialize all lines")

            def __getattr__(self, name):
                return getattr(self._handle, name)

        def guarded_open(path_self, *args, **kwargs):
            mode = str(args[0] if args else kwargs.get("mode", "r"))
            handle = real_open(path_self, *args, **kwargs)
            if "r" in mode and Path(path_self).resolve() == target_path:
                return GuardedReader(handle)
            return handle

        with patch("pathlib.Path.open", new=guarded_open):
            yield read_sizes

    def ema_test_strategy(self, **overrides):
        defaults = {
            "ema_macro_timeframe": "1m",
            "ema_pullback_timeframe": "1m",
            "ema_trigger_timeframe": "1m",
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
            "gold_return": 0.0,
            "btc_return": 0.0,
            "macro_direction_score": 0.0,
            "long_budget_multiplier": 1.0,
            "short_budget_multiplier": 1.0,
            "directional_long_multiplier": 1.0,
            "directional_short_multiplier": 1.0,
            "ladder_multiplier": 1.0,
            "disable_new_entries": False,
            "disable_averaging": False,
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
            "mexc_bid_qty": 10.0,
            "mexc_ask_qty": 10.0,
            "mexc_bid_notional": 999.0,
            "mexc_ask_notional": 1001.0,
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

    def test_log_event_survives_csv_append_failure(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            bot = self.make_bot(Path(raw_tmp))
            bot.csv_path = Path(raw_tmp) / "csv_path_is_directory"
            bot.csv_path.mkdir()

            bot._log_event(
                "INFO", "test message", event="test_event", symbol=SYMBOL, reason="test"
            )

            self.assertTrue(getattr(bot, "_csv_log_failed_once", False))

    def test_jsonl_append_permission_error_does_not_raise(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            bot = self.make_bot(Path(raw_tmp))
            jsonl_path = Path(raw_tmp) / "locked.jsonl"
            original_open = Path.open

            def locked_append(path_self, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                if path_self == jsonl_path and "a" in mode:
                    raise PermissionError("locked")
                return original_open(path_self, *args, **kwargs)

            with patch.object(Path, "open", locked_append):
                bot._append_jsonl(jsonl_path, {"event": "test"})

            self.assertFalse(jsonl_path.exists())

    def test_signal_analytics_monitoring_failures_do_not_raise_step_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.signal_analytics_csv_path = (
                Path(raw_tmp) / "signal_analytics_csv_locked"
            )
            bot.signal_analytics_csv_path.mkdir()

            with patch.object(
                bot,
                "_rotate_jsonl_if_needed",
                side_effect=PermissionError("jsonl locked"),
            ):
                bot._record_signal_analytics(
                    "signal_built",
                    symbol=SYMBOL,
                    signal=self.entry_signal(),
                    context={"note": "monitoring failure must not stop trading"},
                )

            failures = getattr(bot, "_monitoring_write_failures", set())
            self.assertTrue(any("signal_analytics" in item[0] for item in failures))

    def test_trade_csv_concurrent_writes_rotate_without_lost_rows(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            monitoring = replace(
                config.MONITORING, csv_rotate_max_bytes=450, csv_archive_dir="archive"
            )
            with override_config(MONITORING=monitoring):
                tmp_path = Path(raw_tmp)
                bot = self.make_bot(tmp_path)

                def write_event(index):
                    bot._log_event(
                        "INFO",
                        f"concurrent csv event {index}",
                        event="concurrent_csv_event",
                        symbol=SYMBOL,
                        reason=f"row_{index}",
                    )

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(write_event, range(40)))

                csv_paths = [bot.csv_path]
                csv_paths.extend(sorted((tmp_path / "archive").glob("trades.*.csv")))
                rows = []
                for csv_path in csv_paths:
                    if not csv_path.exists():
                        continue
                    with csv_path.open(newline="", encoding="utf-8") as handle:
                        rows.extend(
                            row
                            for row in csv.DictReader(handle)
                            if row.get("event") == "concurrent_csv_event"
                        )

                self.assertEqual(len(rows), 40)
                self.assertEqual(
                    {row["reason"] for row in rows},
                    {f"row_{index}" for index in range(40)},
                )

    def test_live_state_save_is_atomic_and_loadable(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 2.0
                state.entry_price = 10.0

                bot._save_state()

                payload = json.loads(bot.state_path.read_text(encoding="utf-8"))
                self.assertEqual(payload[SYMBOL]["position_size"], 2.0)
                self.assertEqual(list(bot.state_path.parent.glob("*.tmp")), [])

    def test_state_save_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 2.0
            state.entry_price = 10.0
            real_replace = os.replace
            calls = {"count": 0}

            def flaky_replace(src, dst):
                calls["count"] += 1
                if calls["count"] < 3:
                    raise PermissionError("temporary lock")
                return real_replace(src, dst)

            with (
                patch("htxbot.state.os.replace", side_effect=flaky_replace),
                patch("htxbot.fileio.time.sleep") as sleep_mock,
            ):
                bot._save_state()

            payload = json.loads(bot.state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload[SYMBOL]["position_size"], 2.0)
            self.assertEqual(calls["count"], 3)
            self.assertEqual(
                [call.args[0] for call in sleep_mock.call_args_list], [0.1, 0.2]
            )
            self.assertEqual(list(bot.state_path.parent.glob("*.tmp")), [])

    def test_state_save_retries_windows_file_lock_oserror(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 3.0
            state.entry_price = 11.0
            real_replace = os.replace
            calls = {"count": 0}

            def flaky_replace(src, dst):
                calls["count"] += 1
                if calls["count"] < 2:
                    exc = OSError("temporary Windows file lock")
                    exc.winerror = 5
                    raise exc
                return real_replace(src, dst)

            with (
                patch("htxbot.state.os.replace", side_effect=flaky_replace),
                patch("htxbot.fileio.time.sleep") as sleep_mock,
            ):
                bot._save_state()

            payload = json.loads(bot.state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload[SYMBOL]["position_size"], 3.0)
            self.assertEqual(calls["count"], 2)
            self.assertEqual(
                [call.args[0] for call in sleep_mock.call_args_list], [0.1]
            )
            self.assertEqual(list(bot.state_path.parent.glob("*.tmp")), [])

    def test_state_save_retries_transient_write_permission_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 4.0
            state.entry_price = 12.0
            real_write_text = Path.write_text
            calls = {"count": 0}

            def flaky_write_text(path_self, data, *args, **kwargs):
                if Path(path_self).suffix == ".tmp":
                    calls["count"] += 1
                    if calls["count"] < 3:
                        raise PermissionError("temporary write lock")
                return real_write_text(path_self, data, *args, **kwargs)

            with (
                patch(
                    "pathlib.Path.write_text",
                    autospec=True,
                    side_effect=flaky_write_text,
                ),
                patch("htxbot.fileio.time.sleep") as sleep_mock,
            ):
                bot._save_state()

            payload = json.loads(bot.state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload[SYMBOL]["position_size"], 4.0)
            self.assertEqual(calls["count"], 3)
            self.assertEqual(
                [call.args[0] for call in sleep_mock.call_args_list], [0.1, 0.2]
            )
            self.assertEqual(list(bot.state_path.parent.glob("*.tmp")), [])

    def test_monitoring_replace_retries_windows_file_lock_oserror(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            src = Path(raw_tmp) / "new.csv"
            dst = Path(raw_tmp) / "current.csv"
            src.write_text("new\n", encoding="utf-8")
            dst.write_text("old\n", encoding="utf-8")
            real_replace = os.replace
            calls = {"count": 0}

            def flaky_replace(src_path, dst_path):
                calls["count"] += 1
                if calls["count"] < 3:
                    exc = OSError("temporary Windows file lock")
                    exc.winerror = 32
                    raise exc
                return real_replace(src_path, dst_path)

            with patch("htxbot.monitoring.os.replace", side_effect=flaky_replace):
                bot._replace_path_with_retry(src, dst, attempts=3, delay_sec=0.0)

            self.assertEqual(dst.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(calls["count"], 3)
            self.assertFalse(src.exists())

    def test_one_way_order_and_cancel_use_exchange_order_endpoints(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            order = bot._create_one_way_order(
                symbol=SYMBOL,
                order_type="limit",
                side=config.ENTRY_SIDE,
                amount=1.0,
                price=10.0,
                post_only=True,
            )
            canceled = bot._cancel_order_ref(
                SYMBOL,
                {
                    "id": order["id"],
                    "side": config.ENTRY_SIDE,
                    "price": 10.0,
                    "amount": 1.0,
                },
                event="entry_order_canceled",
                reason="test_cancel",
            )

            self.assertEqual(bot.exchange.create_order_calls, 1)
            self.assertTrue(canceled)
            self.assertEqual(
                bot.exchange.canceled_orders,
                [(order["id"], SYMBOL, {"marginMode": config.RISK.margin_mode})],
            )

    def test_unknown_short_entry_cancel_logs_entry_event(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            bot = self.make_bot(Path(raw_tmp))
            unknown_entry = {
                "id": "manual_short_entry",
                "symbol": SYMBOL,
                "side": "sell",
                "price": 10.1,
                "amount": 2.0,
            }

            valid = bot._validate_entry_orders(SYMBOL, [unknown_entry])

            self.assertFalse(valid)
            self.assertIn(
                ("manual_short_entry", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                bot.exchange.canceled_orders,
            )
            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(
                any(
                    row["event"] == "entry_order_canceled"
                    and row["order_id"] == "manual_short_entry"
                    and row["side"] == "sell"
                    and row["reason"] == "unknown_entry_orders"
                    for row in rows
                )
            )
            self.assertFalse(
                any(
                    row["event"] == "sell_order_canceled"
                    and row["order_id"] == "manual_short_entry"
                    for row in rows
                )
            )

    def test_one_way_order_accepts_order_request_with_extra_params(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            order = bot._create_one_way_order(
                OrderRequest(
                    symbol=SYMBOL,
                    order_type="market",
                    side=config.EXIT_SIDE,
                    amount=1.0,
                    reduce_only=True,
                    leverage=50,
                    extra_params={"stopLossPrice": 9.0},
                )
            )

            self.assertEqual(order["params"]["leverRate"], 50)
            self.assertTrue(order["params"]["reduceOnly"])
            self.assertEqual(order["params"]["stopLossPrice"], 9.0)

    def test_runtime_lock_replaces_stale_lock_and_releases(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            bot = self.make_bot(Path(raw_tmp))
            bot.lock_path.write_text("not-a-pid", encoding="utf-8")

            bot._acquire_runtime_lock()

            self.assertEqual(
                bot.lock_path.read_text(encoding="utf-8"), str(os.getpid())
            )
            bot._release_runtime_lock()
            self.assertFalse(bot.lock_path.exists())

    def test_runtime_lock_ownership_check_stops_displaced_process(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            bot = self.make_bot(Path(raw_tmp))
            bot._acquire_runtime_lock()
            bot._assert_runtime_lock_owned()

            bot.lock_path.write_text("999999", encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "stopping to prevent duplicate bot instances"
            ):
                bot._assert_runtime_lock_owned()
            bot._release_runtime_lock()
            self.assertTrue(bot.lock_path.exists())

    def test_windows_live_pid_is_not_treated_as_stale_when_command_line_is_unavailable(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp:
            bot = self.make_bot(Path(raw_tmp))
            with (
                patch("htxbot.state.os.name", "nt"),
                patch("htxbot.state.os.kill"),
                patch.object(bot, "_pid_command_line", return_value=""),
            ):
                self.assertTrue(bot._pid_is_running(12345))

    def test_runtime_lock_treats_pid_permission_error_as_running(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            bot = self.make_bot(Path(raw_tmp))
            with patch("htxbot.state.os.kill", side_effect=PermissionError("denied")):
                self.assertTrue(bot._pid_is_running(12345))

    def test_legacy_state_load_repairs_long_profile_defaults_and_cost_basis(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            risk = replace(config.RISK, leverage=7, margin_mode="cross")
            with override_config(RISK=risk, RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.state_path.write_text(
                    json.dumps(
                        {
                            SYMBOL: {
                                "symbol": SYMBOL,
                                "position_size": 3.0,
                                "position_available": 3.0,
                                "entry_price": 11.0,
                                "total_bought_base": 3.0,
                                "total_bought_quote": 33.0,
                                "total_buy_fees_quote": 0.033,
                            }
                        }
                    ),
                    encoding="utf-8",
                )

                state = bot._load_state()[SYMBOL]

                self.assertEqual(state.leverage, 7)
                self.assertEqual(state.margin_mode, "cross")
                self.assertAlmostEqual(state.remaining_entry_quote, 33.0)
                self.assertAlmostEqual(state.remaining_buy_fees_quote, 0.033)
                self.assertAlmostEqual(state.base_entry_amount, 3.0)
                self.assertAlmostEqual(state.base_entry_quote, 33.0)
                self.assertAlmostEqual(state.base_entry_price, 11.0)

    def test_legacy_state_load_repairs_short_profile_defaults_and_cost_basis(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            risk = replace(config.RISK, leverage=5, margin_mode="cross")
            with override_config(RISK=risk, RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.state_path.write_text(
                    json.dumps(
                        {
                            SYMBOL: {
                                "symbol": SYMBOL,
                                "position_size": 4.0,
                                "position_available": 4.0,
                                "entry_price": 9.5,
                                "total_sold_base": 4.0,
                                "total_sold_quote": 38.0,
                                "total_sell_fees_quote": 0.038,
                                "remaining_entry_quote": 0.0,
                                "remaining_buy_fees_quote": 0.0,
                            }
                        }
                    ),
                    encoding="utf-8",
                )

                state = bot._load_state()[SYMBOL]

                self.assertEqual(state.leverage, 5)
                self.assertEqual(state.margin_mode, "cross")
                self.assertAlmostEqual(state.remaining_entry_quote, 38.0)
                self.assertAlmostEqual(state.remaining_buy_fees_quote, 0.038)
                self.assertAlmostEqual(state.base_entry_amount, 4.0)
                self.assertAlmostEqual(state.base_entry_quote, 38.0)

    def test_state_load_net_open_pnl_includes_remaining_entry_fees(self):
        scenarios = (
            (
                "long",
                {
                    "total_bought_base": 3.0,
                    "total_bought_quote": 33.0,
                    "total_buy_fees_quote": 0.033,
                    "unrealized_pnl": 1.25,
                },
                1.217,
            ),
            (
                "short",
                {
                    "total_sold_base": 4.0,
                    "total_sold_quote": 38.0,
                    "total_sell_fees_quote": 0.038,
                    "unrealized_pnl": 1.25,
                },
                1.212,
            ),
        )
        for profile_name, payload, expected_net in scenarios:
            with self.subTest(profile=profile_name):
                with (
                    tempfile.TemporaryDirectory() as raw_tmp,
                    config.use_profile(profile_name),
                ):
                    bot = self.make_bot(Path(raw_tmp))
                    bot.state_path.write_text(
                        json.dumps(
                            {
                                SYMBOL: {
                                    "symbol": SYMBOL,
                                    "position_size": payload.get(
                                        "total_bought_base",
                                        payload.get("total_sold_base"),
                                    ),
                                    "position_available": payload.get(
                                        "total_bought_base",
                                        payload.get("total_sold_base"),
                                    ),
                                    "entry_price": 11.0,
                                    **payload,
                                }
                            }
                        ),
                        encoding="utf-8",
                    )

                    state = bot._load_state()[SYMBOL]

                    self.assertAlmostEqual(state.net_open_pnl, expected_net)

    def test_legacy_state_load_coerces_string_scalars_and_order_refs(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.state_path.write_text(
                json.dumps(
                    {
                        SYMBOL: {
                            "symbol": SYMBOL,
                            "position_size": "3",
                            "position_available": "2",
                            "position_frozen": "1",
                            "entry_price": "11.5",
                            "total_bought_quote": "34.5",
                            "total_bought_amount": "3",
                            "paid_buy_fees_quote": "0.0345",
                            "frozen_no_more_buys": "false",
                            "average_stage": "2",
                            "retired_strategy_counter": "3",
                            "last_retired_strategy_at": "1700000100",
                            "sell_ladder_orders": {
                                "id": 12345,
                                "side": "sell",
                                "price": "12.0",
                                "amount": "1",
                                "created_at": "1700000000",
                                "stage": "1",
                            },
                            "hard_stop_order": {
                                "id": 67890,
                                "side": "sell",
                                "trigger_price": "10.8",
                                "amount": "3",
                                "created_at": "1700000001",
                                "loss_rate": "0.02",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            state = bot._load_state()[SYMBOL]
            bot.states = {SYMBOL: state}
            reloaded = bot._get_state(SYMBOL)

            self.assertEqual(reloaded.position_size, 3.0)
            self.assertEqual(reloaded.position_available, 2.0)
            self.assertEqual(reloaded.position_frozen, 1.0)
            self.assertEqual(reloaded.entry_price, 11.5)
            self.assertFalse(reloaded.frozen_no_more_buys)
            self.assertEqual(reloaded.average_stage, 2)
            self.assertFalse(hasattr(reloaded, "retired_strategy_counter"))
            self.assertFalse(hasattr(reloaded, "last_retired_strategy_at"))
            self.assertEqual(reloaded.sell_ladder_orders[0]["id"], "12345")
            self.assertEqual(reloaded.sell_ladder_orders[0]["price"], 12.0)
            self.assertEqual(reloaded.sell_ladder_orders[0]["amount"], 1.0)
            self.assertEqual(reloaded.hard_stop_order["id"], "67890")
            self.assertEqual(reloaded.hard_stop_order["trigger_price"], 10.8)
            self.assertEqual(reloaded.hard_stop_order["loss_rate"], 0.02)
            bot._save_state()
            saved_payload = json.loads(bot.state_path.read_text(encoding="utf-8"))[
                SYMBOL
            ]
            self.assertNotIn("retired_strategy_counter", saved_payload)
            self.assertNotIn("last_retired_strategy_at", saved_payload)

    def test_trade_state_serialization_keeps_pending_exit_ladder_metadata(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 2.0
                state.entry_price = 10.0
                state.pending_exit_ladder_since = 1234.5
                state.pending_exit_ladder_reason = "no_closeable_position_available"

                bot._save_state()
                reloaded = bot._load_state()[SYMBOL]

                self.assertAlmostEqual(reloaded.pending_exit_ladder_since, 1234.5)
                self.assertEqual(
                    reloaded.pending_exit_ladder_reason,
                    "no_closeable_position_available",
                )
                self.assertEqual(
                    reloaded.lifecycle, PositionLifecycle.PENDING_CLOSEABLE.value
                )

    def test_trade_state_serialization_keeps_pending_close_order_metadata(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 2.0
            state.entry_price = 10.0
            state.pending_close_order = {
                "id": 12345,
                "side": "sell",
                "amount": "2",
                "created_at": "1700000000",
                "reason": "dust_position_close",
            }
            state.pending_close_reason = "dust_position_close"
            state.soft_defensive_last_signal_timestamp = 1700000010.0
            state.soft_defensive_consecutive_signals = 2
            state.soft_defensive_exit_activated_at = 1700000020.0
            state.soft_defensive_exit_last_rebuild_at = 1700000030.0
            state.soft_defensive_exit_fraction = 0.33

            bot._save_state()
            reloaded = bot._load_state()[SYMBOL]

            self.assertEqual(reloaded.pending_close_order["id"], "12345")
            self.assertEqual(reloaded.pending_close_order["amount"], 2.0)
            self.assertEqual(reloaded.pending_close_reason, "dust_position_close")
            self.assertAlmostEqual(
                reloaded.soft_defensive_last_signal_timestamp, 1700000010.0
            )
            self.assertEqual(reloaded.soft_defensive_consecutive_signals, 2)
            self.assertAlmostEqual(
                reloaded.soft_defensive_exit_activated_at, 1700000020.0
            )
            self.assertAlmostEqual(
                reloaded.soft_defensive_exit_last_rebuild_at, 1700000030.0
            )
            self.assertAlmostEqual(reloaded.soft_defensive_exit_fraction, 0.33)
            self.assertEqual(reloaded.lifecycle, PositionLifecycle.EXITING.value)

    def test_trade_state_lifecycle_is_derived_from_runtime_flags(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            self.assertEqual(state.lifecycle, PositionLifecycle.FLAT.value)

            state.entry_orders = [
                {"id": "entry", "side": config.ENTRY_SIDE, "amount": 1.0}
            ]
            bot._refresh_active_side(state)
            self.assertEqual(state.lifecycle, PositionLifecycle.ENTERING.value)

            state.entry_orders = []
            state.position_size = 2.0
            state.entry_price = 10.0
            bot._refresh_active_side(state)
            self.assertEqual(state.lifecycle, PositionLifecycle.OPEN.value)

            state.sell_ladder_mode = "breakeven"
            state.frozen_no_more_buys = True
            bot._refresh_active_side(state)
            self.assertEqual(state.lifecycle, PositionLifecycle.BREAKEVEN.value)

            state.sell_ladder_signature = bot._pending_exit_ladder_signature(
                "breakeven", SYMBOL, state
            )
            state.pending_exit_ladder_since = time.time()
            bot._refresh_active_side(state)
            self.assertEqual(state.lifecycle, PositionLifecycle.PENDING_CLOSEABLE.value)

            state.pending_exit_ladder_since = None
            state.sell_ladder_signature = ""
            state.sell_ladder_mode = "absolute_force_exit"
            bot._refresh_active_side(state)
            self.assertEqual(state.lifecycle, PositionLifecycle.FORCE_EXIT.value)

            state.sell_ladder_mode = "hard_stop_loss"
            bot._refresh_active_side(state)
            self.assertEqual(state.lifecycle, PositionLifecycle.FORCE_EXIT.value)

    def test_structured_signal_analytics_files_and_jsonl_are_written(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            signal = self.entry_signal()
            signal["api_secret"] = "do-not-log"
            signal["ema1d"] = 101.25
            signal["ema2d"] = 100.75
            signal["macro_gap"] = 0.031
            signal["trigger_gap"] = 0.012
            signal["pullback_depth"] = 0.007
            signal["volume_valid"] = True
            signal["volume_ratio"] = 1.25
            signal["volume_spike_ratio"] = 2.50
            signal["volume_spike_direction"] = "long"
            signal["volume_profile_valid"] = True
            signal["volume_profile_break"] = False
            signal["volume_profile_poc"] = 100.5
            signal["volume_profile_value_area_low"] = 99.5
            signal["volume_profile_value_area_high"] = 102.5
            signal["volume_reason"] = "volume_spike_confirmed"

            bot._record_signal_analytics(
                "signal_built",
                symbol=SYMBOL,
                signal=signal,
                context={"token": "hidden", "note": "kept"},
            )

            with bot.signal_analytics_csv_path.open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["decision"], "signal_built")
            self.assertEqual(rows[-1]["symbol"], SYMBOL)
            self.assertEqual(rows[-1]["signal_ts"], "1000")
            self.assertEqual(rows[-1]["valid"], "1")
            self.assertEqual(rows[-1]["ema1d"], "101.250000000000")
            self.assertEqual(rows[-1]["ema2d"], "100.750000000000")
            self.assertEqual(rows[-1]["macro_gap"], "0.03100000")
            self.assertEqual(rows[-1]["trigger_gap"], "0.01200000")
            self.assertEqual(rows[-1]["pullback_depth"], "0.00700000")
            self.assertEqual(rows[-1]["volume_valid"], "1")
            self.assertEqual(rows[-1]["volume_ratio"], "1.25000000")
            self.assertEqual(rows[-1]["volume_spike_ratio"], "2.50000000")
            self.assertEqual(rows[-1]["volume_spike_direction"], "long")
            self.assertEqual(rows[-1]["volume_profile_valid"], "1")
            self.assertEqual(rows[-1]["volume_profile_break"], "0")
            self.assertEqual(rows[-1]["volume_profile_poc"], "100.500000000000")
            self.assertEqual(
                rows[-1]["volume_profile_value_area_low"], "99.500000000000"
            )
            self.assertEqual(
                rows[-1]["volume_profile_value_area_high"], "102.500000000000"
            )
            self.assertEqual(rows[-1]["volume_reason"], "volume_spike_confirmed")

            payloads = [
                json.loads(line)
                for line in bot.signal_analytics_jsonl_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(payloads[-1]["signal"]["api_secret"], "<redacted>")
            self.assertEqual(payloads[-1]["context"]["token"], "<redacted>")
            self.assertEqual(payloads[-1]["context"]["note"], "kept")

    def test_diagnostics_warning_error_and_fault_rows_are_structured(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            bot._log_event(
                "WARNING",
                "Transient private API timeout",
                event="state_exchange_mismatch",
                symbol=SYMBOL,
                reason="position_fetch_failed",
                exception=ccxt.RequestTimeout("timeout"),
                retryable=True,
                attempt=2,
                hostname="api.hbdm.com",
            )
            bot._log_step_exception(SYMBOL, RuntimeError("logic failed"))

            with bot.diagnostics_csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-2]["severity"], "warning")
            self.assertEqual(rows[-2]["category"], "network")
            self.assertEqual(rows[-2]["exception_type"], "RequestTimeout")
            self.assertEqual(rows[-2]["retryable"], "1")
            self.assertEqual(rows[-2]["attempt"], "2")
            self.assertEqual(rows[-1]["severity"], "fault")
            self.assertEqual(rows[-1]["reason"], "step_error")

            payloads = [
                json.loads(line)
                for line in bot.diagnostics_jsonl_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(
                payloads[-1]["exception"]["exception_type"], "RuntimeError"
            )

    def test_signed_htx_urls_are_redacted_in_trade_csv_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            signed_url = (
                "https://api.hbdm.com/linear-swap-api/v1/swap_cross_cancel?"
                "AccessKeyId=AKIA_TEST&Signature=SIG_SECRET&api_secret=BAD_SECRET&token=TOK_SECRET"
            )
            exc = RuntimeError(
                f'htx {signed_url} {{"status":"error","err_code":1492,"err_msg":"closeable error"}}'
            )

            bot._log_event(
                "ERROR",
                f"Cancel failed for signed request {signed_url}: {exc}",
                event="state_exchange_mismatch",
                symbol=SYMBOL,
                reason="step_error",
                exception=exc,
                retryable=False,
                attempt=3,
                hostname="api.hbdm.com",
            )

            csv_text = bot.csv_path.read_text(encoding="utf-8")
            diagnostics_text = bot.diagnostics_jsonl_path.read_text(encoding="utf-8")
            combined_text = csv_text + diagnostics_text
            for secret in ("AKIA_TEST", "SIG_SECRET", "BAD_SECRET", "TOK_SECRET"):
                self.assertNotIn(secret, combined_text)
            self.assertIn("<redacted>", combined_text)

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["exception_type"], "RuntimeError")
            self.assertEqual(rows[-1]["error_code"], "1492")
            self.assertEqual(rows[-1]["retryable"], "0")
            self.assertNotIn("AKIA_TEST", rows[-1]["message"])

            payloads = [
                json.loads(line)
                for line in bot.diagnostics_jsonl_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(
                payloads[-1]["exception"]["exception_type"], "RuntimeError"
            )
            self.assertEqual(payloads[-1]["exception"]["error_code"], "1492")
            self.assertNotIn("SIG_SECRET", payloads[-1]["exception"]["message"])

    def test_reduce_only_closeable_rejection_logs_redacted_exception_details(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0
                signed_url = (
                    "https://api.hbdm.com/linear-swap-api/v1/swap_cross_order?"
                    "AccessKeyId=AKIA_TEST&Signature=SIG_SECRET"
                )

                def fail_create_order(symbol, type, side, amount, price, params=None):
                    bot.exchange.create_order_calls += 1
                    raise RuntimeError(
                        f'htx {signed_url} {{"status":"error","err_code":1492,'
                        '"err_msg":"Amount of Reduce Only order exceeds the amount available to close."}'
                    )

                bot.exchange.create_order = fail_create_order

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=5.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=5.0,
                        mode="normal",
                    )
                )

                combined_text = bot.csv_path.read_text(
                    encoding="utf-8"
                ) + bot.diagnostics_jsonl_path.read_text(encoding="utf-8")
                self.assertNotIn("AKIA_TEST", combined_text)
                self.assertNotIn("SIG_SECRET", combined_text)
                self.assertIn("<redacted>", combined_text)

                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = [
                        row
                        for row in csv.DictReader(handle)
                        if row["event"] == "reduce_only_violation_prevented"
                    ]
                self.assertEqual(
                    rows[-1]["reason"],
                    "closeable_amount_reserved_by_existing_exit_orders",
                )
                self.assertEqual(rows[-1]["exception_type"], "RuntimeError")
                self.assertEqual(rows[-1]["error_code"], "1492")

    def test_trade_csv_header_migration_adds_diagnostic_columns(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            legacy_header = [
                name
                for name in bot.CSV_HEADER
                if name not in {"message", "exception_type", "error_code", "retryable"}
            ]
            row = {name: "" for name in legacy_header}
            row.update(
                {
                    "ts": "1000",
                    "level": "ERROR",
                    "event": "state_exchange_mismatch",
                    "reason": "legacy",
                }
            )
            with bot.csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(legacy_header)
                writer.writerow([row[name] for name in legacy_header])

            bot._ensure_csv_file()

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
                header = handle.seek(0) or next(csv.reader(handle))
            for column in ("message", "exception_type", "error_code", "retryable"):
                self.assertIn(column, header)
                self.assertEqual(rows[-1][column], "")
            self.assertEqual(rows[-1]["reason"], "legacy")

    def test_trade_csv_header_migration_renames_legacy_ema_columns(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            legacy_header = [
                "ema30" if name == "ema50" else "ema60" if name == "ema100" else name
                for name in bot.CSV_HEADER
            ]
            row = {name: "" for name in legacy_header}
            row.update(
                {
                    "ts": "1000",
                    "level": "INFO",
                    "event": "ema_signal_valid",
                    "ema30": "50.1",
                    "ema60": "100.1",
                }
            )
            with bot.csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(legacy_header)
                writer.writerow([row[name] for name in legacy_header])

            bot._ensure_csv_file()

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["ema50"], "50.1")
            self.assertEqual(rows[-1]["ema100"], "100.1")
            self.assertNotIn("ema30", rows[-1])
            self.assertNotIn("ema60", rows[-1])

    def test_trade_csv_header_migration_streams_legacy_ema_columns_without_unbounded_reads(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            legacy_header = [
                "ema30" if name == "ema50" else "ema60" if name == "ema100" else name
                for name in bot.CSV_HEADER
            ]
            with bot.csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(legacy_header)
                for index in range(1000):
                    row = {name: "" for name in legacy_header}
                    row.update(
                        {
                            "ts": str(1000 + index),
                            "level": "INFO",
                            "event": "ema_signal_valid",
                            "ema30": f"{50.0 + index:.1f}",
                            "ema60": f"{100.0 + index:.1f}",
                        }
                    )
                    writer.writerow([row[name] for name in legacy_header])

            with self.guard_path_against_unbounded_reads(bot.csv_path) as read_sizes:
                bot._ensure_csv_file()

            self.assertTrue(all(0 < size <= 1024 * 1024 for size in read_sizes))
            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1000)
            self.assertEqual(rows[0]["ema50"], "50.0")
            self.assertEqual(rows[0]["ema100"], "100.0")
            self.assertEqual(rows[-1]["ema50"], "1049.0")
            self.assertEqual(rows[-1]["ema100"], "1099.0")

    def test_all_runtime_csv_header_migrations_stream_without_unbounded_reads(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            cases = [
                (bot.csv_path, list(bot.CSV_HEADER)),
                (bot.cycle_stats_path, list(bot.CYCLE_STATS_HEADER)),
                (bot.macro_csv_path, list(bot.MACRO_CSV_HEADER)),
                (bot.external_price_csv_path, list(bot.EXTERNAL_PRICE_CSV_HEADER)),
                (bot.account_pnl_csv_path, list(bot.ACCOUNT_PNL_CSV_HEADER)),
                (bot.signal_analytics_csv_path, list(bot.SIGNAL_ANALYTICS_CSV_HEADER)),
                (bot.diagnostics_csv_path, list(bot.DIAGNOSTICS_CSV_HEADER)),
            ]

            for path, header in cases:
                with self.subTest(path=path.name):
                    legacy_header = header[:-1]
                    legacy_header[0] = "\ufeff" + legacy_header[0]
                    with path.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(legacy_header)
                        for index in range(250):
                            writer.writerow(
                                [
                                    f"{name.lstrip(chr(0xFEFF))}_{index}"
                                    for name in legacy_header
                                ]
                            )

                    with patch(
                        "pathlib.Path.read_text",
                        side_effect=AssertionError("read_text should not be used"),
                    ):
                        with patch(
                            "pathlib.Path.read_bytes",
                            side_effect=AssertionError("read_bytes should not be used"),
                        ):
                            with self.guard_path_against_unbounded_reads(
                                path
                            ) as read_sizes:
                                bot._ensure_headered_csv_file(path, header)

                    self.assertTrue(read_sizes)
                    self.assertTrue(all(0 < size <= 1024 * 1024 for size in read_sizes))
                    with path.open(newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                        handle.seek(0)
                        migrated_header = next(csv.reader(handle))
                    self.assertEqual(migrated_header, header)
                    self.assertEqual(len(rows), 250)
                    self.assertEqual(rows[0][header[0]], f"{header[0]}_0")
                    self.assertEqual(rows[-1][header[-1]], "")

    def test_external_price_htx_symbol_to_mexc_returns_empty_on_invalid_inputs(self):
        settings = replace(config.EXTERNAL_PRICE_FEED)
        feed = ExternalPriceFeed(settings, clock=lambda: 1000.0)

        # Test missing or empty inputs
        self.assertEqual(feed.htx_symbol_to_mexc(""), "")
        self.assertEqual(feed.htx_symbol_to_mexc(None), "")

        # Test base derived from market empty cases
        self.assertEqual(feed.htx_symbol_to_mexc("BTC/USDT", market={}), "BTCUSDT")
        self.assertEqual(
            feed.htx_symbol_to_mexc("BTC/USDT", market={"base": ""}), "BTCUSDT"
        )
        self.assertEqual(
            feed.htx_symbol_to_mexc("BTC/USDT", market={"base": None}), "BTCUSDT"
        )

        # Test non-alphanumeric inputs
        self.assertEqual(feed.htx_symbol_to_mexc("!@#$"), "")
        self.assertEqual(feed.htx_symbol_to_mexc("!@#$/USDT"), "")
        self.assertEqual(
            feed.htx_symbol_to_mexc("!@#$/USDT", market={"base": "!@#$"}), ""
        )

    def test_external_price_csv_records_mexc_quantities_and_notional(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            bot._append_external_price_csv(
                self.external_context(
                    mexc_bid_qty=7.0,
                    mexc_ask_qty=8.0,
                    mexc_bid_notional=699.3,
                    mexc_ask_notional=800.8,
                )
            )

            with bot.external_price_csv_path.open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["mexc_bid_qty"], "7.00000000")
            self.assertEqual(rows[-1]["mexc_ask_qty"], "8.00000000")
            self.assertEqual(rows[-1]["mexc_bid_notional"], "699.30000000")
            self.assertEqual(rows[-1]["mexc_ask_notional"], "800.80000000")

    def test_external_price_csv_header_migration_preserves_existing_columns(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            tmp_path = Path(raw_tmp)
            bot = self.make_bot(tmp_path)
            path = tmp_path / "legacy_external_price.csv"
            old_header = [
                name
                for name in bot.EXTERNAL_PRICE_CSV_HEADER
                if name
                not in {
                    "mexc_bid_qty",
                    "mexc_ask_qty",
                    "mexc_bid_notional",
                    "mexc_ask_notional",
                }
            ]
            row = {name: "" for name in old_header}
            row.update({"ts": "2000", "spread_bps": "12.34000000", "reason": "ok"})
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(old_header)
                writer.writerow([row[name] for name in old_header])

            bot._ensure_headered_csv_file(path, bot.EXTERNAL_PRICE_CSV_HEADER)

            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["spread_bps"], "12.34000000")
            self.assertEqual(rows[-1]["mexc_bid_qty"], "")
            self.assertEqual(rows[-1]["mexc_ask_qty"], "")
            self.assertEqual(rows[-1]["mexc_bid_notional"], "")
            self.assertEqual(rows[-1]["mexc_ask_notional"], "")

    def test_csv_header_prepend_streams_without_reading_whole_file(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            tmp_path = Path(raw_tmp)
            bot = self.make_bot(tmp_path)
            path = tmp_path / "missing_header.csv"
            path.write_text("legacy,row\n" + ("1,2\n" * 1000), encoding="utf-8")

            with patch(
                "pathlib.Path.read_text",
                side_effect=AssertionError("read_text should not be used"),
            ):
                with self.guard_path_against_unbounded_reads(path) as read_sizes:
                    bot._ensure_headered_csv_file(path, bot.CSV_HEADER)

            self.assertTrue(read_sizes)
            self.assertTrue(all(0 < size <= 1024 * 1024 for size in read_sizes))

            with path.open(newline="", encoding="utf-8") as handle:
                first_row = next(csv.reader(handle))
                second_row = next(csv.reader(handle))
            self.assertEqual(first_row, list(bot.CSV_HEADER))
            self.assertEqual(second_row, ["legacy", "row"])

    def test_external_price_feed_computes_spread_rollups_and_changes(self):
        now = [1000.0]
        settings = replace(
            config.EXTERNAL_PRICE_FEED,
            rest_poll_interval_sec=0.0,
            max_internal_spread_bps=50.0,
            min_valid_bid_qty_usdt=1.0,
            min_valid_ask_qty_usdt=1.0,
        )
        client = FakeMexcClient(
            [
                BookTicker(99.9, 100.1, 10.0, 10.0, ts=1000.0),
                BookTicker(100.9, 101.1, 10.0, 10.0, ts=1030.0),
                BookTicker(101.9, 102.1, 10.0, 10.0, ts=1060.0),
            ]
        )
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

    def test_external_price_feed_invalid_fresh_mexc_book_is_not_stale(self):
        now = [1000.0]
        settings = replace(
            config.EXTERNAL_PRICE_FEED,
            rest_poll_interval_sec=0.0,
            max_internal_spread_bps=50.0,
            min_valid_bid_qty_usdt=500.0,
            min_valid_ask_qty_usdt=500.0,
        )
        client = FakeMexcClient([BookTicker(99.9, 100.1, 1.0, 1.0, ts=1000.0)])
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: now[0])

        context = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertFalse(context["valid"])
        self.assertFalse(context["stale"])
        self.assertEqual(context["reason"], "mexc_bid_ask_notional_below_min")
        self.assertAlmostEqual(context["mexc_bid_notional"], 99.9)
        self.assertAlmostEqual(context["mexc_ask_notional"], 100.1)

    def test_external_price_stale_context_is_invalid(self):
        now = [2000.0]
        settings = replace(
            config.EXTERNAL_PRICE_FEED, stale_after_ms=3000, max_price_age_ms=3000
        )
        client = FakeMexcClient([BookTicker(99.9, 100.1, 10.0, 10.0, ts=1990.0)])
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: now[0])

        context = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertFalse(context["valid"])
        self.assertTrue(context["stale"])
        self.assertGreater(context["age_ms"], 3000)

    def test_external_price_rejects_unsupported_reference_exchange(self):
        settings = replace(config.EXTERNAL_PRICE_FEED, reference_exchanges=("binance",))
        client = FakeMexcClient([BookTicker(99.9, 100.1, 10.0, 10.0, ts=1000.0)])
        feed = ExternalPriceFeed(settings, mexc_client=client, clock=lambda: 1000.0)

        context = feed.get_context(SYMBOL, {"bid": 99.9, "ask": 100.1}, market=MARKET)

        self.assertFalse(context["valid"])
        self.assertTrue(context["stale"])
        self.assertEqual(context["reason"], "reference_exchange_unsupported")
        self.assertEqual(client.calls, [])

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
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(spread_bps=25.0)
                )
                bot.signal_cache["symbols"] = {SYMBOL: self.entry_signal()}

                bot._maybe_place_initial_buy(
                    SYMBOL, bot.signal_cache["symbols"][SYMBOL]
                )

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("external_premium_blocked", rows[-1]["reason"])

    def test_external_price_short_discount_blocks_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(spread_bps=-25.0)
                )
                bot.signal_cache["symbols"] = {
                    SYMBOL: self.entry_signal(rs30=-0.002, rs60=-0.003)
                }

                bot._maybe_place_initial_buy(
                    SYMBOL, bot.signal_cache["symbols"][SYMBOL]
                )

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("external_discount_blocked", rows[-1]["reason"])

    def test_entry_gate_external_and_budget_blocks_write_signal_analytics(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.entry_gate = {
                "signal_ts": 1000,
                "allowed_symbols": set(),
                "blocked_reasons": {SYMBOL: "entry_top_n_blocked"},
                "ranked_symbols": [SYMBOL],
            }

            bot._maybe_place_initial_buy(SYMBOL, self.entry_signal(ts=1000))

            with bot.signal_analytics_csv_path.open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["decision"], "entry_gate_checked")
            self.assertEqual(rows[-1]["block_reason"], "entry_top_n_blocked")

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.external_price_feed = StaticExternalPriceFeed(
                self.external_context(spread_bps=25.0)
            )

            bot._maybe_place_initial_buy(SYMBOL, self.entry_signal(ts=1001))

            with bot.signal_analytics_csv_path.open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["decision"], "entry_gate_checked")
            self.assertIn("external_premium_blocked", rows[-1]["block_reason"])
            self.assertEqual(rows[-1]["external_valid"], "1")

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.balance_free = 0.0
                bot.exchange.balance_total = 0.0

                bot._maybe_place_initial_buy(SYMBOL, self.entry_signal(ts=1002))

                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["decision"], "entry_budget_blocked")
            self.assertEqual(rows[-1]["block_reason"], "free_margin_below_reserve")

    def test_external_price_stale_is_ignored_by_default_for_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(valid=False, stale=True, reason="stale")
                )
                signal = self.entry_signal()

                bot._maybe_place_initial_buy(SYMBOL, signal)

                self.assertTrue(bot._get_state(SYMBOL).entry_orders)

    def test_external_price_invalid_fresh_context_blocks_entry_by_default(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        valid=False, stale=False, reason="internal_spread_too_wide"
                    )
                )

                bot._maybe_place_initial_buy(SYMBOL, self.entry_signal())

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("external_reference_invalid", rows[-1]["reason"])
                self.assertIn("internal_spread_too_wide", rows[-1]["reason"])

    def test_external_price_disable_stale_reference_blocks_entry_even_when_ignore_is_true(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
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

    def test_default_external_impulse_bonus_matches_entry_score_scale(self):
        self.assertAlmostEqual(config.EXTERNAL_PRICE_FEED.impulse_score_bonus, 0.02)
        self.assertLess(
            config.EXTERNAL_PRICE_FEED.impulse_score_bonus,
            config.STRATEGY.entry_min_score,
        )

    def test_pending_entry_keeps_external_impulse_bonus_during_signal_revalidation(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(config.STRATEGY, entry_min_score=0.03)
            settings = replace(
                config.EXTERNAL_PRICE_FEED,
                impulse_confirmation_enabled=True,
                mexc_lead_threshold_bps_30s=5.0,
                impulse_score_bonus=0.02,
            )
            with override_config(
                RUNTIME=runtime, STRATEGY=strategy, EXTERNAL_PRICE_FEED=settings
            ):
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

                bot._manage_entry_orders(
                    SYMBOL,
                    signal,
                    open_orders=[
                        {
                            "id": "pending_entry",
                            "symbol": SYMBOL,
                            "side": config.ENTRY_SIDE,
                            "amount": 1.0,
                            "remaining": 1.0,
                        }
                    ],
                )

                self.assertEqual(
                    [order["id"] for order in state.entry_orders], ["pending_entry"]
                )

    def test_external_price_divergence_sets_entry_cooldown(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        spread_bps=0.0, htx_change_1m_bps=80.0, mexc_change_1m_bps=10.0
                    )
                )
                signal = self.entry_signal()

                bot._maybe_place_initial_buy(SYMBOL, signal)

                state = bot._get_state(SYMBOL)
                self.assertFalse(state.entry_orders)
                self.assertGreater(state.cooldown_until, time.time())

    def test_external_price_context_is_cached_within_cycle(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            feed = StaticExternalPriceFeed(self.external_context(spread_bps=0.0))
            bot.external_price_feed = feed

            first = bot._external_price_context(SYMBOL)
            second = bot._external_price_context(SYMBOL)

            self.assertEqual(first["spread_bps"], second["spread_bps"])
            self.assertEqual(len(feed.calls), 1)

            bot._reset_private_caches()
            bot._external_price_context(SYMBOL)

            self.assertEqual(len(feed.calls), 2)

    def test_external_price_context_cache_is_singleflight_across_threads(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            feed = StaticExternalPriceFeed(self.external_context(spread_bps=0.0))
            bot.external_price_feed = feed

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                contexts = list(
                    executor.map(
                        lambda _index: bot._external_price_context(SYMBOL), range(8)
                    )
                )

            self.assertEqual(len(feed.calls), 1)
            self.assertTrue(all(context["spread_bps"] == 0.0 for context in contexts))

    def test_external_price_directional_1m_blocks_adverse_long_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        spread_bps=0.0,
                        htx_change_1m_bps=-60.0,
                        mexc_change_1m_bps=-55.0,
                    )
                )

                bot._maybe_place_initial_buy(SYMBOL, self.entry_signal())

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn(
                    "external_directional_1m_blocked", rows[-1]["block_reason"]
                )

    def test_htx_orderbook_spread_filter_blocks_fresh_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                entry_spread_filter_enabled=True,
                entry_spread_filter_max_bps=30.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(spread_bps=0.0)
                )
                bot.exchange.order_book = {
                    "bids": [[10.0, 100.0]],
                    "asks": [[10.2, 100.0]],
                }

                bot._maybe_place_initial_buy(SYMBOL, self.entry_signal())

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn("htx_orderbook_spread_too_wide", rows[-1]["block_reason"])

    def test_order_book_prefetch_serializes_exchange_calls_and_reuses_cycle_cache(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(
                config.RUNTIME, market_data_max_workers=4, poll_interval_sec=3
            )
            strategy = replace(
                config.STRATEGY,
                entry_spread_filter_enabled=True,
                entry_spread_filter_max_bps=30.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                symbols = [SYMBOL, SECOND_SYMBOL, "ALT3/USDT:USDT", "ALT4/USDT:USDT"]
                bot.symbols = list(symbols)

                original_fetch_order_book = bot.exchange.fetch_order_book
                active = {"count": 0, "max": 0}
                lock = threading.Lock()

                def slow_fetch_order_book(symbol, limit=None):
                    with lock:
                        active["count"] += 1
                        active["max"] = max(active["max"], active["count"])
                    try:
                        time.sleep(0.03)
                        return original_fetch_order_book(symbol, limit=limit)
                    finally:
                        with lock:
                            active["count"] -= 1

                bot.exchange.fetch_order_book = slow_fetch_order_book

                bot._reset_market_data_caches()
                bot._prefetch_market_data_snapshots()

                self.assertEqual(active["max"], 1)
                self.assertEqual(set(bot.exchange.order_book_calls), set(symbols))
                calls_after_prefetch = bot.exchange.fetch_order_book_calls

                for symbol in symbols:
                    spread_bps, bid, ask = bot._entry_orderbook_spread_bps(symbol)
                    self.assertGreater(spread_bps, 0.0)
                    self.assertGreater(bid, 0.0)
                    self.assertGreater(ask, 0.0)

                self.assertEqual(
                    bot.exchange.fetch_order_book_calls, calls_after_prefetch
                )

    def test_parallel_order_book_prefetch_preserves_profile_context(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, market_data_max_workers=2)
            strategy = replace(
                config.STRATEGY,
                entry_spread_filter_enabled=True,
                entry_spread_filter_max_bps=30.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.symbols = [SYMBOL, SECOND_SYMBOL]
                seen_sides = []
                lock = threading.Lock()

                def fetch_order_book_with_context(symbol, limit=None):
                    with lock:
                        seen_sides.append(config.POSITION_SIDE)
                    time.sleep(0.01)
                    return {"bids": [[9.99, 100.0]], "asks": [[10.01, 100.0]]}

                bot.exchange.fetch_order_book = fetch_order_book_with_context

                bot._reset_market_data_caches()
                bot._prefetch_market_data_snapshots()

                self.assertTrue(seen_sides)
                self.assertEqual(set(seen_sides), {"short"})

    def test_ticker_prefetch_serializes_exchange_calls_and_reuses_cycle_cache(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(
                config.RUNTIME, market_data_max_workers=4, poll_interval_sec=3
            )
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                symbols = [SYMBOL, SECOND_SYMBOL, "ALT3/USDT:USDT", "ALT4/USDT:USDT"]
                bot.symbols = list(symbols)
                bot.market_by_symbol.update(
                    {
                        symbol: MARKET
                        for symbol in symbols
                        if symbol not in bot.market_by_symbol
                    }
                )
                bot.exchange.has["fetchTickers"] = False

                original_fetch_ticker = bot.exchange.fetch_ticker
                active = {"count": 0, "max": 0}
                lock = threading.Lock()

                def slow_fetch_ticker(symbol):
                    with lock:
                        active["count"] += 1
                        active["max"] = max(active["max"], active["count"])
                    try:
                        time.sleep(0.03)
                        return original_fetch_ticker(symbol)
                    finally:
                        with lock:
                            active["count"] -= 1

                bot.exchange.fetch_ticker = slow_fetch_ticker

                bot._reset_private_caches()
                bot._reset_market_data_caches()
                bot._prefetch_ticker_snapshots(symbols)

                self.assertEqual(active["max"], 1)
                self.assertEqual(set(bot.exchange.ticker_calls), set(symbols))
                calls_after_prefetch = bot.exchange.fetch_ticker_calls

                for symbol in symbols:
                    reference, last = bot._fetch_reference_price(symbol)
                    self.assertGreater(reference, 0.0)
                    self.assertGreater(last, 0.0)

                self.assertEqual(bot.exchange.fetch_ticker_calls, calls_after_prefetch)

    def test_parallel_ticker_prefetch_preserves_profile_context(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, market_data_max_workers=2)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.symbols = [SYMBOL, SECOND_SYMBOL]
                bot.exchange.has["fetchTickers"] = False
                seen_sides = []
                lock = threading.Lock()

                def fetch_ticker_with_context(symbol):
                    with lock:
                        seen_sides.append(config.POSITION_SIDE)
                    time.sleep(0.01)
                    return {"bid": 9.9, "ask": 10.1, "last": 10.0, "symbol": symbol}

                bot.exchange.fetch_ticker = fetch_ticker_with_context

                bot._reset_private_caches()
                bot._reset_market_data_caches()
                bot._prefetch_ticker_snapshots()

                self.assertTrue(seen_sides)
                self.assertEqual(set(seen_sides), {"short"})

    def test_profitable_cycle_uses_post_win_cooldown(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            risk = replace(
                config.RISK,
                cooldown_minutes_after_close=10.0,
                post_win_cooldown_minutes_after_close=90.0,
            )
            with override_config(RISK=risk):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.total_bought_amount = 10.0
                state.total_bought_quote = 100.0
                state.total_sold_amount = 10.0
                state.total_sold_quote = 110.0
                state.cycle_opened_at = time.time() - 600.0

                before = time.time()
                bot._close_cycle(SYMBOL, reason="test_profit")

                cooldown_until = bot._get_state(SYMBOL).cooldown_until
                self.assertIsNotNone(cooldown_until)
                self.assertGreater(cooldown_until, before + 85.0 * 60.0)
                self.assertLess(cooldown_until, before + 95.0 * 60.0)

    def test_external_price_directional_1m_blocks_adverse_short_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        spread_bps=0.0, htx_change_1m_bps=65.0, mexc_change_1m_bps=60.0
                    )
                )

                bot._maybe_place_initial_buy(
                    SYMBOL, self.entry_signal(rs30=-0.002, rs60=-0.003)
                )

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn(
                    "external_directional_1m_blocked", rows[-1]["block_reason"]
                )

    def test_pending_entry_is_canceled_when_directional_1m_turns_adverse(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        spread_bps=0.0,
                        htx_change_1m_bps=-70.0,
                        mexc_change_1m_bps=-52.0,
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

                bot._manage_entry_orders(SYMBOL, self.entry_signal(), open_orders=[])

                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])

    def test_external_price_favorable_premium_tightens_long_exit_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            external = replace(config.EXTERNAL_PRICE_FEED, exit_adjustment_enabled=True)
            with override_config(RUNTIME=runtime, EXTERNAL_PRICE_FEED=external):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(spread_bps=25.0)
                )
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )

                self.assertEqual(
                    [order["amount"] for order in bot.exchange.created_orders],
                    [40.0, 30.0, 20.0],
                )
                self.assertEqual(
                    [order["price"] for order in bot.exchange.created_orders],
                    [100.5, 101.0, 102.0],
                )
                self.assertEqual(state.exit_runner_contracts, 10.0)
                self.assertEqual(
                    state.sell_ladder_orders[0]["ladder_name"], "external_tightened"
                )

    def test_external_price_stale_keeps_normal_exit_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(valid=False, stale=True, spread_bps=25.0)
                )
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )

                self.assertEqual(
                    [order["amount"] for order in bot.exchange.created_orders], [30.0]
                )
                self.assertEqual(
                    [order["price"] for order in bot.exchange.created_orders], [100.8]
                )
                self.assertEqual(state.exit_runner_contracts, 70.0)

    def test_calculate_rsi_basic_shapes(self):
        rising = [float(index) for index in range(1, 40)]
        falling = list(reversed(rising))
        flat = [10.0] * 40

        self.assertGreater(calculate_rsi(rising, 14), 50.0)
        self.assertLess(calculate_rsi(falling, 14), 50.0)
        self.assertEqual(calculate_rsi(flat, 14), 50.0)
        self.assertEqual(calculate_rsi([1.0, 2.0], 14), 0.0)

    def test_realized_volatility(self):
        # Edge cases: window <= 1 or not enough data
        self.assertEqual(realized_volatility([100.0, 101.0, 102.0], 1), 0.0)
        self.assertEqual(realized_volatility([100.0, 101.0], 2), 0.0)

        # Invalid elements: not enough valid returns (<= 0)
        self.assertEqual(realized_volatility([100.0, 0.0, -1.0, 102.0], 3), 0.0)

        # Happy path testing both numpy and fallback
        closes = [100.0, 101.0, 100.5, 99.0, 102.0, 101.5]

        # We need a stable output, so we calculate what it should be manually or roughly check bounds
        # Returns: ln(101/100) = 0.00995, ln(100.5/101) = -0.00496, ln(99/100.5) = -0.01504,
        #          ln(102/99) = 0.02985, ln(101.5/102) = -0.00491
        # It should just be a positive float.

        with patch("htxbot.indicators.HAS_NUMPY", True):
            vol_np = realized_volatility(closes, 4)
            self.assertGreater(vol_np, 0.0)
            self.assertLess(vol_np, 0.1)  # shouldn't be massive

        with patch("htxbot.indicators.HAS_NUMPY", False):
            vol_fallback = realized_volatility(closes, 4)
            self.assertGreater(vol_fallback, 0.0)
            self.assertLess(vol_fallback, 0.1)

        # They should be essentially equal
        self.assertAlmostEqual(vol_np, vol_fallback, places=6)

    def test_average_true_range_from_ohlcv(self):
        candles = [
            [1, 10.0, 11.0, 9.5, 10.5, 1.0],
            [2, 10.5, 12.0, 10.0, 11.0, 1.0],
            [3, 11.0, 11.5, 10.5, 10.75, 1.0],
            [4, 10.75, 13.0, 10.25, 12.5, 1.0],
        ]

        self.assertAlmostEqual(average_true_range(candles, 3), (2.0 + 1.0 + 2.75) / 3.0)
        self.assertEqual(average_true_range(candles[:2], 3), 0.0)

    def macro_regime_bot(
        self, tmp_path: Path, gold_rsi: float, btc_rsi: float
    ) -> HtxFuturesBot:
        bot = self.make_bot(tmp_path)
        bot.benchmark_symbol = BTC_SYMBOL
        bot.macro_gold_symbol = XAUT_SYMBOL
        bot._macro_gold_lookup_done = True
        bot.exchange.ohlcv[(XAUT_SYMBOL, "4h")] = ohlcv_series(
            [100.0] * 40, 4 * 60 * 60
        )
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
                with (
                    tempfile.TemporaryDirectory() as raw_tmp,
                    self.subTest(regime=regime),
                ):
                    bot = self.macro_regime_bot(Path(raw_tmp), gold_rsi, btc_rsi)

                    context = bot._gold_btc_rsi_context()

                    self.assertTrue(context["ok"])
                    self.assertEqual(context["regime"], regime)

    def test_gold_directional_bias_boosts_short_when_gold_outperforms_crypto(self):
        macro = replace(
            config.MACRO,
            enable_gold_directional_bias=True,
            gold_directional_bias_strength=0.30,
            gold_directional_bias_min_multiplier=0.50,
            gold_directional_bias_max_multiplier=1.25,
        )
        with tempfile.TemporaryDirectory() as raw_tmp, override_config(MACRO=macro):
            bot = self.make_bot(Path(raw_tmp))

            context = bot._classify_gold_btc_rsi_context(
                XAUT_SYMBOL,
                BTC_SYMBOL,
                gold_rsi=65.0,
                btc_rsi=42.0,
                ratio_return=0.05,
                gold_return=0.04,
                btc_return=-0.02,
            )

            self.assertEqual(context["regime"], "crypto_underperforms_gold")
            self.assertLess(context["macro_direction_score"], 0.0)
            self.assertAlmostEqual(
                context["long_budget_multiplier"], macro.risk_off_long_budget_multiplier
            )
            self.assertGreater(context["short_budget_multiplier"], 1.0)
            self.assertLessEqual(
                context["short_budget_multiplier"],
                macro.gold_directional_bias_max_multiplier,
            )

    def test_gold_directional_bias_boosts_long_when_crypto_leads_gold(self):
        macro = replace(
            config.MACRO,
            enable_gold_directional_bias=True,
            gold_directional_bias_strength=0.30,
            gold_directional_bias_min_multiplier=0.50,
            gold_directional_bias_max_multiplier=1.25,
        )
        with tempfile.TemporaryDirectory() as raw_tmp, override_config(MACRO=macro):
            bot = self.make_bot(Path(raw_tmp))

            context = bot._classify_gold_btc_rsi_context(
                XAUT_SYMBOL,
                BTC_SYMBOL,
                gold_rsi=50.0,
                btc_rsi=70.0,
                ratio_return=-0.04,
                gold_return=0.01,
                btc_return=0.05,
            )

            self.assertEqual(context["regime"], "crypto_risk_on")
            self.assertGreater(context["macro_direction_score"], 0.0)
            self.assertGreater(context["long_budget_multiplier"], 1.0)
            self.assertLess(context["short_budget_multiplier"], 1.0)
            self.assertLessEqual(
                context["long_budget_multiplier"],
                macro.gold_directional_bias_max_multiplier,
            )

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

    def test_macro_long_budget_zero_blocks_initial_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            self.set_macro_context(
                bot,
                regime="deleveraging",
                long_budget_multiplier=0.0,
                reason="btc_weak_gold_weak",
            )

            signal = self.entry_signal()
            signal["budget_multiplier"] = 0.0  # reflect long_budget_multiplier applied
            bot._maybe_place_initial_buy(SYMBOL, signal)

            self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
            self.assertEqual(bot.exchange.created_orders, [])

    def test_macro_disable_averaging_blocks_average_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]
                self.set_macro_context(
                    bot,
                    regime="crypto_underperforms_gold",
                    disable_averaging=True,
                    reason="gold_strong_btc_weak",
                )

                signal = self.entry_signal(ts=1000)
                signal.update({"ladder_multiplier": 1.0, "budget_multiplier": 1.0})

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertFalse(state.entry_orders)

    def test_macro_gold_symbol_is_not_added_to_entry_universe(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
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
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                buy_ref = {"id": "buy_1", "side": "buy", "price": 99.0, "amount": 2.0}
                sell_ref = {
                    "id": "sell_1",
                    "side": "sell",
                    "price": 101.0,
                    "amount": 2.0,
                }
                state.entry_orders = [buy_ref]
                state.sell_ladder_orders = [sell_ref]
                bot.exchange.cancel_fail_ids.add("sell_1")

                bot._cancel_all_orders(SYMBOL, reason="test_cancel_failure")

                self.assertEqual(state.entry_orders, [buy_ref])
                self.assertEqual(state.sell_ladder_orders, [sell_ref])

    def test_frozen_no_more_buys_blocks_averaging(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_enabled=True,
                ema_averaging_interval_hours=0.0,
                ema_max_averaging_stages=2,
                account_pnl_enabled=False,
            )
            with override_config(STRATEGY=strategy, RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.ticker = {"bid": 9.75, "ask": 9.85, "last": 9.80}
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 10.4
                state.initial_entry_notional = 104.0
                state.base_entry_amount = 10.0
                state.base_entry_price = 10.4
                state.frozen_no_more_buys = True
                state.sell_ladder_orders = [
                    {"id": "sell", "side": "sell", "price": 10.6, "amount": 10.0}
                ]
                signal = self.entry_signal(ts=2000)
                signal.update(
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "volatility_budget_multiplier": 1.0,
                    }
                )

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertTrue(state.frozen_no_more_buys)

    def test_entry_ladder_prices_round_away_from_crossing_book(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertLessEqual(
                    bot._get_state(SYMBOL).entry_orders[0]["price"], raw_long_price
                )

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertGreaterEqual(
                    bot._get_state(SYMBOL).entry_orders[0]["price"], raw_short_price
                )

    def test_ema_entry_ladder_uses_two_one_percent_levels(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(
                BUYING=replace(config.BUYING, ladder_offsets=(0.0, 0.01)),
                RUNTIME=config.RUNTIME,
            ):
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
            with override_config(
                BUYING=replace(config.BUYING, ladder_offsets=(0.0, 0.01)),
                RUNTIME=config.RUNTIME,
            ):
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

    def test_decimal_places_zero_price_precision_rounds_away_from_crossing_book(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                market = {**MARKET, "precision": {"price": 0}}
                bot.exchange.markets[SYMBOL] = market
                bot.market_by_symbol[SYMBOL] = market
                bot.exchange.precisionMode = ccxt.DECIMAL_PLACES

                def integer_price_precision(_symbol, price):
                    return f"{round(float(price)):.0f}"

                bot.exchange.price_to_precision = integer_price_precision

                self.assertEqual(bot._price_at_or_above(SYMBOL, 10.2), 11.0)
                self.assertEqual(bot._price_at_or_below(SYMBOL, 10.8), 10.0)

    def test_price_to_precision(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            # normal conversions
            def mock_precision_normal(symbol, price):
                return str(round(price, 2))

            bot.exchange.price_to_precision = mock_precision_normal
            self.assertEqual(bot._price_to_precision(SYMBOL, 10.123), 10.12)
            self.assertEqual(bot._price_to_precision(SYMBOL, 10.128), 10.13)

            # test edge case where string conversion fails
            def mock_precision_error(symbol, price):
                return "invalid_float"

            bot.exchange.price_to_precision = mock_precision_error
            with self.assertRaises(ValueError):
                bot._price_to_precision(SYMBOL, 10.0)

    def test_mock_exchange_price_and_amount_precision_helpers(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            self.assertEqual(bot._price_at_or_above(SYMBOL, 10.001), 10.01)
            self.assertEqual(bot._price_at_or_below(SYMBOL, 10.009), 10.0)
            self.assertEqual(bot._amount_to_precision(SYMBOL, 5.9), 5.0)
            self.assertEqual(bot._amount_to_precision(SYMBOL, 0.9), 0.0)

    def test_min_contracts_handles_htx_base_limit_and_contract_limit_shapes(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            htx_base_limit_market = {
                **MARKET,
                "contractSize": 0.01,
                "limits": {"amount": {"min": 0.01}},
                "precision": {"price": 0.01, "amount": 1.0},
            }
            bot.exchange.markets[SYMBOL] = htx_base_limit_market
            bot.market_by_symbol[SYMBOL] = htx_base_limit_market
            self.assertEqual(bot._get_min_contracts(SYMBOL), 1.0)

            contract_limit_market = {
                **MARKET,
                "contractSize": 0.01,
                "limits": {"amount": {"min": 1.0}},
                "precision": {"price": 0.01, "amount": 1.0},
            }
            bot.exchange.markets[SYMBOL] = contract_limit_market
            bot.market_by_symbol[SYMBOL] = contract_limit_market
            self.assertEqual(bot._get_min_contracts(SYMBOL), 1.0)

    def test_order_remaining_amount_respects_explicit_zero_remaining(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            self.assertEqual(
                bot._order_remaining_amount({"amount": 5.0, "remaining": 0.0}), 0.0
            )
            self.assertEqual(bot._order_remaining_amount({"amount": 5.0}), 5.0)

    def test_profile_validation_rejects_mismatched_ema_ladder_lengths(self):
        profile = config.resolve_profile("long")
        invalid = replace(
            profile,
            buying=replace(
                profile.buying, ladder_fractions=(0.5, 0.5), ladder_offsets=(0.0,)
            ),
        )

        with self.assertRaisesRegex(ValueError, "ladder_fractions and ladder_offsets"):
            config._validate_profile(invalid)

    def test_entry_ladder_uses_manual_account_leverage_not_sizing_leverage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, post_only_enabled=False)
            risk = replace(config.RISK, leverage=30, account_leverage=0)
            buying = replace(
                config.BUYING, ladder_fractions=(1.0,), ladder_offsets=(0.0,)
            )
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
                self.assertEqual(order["amount"], 50.0)
                state_order = bot._get_state(SYMBOL).entry_orders[0]
                self.assertEqual(state_order["leverage"], 50.0)
                self.assertEqual(state_order["sizing_leverage"], 50.0)
                self.assertEqual(state_order["amount"], 50.0)

    def test_entry_ladder_caps_sizing_to_lower_manual_account_leverage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, post_only_enabled=False)
            risk = replace(config.RISK, leverage=30, account_leverage=0)
            buying = replace(
                config.BUYING, ladder_fractions=(1.0,), ladder_offsets=(0.0,)
            )
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

    def test_entry_ladder_stops_after_insufficient_margin_rejection(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, post_only_enabled=False)
            buying = replace(
                config.BUYING, ladder_fractions=(0.5, 0.5), ladder_offsets=(0.0, 0.01)
            )
            with override_config(RUNTIME=runtime, BUYING=buying):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.create_order_failures = [
                    ccxt.InsufficientFunds(
                        'htx {"status":"error","err_code":1047,"err_msg":"Insufficient margin available."}'
                    )
                ]

                placed = bot._place_buy_ladder(
                    SYMBOL,
                    margin_budget=10.0,
                    reference_price=10.0,
                    signal={"ts": 1000, "ladder_multiplier": 1.0},
                    reason="ema_initial_signal",
                )

                self.assertEqual(placed, 0)
                self.assertEqual(bot.exchange.create_order_calls, 1)
                self.assertEqual(bot._get_state(SYMBOL).entry_orders, [])
                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["decision"], "entry_ladder_rejected")
                self.assertIn("entry_insufficient_margin", rows[-1]["block_reason"])
                with bot.diagnostics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    diagnostics = list(csv.DictReader(handle))
                self.assertEqual(diagnostics[-1]["event"], "entry_order_canceled")
                self.assertEqual(diagnostics[-1]["error_code"], "1047")

    def test_step_symbol_places_initial_entry_ladder_when_flat_signal_valid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, post_only_enabled=False)
            buying = replace(
                config.BUYING, ladder_fractions=(1.0,), ladder_offsets=(0.0,)
            )
            with override_config(RUNTIME=runtime, BUYING=buying):
                bot = self.make_bot(Path(raw_tmp))
                bot.signal_cache["symbols"] = {SYMBOL: self.entry_signal(ts=2000)}
                bot._prepare_new_entry_gate()

                bot.step_symbol(SYMBOL)

                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "limit")
                self.assertEqual(order["side"], "buy")
                self.assertFalse(order["params"].get("reduceOnly", False))
                state = bot._get_state(SYMBOL)
                self.assertEqual(len(state.entry_orders), 1)
                self.assertEqual(state.entry_orders[0]["id"], order["id"])

    def test_step_symbol_post_close_cleans_unknown_flat_orders_before_return(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 5.0
            state.position_available = 5.0
            state.position_side = "long"
            state.entry_price = 100.0
            state.total_bought_amount = 5.0
            state.total_bought_quote = 500.0
            bot.exchange.open_orders = [
                {
                    "id": "orphan_close",
                    "symbol": SYMBOL,
                    "side": "sell",
                    "price": 101.0,
                    "amount": 1.0,
                    "remaining": 1.0,
                    "reduceOnly": True,
                },
                {
                    "id": "orphan_entry",
                    "symbol": SYMBOL,
                    "side": "buy",
                    "price": 99.0,
                    "amount": 1.0,
                    "remaining": 1.0,
                },
            ]

            bot.step_symbol(SYMBOL)

            self.assertIn(
                ("orphan_close", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                bot.exchange.canceled_orders,
            )
            self.assertIn(
                ("orphan_entry", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                bot.exchange.canceled_orders,
            )
            self.assertEqual(bot._get_state(SYMBOL).position_size, 0.0)

    def test_step_symbol_adopts_unknown_reduce_only_exit_when_position_appears(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 5.0,
                    "available": 0.0,
                    "frozen": 5.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                    "leverage": config.RISK.leverage,
                }
            ]
            bot.exchange.open_orders = [
                {
                    "id": "orphan_reduce_only_exit",
                    "symbol": SYMBOL,
                    "side": "sell",
                    "price": 101.0,
                    "amount": 5.0,
                    "remaining": 5.0,
                    "reduceOnly": True,
                }
            ]

            bot.step_symbol(SYMBOL)

            state = bot._get_state(SYMBOL)
            self.assertEqual(state.position_size, 5.0)
            self.assertEqual(
                [order["id"] for order in state.sell_ladder_orders],
                ["orphan_reduce_only_exit"],
            )
            self.assertEqual(bot.exchange.created_orders, [])

    def test_step_symbol_builds_exit_ladder_same_cycle_after_adopting_position(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(config.STRATEGY, hard_stop_loss_enabled=False)
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 5.0,
                        "available": 5.0,
                        "frozen": 0.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": config.RISK.leverage,
                    }
                ]
                bot.exchange.open_orders = []

                bot.step_symbol(SYMBOL)

                state = bot._get_state(SYMBOL)
                self.assertEqual(state.position_size, 5.0)
                self.assertTrue(state.sell_ladder_orders)
                self.assertEqual(
                    sum(ref["amount"] for ref in state.sell_ladder_orders)
                    + state.exit_runner_contracts,
                    5.0,
                )
                self.assertTrue(
                    all(
                        order["params"].get("reduceOnly")
                        for order in bot.exchange.created_orders
                    )
                )

    def test_step_symbol_does_not_readopt_stale_tracked_exit_after_position_change(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 5.0
            state.position_available = 5.0
            state.position_side = "long"
            state.entry_price = 100.0
            state.sell_ladder_orders = [
                {"id": "old_exit", "side": "sell", "price": 101.0, "amount": 5.0}
            ]
            bot.exchange.positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 6.0,
                    "available": 6.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                    "leverage": config.RISK.leverage,
                }
            ]
            bot.exchange.open_orders = [
                {
                    "id": "old_exit",
                    "symbol": SYMBOL,
                    "side": "sell",
                    "price": 101.0,
                    "amount": 5.0,
                    "remaining": 5.0,
                    "reduceOnly": True,
                }
            ]

            bot.step_symbol(SYMBOL)

            self.assertIn(
                ("old_exit", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                bot.exchange.canceled_orders,
            )
            self.assertEqual(state.sell_ladder_orders, [])
            self.assertEqual(state.sell_ladder_signature, "")

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

    def test_profile_reads_global_htxbot_env_prefix(self):
        env_keys = (
            "POLL_INTERVAL_SEC",
            "HTXBOT_POLL_INTERVAL_SEC",
            "ALIAS_POLL_INTERVAL_SEC",
            "HTXBOT_ALIAS_POLL_INTERVAL_SEC",
        )
        previous = {key: os.environ.get(key) for key in env_keys}
        try:
            for key in env_keys:
                os.environ.pop(key, None)
            os.environ["HTXBOT_POLL_INTERVAL_SEC"] = "17"
            profile = config._make_profile("alias", "long", ("test",))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(profile.runtime.poll_interval_sec, 17)

    def test_profile_reads_set_leverage_on_start_env(self):
        env_key = "ALIAS_SET_LEVERAGE_ON_START"
        previous = os.environ.get(env_key)
        try:
            os.environ[env_key] = "true"
            profile = config._make_profile("alias", "long", ("test",))
        finally:
            if previous is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = previous

        self.assertTrue(profile.exchange.set_leverage_on_start)

    def test_entry_ladder_does_not_retry_with_lower_leverage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, post_only_enabled=False)
            risk = replace(config.RISK, leverage=30, account_leverage=50)
            buying = replace(
                config.BUYING, ladder_fractions=(1.0,), ladder_offsets=(0.0,)
            )
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

    def test_normal_exit_ladder_uses_fixed_take_profit_and_trailing_runner(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.balance_free = 10000.0
                bot.exchange.balance_total = 10000.0
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )

                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(
                    [order["type"] for order in bot.exchange.created_orders], ["limit"]
                )
                self.assertEqual(
                    [order["side"] for order in bot.exchange.created_orders], ["sell"]
                )
                self.assertEqual(
                    [order["amount"] for order in bot.exchange.created_orders], [30.0]
                )
                self.assertEqual(
                    [order["price"] for order in bot.exchange.created_orders], [100.8]
                )
                self.assertTrue(
                    all(
                        order["params"].get("reduceOnly")
                        for order in bot.exchange.created_orders
                    )
                )
                self.assertEqual(state.exit_runner_contracts, 70.0)
                self.assertFalse(state.exit_runner_active)

    def test_ensure_sell_ladder_rebuilds_missing_fixed_exit_with_matching_runner_signature(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                state.sell_ladder_orders = []
                state.exit_runner_contracts = 70.0
                state.sell_ladder_signature = bot._exit_ladder_signature(
                    "normal", SYMBOL, state
                )

                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 30.0)
                self.assertTrue(
                    bot.exchange.created_orders[0]["params"].get("reduceOnly")
                )
                self.assertEqual(
                    [ref["amount"] for ref in state.sell_ladder_orders], [30.0]
                )
                self.assertEqual(state.exit_runner_contracts, 70.0)

    def test_short_ensure_sell_ladder_rebuilds_missing_fixed_exit_with_matching_runner_signature(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                state.sell_ladder_orders = []
                state.exit_runner_contracts = 70.0
                state.sell_ladder_signature = bot._exit_ladder_signature(
                    "normal", SYMBOL, state
                )

                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["side"], "buy")
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 30.0)
                self.assertTrue(
                    bot.exchange.created_orders[0]["params"].get("reduceOnly")
                )
                self.assertEqual(
                    [ref["amount"] for ref in state.sell_ladder_orders], [30.0]
                )
                self.assertEqual(state.exit_runner_contracts, 70.0)

    def test_ensure_sell_ladder_keeps_runner_only_remainder_when_signature_matches(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 65.0
                state.position_available = 65.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                state.sell_ladder_orders = []
                state.exit_runner_contracts = 65.0
                state.sell_ladder_signature = bot._exit_ladder_signature(
                    "normal", SYMBOL, state
                )

                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(state.exit_runner_contracts, 65.0)

    def test_exit_ladder_preflight_caps_to_position_and_blocks_duplicate_tracked_ladder(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.initial_entry_notional = 500.0

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=10.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=10.0,
                        mode="normal",
                    )
                )

                self.assertLessEqual(
                    sum(order["amount"] for order in bot.exchange.created_orders), 5.0
                )
                created_before_duplicate = len(bot.exchange.created_orders)
                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=5.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=5.0,
                        mode="normal",
                    )
                )

                self.assertEqual(
                    len(bot.exchange.created_orders), created_before_duplicate
                )
                self.assertEqual(
                    len(state.sell_ladder_orders), created_before_duplicate
                )

    def test_split_exit_keeps_base_ladder_on_base_average_and_adds_recovery(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 145.0
                state.position_available = 145.0
                state.entry_price = (100.0 * 100.0 + 45.0 * 90.0) / 145.0
                state.initial_entry_notional = 10000.0
                state.remaining_entry_quote = 14050.0
                state.remaining_buy_fees_quote = 1.405
                state.base_entry_amount = 100.0
                state.base_entry_quote = 10000.0
                state.base_entry_fees_quote = 1.0
                state.base_entry_price = 100.0
                state.averaging_entry_amount = 45.0
                state.averaging_entry_quote = 4050.0
                state.averaging_entry_fees_quote = 0.405

                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(
                    [order["amount"] for order in bot.exchange.created_orders],
                    [35.0, 25.0, 25.0, 15.0, 45.0],
                )
                self.assertEqual(
                    [order["price"] for order in bot.exchange.created_orders],
                    [100.8, 101.6, 103.0, 105.0, 100.04],
                )
                self.assertEqual(
                    [ref["exit_scope"] for ref in state.sell_ladder_orders],
                    ["base", "base", "base", "base", "average_recovery"],
                )
                self.assertEqual(state.exit_runner_contracts, 0.0)
                self.assertIn("split_exit=1", state.sell_ladder_signature)

    def test_exit_ladder_partial_create_failure_clears_signature_for_retry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_adaptive_exit_enabled=False,
                ema_exit_ladder_fractions=(0.5, 0.5),
                ema_exit_runner_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                original_create_order = bot.exchange.create_order
                calls = {"count": 0}

                def flaky_create_order(symbol, type, side, amount, price, params=None):
                    calls["count"] += 1
                    if calls["count"] == 2:
                        bot.exchange.create_order_calls += 1
                        raise RuntimeError("stage rejected")
                    return original_create_order(
                        symbol, type, side, amount, price, params=params
                    )

                bot.exchange.create_order = flaky_create_order

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=10.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=10.0,
                        mode="normal",
                    )
                )

                self.assertEqual(
                    [order["amount"] for order in bot.exchange.created_orders], [5.0]
                )
                self.assertEqual(
                    [ref["amount"] for ref in state.sell_ladder_orders], [5.0]
                )
                self.assertEqual(state.sell_ladder_signature, "")

    def test_exit_runner_ladder_full_create_failure_resets_runner_state(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                def failing_create_order(
                    symbol, type, side, amount, price, params=None
                ):
                    bot.exchange.create_order_calls += 1
                    raise RuntimeError("exchange rejected")

                bot.exchange.create_order = failing_create_order

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(state.sell_ladder_signature, "")
                self.assertEqual(state.exit_runner_contracts, 0.0)

    def test_split_exit_ladder_recovery_failure_clears_signature_for_retry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 145.0
                state.position_available = 145.0
                state.entry_price = (100.0 * 100.0 + 45.0 * 90.0) / 145.0
                state.initial_entry_notional = 10000.0
                state.remaining_entry_quote = 14050.0
                state.remaining_buy_fees_quote = 1.405
                state.base_entry_amount = 100.0
                state.base_entry_quote = 10000.0
                state.base_entry_fees_quote = 1.0
                state.base_entry_price = 100.0
                state.averaging_entry_amount = 45.0
                state.averaging_entry_quote = 4050.0
                state.averaging_entry_fees_quote = 0.405
                original_create_order = bot.exchange.create_order

                def reject_recovery_order(
                    symbol, type, side, amount, price, params=None
                ):
                    if float(amount) == 45.0:
                        bot.exchange.create_order_calls += 1
                        raise RuntimeError("recovery rejected")
                    return original_create_order(
                        symbol, type, side, amount, price, params=params
                    )

                bot.exchange.create_order = reject_recovery_order

                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(
                    [order["amount"] for order in bot.exchange.created_orders],
                    [35.0, 25.0, 25.0, 15.0],
                )
                self.assertEqual(
                    [ref["exit_scope"] for ref in state.sell_ladder_orders],
                    ["base", "base", "base", "base"],
                )
                self.assertEqual(state.sell_ladder_signature, "")

    def test_average_recovery_fill_reduces_only_average_bucket(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 145.0
                state.position_available = 145.0
                state.entry_price = (100.0 * 100.0 + 45.0 * 90.0) / 145.0
                state.remaining_entry_quote = 14050.0
                state.remaining_buy_fees_quote = 1.405
                state.base_entry_amount = 100.0
                state.base_entry_quote = 10000.0
                state.base_entry_fees_quote = 1.0
                state.base_entry_price = 100.0
                state.averaging_entry_amount = 45.0
                state.averaging_entry_quote = 4050.0
                state.averaging_entry_fees_quote = 0.405
                state.sell_ladder_orders = [
                    {
                        "id": "recovery",
                        "side": config.EXIT_SIDE,
                        "price": 100.04,
                        "amount": 20.0,
                        "exit_scope": "average_recovery",
                    }
                ]

                bot._record_sell_fill(
                    SYMBOL,
                    state,
                    contracts=20.0,
                    reason="position_decreased",
                    fill_details=[
                        {
                            "order_id": "recovery",
                            "contracts": 20.0,
                            "quote": 2000.8,
                            "price": 100.04,
                            "fee_quote": 0.20008,
                            "source": "test",
                        }
                    ],
                )

                self.assertEqual(state.base_entry_amount, 100.0)
                self.assertEqual(state.base_entry_quote, 10000.0)
                self.assertAlmostEqual(state.averaging_entry_amount, 25.0)
                self.assertAlmostEqual(state.averaging_entry_quote, 2250.0)
                self.assertAlmostEqual(state.remaining_entry_quote, 12250.0)

    def test_exit_ladder_switches_to_medium_and_heavy_by_position_ratio(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0

                state.position_size = 150.0
                state.position_available = 150.0
                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=150.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=150.0,
                        mode="normal",
                    )
                )
                self.assertEqual(
                    [order["price"] for order in bot.exchange.created_orders],
                    [100.4, 101.0, 102.0, 103.5],
                )
                self.assertEqual(state.exit_runner_contracts, 0.0)

                bot.exchange.created_orders.clear()
                state.sell_ladder_orders = []
                state.sell_ladder_signature = ""
                state.position_size = 200.0
                state.position_available = 200.0
                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=200.0,
                        avg_entry_price=100.0,
                        rebuild=True,
                        closeable_contracts=200.0,
                        mode="normal",
                    )
                )
                self.assertEqual(
                    [order["price"] for order in bot.exchange.created_orders],
                    [100.3, 100.8, 101.5],
                )

    def test_exit_ladder_time_decay_removes_runner_after_six_hours(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(config.STRATEGY, ema_exit_runner_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                state.cycle_opened_at = time.time() - 6.1 * 60.0 * 60.0

                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )

                self.assertEqual(
                    [order["amount"] for order in bot.exchange.created_orders],
                    [35.0, 25.0, 40.0],
                )
                self.assertEqual(
                    [order["price"] for order in bot.exchange.created_orders],
                    [100.8, 101.6, 103.0],
                )
                self.assertEqual(state.exit_runner_contracts, 0.0)

    def test_normal_runner_closes_on_trailing_pullback(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )
                self.assertEqual(state.exit_runner_contracts, 70.0)

                bot.exchange.ticker = {"bid": 102.1, "ask": 102.2, "last": 102.1}
                managed = bot._maybe_manage_exit_runner(SYMBOL, {"trigger_valid": True})
                self.assertFalse(managed)
                self.assertTrue(state.exit_runner_active)
                self.assertEqual(len(bot.exchange.created_orders), 2)
                self.assertAlmostEqual(state.hard_stop_order["trigger_price"], 100.04)

                state.position_available = 15.0
                bot.exchange.ticker = {"bid": 101.0, "ask": 101.1, "last": 101.0}
                managed = bot._maybe_manage_exit_runner(SYMBOL, {"trigger_valid": True})

                self.assertTrue(managed)
                self.assertEqual(len(bot.exchange.created_orders), 3)
                runner_order = bot.exchange.created_orders[-1]
                self.assertEqual(runner_order["amount"], 15.0)
                self.assertEqual(runner_order["price"], 101.0)
                self.assertTrue(runner_order["params"].get("reduceOnly"))
                self.assertTrue(state.sell_ladder_orders[-1]["runner"])

    def test_trailing_runner_uses_atr_pullback_before_closing(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
                ema_exit_trailing_pullback=0.010,
                ema_exit_trailing_atr_multiplier=2.0,
                ema_exit_trailing_max_pullback=0.030,
                ema_exit_trailing_take_profit_markup=0.50,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )

                signal = {"trigger_valid": True, "atr_rate": 0.015}
                bot.exchange.ticker = {"bid": 110.0, "ask": 110.1, "last": 110.0}
                self.assertFalse(bot._maybe_manage_exit_runner(SYMBOL, signal))
                self.assertTrue(state.exit_runner_active)

                bot.exchange.ticker = {"bid": 108.0, "ask": 108.1, "last": 108.0}
                self.assertFalse(bot._maybe_manage_exit_runner(SYMBOL, signal))
                self.assertEqual(len(bot.exchange.created_orders), 2)

                bot.exchange.ticker = {"bid": 106.6, "ask": 106.7, "last": 106.6}
                self.assertTrue(bot._maybe_manage_exit_runner(SYMBOL, signal))
                runner_order = bot.exchange.created_orders[-1]
                self.assertEqual(runner_order["amount"], 70.0)
                self.assertEqual(runner_order["price"], 106.6)
                self.assertIn(
                    "volatility_pullback=0.030000",
                    state.sell_ladder_orders[-1]["reason"],
                )

    def test_short_trailing_runner_profit_lock_and_close_are_mirrored(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_exit_runner_enabled=True,
                ema_exit_trailing_enabled=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.initial_entry_notional = 10000.0
                bot._place_sell_ladder(
                    SellLadderParams(
                        symbol=SYMBOL,
                        total_contracts=100.0,
                        avg_entry_price=100.0,
                        rebuild=False,
                        closeable_contracts=100.0,
                        mode="normal",
                    )
                )

                self.assertEqual(bot.exchange.created_orders[0]["side"], "buy")
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 30.0)
                self.assertEqual(bot.exchange.created_orders[0]["price"], 99.2)
                self.assertEqual(state.exit_runner_contracts, 70.0)

                bot.exchange.ticker = {"bid": 97.8, "ask": 97.9, "last": 97.9}
                self.assertFalse(
                    bot._maybe_manage_exit_runner(SYMBOL, {"trigger_valid": True})
                )
                self.assertTrue(state.exit_runner_active)
                self.assertEqual(state.hard_stop_order["side"], "buy")
                self.assertAlmostEqual(state.hard_stop_order["trigger_price"], 99.96)

                bot.exchange.ticker = {"bid": 98.9, "ask": 99.0, "last": 99.0}
                self.assertTrue(
                    bot._maybe_manage_exit_runner(SYMBOL, {"trigger_valid": True})
                )
                runner_order = bot.exchange.created_orders[-1]
                self.assertEqual(runner_order["side"], "buy")
                self.assertEqual(runner_order["amount"], 70.0)
                self.assertEqual(runner_order["price"], 99.0)
                self.assertTrue(runner_order["params"].get("reduceOnly"))

    def test_entry_expansion_orders_are_canceled_by_ema_entry_check(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                    "rs_edge": 0.0,
                    "rs30": 0.0,
                    "rs60": config.STRATEGY.entry_min_rs60_abs * 0.80,
                    "ema_gap": 0.0,
                    "recent_return_5m": 0.0,
                    "recent_return_15m": 0.0,
                    "local_reversion": 0.0,
                }

                bot._manage_entry_orders(SYMBOL, signal, [])

                self.assertFalse(state.entry_orders)
                bot.signal_cache["benchmark_ok"] = False
                self.assertFalse(bot._is_entry_expansion_signal_valid(signal))

    def test_tiny_partial_entry_timeout_closes_market_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(
                config.RUNTIME, reduce_only_enabled=True, order_timeout_sec=1
            )
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

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 2.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertTrue(state.zombie_position)
                self.assertTrue(state.frozen_no_more_buys)

    def test_dust_close_waits_when_closeable_amount_is_frozen(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            risk = replace(
                config.RISK, dust_position_notional=100.0, dust_close_enabled=True
            )
            with override_config(RUNTIME=runtime, RISK=risk):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 10.0

                closed = bot._maybe_close_dust_position(SYMBOL, [])

                self.assertTrue(closed)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertTrue(state.frozen_no_more_buys)

    def test_dust_close_caps_market_order_to_visible_closeable_amount(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            risk = replace(
                config.RISK, dust_position_notional=100.0, dust_close_enabled=True
            )
            with override_config(RUNTIME=runtime, RISK=risk):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 10.0
                open_orders = [
                    {
                        "id": "manual_exit",
                        "symbol": SYMBOL,
                        "side": "sell",
                        "price": 11.0,
                        "amount": 3.0,
                        "remaining": 3.0,
                    }
                ]

                closed = bot._maybe_close_dust_position(SYMBOL, open_orders)

                self.assertTrue(closed)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 2.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertTrue(state.zombie_position)

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

                self.assertTrue(
                    bot._is_entry_signal_valid(
                        self.entry_signal(score=0.04, rs30=0.002, rs60=0.003)
                    )
                )
                self.assertFalse(
                    bot._is_entry_signal_valid(
                        self.entry_signal(score=0.02, rs30=0.002, rs60=0.003)
                    )
                )
                self.assertFalse(
                    bot._is_entry_signal_valid(
                        self.entry_signal(score=0.04, rs30=0.002, rs60=0.001)
                    )
                )
                self.assertFalse(
                    bot._is_entry_signal_valid(
                        self.entry_signal(score=0.04, rs30=0.0005, rs60=0.003)
                    )
                )

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            strategy = replace(
                config.STRATEGY,
                entry_min_score=0.03,
                entry_min_rs60_abs=0.002,
                entry_min_rs30_abs=0.001,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))

                self.assertTrue(
                    bot._is_entry_signal_valid(
                        self.entry_signal(score=0.04, rs30=-0.002, rs60=-0.003)
                    )
                )
                self.assertFalse(
                    bot._is_entry_signal_valid(
                        self.entry_signal(score=0.04, rs30=0.002, rs60=-0.003)
                    )
                )

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
                        symbols[0]: self.entry_signal(
                            score=0.03, rs30=0.003, rs60=0.003
                        ),
                        symbols[1]: self.entry_signal(
                            score=0.08, rs30=0.002, rs60=0.002
                        ),
                        symbols[2]: self.entry_signal(
                            score=0.05, rs30=0.004, rs60=0.004
                        ),
                    },
                }

                gate = bot._prepare_new_entry_gate()

                self.assertEqual(
                    gate["ranked_symbols"], [symbols[1], symbols[2], symbols[0]]
                )
                self.assertEqual(gate["allowed_symbols"], {symbols[1], symbols[2]})
                self.assertIn(
                    "entry_top_n_blocked", gate["blocked_reasons"][symbols[0]]
                )

    def test_entry_gate_skips_external_blocked_candidates_before_top_n(self):
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
                symbols = (SYMBOL, SECOND_SYMBOL, BTC_SYMBOL)
                bot.entry_symbols = set(symbols)
                bot.symbols = list(symbols)
                bot.market_by_symbol = {
                    SYMBOL: MARKET,
                    SECOND_SYMBOL: SECOND_MARKET,
                    BTC_SYMBOL: BTC_MARKET,
                }
                valid_context = self.external_context(
                    spread_bps=0.0, htx_mid=10.0, mexc_mid=10.0
                )
                bot.external_price_feed = PerSymbolExternalPriceFeed(
                    {
                        SYMBOL: self.external_context(
                            valid=False,
                            stale=False,
                            reason="internal_spread_too_wide",
                            spread_bps=-80.0,
                            htx_mid=10.0,
                            mexc_mid=10.0,
                        ),
                        SECOND_SYMBOL: valid_context,
                        BTC_SYMBOL: valid_context,
                    }
                )
                bot.signal_cache = {
                    "benchmark_ok": True,
                    "closed_candle_ts": 1000,
                    "symbols": {
                        SYMBOL: self.entry_signal(score=0.10, rs30=0.003, rs60=0.003),
                        SECOND_SYMBOL: self.entry_signal(
                            score=0.08, rs30=0.002, rs60=0.002
                        ),
                        BTC_SYMBOL: self.entry_signal(
                            score=0.07, rs30=0.001, rs60=0.001
                        ),
                    },
                }

                gate = bot._prepare_new_entry_gate()

                self.assertEqual(gate["external_blocked_count"], 1)
                self.assertNotIn(SYMBOL, gate["ranked_symbols"])
                self.assertEqual(gate["allowed_symbols"], {SECOND_SYMBOL, BTC_SYMBOL})
                self.assertIn(
                    "external_reference_invalid", gate["blocked_reasons"][SYMBOL]
                )

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
                    "symbols": {
                        symbol: self.entry_signal(score=0.05) for symbol in symbols
                    },
                }

                gate = bot._prepare_new_entry_gate()

                self.assertEqual(gate["allowed_symbols"], set())
                self.assertEqual(gate["rate_remaining"], 0)
                self.assertTrue(
                    all(
                        "entry_rate_limited" in reason
                        for reason in gate["blocked_reasons"].values()
                    )
                )

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
                        symbols[0]: self.entry_signal(
                            score=0.06, rs30=0.002, rs60=0.004
                        ),
                        symbols[1]: self.entry_signal(
                            score=0.04, rs30=0.004, rs60=0.004
                        ),
                        symbols[2]: self.entry_signal(
                            score=0.061, rs30=0.001, rs60=0.004
                        ),
                    },
                }

                gate = bot._prepare_new_entry_gate()

                self.assertTrue(gate["crowded"])
                self.assertEqual(gate["allowed_symbols"], {symbols[0]})
                self.assertIn(
                    "entry_weighted_score_below_min",
                    gate["blocked_reasons"][symbols[1]],
                )
                self.assertIn("penalty_rs30", gate["blocked_reasons"][symbols[2]])

    def test_ema_long_entry_signal_is_valid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
                benchmark_closes = [100.0] * 120
                while len(benchmark_closes) < len(closes):
                    benchmark_closes.append(100.0)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertTrue(signal["macro_valid"])
                self.assertTrue(signal["pullback_valid"])
                self.assertTrue(signal["pullback_had_pullback"])
                self.assertLessEqual(signal["pullback_cross_age_candles"], 6)
                self.assertTrue(signal["trigger_valid"])
                self.assertTrue(signal["data_valid"])
                self.assertTrue(signal["direction_valid"])
                self.assertTrue(signal["valid"])
                self.assertTrue(signal["entry_valid"])
                self.assertEqual(strategy.ema_pullback_slow_minutes, 8)

    def test_ema_entry_signal_accepts_pullback_when_trigger_conflicts(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
                ema_chop_filter_enabled=False,
                ema_volume_confirmation_enabled=False,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                trigger_closes = [100.0 + index for index in range(79)] + [177.5]
                macro_closes = [100.0 + index * 2.0 for index in range(80)]
                pullback_closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
                benchmark_closes = [100.0] * len(trigger_closes)
                ctx = SignalContext(
                    closes=trigger_closes,
                    benchmark_closes=benchmark_closes,
                    btc_risk={
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                    cache_key=SYMBOL,
                    macro_closes=macro_closes,
                    macro_latest_ts=1000,
                    pullback_closes=pullback_closes,
                    pullback_latest_ts=1000,
                )

                signal = bot._build_signal_from_closes(ctx)

                self.assertIsNotNone(signal)
                self.assertTrue(signal["macro_valid"])
                self.assertTrue(signal["pullback_valid"])
                self.assertFalse(signal["trigger_valid"])
                self.assertTrue(signal["entry_setup_valid"])
                self.assertTrue(signal["entry_side_valid"])
                self.assertEqual(signal["entry_signal_source"], "pullback")
                self.assertTrue(signal["ema_entry_valid"])
                self.assertTrue(signal["raw_entry_valid"])
                self.assertTrue(signal["entry_valid"])

    def test_ema_short_entry_signal_accepts_pullback_when_trigger_conflicts(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            strategy = self.ema_test_strategy(
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
                ema_chop_filter_enabled=False,
                ema_volume_confirmation_enabled=False,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                trigger_closes = [200.0 - index for index in range(79)] + [122.5]
                macro_closes = [260.0 - index * 2.0 for index in range(80)]
                long_pullback = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
                pullback_closes = [300.0 - close for close in long_pullback]
                benchmark_closes = [100.0] * len(trigger_closes)
                ctx = SignalContext(
                    closes=trigger_closes,
                    benchmark_closes=benchmark_closes,
                    btc_risk={
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                    cache_key=SYMBOL,
                    macro_closes=macro_closes,
                    macro_latest_ts=1000,
                    pullback_closes=pullback_closes,
                    pullback_latest_ts=1000,
                )

                signal = bot._build_signal_from_closes(ctx)

                self.assertIsNotNone(signal)
                self.assertTrue(signal["macro_valid"])
                self.assertTrue(signal["pullback_valid"])
                self.assertFalse(signal["trigger_valid"])
                self.assertTrue(signal["entry_setup_valid"])
                self.assertTrue(signal["entry_side_valid"])
                self.assertEqual(signal["entry_signal_source"], "pullback")
                self.assertTrue(signal["ema_entry_valid"])
                self.assertTrue(signal["raw_entry_valid"])
                self.assertTrue(signal["entry_valid"])

    def test_ema_entry_signal_requires_recent_volume_confirmation(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_chop_filter_enabled=False,
                ema_volume_confirmation_enabled=True,
                ema_volume_short_window=5,
                ema_volume_long_window=20,
                ema_volume_min_ratio=1.05,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
                benchmark_closes = [100.0] * len(closes)
                ctx = SignalContext(
                    closes=closes,
                    benchmark_closes=benchmark_closes,
                    btc_risk={
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                    candles=ohlcv_series(closes),
                    cache_key=SYMBOL,
                )

                signal = bot._build_signal_from_closes(ctx)

                self.assertIsNotNone(signal)
                self.assertTrue(signal["macro_valid"])
                self.assertTrue(signal["pullback_valid"])
                self.assertTrue(signal["trigger_valid"])
                self.assertTrue(signal["valid"])
                self.assertFalse(signal["volume_valid"])
                self.assertFalse(signal["market_structure_valid"])
                self.assertFalse(signal["entry_valid"])
                block_reason = bot._signal_block_reason(signal)
                self.assertIn("entry_weighted_score_below_min", block_reason)
                self.assertIn("penalty_market_structure", block_reason)
                self.assertIn("penalty_volume", block_reason)
                self.assertIn("volume_reason=volume_ratio_below_min", block_reason)
                self.assertIn("volume_reason=volume_ratio_below_min", signal["reason"])

    def test_entry_signal_quality_logs_raw_signal_components(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            signal = self.entry_signal()
            signal.update(
                {
                    "entry_valid": False,
                    "pullback_valid": False,
                    "rs_confirm_valid": False,
                    "score": 0.0123,
                    "rs30": -0.0045,
                    "rs60": 0.0012,
                    "volume_reason": "volume_confirmed",
                    "chop_reason": "disabled",
                }
            )

            block_reason = bot._entry_signal_quality_block_reason(signal)

            self.assertTrue(block_reason.startswith("entry_weighted_score_below_min;"))
            self.assertIn("entry_valid=0", block_reason)
            self.assertIn("pullback_valid=0", block_reason)
            self.assertIn("rs_confirm_valid=0", block_reason)
            self.assertIn("raw_score=0.012300", block_reason)

            bot._record_signal_analytics(
                "entry_gate_checked", symbol=SYMBOL, signal=signal
            )
            with (Path(raw_tmp) / "signal_analytics.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))

            self.assertTrue(rows)
            self.assertIn("pullback_valid=0", rows[-1]["block_reason"])
            self.assertIn("rs_confirm_valid=0", rows[-1]["block_reason"])

    def test_entry_quality_cannot_override_raw_entry_invalid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            signal = self.entry_signal(score=0.50, rs30=0.10, rs60=0.20)
            signal["entry_valid"] = False

            block_reason = bot._entry_signal_quality_block_reason(signal)

            self.assertIn("penalty_ema_entry", block_reason)
            self.assertFalse(bot._is_entry_signal_valid(signal))

    def test_ema_entry_signal_accepts_aligned_volume_spike_confirmation(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_chop_filter_enabled=False,
                ema_volume_confirmation_enabled=True,
                ema_volume_short_window=5,
                ema_volume_long_window=20,
                ema_volume_min_ratio=10.0,
                ema_volume_spike_filter_enabled=True,
                ema_volume_spike_window=5,
                ema_volume_spike_min_ratio=2.0,
                ema_volume_adverse_spike_min_ratio=2.0,
                ema_volume_profile_filter_enabled=False,
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
                ema_pullback_recovery_gap=0.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
                benchmark_closes = [100.0] * len(closes)
                volumes = [10.0] * (len(closes) - 1) + [80.0]
                candles = ohlcv_series(closes, volumes=volumes)
                candles[-1][1] = closes[-1] - 2.0
                candles[-1][2] = closes[-1] + 1.0
                candles[-1][3] = closes[-1] - 3.0
                ctx = SignalContext(
                    closes=closes,
                    benchmark_closes=benchmark_closes,
                    btc_risk={
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                    candles=candles,
                    cache_key=SYMBOL,
                )

                signal = bot._build_signal_from_closes(ctx)

                self.assertIsNotNone(signal)
                self.assertTrue(signal["volume_valid"])
                self.assertFalse(signal["volume_average_valid"])
                self.assertTrue(signal["market_structure_valid"])
                self.assertTrue(signal["entry_valid"])
                self.assertEqual(signal["volume_spike_direction"], "long")
                self.assertEqual(signal["volume_reason"], "volume_spike_confirmed")

    def test_market_structure_blocks_adverse_volume_profile_break(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_chop_filter_enabled=False,
                ema_volume_confirmation_enabled=True,
                ema_volume_short_window=5,
                ema_volume_long_window=20,
                ema_volume_min_ratio=1.0,
                ema_volume_spike_filter_enabled=True,
                ema_volume_spike_window=5,
                ema_volume_spike_min_ratio=1.80,
                ema_volume_adverse_spike_min_ratio=2.00,
                ema_volume_profile_filter_enabled=True,
                ema_volume_profile_window=60,
                ema_volume_profile_bins=12,
                ema_volume_profile_value_area=0.70,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                candles = [
                    [index, 100.0, 101.0, 99.0, 100.0, 10.0] for index in range(59)
                ]
                candles.append([59, 100.0, 101.0, 89.0, 90.0, 30.0])

                context = bot._ema_market_structure_context(candles)
                signal = {
                    "valid": True,
                    "data_valid": True,
                    "direction_valid": True,
                    "entry_valid": False,
                    **context,
                }
                block_reason = bot._entry_signal_quality_block_reason(signal)

                self.assertFalse(context["volume_valid"])
                self.assertFalse(context["market_structure_valid"])
                self.assertFalse(context["volume_profile_valid"])
                self.assertTrue(context["volume_profile_break"])
                self.assertEqual(context["volume_spike_direction"], "short")
                self.assertEqual(
                    context["volume_reason"], "volume_profile_adverse_break"
                )
                self.assertIn("volume_profile_break=1", block_reason)
                self.assertIn("volume_spike_direction=short", block_reason)

    def test_ema_entry_signal_blocks_choppy_trigger_noise(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_chop_filter_enabled=True,
                ema_chop_period=14,
                ema_chop_max=61.8,
                ema_volume_confirmation_enabled=True,
                ema_volume_short_window=5,
                ema_volume_long_window=20,
                ema_volume_min_ratio=1.05,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
                benchmark_closes = [100.0] * len(closes)
                volumes = [1.0] * (len(closes) - 5) + [3.0] * 5
                ctx = SignalContext(
                    closes=closes,
                    benchmark_closes=benchmark_closes,
                    btc_risk={
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                    candles=ohlcv_series(closes, volumes=volumes, range_width=8.0),
                    cache_key=SYMBOL,
                )

                signal = bot._build_signal_from_closes(ctx)
                block_reason = bot._entry_signal_quality_block_reason(signal)

                self.assertIsNotNone(signal)
                self.assertTrue(signal["volume_valid"])
                self.assertFalse(signal["chop_valid"])
                self.assertFalse(signal["market_structure_valid"])
                self.assertFalse(signal["entry_valid"])
                self.assertGreater(signal["chop"], strategy.ema_chop_max)
                self.assertIn("entry_weighted_score_below_min", block_reason)
                self.assertIn("penalty_chop", block_reason)
                self.assertIn("penalty_market_structure", block_reason)
                self.assertIn("chop_reason=chop_above_max", block_reason)

    def test_ema_pullback_recovery_requires_fresh_cross(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_pullback_recovery_max_cross_age_minutes=2
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                    198,
                    200,
                ]
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertTrue(signal["pullback_recovered"])
                self.assertTrue(signal["pullback_had_pullback"])
                self.assertGreater(signal["pullback_cross_age_candles"], 2)
                self.assertTrue(signal["data_valid"])
                self.assertTrue(signal["direction_valid"])
                self.assertTrue(signal["valid"])
                self.assertFalse(signal["pullback_valid"])
                self.assertFalse(signal["entry_pullback_required"])
                self.assertTrue(signal["entry_pullback_gate_valid"])
                self.assertTrue(signal["raw_entry_valid"])
                self.assertTrue(signal["entry_valid"])
                self.assertLess(signal["entry_quality_budget_multiplier"], 1.0)

    def test_ema_pullback_recovery_can_be_required_for_initial_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_pullback_recovery_max_cross_age_minutes=2,
                ema_entry_require_pullback_recovery=True,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                    198,
                    200,
                ]
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertTrue(signal["entry_pullback_required"])
                self.assertFalse(signal["pullback_valid"])
                self.assertFalse(signal["entry_pullback_gate_valid"])
                self.assertFalse(signal["raw_entry_valid"])
                self.assertFalse(signal["entry_valid"])
                self.assertIn(
                    "penalty_ema_entry", bot._entry_signal_quality_block_reason(signal)
                )

    def test_signal_build_applies_macro_budget_and_ladder_multipliers(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
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
                    {
                        "budget_multiplier": 0.80,
                        "ladder_multiplier": 1.20,
                        "reason": "btc_drop",
                    },
                    latest_ts=1000,
                    macro_context=macro_context,
                )

                self.assertIsNotNone(signal)
                self.assertGreater(signal["entry_quality_budget_multiplier"], 0.0)
                self.assertLessEqual(signal["entry_quality_budget_multiplier"], 1.0)
                self.assertAlmostEqual(
                    signal["budget_multiplier"],
                    0.44 * signal["entry_quality_budget_multiplier"],
                )
                self.assertAlmostEqual(signal["ladder_multiplier"], 1.50)
                self.assertEqual(signal["macro_regime"], "crypto_underperforms_gold")
                self.assertTrue(signal["macro_disable_averaging"])

    def test_signal_build_allows_long_macro_budget_above_one(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(100, 201, 2)) + [
                    198,
                    195,
                    192,
                    189,
                    186,
                    183,
                    180,
                    184,
                    188,
                    192,
                    196,
                ]
                benchmark_closes = [100.0] * len(closes)
                macro_context = self.macro_context(
                    regime="crypto_risk_on",
                    long_budget_multiplier=1.20,
                    short_budget_multiplier=0.75,
                    macro_direction_score=0.75,
                )

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "neutral",
                    },
                    latest_ts=1000,
                    macro_context=macro_context,
                )

                self.assertIsNotNone(signal)
                self.assertAlmostEqual(signal["macro_budget_multiplier"], 1.20)
                self.assertGreater(signal["entry_quality_budget_multiplier"], 0.0)
                self.assertLessEqual(signal["entry_quality_budget_multiplier"], 1.0)
                self.assertAlmostEqual(
                    signal["budget_multiplier"],
                    1.20 * signal["entry_quality_budget_multiplier"],
                )
                self.assertAlmostEqual(signal["macro_direction_score"], 0.75)

    def test_signal_build_allows_short_macro_budget_above_one(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(200, 99, -2)) + [
                    102,
                    105,
                    108,
                    111,
                    114,
                    117,
                    120,
                    116,
                    112,
                    108,
                    104,
                ]
                benchmark_closes = [100.0] * len(closes)
                macro_context = self.macro_context(
                    regime="crypto_underperforms_gold",
                    long_budget_multiplier=0.55,
                    short_budget_multiplier=1.20,
                    macro_direction_score=-0.75,
                )

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "neutral",
                    },
                    latest_ts=1000,
                    macro_context=macro_context,
                )

                self.assertIsNotNone(signal)
                self.assertAlmostEqual(signal["macro_budget_multiplier"], 1.20)
                self.assertAlmostEqual(signal["budget_multiplier"], 1.20)
                self.assertAlmostEqual(signal["macro_direction_score"], -0.75)

    def test_ema_short_entry_signal_is_valid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = list(range(200, 99, -2)) + [
                    102,
                    105,
                    108,
                    111,
                    114,
                    117,
                    120,
                    116,
                    112,
                    108,
                    104,
                ]
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
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
                closes = list(range(150, 99, -2)) + [
                    102,
                    105,
                    108,
                    111,
                    114,
                    117,
                    120,
                    118,
                    116,
                    114,
                ]
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertFalse(signal["macro_valid"])
                self.assertFalse(signal["entry_valid"])

    def test_ema_router_routes_same_market_to_one_profile_side(self):
        closes = list(range(200, 99, -2)) + [
            102,
            105,
            108,
            111,
            114,
            117,
            120,
            116,
            112,
            108,
            104,
        ]
        benchmark_closes = [100.0] * len(closes)

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))

                long_signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                )

                self.assertIsNotNone(long_signal)
                self.assertEqual(long_signal["ema_side"], "short")
                self.assertFalse(long_signal["ema_side_valid"])
                self.assertFalse(long_signal["direction_valid"])
                self.assertFalse(long_signal["entry_valid"])

        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            strategy = self.ema_test_strategy()
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))

                short_signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                )

                self.assertIsNotNone(short_signal)
                self.assertEqual(short_signal["ema_side"], "short")
                self.assertTrue(short_signal["ema_side_valid"])
                self.assertTrue(short_signal["direction_valid"])
                self.assertTrue(short_signal["entry_valid"])

    def test_daily_volatility_falls_back_to_neutral_without_history(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                daily_volatility_window=220,
                daily_volatility_reference=0.020,
                enable_volatility_targeted_sizing=True,
                min_volatility_budget_multiplier=0.65,
                max_volatility_budget_multiplier=1.50,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = [100.0] * 120
                benchmark_closes = [100.0] * len(closes)

                signal = bot._build_signal_from_closes(
                    closes,
                    benchmark_closes,
                    {
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "test",
                    },
                    latest_ts=1000,
                )

                self.assertIsNotNone(signal)
                self.assertEqual(signal["daily_volatility"], 0.0)
                self.assertEqual(signal["daily_volatility_multiplier"], 1.0)
                self.assertEqual(signal["volatility_budget_multiplier"], 1.0)

    def test_risk_budget_finish_method_updates_context(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)

            # Use bot._account_snapshot to trigger 'free_margin_below_reserve' condition early
            # which calls finish()
            with patch.object(
                bot, "_account_snapshot", return_value={"free": 0.0, "total": 0.0}
            ):
                budget, reason = bot._risk_budget(
                    SYMBOL,
                    state,
                    reference_price=10.0,
                    is_new_position=True,
                    signal=None,
                )

            self.assertEqual(budget, 0.0)
            self.assertEqual(reason, "free_margin_below_reserve")

            context = bot._last_risk_budget_context
            self.assertIsNotNone(context)
            self.assertEqual(context["free"], 0.0)
            self.assertEqual(context["equity"], 0.0)
            self.assertEqual(context["reserve"], config.RISK.min_quote_reserve)
            self.assertTrue(context["is_new_position"])
            self.assertEqual(context["budget_scale"], 1.0)

    def test_risk_budget_applies_volatility_budget_multiplier(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            buying = replace(config.BUYING, position_budget_fraction=0.02)
            with override_config(RUNTIME=config.RUNTIME, BUYING=buying):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)

                reduced_budget, reduced_reason = bot._risk_budget(
                    SYMBOL,
                    state,
                    reference_price=10.0,
                    is_new_position=True,
                    signal={
                        "budget_multiplier": 1.0,
                        "volatility_budget_multiplier": 0.50,
                    },
                )
                expanded_budget, expanded_reason = bot._risk_budget(
                    SYMBOL,
                    state,
                    reference_price=10.0,
                    is_new_position=True,
                    signal={
                        "budget_multiplier": 1.0,
                        "volatility_budget_multiplier": 1.50,
                    },
                )

                self.assertAlmostEqual(reduced_budget, 10.0)
                self.assertAlmostEqual(expanded_budget, 30.0)
                self.assertIn("effective_budget_multiplier=0.500", reduced_reason)
                self.assertIn("effective_budget_multiplier=1.500", expanded_reason)
                self.assertAlmostEqual(
                    bot._last_risk_budget_context["planned_notional"], 900.0
                )
                self.assertAlmostEqual(bot._last_risk_budget_context["free"], 1000.0)

    def test_risk_budget_counts_combined_profile_notional(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            risk = replace(
                config.RISK,
                leverage=10,
                max_total_notional_fraction=0.50,
                max_position_notional_fraction=1.0,
            )
            runtime = config.RUNTIME
            buying = replace(config.BUYING, position_budget_fraction=0.02)
            with override_config(RUNTIME=runtime, RISK=risk, BUYING=buying):
                bot = self.make_bot(Path(raw_tmp) / "long")
                other = self.make_bot(Path(raw_tmp) / "short")
                bot.account_pnl_bots = [bot, other]
                other.account_pnl_bots = [bot, other]
                other_state = other._get_state(SYMBOL)
                other_state.position_size = 490.0
                other_state.position_available = 490.0
                other_state.entry_price = 10.0

                budget, reason = bot._risk_budget(
                    SYMBOL,
                    bot._get_state(SYMBOL),
                    reference_price=10.0,
                    is_new_position=True,
                    signal={
                        "budget_multiplier": 1.0,
                        "volatility_budget_multiplier": 1.0,
                    },
                )

                # current_total_notional includes other_state.position_size (490) * 10 = 4900
                # equity (1000) * leverage (10) * max_total_notional_fraction (0.50) = 5000
                # remaining = 5000 - 4900 = 100
                self.assertAlmostEqual(budget * config.RISK.leverage, 100.0)
                self.assertIn("budget_scale=1.000", reason)

    def test_ema_averaging_respects_drawdown_threshold(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                averaging_drawdown_steps=(0.01, 0.02),
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.2, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.98, "ask": 10.0, "last": 9.99}

                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)

    def test_ema_averaging_atr_widens_drawdown_threshold(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                averaging_drawdown_steps=(0.005, 0.01),
                ema_averaging_atr_enabled=True,
                ema_averaging_atr_multiplier=1.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.2, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.9, "ask": 10.0, "last": 9.95}
                signal = self.entry_signal(ts=1000)
                signal["atr_rate"] = 0.03

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)

    def test_ema_averaging_hard_floor_blocks_zero_configured_drawdown(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                ema_averaging_drawdown_step=0.0,
                ema_averaging_min_drawdown_step=0.01,
                averaging_drawdown_steps=(0.0, 0.0),
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.2, "amount": 20.0}
                ]

                bot.exchange.ticker = {"bid": 9.95, "ask": 9.97, "last": 9.96}
                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))
                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)

                bot.exchange.ticker = {"bid": 9.88, "ask": 9.90, "last": 9.89}
                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1001))
                self.assertTrue(state.entry_orders)
                self.assertEqual(state.average_stage, 1)

    def test_ema_averaging_hard_atr_floor_applies_when_optional_atr_disabled(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                averaging_drawdown_steps=(0.01, 0.02),
                ema_averaging_atr_enabled=False,
                ema_averaging_min_atr_multiplier=1.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.2, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.80, "ask": 9.82, "last": 9.81}
                signal = self.entry_signal(ts=1000)
                signal["atr_rate"] = 0.03

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)

    def test_ema_averaging_requires_pullback_recovery_not_trigger_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                ema_averaging_require_pullback_recovery=True,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.average_stage = 1
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.75, "ask": 9.77, "last": 9.76}
                signal = self.entry_signal(ts=1000)
                signal["entry_valid"] = False
                signal["add_valid"] = True
                signal["trigger_valid"] = True
                signal["pullback_valid"] = False
                signal["pullback_recovery_gap"] = -0.002
                signal["pullback_recovery_min_gap"] = 0.001

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 1)
                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn(
                    "ema_averaging_pullback_recovery_required", rows[-1]["block_reason"]
                )

    def test_first_ema_averaging_can_bypass_pullback_when_btc_not_against(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                ema_averaging_require_pullback_recovery=True,
                averaging_drawdown_steps=(0.01, 0.02),
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.75, "ask": 9.77, "last": 9.76}
                signal = self.entry_signal(ts=1000)
                signal["entry_valid"] = False
                signal["add_valid"] = True
                signal["trigger_valid"] = True
                signal["pullback_valid"] = False
                signal["btc_return_30m"] = 0.0

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertTrue(state.entry_orders)
                self.assertEqual(state.average_stage, 1)

    def test_first_ema_averaging_still_requires_pullback_when_btc_against(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                ema_averaging_require_pullback_recovery=True,
                averaging_drawdown_steps=(0.01, 0.02),
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.75, "ask": 9.77, "last": 9.76}
                signal = self.entry_signal(ts=1000)
                signal["entry_valid"] = False
                signal["add_valid"] = True
                signal["trigger_valid"] = True
                signal["pullback_valid"] = False
                signal["btc_return_30m"] = -0.005

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)
                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertIn(
                    "ema_averaging_pullback_recovery_required", rows[-1]["block_reason"]
                )

    def test_ema_averaging_places_power_sized_entry_after_drawdown(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=8.0,
                ema_max_averaging_stages=2,
                averaging_drawdown_steps=(0.02, 0.04),
                ema_averaging_base_fraction=0.45,
                ema_averaging_power=0.80,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.balance_free = 10000.0
                bot.exchange.balance_total = 10000.0
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]

                signal = self.entry_signal(ts=1000)
                signal.update({"ladder_multiplier": 1.0, "budget_multiplier": 1.0})

                budget, reason = bot._ema_averaging_budget(
                    SYMBOL, state, reference_price=9.9
                )
                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertTrue(state.entry_orders)
                self.assertEqual(state.average_stage, 1)
                self.assertIsNotNone(state.last_average_at)
                self.assertAlmostEqual(
                    budget * config.RISK.leverage, 20.0 * 10.2 * 0.45
                )
                self.assertIn("ema_average_power=0.800", reason)

    def test_short_averaging_triggers_immediately_on_signal(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = config.RUNTIME
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                ema_max_averaging_stages=2,
                averaging_drawdown_steps=(0.005, 0.01),
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "buy", "price": 9.9, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.9, "ask": 10.1, "last": 10.0}

                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))

                self.assertTrue(state.entry_orders)
                self.assertEqual(state.average_stage, 1)

    def test_ema_averaging_is_blocked_by_adverse_external_directional_1m(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(config.STRATEGY, ema_averaging_interval_hours=0.0)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_price_feed = StaticExternalPriceFeed(
                    self.external_context(
                        spread_bps=0.0,
                        htx_change_1m_bps=-70.0,
                        mexc_change_1m_bps=-55.0,
                    )
                )
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]

                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)
                with bot.signal_analytics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["decision"], "averaging_checked")
                self.assertIn(
                    "external_directional_1m_blocked", rows[-1]["block_reason"]
                )

    def test_ema_averaging_uses_add_signal_even_if_full_entry_invalid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(config.STRATEGY, ema_averaging_interval_hours=0.0)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]
                signal = self.entry_signal(ts=1000)
                signal["entry_valid"] = False
                signal["add_valid"] = True

                bot._maybe_place_average_buy(SYMBOL, signal)

                self.assertTrue(state.entry_orders)
                self.assertEqual(state.average_stage, 1)

    def test_active_averaging_order_survives_when_add_signal_remains_valid(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.entry_orders = [
                    {
                        "id": "avg_1",
                        "side": config.ENTRY_SIDE,
                        "price": 9.8,
                        "amount": 5.0,
                        "created_at": time.time(),
                        "reason": "ema_averaging_stage_1",
                    }
                ]
                signal = self.entry_signal(ts=1000)
                signal["entry_valid"] = False
                signal["add_valid"] = True

                bot._manage_entry_orders(
                    SYMBOL,
                    signal,
                    open_orders=[
                        {
                            "id": "avg_1",
                            "symbol": SYMBOL,
                            "side": config.ENTRY_SIDE,
                            "amount": 5.0,
                            "remaining": 5.0,
                        }
                    ],
                )

                self.assertEqual(len(state.entry_orders), 1)
                self.assertEqual(state.entry_orders[0]["id"], "avg_1")

    def test_active_averaging_order_is_canceled_when_add_signal_breaks(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.entry_orders = [
                    {
                        "id": "avg_1",
                        "side": config.ENTRY_SIDE,
                        "price": 9.8,
                        "amount": 5.0,
                        "created_at": time.time(),
                        "reason": "ema_averaging_stage_1",
                    }
                ]
                signal = self.entry_signal(ts=1000)
                signal["entry_valid"] = False
                signal["add_valid"] = False

                bot._manage_entry_orders(SYMBOL, signal, open_orders=[])

                self.assertFalse(state.entry_orders)

    def test_ema_averaging_stage_thresholds_and_max_stage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(
                config.STRATEGY,
                ema_averaging_interval_hours=0.0,
                ema_max_averaging_stages=2,
                averaging_drawdown_steps=(0.02, 0.04),
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]

                # Averaging still requires the configured drawdown threshold.
                bot.exchange.ticker = {"bid": 9.79, "ask": 9.80, "last": 9.79}
                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))
                self.assertEqual(state.average_stage, 1)
                self.assertTrue(state.entry_orders)

                state.entry_orders = []
                state.last_average_at = None
                bot.exchange.ticker = {"bid": 9.59, "ask": 9.60, "last": 9.59}
                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1001))
                self.assertEqual(state.average_stage, 2)
                self.assertTrue(state.entry_orders)

                # Cooldown/interval and max stages still apply
                state.entry_orders = []
                # Don’t reset last_average_at to test interval
                bot.exchange.ticker = {"bid": 9.61, "ask": 9.62, "last": 9.61}
                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1002))
                self.assertEqual(state.average_stage, 2)
                self.assertEqual(state.entry_orders, [])

    def test_ema_averaging_position_fraction_uses_current_notional(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(
                config.STRATEGY,
                ema_averaging_base_fraction=0.45,
                ema_averaging_power=0.80,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.balance_free = 10000.0
                bot.exchange.balance_total = 10000.0
                state = bot._get_state(SYMBOL)
                state.position_size = 200.0
                state.position_available = 200.0
                state.entry_price = 10.0
                state.initial_entry_notional = 1000.0

                budget, reason = bot._ema_averaging_budget(
                    SYMBOL, state, reference_price=10.0
                )
                planned_notional = budget * config.RISK.leverage
                expected_notional = 0.45 * 2000.0

                expected_notional = 0.45 * 1000.0 * (2.0**0.8)
                self.assertAlmostEqual(planned_notional, expected_notional)
                self.assertIn("ratio=2.000000", reason)
                self.assertIn("ema_average_base_fraction=0.450", reason)

    def test_ema_averaging_is_blocked_after_breakeven(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.sell_ladder_mode = "breakeven"
                state.breakeven_activated_at = time.time()
                state.sell_ladder_orders = [
                    {"id": "be", "side": "sell", "price": 10.21, "amount": 20.0}
                ]

                bot._maybe_place_average_buy(
                    SYMBOL,
                    {"valid": True, "add_valid": True, "macro_valid": True, "ts": 1000},
                )

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)

    def test_ema_averaging_is_blocked_after_no_more_averaging_age(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(
                config.STRATEGY,
                ema_breakeven_enabled=False,
                no_more_averaging_after_minutes=48.0 * 60.0,
                ema_averaging_interval_hours=0.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.cycle_opened_at = time.time() - 49.0 * 60.0 * 60.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]

                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)

    def test_account_pnl_runtime_sampling_is_thread_safe(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                account_pnl_enabled=True,
                account_pnl_sample_interval_sec=0.0,
                account_pnl_window_minutes=60.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.entry_price = 10.0
                state.net_open_pnl = 1.0
                state.unrealized_pnl = 1.0

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    samples = list(
                        executor.map(
                            lambda _: bot._account_pnl_context(force_sample=True),
                            range(40),
                        )
                    )

                self.assertEqual(len(samples), 40)
                self.assertEqual(len(bot.account_pnl_runtime["history"]), 40)
                self.assertEqual(
                    max(sample["history_samples"] for sample in samples), 40
                )

    def test_account_profit_unload_places_reduce_only_partial_close(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                account_profit_unload_enabled=True,
                account_profit_unload_min_pnl_quote=5.0,
                account_profit_unload_min_pnl_rate=0.0,
                account_profit_unload_percentile=0.50,
                account_profit_unload_fraction=0.25,
                account_profit_unload_min_position_pnl_quote=0.50,
                account_profit_unload_min_position_pnl_rate=0.0,
                account_profit_unload_cooldown_sec=0.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 100.0
                state.position_available = 100.0
                state.entry_price = 100.0
                state.net_open_pnl = 10.0
                state.unrealized_pnl = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 101.0, "amount": 100.0}
                ]
                bot.exchange.ticker = {"bid": 101.0, "ask": 101.1, "last": 101.0}
                bot.account_pnl_runtime["history"] = [
                    {"ts": time.time() - 60.0, "open_pnl": 1.0},
                    {"ts": time.time() - 30.0, "open_pnl": 4.0},
                ]

                applied = bot._maybe_apply_account_profit_unload(
                    SYMBOL, self.entry_signal()
                )

                self.assertTrue(applied)
                self.assertIn(
                    ("tp", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 25.0)
                self.assertEqual(order["price"], 101.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertEqual(state.sell_ladder_mode, "account_unload")
                self.assertTrue(state.frozen_no_more_buys)

    def test_account_pnl_trailing_closes_all_positions_with_market_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                account_pnl_trailing_enabled=True,
                account_pnl_trailing_activation_rate=0.05,
                account_pnl_trailing_stop_rate=0.035,
                account_pnl_trailing_min_pnl_quote=0.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.symbols = [SYMBOL, SECOND_SYMBOL]
                bot.market_by_symbol[SECOND_SYMBOL] = SECOND_MARKET
                first = bot._get_state(SYMBOL)
                first.position_size = 10.0
                first.position_available = 10.0
                first.entry_price = 100.0
                first.net_open_pnl = 40.0
                first.unrealized_pnl = 40.0
                first.sell_ladder_orders = [
                    {"id": "tp1", "side": "sell", "price": 101.0, "amount": 10.0}
                ]
                second = bot._get_state(SECOND_SYMBOL)
                second.position_size = 10.0
                second.position_available = 10.0
                second.entry_price = 100.0
                second.net_open_pnl = 30.0
                second.unrealized_pnl = 30.0
                second.sell_ladder_orders = [
                    {"id": "tp2", "side": "sell", "price": 101.0, "amount": 10.0}
                ]
                now = time.time()
                bot.account_pnl_runtime["history"] = [
                    {
                        "ts": now - 60.0,
                        "open_pnl": 110.0,
                        "open_notional": 2000.0,
                        "open_pnl_rate": 0.055,
                    },
                    {
                        "ts": now - 30.0,
                        "open_pnl": 90.0,
                        "open_notional": 2000.0,
                        "open_pnl_rate": 0.045,
                    },
                ]
                bot.account_pnl_runtime["last_sample_at"] = now

                applied = bot._maybe_apply_account_pnl_trailing(
                    SYMBOL, self.entry_signal()
                )

                self.assertTrue(applied)
                self.assertEqual(len(bot.exchange.created_orders), 2)
                self.assertTrue(
                    all(
                        order["type"] == "market"
                        for order in bot.exchange.created_orders
                    )
                )
                self.assertTrue(
                    all(
                        order["params"].get("reduceOnly")
                        for order in bot.exchange.created_orders
                    )
                )
                self.assertEqual(
                    {order["symbol"] for order in bot.exchange.created_orders},
                    {SYMBOL, SECOND_SYMBOL},
                )
                self.assertIn(
                    ("tp1", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertIn(
                    ("tp2", SECOND_SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertEqual(first.sell_ladder_mode, "account_unload")
                self.assertEqual(second.sell_ladder_mode, "account_unload")

    def test_account_averaging_blocks_while_account_pnl_is_falling(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(
                config.STRATEGY,
                account_averaging_enabled=True,
                account_averaging_min_samples=3,
                account_averaging_near_trough_quote=50.0,
                account_averaging_falling_guard_quote=1.0,
                account_averaging_falling_guard_fraction=0.0,
                ema_averaging_interval_hours=0.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.net_open_pnl = -10.0
                state.unrealized_pnl = -10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.9, "ask": 10.0, "last": 9.9}
                now = time.time()
                bot.account_pnl_runtime["history"] = [
                    {"ts": now - 90.0, "open_pnl": -2.0},
                    {"ts": now - 60.0, "open_pnl": -5.0},
                    {"ts": now - 30.0, "open_pnl": -10.0},
                ]
                bot.account_pnl_runtime["last_sample_at"] = now

                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))

                self.assertFalse(state.entry_orders)
                self.assertEqual(state.average_stage, 0)

    def test_account_averaging_allows_bounce_near_trough_and_scales_budget(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = config.RUNTIME
            strategy = replace(
                config.STRATEGY,
                account_averaging_enabled=True,
                account_averaging_min_samples=3,
                account_averaging_near_trough_quote=6.0,
                account_averaging_bounce_quote=1.0,
                account_averaging_falling_guard_quote=1.0,
                account_averaging_falling_guard_fraction=0.0,
                account_averaging_budget_scale=0.25,
                ema_averaging_interval_hours=0.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 20.0
                state.position_available = 20.0
                state.entry_price = 10.2
                state.net_open_pnl = -8.0
                state.unrealized_pnl = -8.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 10.3, "amount": 20.0}
                ]
                bot.exchange.ticker = {"bid": 9.9, "ask": 10.0, "last": 9.9}
                now = time.time()
                bot.account_pnl_runtime["history"] = [
                    {"ts": now - 90.0, "open_pnl": -12.0},
                    {"ts": now - 60.0, "open_pnl": -10.0},
                    {"ts": now - 30.0, "open_pnl": -8.0},
                ]
                bot.account_pnl_runtime["last_sample_at"] = now

                bot._maybe_place_average_buy(SYMBOL, self.entry_signal(ts=1000))

                self.assertTrue(state.entry_orders)
                self.assertEqual(state.average_stage, 1)
                self.assertIn(
                    "account_budget_scale=0.250", state.entry_orders[0]["reason"]
                )
                self.assertAlmostEqual(
                    state.planned_quote_budget * config.RISK.leverage,
                    20.0 * 10.2 * 0.50 * 0.25,
                )

    def test_ema_breakeven_activates_without_market_or_stop_order(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                ema_breakeven_after_hours=48.0,
                ema_breakeven_fee_buffer=0.0002,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 48.1 * 60.0 * 60.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 101.0, "amount": 10.0}
                ]

                applied = bot._maybe_apply_time_based_exit(
                    SYMBOL, signal={"valid": True, "add_valid": True}
                )

                self.assertTrue(applied)
                self.assertEqual(state.sell_ladder_mode, "breakeven")
                self.assertTrue(state.frozen_no_more_buys)
                self.assertIsNotNone(state.breakeven_activated_at)
                self.assertIn(
                    ("tp", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "limit")
                self.assertEqual(order["side"], "sell")
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertFalse(
                    any(
                        item["type"].startswith("stop") or item["type"] == "market"
                        for item in bot.exchange.created_orders
                    )
                )

    def test_hard_stop_loss_places_reduce_only_tpsl_for_long_position(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.05,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0

                placed = bot._ensure_hard_stop_loss(SYMBOL)
                placed_again = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(placed)
                self.assertFalse(placed_again)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 10.0)
                self.assertIsNone(order["price"])
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertEqual(order["params"].get("stopLossPrice"), 95.0)
                self.assertEqual(state.hard_stop_order["id"], order["id"])
                self.assertEqual(
                    state.hard_stop_order["cancel_params"], {"stopLossTakeProfit": True}
                )

    def test_hard_stop_loss_uses_atr_cap_when_signal_volatility_is_wider(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=True,
                hard_stop_loss_atr_multiplier=2.0,
                hard_stop_loss_atr_max_pct=0.03,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0

                bot._ensure_hard_stop_loss(SYMBOL, signal={"atr_rate": 0.025})

                order = bot.exchange.created_orders[0]
                self.assertEqual(order["params"].get("stopLossPrice"), 97.0)
                self.assertAlmostEqual(state.hard_stop_order["loss_rate"], 0.03)

    def test_hard_stop_loss_uses_wider_emergency_floor(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.04,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0

                placed = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(placed)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["params"].get("stopLossPrice"), 96.0)
                self.assertAlmostEqual(state.hard_stop_order["loss_rate"], 0.04)

    def test_emergency_hard_stop_full_closes_only_after_wider_floor_is_crossed(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.04,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                bot.exchange.ticker = {"bid": 95.9, "ask": 96.0, "last": 95.95}
                bot.exchange.reject_stop_loss_trigger_crossed = True

                placed = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(placed)
                self.assertEqual(state.sell_ladder_mode, "hard_stop_loss")
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 10.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertNotIn("stopLossPrice", order["params"])

    def test_soft_defensive_exit_waits_when_short_signal_is_alive_and_btc_not_against(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                soft_defensive_exit_enabled=True,
                soft_defensive_exit_min_drawdown=0.02,
                soft_defensive_exit_btc_against_return=0.003,
                soft_defensive_exit_confirmations=2,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.position_side = "short"
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "buy", "price": 9.9, "amount": 10.0}
                ]
                bot.exchange.ticker = {"bid": 10.29, "ask": 10.31, "last": 10.30}
                signal = self.entry_signal(ts=1000)
                signal["pullback_valid"] = False
                signal["pullback_recovery_gap"] = 0.013
                signal["btc_return_30m"] = -0.001

                applied = bot._maybe_apply_soft_defensive_exit(SYMBOL, signal)

                self.assertFalse(applied)
                self.assertEqual(state.sell_ladder_mode, "normal")
                self.assertEqual(state.sell_ladder_orders[0]["id"], "tp")
                self.assertEqual(bot.exchange.created_orders, [])

    def test_soft_defensive_exit_places_partial_after_signal_and_btc_confirm(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                soft_defensive_exit_enabled=True,
                soft_defensive_exit_min_drawdown=0.02,
                soft_defensive_exit_btc_against_return=0.003,
                soft_defensive_exit_confirmations=2,
                soft_defensive_exit_initial_fraction=0.33,
                soft_defensive_exit_step_fraction=0.33,
                soft_defensive_exit_max_fraction=1.0,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.position_side = "short"
                state.entry_price = 10.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "buy", "price": 9.9, "amount": 10.0}
                ]
                bot.exchange.ticker = {"bid": 10.29, "ask": 10.31, "last": 10.30}
                signal = self.entry_signal(ts=1000)
                signal["trigger_valid"] = False
                signal["btc_return_30m"] = 0.006

                waiting = bot._maybe_apply_soft_defensive_exit(SYMBOL, signal)
                self.assertTrue(waiting)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(state.soft_defensive_consecutive_signals, 1)

                signal["ts"] = 1001
                placed = bot._maybe_apply_soft_defensive_exit(SYMBOL, signal)

                self.assertTrue(placed)
                self.assertIn(
                    ("tp", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertEqual(state.sell_ladder_mode, "soft_defensive_exit")
                self.assertEqual(state.soft_defensive_consecutive_signals, 2)
                self.assertAlmostEqual(state.soft_defensive_exit_fraction, 0.33)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "limit")
                self.assertEqual(order["side"], "buy")
                self.assertEqual(order["amount"], 3.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertEqual(
                    state.sell_ladder_orders[0]["reason"], "soft_defensive_exit_partial"
                )

    def test_soft_defensive_wait_clears_freeze_when_signal_recovers_before_order(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                soft_defensive_exit_enabled=True,
                soft_defensive_exit_min_drawdown=0.02,
                soft_defensive_exit_btc_against_return=0.003,
                soft_defensive_exit_confirmations=2,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.position_side = "short"
                state.entry_price = 10.0
                bot.exchange.ticker = {"bid": 10.29, "ask": 10.31, "last": 10.30}
                signal = self.entry_signal(ts=1000)
                signal["trigger_valid"] = False
                signal["btc_return_30m"] = 0.006

                waiting = bot._maybe_apply_soft_defensive_exit(SYMBOL, signal)

                self.assertTrue(waiting)
                self.assertTrue(state.frozen_no_more_buys)
                self.assertEqual(state.soft_defensive_consecutive_signals, 1)

                signal["ts"] = 1001
                signal["trigger_valid"] = True
                signal["btc_return_30m"] = -0.001
                recovered = bot._maybe_apply_soft_defensive_exit(SYMBOL, signal)

                self.assertFalse(recovered)
                self.assertFalse(state.frozen_no_more_buys)
                self.assertEqual(state.soft_defensive_consecutive_signals, 0)
                self.assertEqual(bot.exchange.created_orders, [])

    def test_soft_defensive_exit_clears_when_confluence_recovers(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                soft_defensive_exit_enabled=True,
                soft_defensive_exit_min_drawdown=0.02,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.position_side = "short"
                state.entry_price = 10.0
                state.sell_ladder_mode = "soft_defensive_exit"
                state.sell_ladder_orders = [
                    {
                        "id": "soft_exit",
                        "side": "buy",
                        "price": 10.3,
                        "amount": 3.0,
                        "mode": "soft_defensive_exit",
                        "reason": "soft_defensive_exit_partial",
                    }
                ]
                state.frozen_no_more_buys = True
                bot.exchange.ticker = {"bid": 10.29, "ask": 10.31, "last": 10.30}
                signal = self.entry_signal(ts=1002)
                signal["btc_return_30m"] = -0.001

                cleared = bot._maybe_apply_soft_defensive_exit(SYMBOL, signal)

                self.assertTrue(cleared)
                self.assertEqual(state.sell_ladder_mode, "normal")
                self.assertFalse(state.sell_ladder_orders)
                self.assertFalse(state.frozen_no_more_buys)
                self.assertIn(
                    ("soft_exit", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )

    def test_hard_stop_loss_mirrors_trigger_for_short_position(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.05,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0

                bot._ensure_hard_stop_loss(SYMBOL)

                order = bot.exchange.created_orders[0]
                self.assertEqual(order["side"], "buy")
                self.assertEqual(order["params"].get("stopLossPrice"), 105.0)

    def test_hard_stop_loss_cancels_tp_ladder_and_retries_when_closeable_is_reserved(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 101.0, "amount": 10.0}
                ]
                original_create_order = bot.exchange.create_order
                calls = {"count": 0}

                def reject_first_stop(symbol, type, side, amount, price, params=None):
                    if params and params.get("stopLossPrice") and calls["count"] == 0:
                        calls["count"] += 1
                        raise RuntimeError(
                            'htx {"status":"error","err_code":1492,'
                            '"err_msg":"Amount of Reduce Only order exceeds the amount available to close."}'
                        )
                    return original_create_order(
                        symbol, type, side, amount, price, params=params
                    )

                bot.exchange.create_order = reject_first_stop

                placed = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(placed)
                self.assertEqual(calls["count"], 1)
                self.assertIn(
                    ("tp", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(
                    state.hard_stop_order["id"], bot.exchange.created_orders[0]["id"]
                )

    def test_hard_stop_loss_crossed_trigger_switches_to_reduce_only_market_close(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 0.0
                state.position_frozen = 10.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [
                    {"id": "tp", "side": "sell", "price": 101.0, "amount": 10.0}
                ]
                bot.exchange.reject_stop_loss_trigger_crossed = True

                placed = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(placed)
                self.assertTrue(state.frozen_no_more_buys)
                self.assertEqual(state.sell_ladder_mode, "hard_stop_loss")
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertIn(
                    ("tp", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 10.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertNotIn("stopLossPrice", order["params"])
                self.assertEqual(state.hard_stop_order["id"], order["id"])
                self.assertTrue(state.hard_stop_order["market_close"])
                self.assertEqual(
                    state.hard_stop_order["reason"], "hard_stop_loss_market_close"
                )

                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                crossed = [
                    row
                    for row in rows
                    if row["event"] == "hard_stop_loss_trigger_crossed"
                ]
                self.assertTrue(crossed)
                self.assertEqual(crossed[-1]["error_code"], "1407")
                self.assertTrue(
                    any(
                        row["event"] == "hard_stop_loss_market_close_placed"
                        for row in rows
                    )
                )
                self.assertFalse(
                    any(
                        row["reason"].startswith("hard_stop_loss_order_rejected")
                        for row in rows
                    )
                )

    def test_hard_stop_loss_spaced_htx_1407_switches_to_reduce_only_market_close(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 4.0
                state.position_available = 4.0
                state.entry_price = 100.0
                original_create_order = bot.exchange.create_order

                def reject_stop_with_spaced_code(
                    symbol, type, side, amount, price, params=None
                ):
                    params = params or {}
                    if "stopLossPrice" in params:
                        raise RuntimeError(
                            'htx {"status":"error","err_code": 1407,'
                            '"err_msg":"The stop loss price shall not be >= 98USDT"}'
                        )
                    return original_create_order(
                        symbol, type, side, amount, price, params=params
                    )

                bot.exchange.create_order = reject_stop_with_spaced_code

                placed = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(placed)
                self.assertEqual(state.sell_ladder_mode, "hard_stop_loss")
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertEqual(order["amount"], 4.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertNotIn("stopLossPrice", order["params"])
                self.assertTrue(state.hard_stop_order["market_close"])

    def test_hard_stop_loss_crossed_trigger_market_close_mirrors_short_side(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 7.0
                state.position_available = 7.0
                state.entry_price = 100.0
                bot.exchange.reject_stop_loss_trigger_crossed = True

                placed = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(placed)
                self.assertEqual(state.sell_ladder_mode, "hard_stop_loss")
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["side"], "buy")
                self.assertEqual(order["amount"], 7.0)
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertNotIn("stopLossPrice", order["params"])
                self.assertEqual(state.hard_stop_order["id"], order["id"])
                self.assertTrue(state.hard_stop_order["market_close"])

    def test_pending_hard_stop_market_close_is_not_duplicated_before_position_sync(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(
                config.RUNTIME, reduce_only_enabled=True, poll_interval_sec=5.0
            )
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 0.0
                state.position_frozen = 10.0
                state.entry_price = 100.0
                bot.exchange.reject_stop_loss_trigger_crossed = True

                first = bot._ensure_hard_stop_loss(SYMBOL)
                second = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(first)
                self.assertTrue(second)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.create_order_calls, 2)
                self.assertTrue(state.hard_stop_order["market_close"])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(
                        row["event"] == "hard_stop_loss_market_close_pending"
                        for row in rows
                    )
                )

    def test_hard_stop_loss_market_close_retries_after_closeable_rejection(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 6.0
                state.position_available = 0.0
                state.position_frozen = 6.0
                state.entry_price = 100.0
                bot.exchange.reject_stop_loss_trigger_crossed = True
                bot.exchange.reject_reduce_only_closeable_amount = True

                first = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(first)
                self.assertEqual(state.sell_ladder_mode, "hard_stop_loss")
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 2)

                bot.exchange.reject_reduce_only_closeable_amount = False
                second = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertTrue(second)
                self.assertEqual(bot.exchange.create_order_calls, 3)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertNotIn(
                    "stopLossPrice", bot.exchange.created_orders[0]["params"]
                )

    def test_hard_stop_loss_rejection_logs_original_exception(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                bot.exchange.reject_reduce_only_closeable_amount = True

                placed = bot._ensure_hard_stop_loss(SYMBOL)

                self.assertFalse(placed)
                self.assertTrue(state.frozen_no_more_buys)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                errors = [
                    row
                    for row in rows
                    if row["event"] == "reduce_only_violation_prevented"
                    and row["reason"].startswith("hard_stop_loss_order_rejected")
                ]
                self.assertTrue(errors)
                self.assertEqual(errors[-1]["exception_type"], "RuntimeError")
                self.assertEqual(errors[-1]["error_code"], "1492")
                self.assertIn(
                    "Amount of Reduce Only order exceeds", errors[-1]["message"]
                )

    def test_ema_pullback_default_matches_fast_trigger_router(self):
        with config.use_profile("long"):
            self.assertEqual(config.STRATEGY.ema_pullback_timeframe, "5m")
            self.assertEqual(config.STRATEGY.ema_pullback_fast_minutes, 120)
            self.assertEqual(config.STRATEGY.ema_pullback_slow_minutes, 360)
            self.assertFalse(config.STRATEGY.ema_entry_require_pullback_recovery)

    def test_ema_live_launch_averaging_defaults_are_conservative(self):
        with config.use_profile("long"):
            self.assertEqual(config.STRATEGY.ema_averaging_drawdown_step, 0.01)
            self.assertEqual(config.STRATEGY.ema_averaging_min_drawdown_step, 0.01)
            self.assertEqual(config.STRATEGY.averaging_drawdown_steps, (0.01, 0.02))
            self.assertEqual(config.STRATEGY.ema_averaging_base_fraction, 0.50)
            self.assertEqual(config.STRATEGY.ema_averaging_power, 1.0)
            self.assertFalse(config.STRATEGY.ema_averaging_atr_enabled)
            self.assertEqual(config.STRATEGY.ema_averaging_atr_period, 14)
            self.assertEqual(config.STRATEGY.ema_averaging_atr_multiplier, 1.0)
            self.assertEqual(config.STRATEGY.ema_averaging_min_atr_multiplier, 1.0)
            self.assertEqual(
                config.STRATEGY.ema_averaging_min_daily_volatility_fraction, 0.18
            )
            self.assertTrue(config.STRATEGY.ema_averaging_require_pullback_recovery)
            self.assertEqual(config.STRATEGY.ema_max_averaging_stages, 2)
            self.assertEqual(config.STRATEGY.max_buy_stages, 3)
            self.assertTrue(config.STRATEGY.ema_exit_runner_enabled)
            self.assertTrue(config.STRATEGY.ema_exit_trailing_enabled)
            self.assertEqual(config.STRATEGY.ema_exit_trailing_fixed_fraction, 0.30)
            self.assertEqual(config.STRATEGY.ema_exit_trailing_atr_multiplier, 1.5)
            self.assertEqual(config.STRATEGY.ema_exit_trailing_min_pullback, 0.006)
            self.assertEqual(config.STRATEGY.ema_exit_trailing_max_pullback, 0.030)
            self.assertTrue(config.STRATEGY.ema_exit_runner_profit_lock_enabled)
            self.assertTrue(config.STRATEGY.ema_exit_runner_use_aggressive_limit)
            self.assertFalse(config.STRATEGY.account_profit_unload_enabled)
            self.assertFalse(config.STRATEGY.account_pnl_trailing_enabled)
            self.assertFalse(config.STRATEGY.account_averaging_enabled)
            self.assertTrue(config.STRATEGY.entry_spread_filter_enabled)
            self.assertEqual(config.STRATEGY.entry_spread_filter_max_bps, 30.0)
            self.assertTrue(config.STRATEGY.ema_chop_filter_enabled)
            self.assertEqual(config.STRATEGY.ema_chop_period, 14)
            self.assertEqual(config.STRATEGY.ema_chop_max, 61.8)
            self.assertTrue(config.STRATEGY.ema_volume_confirmation_enabled)
            self.assertEqual(config.STRATEGY.ema_volume_short_window, 5)
            self.assertEqual(config.STRATEGY.ema_volume_long_window, 20)
            self.assertEqual(config.STRATEGY.ema_volume_min_ratio, 1.05)
            self.assertTrue(config.STRATEGY.ema_volume_spike_filter_enabled)
            self.assertEqual(config.STRATEGY.ema_volume_spike_window, 5)
            self.assertEqual(config.STRATEGY.ema_volume_spike_min_ratio, 1.80)
            self.assertEqual(config.STRATEGY.ema_volume_adverse_spike_min_ratio, 2.00)
            self.assertTrue(config.STRATEGY.ema_volume_profile_filter_enabled)
            self.assertEqual(config.STRATEGY.ema_volume_profile_window, 60)
            self.assertEqual(config.STRATEGY.ema_volume_profile_bins, 12)
            self.assertEqual(config.STRATEGY.ema_volume_profile_value_area, 0.70)
            self.assertEqual(config.STRATEGY.hard_time_exit_after_minutes, 96.0 * 60.0)
            self.assertEqual(config.STRATEGY.hard_time_exit_close_fraction, 0.25)
            self.assertEqual(config.STRATEGY.hard_time_exit_fraction_step, 0.25)
            self.assertEqual(config.STRATEGY.hard_time_exit_max_loss_on_notional, 0.03)
            self.assertTrue(config.STRATEGY.hard_time_exit_bypass_profit_bank)
            self.assertEqual(config.STRATEGY.controlled_loss_min_move_fraction, 0.10)
            self.assertEqual(config.STRATEGY.controlled_loss_ramp_minutes, 24.0 * 60.0)
            self.assertEqual(config.STRATEGY.controlled_loss_reprice_minutes, 60.0)
            self.assertEqual(config.STRATEGY.controlled_loss_macro_gap_reference, 0.02)
            self.assertEqual(
                config.STRATEGY.controlled_loss_macro_max_speed_multiplier, 2.0
            )
            self.assertTrue(config.STRATEGY.controlled_loss_volatility_speed_enabled)
            self.assertEqual(config.STRATEGY.controlled_loss_volatility_reference, 0.0)
            self.assertEqual(
                config.STRATEGY.controlled_loss_volatility_trigger_multiplier, 1.5
            )
            self.assertEqual(
                config.STRATEGY.controlled_loss_volatility_max_speed_multiplier, 3.0
            )
            self.assertEqual(config.STRATEGY.controlled_loss_volatility_exponent, 2.0)
            self.assertEqual(
                config.STRATEGY.controlled_loss_volatility_reprice_min_move_delta, 0.05
            )
            self.assertTrue(config.STRATEGY.hard_stop_loss_enabled)
            self.assertEqual(config.STRATEGY.hard_stop_loss_pct, 0.02)
            self.assertEqual(config.STRATEGY.hard_stop_loss_min_emergency_pct, 0.04)
            self.assertTrue(config.STRATEGY.hard_stop_loss_atr_enabled)
            self.assertEqual(config.STRATEGY.hard_stop_loss_atr_multiplier, 2.0)
            self.assertEqual(config.STRATEGY.hard_stop_loss_atr_max_pct, 0.03)
            self.assertTrue(config.STRATEGY.soft_defensive_exit_enabled)
            self.assertEqual(config.STRATEGY.soft_defensive_exit_min_drawdown, 0.02)
            self.assertEqual(
                config.STRATEGY.soft_defensive_exit_btc_against_return, 0.003
            )
            self.assertEqual(config.STRATEGY.soft_defensive_exit_confirmations, 2)
            self.assertEqual(config.STRATEGY.soft_defensive_exit_initial_fraction, 0.33)
            self.assertEqual(config.STRATEGY.soft_defensive_exit_step_fraction, 0.33)
            self.assertEqual(config.STRATEGY.soft_defensive_exit_max_fraction, 1.0)
            self.assertEqual(config.STRATEGY.soft_defensive_exit_reprice_minutes, 6.0)
            self.assertFalse(config.EXTERNAL_PRICE_FEED.exit_adjustment_enabled)

    def test_ema_large_periods_convert_to_configured_timeframes(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            periods = bot._ema_periods(converted=True)

            self.assertEqual(config.STRATEGY.ema_macro_timeframe, "1h")
            self.assertEqual(config.STRATEGY.ema_pullback_timeframe, "5m")
            self.assertEqual(config.STRATEGY.ema_trigger_timeframe, "5m")
            self.assertFalse(config.STRATEGY.ema_entry_require_pullback_recovery)
            self.assertEqual(periods["ema_macro_fast"], 48)
            self.assertEqual(periods["ema_macro_slow"], 120)
            self.assertEqual(periods["ema_pullback_fast"], 24)
            self.assertEqual(periods["ema_pullback_slow"], 72)
            self.assertEqual(periods["ema_trigger_fast"], 24)
            self.assertEqual(periods["ema_trigger_slow"], 72)
            self.assertEqual(
                bot._ema_pullback_recovery_windows(converted=True), (144, 36)
            )
            self.assertEqual(bot._ema_required_history("pullback", converted=True), 216)

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
                    80.0,
                    82.0,
                    84.0,
                    86.0,
                    88.0,
                    90.0,
                    89.0,
                    90.0,
                    91.0,
                    92.0,
                    93.0,
                    94.0,
                    95.0,
                    100.0,
                    102.0,
                    104.0,
                    106.0,
                    108.0,
                    110.0,
                    112.0,
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
                self.assertTrue(
                    math.isclose(signal["rs30"], math.log(112.0 / 100.0), rel_tol=1e-12)
                )
                self.assertTrue(
                    math.isclose(signal["rs60"], math.log(112.0 / 90.0), rel_tol=1e-12)
                )
                self.assertEqual(signal["btc_return_30m"], 0.0)

    def test_signal_builder_handles_missing_macro_context(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
                ema_pullback_recovery_gap=0.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                closes = [100.0 + index for index in range(80)]
                benchmark = [100.0] * len(closes)
                ctx = SignalContext(
                    closes=closes,
                    benchmark_closes=benchmark,
                    btc_risk={
                        "budget_multiplier": 1.0,
                        "ladder_multiplier": 1.0,
                        "reason": "neutral",
                    },
                    latest_ts=1000,
                    candles=ohlcv_series(
                        closes, volumes=[1.0] * (len(closes) - 5) + [3.0] * 5
                    ),
                    cache_key=SYMBOL,
                    macro_context=None,
                )

                signal = bot._build_signal_from_closes(ctx)

                self.assertIsNotNone(signal)
                self.assertTrue(signal["add_valid"])
                self.assertFalse(any(key.startswith("frozen_") for key in signal))

    def test_signal_update_fetches_higher_timeframe_ema_history(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            trigger_closes = (
                [100.0 + index * 0.20 for index in range(220)]
                + [144.0 - index * 0.60 for index in range(35)]
                + [123.0 + index * 1.00 for index in range(45)]
            )
            macro_closes = [100.0 + index for index in range(140)]

            trigger_start = 1_760_000_000_000
            trigger_latest = trigger_start + (len(trigger_closes) - 1) * 5 * 60 * 1000
            macro_start = trigger_latest - (len(macro_closes) - 1) * 60 * 60 * 1000

            trigger_volumes = [1.0] * (len(trigger_closes) - 5) + [3.0] * 5
            bot.exchange.ohlcv[(SYMBOL, "5m")] = ohlcv_series(
                trigger_closes,
                5 * 60,
                trigger_start,
                volumes=trigger_volumes,
            )
            bot.exchange.ohlcv[(SYMBOL, "1h")] = ohlcv_series(
                macro_closes, 60 * 60, macro_start
            )

            updated = bot._update_signal_cache_if_needed()

            self.assertTrue(updated)
            signal = bot.signal_cache["symbols"][SYMBOL]
            self.assertTrue(signal["entry_valid"])
            self.assertEqual(signal["ema_side"], "long")
            self.assertEqual(signal["ema_macro_timeframe"], "1h")
            self.assertEqual(signal["ema_pullback_timeframe"], "5m")
            self.assertEqual(signal["ema_trigger_timeframe"], "5m")
            limits = {}
            for call in bot.exchange.ohlcv_calls:
                timeframe = call["timeframe"]
                limits[timeframe] = max(
                    limits.get(timeframe, 0), int(call["limit"] or 0)
                )
            self.assertLess(limits["1h"], 200)
            self.assertLessEqual(limits["5m"], 1500)

    def test_signal_update_fetches_volume_profile_history(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_macro_timeframe="1m",
                ema_pullback_timeframe="1m",
                ema_trigger_timeframe="1m",
                ema_macro_fast_minutes=5,
                ema_macro_slow_minutes=10,
                ema_pullback_fast_minutes=3,
                ema_pullback_slow_minutes=8,
                ema_trigger_fast_minutes=5,
                ema_trigger_slow_minutes=10,
                ema_chop_filter_enabled=False,
                ema_volume_confirmation_enabled=True,
                ema_volume_short_window=5,
                ema_volume_long_window=20,
                ema_volume_profile_filter_enabled=True,
                ema_volume_profile_window=160,
                ema_volume_spike_filter_enabled=True,
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
                ema_pullback_recovery_gap=0.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.benchmark_symbol = BTC_SYMBOL
                bot.market_by_symbol = {SYMBOL: MARKET, BTC_SYMBOL: BTC_MARKET}
                closes = [100.0 + index * 0.1 for index in range(170)]
                bot.exchange.ohlcv[(BTC_SYMBOL, "1m")] = ohlcv_series([100.0] * 170)
                bot.exchange.ohlcv[(SYMBOL, "1m")] = ohlcv_series(
                    closes,
                    volumes=[10.0] * 165 + [30.0] * 5,
                )

                updated = bot._update_signal_cache_if_needed()

                self.assertTrue(updated)
                symbol_limits = [
                    int(call["limit"] or 0)
                    for call in bot.exchange.ohlcv_calls
                    if call["symbol"] == SYMBOL and call["timeframe"] == "1m"
                ]
                self.assertTrue(symbol_limits)
                self.assertGreaterEqual(
                    max(symbol_limits), strategy.ema_volume_profile_window
                )
                self.assertIn(SYMBOL, bot.signal_cache["symbols"])

    def test_signal_update_serializes_exchange_candle_fetches(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, market_data_max_workers=4)
            strategy = self.ema_test_strategy(
                ema_macro_timeframe="1m",
                ema_pullback_timeframe="1m",
                ema_trigger_timeframe="1m",
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                symbols = [
                    SYMBOL,
                    SECOND_SYMBOL,
                    "ALT3/USDT:USDT",
                    "ALT4/USDT:USDT",
                    "ALT5/USDT:USDT",
                    "ALT6/USDT:USDT",
                ]
                bot.benchmark_symbol = BTC_SYMBOL
                bot.symbols = list(symbols)
                bot.entry_symbols = set(symbols)

                closes = [100.0 + index * 0.1 for index in range(90)]
                bot.exchange.ohlcv[(BTC_SYMBOL, "1m")] = ohlcv_series([100.0] * 90)
                for index, symbol in enumerate(symbols):
                    bot.exchange.ohlcv[(symbol, "1m")] = ohlcv_series(
                        [price + index for price in closes]
                    )

                original_fetch_ohlcv = bot.exchange.fetch_ohlcv
                active = {"count": 0, "max": 0}
                lock = threading.Lock()

                def slow_fetch_ohlcv(
                    symbol, timeframe="1m", since=None, limit=None, params=None
                ):
                    with lock:
                        active["count"] += 1
                        active["max"] = max(active["max"], active["count"])
                    try:
                        time.sleep(0.03)
                        return original_fetch_ohlcv(
                            symbol,
                            timeframe=timeframe,
                            since=since,
                            limit=limit,
                            params=params,
                        )
                    finally:
                        with lock:
                            active["count"] -= 1

                bot.exchange.fetch_ohlcv = slow_fetch_ohlcv

                updated = bot._update_signal_cache_if_needed()

                self.assertTrue(updated)
                self.assertEqual(active["max"], 1)
                self.assertEqual(set(bot.signal_cache["symbols"]), set(symbols))

    def test_parallel_signal_fetch_preserves_profile_context(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            runtime = replace(config.RUNTIME, market_data_max_workers=2)
            strategy = self.ema_test_strategy(
                ema_macro_timeframe="1m",
                ema_pullback_timeframe="1m",
                ema_trigger_timeframe="1m",
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.benchmark_symbol = BTC_SYMBOL
                bot.symbols = [SYMBOL, SECOND_SYMBOL]
                bot.entry_symbols = set(bot.symbols)
                candles_by_symbol = {
                    BTC_SYMBOL: ohlcv_series([100.0] * 90),
                    SYMBOL: ohlcv_series([100.0 + index * 0.1 for index in range(90)]),
                    SECOND_SYMBOL: ohlcv_series(
                        [120.0 + index * 0.1 for index in range(90)]
                    ),
                }
                seen_sides = []
                lock = threading.Lock()

                def fake_closed_candles(
                    symbol, limit, max_ts=None, timeframe=None, exchange=None
                ):
                    with lock:
                        seen_sides.append(config.POSITION_SIDE)
                    rows = candles_by_symbol[symbol]
                    if max_ts is not None:
                        rows = [row for row in rows if int(row[0]) <= max_ts]
                    return rows[-int(limit) :]

                bot._closed_candles = fake_closed_candles

                bot._update_signal_cache_if_needed()

                self.assertTrue(seen_sides)
                self.assertEqual(set(seen_sides), {"short"})

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
                for held_minutes, expected_contracts in (
                    (480.0, 25.0),
                    (540.0, 50.0),
                    (660.0, 100.0),
                ):
                    state.cycle_opened_at = now - held_minutes * 60.0
                    contracts = bot._controlled_loss_contracts(
                        SYMBOL,
                        state,
                        reference_price=9.0,
                        had_sell_ladder=False,
                    )
                    self.assertEqual(contracts, expected_contracts)

    def test_controlled_loss_move_fraction_accelerates_with_opposite_macro_gap(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                controlled_loss_min_move_fraction=0.10,
                controlled_loss_ramp_minutes=1440.0,
                controlled_loss_macro_gap_reference=0.02,
                controlled_loss_macro_max_speed_multiplier=2.0,
                hard_time_exit_after_minutes=0.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.sell_ladder_mode = "controlled_loss_exit"
                now = 1_700_000_000.0
                state.time_exit_activated_at = now - 360.0 * 60.0

                with patch("time.time", return_value=now):
                    neutral = bot._controlled_loss_move_fraction(
                        state,
                        symbol=SYMBOL,
                        signal={"data_valid": True, "trend_ema_gap": 0.02},
                    )
                    adverse = bot._controlled_loss_move_fraction(
                        state,
                        symbol=SYMBOL,
                        signal={"data_valid": True, "trend_ema_gap": -0.02},
                    )

                self.assertAlmostEqual(neutral, 0.325)
                self.assertAlmostEqual(adverse, 0.55)

    def test_controlled_loss_move_fraction_uses_exponential_adverse_volatility(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                controlled_loss_min_move_fraction=0.10,
                controlled_loss_ramp_minutes=1440.0,
                controlled_loss_macro_gap_reference=0.02,
                controlled_loss_macro_max_speed_multiplier=1.0,
                controlled_loss_volatility_speed_enabled=True,
                controlled_loss_volatility_reference=0.001,
                controlled_loss_volatility_trigger_multiplier=1.5,
                controlled_loss_volatility_max_speed_multiplier=2.0,
                controlled_loss_volatility_exponent=2.0,
                hard_time_exit_after_minutes=0.0,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.sell_ladder_mode = "controlled_loss_exit"
                now = 1_700_000_000.0
                state.time_exit_activated_at = now - 360.0 * 60.0

                with patch("time.time", return_value=now):
                    neutral = bot._controlled_loss_ramp_context(
                        state,
                        symbol=SYMBOL,
                        signal={
                            "data_valid": True,
                            "trend_ema_gap": -0.02,
                            "atr_rate": 0.001,
                        },
                    )
                    spike = bot._controlled_loss_ramp_context(
                        state,
                        symbol=SYMBOL,
                        signal={
                            "data_valid": True,
                            "trend_ema_gap": -0.02,
                            "atr_rate": 0.003,
                        },
                    )

                self.assertAlmostEqual(neutral["move_fraction"], 0.325)
                self.assertEqual(neutral["ramp_profile"], "linear")
                self.assertEqual(spike["ramp_profile"], "exponential_volatility")
                self.assertAlmostEqual(spike["volatility_intensity"], 1.0)
                self.assertAlmostEqual(spike["volatility_speed_multiplier"], 2.0)
                self.assertGreater(spike["move_fraction"], 0.66)
                self.assertLess(spike["move_fraction"], 0.67)

    def test_controlled_loss_macro_ramp_is_direction_symmetric(self):
        results = {}
        for profile_name in ("long", "short"):
            with (
                self.subTest(profile=profile_name),
                tempfile.TemporaryDirectory() as raw_tmp,
                config.use_profile(profile_name),
            ):
                strategy = replace(
                    config.STRATEGY,
                    controlled_loss_min_move_fraction=0.10,
                    controlled_loss_ramp_minutes=1440.0,
                    controlled_loss_macro_gap_reference=0.02,
                    controlled_loss_macro_max_speed_multiplier=2.0,
                    hard_time_exit_after_minutes=0.0,
                )
                with override_config(STRATEGY=strategy):
                    bot = self.make_bot(Path(raw_tmp))
                    state = bot._get_state(SYMBOL)
                    state.sell_ladder_mode = "controlled_loss_exit"
                    now = 1_700_000_000.0
                    state.time_exit_activated_at = now - 360.0 * 60.0

                    with patch("time.time", return_value=now):
                        results[profile_name] = bot._controlled_loss_move_fraction(
                            state,
                            symbol=SYMBOL,
                            signal={"data_valid": True, "trend_ema_gap": -0.02},
                        )

        self.assertAlmostEqual(results["long"], results["short"])

    def test_controlled_loss_volatility_ramp_is_direction_symmetric(self):
        results = {}
        for profile_name in ("long", "short"):
            with (
                self.subTest(profile=profile_name),
                tempfile.TemporaryDirectory() as raw_tmp,
                config.use_profile(profile_name),
            ):
                strategy = replace(
                    config.STRATEGY,
                    controlled_loss_min_move_fraction=0.10,
                    controlled_loss_ramp_minutes=1440.0,
                    controlled_loss_macro_gap_reference=0.02,
                    controlled_loss_macro_max_speed_multiplier=1.0,
                    controlled_loss_volatility_speed_enabled=True,
                    controlled_loss_volatility_reference=0.001,
                    controlled_loss_volatility_trigger_multiplier=1.5,
                    controlled_loss_volatility_max_speed_multiplier=2.0,
                    controlled_loss_volatility_exponent=2.0,
                    hard_time_exit_after_minutes=0.0,
                )
                with override_config(STRATEGY=strategy):
                    bot = self.make_bot(Path(raw_tmp))
                    state = bot._get_state(SYMBOL)
                    state.sell_ladder_mode = "controlled_loss_exit"
                    now = 1_700_000_000.0
                    state.time_exit_activated_at = now - 360.0 * 60.0

                    with patch("time.time", return_value=now):
                        results[profile_name] = bot._controlled_loss_move_fraction(
                            state,
                            symbol=SYMBOL,
                            signal={
                                "data_valid": True,
                                "trend_ema_gap": -0.02,
                                "atr_rate": 0.003,
                            },
                        )

        self.assertAlmostEqual(results["long"], results["short"])

    def test_controlled_loss_reprices_stale_ladder_with_dynamic_macro_move(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=False,
                enable_controlled_loss_exit=False,
                urgent_time_exit_after_minutes=0.0,
                hard_time_exit_after_minutes=1.0,
                hard_time_exit_close_fraction=1.0,
                hard_time_exit_step_minutes=0.0,
                hard_time_exit_fraction_step=0.0,
                hard_time_exit_bypass_profit_bank=True,
                controlled_loss_reprice_minutes=1.0,
                controlled_loss_min_move_fraction=0.10,
                controlled_loss_ramp_minutes=1440.0,
                controlled_loss_macro_gap_reference=0.02,
                controlled_loss_macro_max_speed_multiplier=2.0,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                now = 1_700_000_000.0
                bot.exchange.ticker = {"bid": 90.0, "ask": 90.1, "last": 90.0}
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = now - 2.0 * 60.0
                state.time_exit_activated_at = now - 360.0 * 60.0
                state.sell_ladder_mode = "controlled_loss_exit"
                state.sell_ladder_orders = [
                    {
                        "id": "old_exit",
                        "side": "sell",
                        "price": 100.0,
                        "amount": 10.0,
                        "created_at": now - 2.0 * 60.0,
                    }
                ]

                signal = {"data_valid": True, "valid": False, "trend_ema_gap": -0.02}
                bot.signal_cache["symbols"][SYMBOL] = signal
                with patch("time.time", return_value=now):
                    applied = bot._maybe_apply_controlled_loss_exit(SYMBOL, signal)

                self.assertTrue(applied)
                self.assertIn(
                    ("old_exit", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertTrue(bot.exchange.created_orders)
                self.assertEqual(state.sell_ladder_mode, "controlled_loss_exit")
                self.assertAlmostEqual(
                    state.sell_ladder_orders[0]["loss_move_fraction"], 0.55
                )
                self.assertAlmostEqual(
                    state.sell_ladder_orders[0]["loss_macro_intensity"], 1.0
                )
                self.assertAlmostEqual(
                    state.sell_ladder_orders[0]["loss_speed_multiplier"], 2.0
                )
                self.assertTrue(
                    bot.exchange.created_orders[-1]["params"].get("reduceOnly")
                )

    def test_controlled_loss_reprices_immediately_on_adverse_volatility_spike(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=False,
                enable_controlled_loss_exit=False,
                urgent_time_exit_after_minutes=0.0,
                hard_time_exit_after_minutes=1.0,
                hard_time_exit_close_fraction=1.0,
                hard_time_exit_step_minutes=0.0,
                hard_time_exit_fraction_step=0.0,
                hard_time_exit_bypass_profit_bank=True,
                controlled_loss_reprice_minutes=60.0,
                controlled_loss_min_move_fraction=0.10,
                controlled_loss_ramp_minutes=1440.0,
                controlled_loss_macro_gap_reference=0.02,
                controlled_loss_macro_max_speed_multiplier=1.0,
                controlled_loss_volatility_speed_enabled=True,
                controlled_loss_volatility_reference=0.001,
                controlled_loss_volatility_trigger_multiplier=1.5,
                controlled_loss_volatility_max_speed_multiplier=2.0,
                controlled_loss_volatility_exponent=2.0,
                controlled_loss_volatility_reprice_min_move_delta=0.05,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                now = 1_700_000_000.0
                bot.exchange.ticker = {"bid": 90.0, "ask": 90.1, "last": 90.0}
                bot.signal_cache["symbols"][SYMBOL] = {
                    "data_valid": True,
                    "trend_ema_gap": 0.02,
                    "atr_rate": 0.0,
                }
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = now - 2.0 * 60.0
                state.time_exit_activated_at = now - 360.0 * 60.0
                state.sell_ladder_mode = "controlled_loss_exit"
                state.sell_ladder_orders = [
                    {
                        "id": "old_exit",
                        "side": "sell",
                        "price": 100.0,
                        "amount": 10.0,
                        "created_at": now - 10.0,
                        "loss_move_fraction": 0.325,
                    }
                ]

                fresh_signal = {
                    "data_valid": True,
                    "valid": False,
                    "trend_ema_gap": -0.02,
                    "atr_rate": 0.003,
                }
                with patch("time.time", return_value=now):
                    applied = bot._maybe_apply_controlled_loss_exit(
                        SYMBOL, fresh_signal
                    )

                self.assertTrue(applied)
                self.assertIn(
                    ("old_exit", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertTrue(bot.exchange.created_orders)
                self.assertEqual(state.sell_ladder_mode, "controlled_loss_exit")
                self.assertEqual(
                    state.sell_ladder_orders[0]["loss_ramp_profile"],
                    "exponential_volatility",
                )
                self.assertAlmostEqual(
                    state.sell_ladder_orders[0]["loss_volatility_intensity"], 1.0
                )
                self.assertGreater(
                    state.sell_ladder_orders[0]["loss_move_fraction"], 0.66
                )
                self.assertTrue(
                    bot.exchange.created_orders[-1]["params"].get("reduceOnly")
                )

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

    def test_absolute_force_exit_waits_on_frozen_position_without_closeable(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=True,
                absolute_force_exit_after_minutes=10.0,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 0.0
                state.position_frozen = 10.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 11.0 * 60.0

                applied = bot._maybe_apply_absolute_force_exit(
                    SYMBOL, reason="test_force_exit"
                )

                self.assertTrue(applied)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertTrue(state.zombie_position)
                self.assertEqual(state.sell_ladder_mode, "absolute_force_exit")
                self.assertTrue(
                    state.sell_ladder_signature.startswith(
                        "pending_closeable:absolute_force_exit|"
                    )
                )
                self.assertIn(
                    "absolute_force_exit_no_closeable", state.pending_exit_ladder_reason
                )

    def test_absolute_force_exit_market_order_is_capped_to_available_amount(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=True,
                absolute_force_exit_after_minutes=10.0,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 10.0
                state.position_available = 4.0
                state.position_frozen = 6.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 11.0 * 60.0

                applied = bot._maybe_apply_absolute_force_exit(
                    SYMBOL, reason="test_force_exit"
                )

                self.assertTrue(applied)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["type"], "market")
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 4.0)
                self.assertTrue(
                    bot.exchange.created_orders[0]["params"].get("reduceOnly")
                )

    def test_absolute_force_exit_waits_after_canceling_exit_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=True,
                absolute_force_exit_after_minutes=10.0,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
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

                applied = bot._maybe_apply_absolute_force_exit(
                    SYMBOL, reason="test_force_exit"
                )

                self.assertTrue(applied)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(state.sell_ladder_mode, "absolute_force_exit")
                self.assertIn(
                    ("sell_1", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )

                applied = bot._maybe_apply_absolute_force_exit(
                    SYMBOL, reason="test_force_exit"
                )

                self.assertTrue(applied)
                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["type"], "market")
                self.assertEqual(bot.exchange.created_orders[0]["amount"], 10.0)
                self.assertTrue(
                    bot.exchange.created_orders[0]["params"].get("reduceOnly")
                )

    def test_step_symbol_invokes_absolute_force_exit_before_normal_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=True,
                absolute_force_exit_after_minutes=10.0,
                urgent_time_exit_after_minutes=0.0,
                hard_time_exit_after_minutes=0.0,
                enable_controlled_loss_exit=False,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 4.0
                state.position_available = 4.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 16.0 * 60.0
                bot.signal_cache["symbols"][SYMBOL] = self.entry_signal()
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 4.0,
                        "available": 4.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": config.RISK.leverage,
                    }
                ]

                bot.step_symbol(SYMBOL)

                self.assertEqual(len(bot.exchange.created_orders), 1)
                self.assertEqual(bot.exchange.created_orders[0]["type"], "market")
                self.assertEqual(bot.exchange.created_orders[0]["side"], "sell")
                self.assertEqual(state.sell_ladder_mode, "absolute_force_exit")

    def test_step_symbol_stops_after_crossed_hard_stop_market_close(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                hard_stop_loss_enabled=True,
                hard_stop_loss_pct=0.02,
                hard_stop_loss_min_emergency_pct=0.0,
                hard_stop_loss_atr_enabled=False,
                enable_absolute_force_exit=False,
                enable_controlled_loss_exit=False,
                urgent_time_exit_after_minutes=0.0,
                hard_time_exit_after_minutes=0.0,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 4.0
                state.position_available = 4.0
                state.entry_price = 100.0
                bot.signal_cache["symbols"][SYMBOL] = self.entry_signal()
                bot.exchange.reject_stop_loss_trigger_crossed = True
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 4.0,
                        "available": 4.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": config.RISK.leverage,
                    }
                ]

                bot.step_symbol(SYMBOL)

                self.assertEqual(state.sell_ladder_mode, "hard_stop_loss")
                self.assertEqual(len(bot.exchange.created_orders), 1)
                order = bot.exchange.created_orders[0]
                self.assertEqual(order["type"], "market")
                self.assertEqual(order["side"], "sell")
                self.assertTrue(order["params"].get("reduceOnly"))
                self.assertNotIn("stopLossPrice", order["params"])

    def test_step_symbol_invokes_hard_time_controlled_exit_when_controlled_disabled(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=False,
                enable_controlled_loss_exit=False,
                urgent_time_exit_after_minutes=0.0,
                hard_time_exit_after_minutes=10.0,
                hard_time_exit_close_fraction=1.0,
                hard_time_exit_step_minutes=0.0,
                hard_time_exit_fraction_step=0.0,
                hard_time_exit_max_loss_on_notional=0.05,
                hard_time_exit_bypass_profit_bank=True,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 11.0 * 60.0
                bot.signal_cache["symbols"][SYMBOL] = self.entry_signal()
                bot.exchange.ticker = {"bid": 90.0, "ask": 90.1, "last": 90.0}
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 5.0,
                        "available": 5.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": config.RISK.leverage,
                    }
                ]

                bot.step_symbol(SYMBOL)

                self.assertEqual(state.sell_ladder_mode, "controlled_loss_exit")
                self.assertTrue(bot.exchange.created_orders)
                self.assertTrue(
                    all(
                        order["params"].get("reduceOnly")
                        for order in bot.exchange.created_orders
                    )
                )

    def test_step_symbol_invokes_urgent_time_exit_before_breakeven_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                ema_breakeven_enabled=True,
                ema_breakeven_after_hours=48.0,
                enable_absolute_force_exit=False,
                enable_controlled_loss_exit=False,
                urgent_time_exit_after_minutes=10.0,
                hard_time_exit_after_minutes=0.0,
            )
            runtime = replace(config.RUNTIME, reduce_only_enabled=True)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 16.0 * 60.0
                bot.signal_cache["symbols"][SYMBOL] = self.entry_signal()
                bot.exchange.positions = [
                    {
                        "symbol": SYMBOL,
                        "side": "long",
                        "contracts": 5.0,
                        "available": 5.0,
                        "entryPrice": 100.0,
                        "marginMode": config.RISK.margin_mode,
                        "leverage": config.RISK.leverage,
                    }
                ]

                bot.step_symbol(SYMBOL)

                self.assertEqual(state.sell_ladder_mode, "urgent_time_exit")
                self.assertTrue(state.frozen_no_more_buys)
                self.assertTrue(bot.exchange.created_orders)

    def test_unknown_short_exit_orders_over_position_are_canceled(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertIn(
                    ("unknown_buy", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )

    def test_tracked_exit_order_without_reduce_only_is_canceled(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [
                    {"id": "sell_1", "side": "sell", "price": 101.0, "amount": 5.0}
                ]

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
                self.assertIn(
                    ("sell_1", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )

    def test_unknown_reduce_only_exit_orders_are_adopted(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertEqual(
                    [order["id"] for order in state.sell_ladder_orders], ["orphan_sell"]
                )
                self.assertEqual(
                    state.sell_ladder_signature, bot._sell_ladder_signature("normal")
                )
                bot._ensure_sell_ladder(SYMBOL)
                self.assertEqual(bot.exchange.created_orders, [])

    def test_partial_unknown_reduce_only_exit_is_rebuilt_to_cover_position(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                created_at = time.time() - 10.0

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "partial_manual_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 2.0,
                            "remaining": 2.0,
                            "reduceOnly": True,
                            "timestamp": created_at,
                        }
                    ],
                )

                self.assertTrue(valid)
                self.assertEqual(
                    [order["id"] for order in state.sell_ladder_orders],
                    ["partial_manual_sell"],
                )
                self.assertEqual(state.sell_ladder_signature, "")
                self.assertAlmostEqual(
                    state.sell_ladder_orders[0]["created_at"], created_at
                )
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(
                        row["event"] == "state_exchange_mismatch"
                        and "partial_external_exit_coverage" in row["reason"]
                        for row in rows
                    )
                )

                bot._ensure_sell_ladder(SYMBOL)

                self.assertIn(
                    (
                        "partial_manual_sell",
                        SYMBOL,
                        {"marginMode": config.RISK.margin_mode},
                    ),
                    bot.exchange.canceled_orders,
                )
                self.assertTrue(bot.exchange.created_orders)
                self.assertTrue(
                    all(
                        order["params"].get("reduceOnly")
                        for order in bot.exchange.created_orders
                    )
                )
                self.assertAlmostEqual(
                    sum(order["amount"] for order in bot.exchange.created_orders)
                    + state.exit_runner_contracts,
                    5.0,
                )
                self.assertAlmostEqual(
                    sum(ref["amount"] for ref in state.sell_ladder_orders)
                    + state.exit_runner_contracts,
                    5.0,
                )

    def test_offset_close_exit_orders_are_adopted_as_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.entry_price = 100.0

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "offset_close_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 5.0,
                            "remaining": 5.0,
                            "info": {"offset": "close"},
                        }
                    ],
                )

                self.assertTrue(valid)
                self.assertEqual(
                    [order["id"] for order in state.sell_ladder_orders],
                    ["offset_close_sell"],
                )
                self.assertEqual(bot.exchange.canceled_orders, [])

    def test_adopted_hidden_close_order_cancel_uses_cancel_params(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "hidden_tp",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "triggerPrice": 101.0,
                            "amount": 5.0,
                            "remaining": 5.0,
                            "bot_hidden_order_type": "tpsl",
                            "bot_cancel_params": {"orderType": "tpsl"},
                            "info": {"trade_type": "4"},
                        }
                    ],
                )

                self.assertTrue(valid)
                self.assertEqual(
                    state.sell_ladder_orders[0]["cancel_params"], {"orderType": "tpsl"}
                )
                bot._cancel_sell_orders(SYMBOL, reason="test_hidden_cancel")
                self.assertIn(
                    (
                        "hidden_tp",
                        SYMBOL,
                        {"marginMode": config.RISK.margin_mode, "orderType": "tpsl"},
                    ),
                    bot.exchange.canceled_orders,
                )

    def test_flat_unknown_reduce_only_exit_orders_are_canceled_and_block_entry(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "flat_close",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 1.0,
                            "remaining": 1.0,
                            "reduceOnly": True,
                        }
                    ],
                )

                self.assertFalse(valid)
                self.assertIn(
                    ("flat_close", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )

    def test_flat_unknown_unsafe_exit_orders_block_entry_without_cancel(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "manual_flat_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 1.0,
                            "remaining": 1.0,
                            "reduceOnly": False,
                        }
                    ],
                )

                self.assertFalse(valid)
                self.assertEqual(bot.exchange.canceled_orders, [])

    def test_tracked_exit_orders_are_preserved_when_temporarily_invisible(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertEqual(
                    [order["id"] for order in state.sell_ladder_orders], ["sell_1"]
                )
                self.assertFalse(state.frozen_no_more_buys)
                self.assertEqual(bot.exchange.canceled_orders, [])
                bot._ensure_sell_ladder(SYMBOL)
                self.assertEqual(bot.exchange.created_orders, [])

    def test_tracked_exit_orders_clear_after_invisible_timeout(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, order_timeout_sec=1, poll_interval_sec=1)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                timeout = bot._unknown_exit_wait_timeout_sec()
                state.sell_ladder_signature = bot._sell_ladder_signature("normal")
                state.sell_ladder_orders = [
                    {
                        "id": "sell_1",
                        "side": "sell",
                        "price": 101.0,
                        "amount": 5.0,
                        "invisible_preserved_at": time.time() - timeout - 1.0,
                    }
                ]

                valid = bot._validate_sell_orders(SYMBOL, [])

                self.assertFalse(valid)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(state.sell_ladder_signature, "")
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(
                        row["event"] == "state_exchange_mismatch"
                        and "tracked_exit_orders_invisible_timeout" in row["reason"]
                        for row in rows
                    )
                )

    def test_tracked_exit_order_id_rotation_adopts_visible_reduce_only_exit(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertEqual(
                    [order["id"] for order in state.sell_ladder_orders],
                    ["rotated_sell"],
                )
                self.assertEqual(
                    state.sell_ladder_signature, bot._sell_ladder_signature("normal")
                )
                self.assertEqual(bot.exchange.canceled_orders, [])

    def test_tracked_and_unknown_safe_close_orders_are_merged_without_exceeding_position(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [
                    {
                        "id": "tracked_sell",
                        "side": "sell",
                        "price": 101.0,
                        "amount": 3.0,
                    }
                ]

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "tracked_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 3.0,
                            "remaining": 3.0,
                            "reduceOnly": True,
                        },
                        {
                            "id": "safe_manual_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 102.0,
                            "amount": 2.0,
                            "remaining": 2.0,
                            "reduceOnly": True,
                        },
                    ],
                )

                self.assertTrue(valid)
                self.assertEqual(
                    [order["id"] for order in state.sell_ladder_orders],
                    ["tracked_sell", "safe_manual_sell"],
                )
                self.assertAlmostEqual(
                    sum(order["amount"] for order in state.sell_ladder_orders), 5.0
                )
                self.assertEqual(bot.exchange.canceled_orders, [])

    def test_tracked_ladder_with_unadoptable_unknown_exit_waits(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [
                    {
                        "id": "tracked_sell",
                        "side": "sell",
                        "price": 101.0,
                        "amount": 3.0,
                    }
                ]

                valid = bot._validate_sell_orders(
                    SYMBOL,
                    [
                        {
                            "id": "tracked_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 3.0,
                            "remaining": 3.0,
                            "reduceOnly": True,
                        },
                        {
                            "id": "manual_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 102.0,
                            "amount": 1.0,
                            "remaining": 1.0,
                            "reduceOnly": False,
                        },
                    ],
                )

                self.assertFalse(valid)
                self.assertTrue(state.frozen_no_more_buys)
                self.assertEqual(
                    [order["id"] for order in state.sell_ladder_orders],
                    ["tracked_sell"],
                )
                self.assertEqual(bot.exchange.canceled_orders, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(
                        row["event"] == "reduce_only_violation_prevented"
                        and row["reason"].startswith(
                            "tracked_unknown_exit_orders_unadoptable;unknown_exit_order_not_reduce_only"
                        )
                        for row in rows
                    )
                )

    def test_zero_remaining_exit_order_is_not_counted_as_open_exposure(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0

                exposure = bot._exit_order_exposure(
                    SYMBOL,
                    [
                        {
                            "id": "filled_manual_sell",
                            "symbol": SYMBOL,
                            "side": "sell",
                            "price": 101.0,
                            "amount": 5.0,
                            "remaining": 0.0,
                            "reduceOnly": False,
                        }
                    ],
                )

                self.assertEqual(exposure["open_exit_orders"], [])
                self.assertEqual(exposure["unknown_remaining"], 0.0)

    def test_exit_ladder_waits_on_frozen_position_without_closeable(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0

                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("normal"),
                )
                self.assertIn(
                    "closeable_amount_reserved", state.pending_exit_ladder_reason
                )
                self.assertEqual(state.exit_runner_contracts, 0.0)

    def test_exit_ladder_waits_when_exchange_reports_closeable_reserved(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("normal"),
                )
                self.assertGreater(state.pending_exit_ladder_since, 0)
                self.assertIn(
                    state.pending_exit_ladder_reason,
                    {
                        "exchange_closeable_amount_reserved",
                        "closeable_amount_reserved_by_existing_exit_orders",
                    },
                )

    def test_pending_closeable_exit_ladder_retries_after_timeout(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, order_timeout_sec=1, poll_interval_sec=1)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.reject_reduce_only_closeable_amount = True
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0

                bot._ensure_sell_ladder(SYMBOL)
                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)

                bot.exchange.reject_reduce_only_closeable_amount = False
                state.position_available = 5.0
                state.position_frozen = 0.0
                state.pending_exit_ladder_since = time.time() - 5.0
                bot._ensure_sell_ladder(SYMBOL)

                self.assertGreaterEqual(bot.exchange.create_order_calls, 1)
                self.assertTrue(bot.exchange.created_orders)
                self.assertEqual(state.pending_exit_ladder_since, None)
                self.assertEqual(state.pending_exit_ladder_reason, "")

    def test_pending_closeable_exit_ladder_does_not_retry_after_timeout_while_frozen(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, order_timeout_sec=1, poll_interval_sec=1)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.reject_reduce_only_closeable_amount = True
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0

                bot._ensure_sell_ladder(SYMBOL)
                state.pending_exit_ladder_since = time.time() - 5.0
                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("normal"),
                )
                self.assertIn(
                    "closeable_amount_reserved", state.pending_exit_ladder_reason
                )

    def test_pending_closeable_exit_ladder_keeps_original_wait_after_force_reset_window(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, order_timeout_sec=1, poll_interval_sec=1)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0

                bot._ensure_sell_ladder(SYMBOL)
                old_pending_since = time.time() - 600.0
                state.pending_exit_ladder_since = old_pending_since
                bot._ensure_sell_ladder(SYMBOL)

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.canceled_orders, [])
                self.assertAlmostEqual(
                    state.pending_exit_ladder_since, old_pending_since, places=3
                )
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("normal"),
                )
                with bot.diagnostics_csv_path.open(
                    newline="", encoding="utf-8"
                ) as handle:
                    reasons = [row["reason"] for row in csv.DictReader(handle)]
                self.assertNotIn("pending_closeable_force_reset", reasons)

    def test_breakeven_waits_on_pending_closeable_without_retrying_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, order_timeout_sec=1, poll_interval_sec=1)
            with override_config(RUNTIME=runtime):
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
                state.pending_exit_ladder_since = time.time() - 5.0
                self.assertTrue(bot._maybe_apply_time_based_exit(SYMBOL, None))

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("breakeven"),
                )

    def test_urgent_time_exit_preserves_pending_closeable_without_duplicate_ladder(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, order_timeout_sec=1, poll_interval_sec=1)
            strategy = replace(config.STRATEGY, urgent_time_exit_after_minutes=1.0)
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.reject_reduce_only_closeable_amount = True
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 16 * 60

                self.assertTrue(bot._maybe_apply_urgent_time_exit(SYMBOL, None))
                state.pending_exit_ladder_since = time.time() - 5.0
                self.assertTrue(bot._maybe_apply_urgent_time_exit(SYMBOL, None))

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(state.sell_ladder_mode, "urgent_time_exit")
                self.assertEqual(
                    state.sell_ladder_signature,
                    bot._pending_exit_ladder_signature("urgent_time_exit"),
                )

    def test_controlled_loss_waits_on_pending_closeable_without_retrying_reduce_only(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(
                config.RUNTIME,
                reduce_only_enabled=True,
                order_timeout_sec=1,
                poll_interval_sec=1,
            )
            strategy = replace(
                config.STRATEGY,
                enable_absolute_force_exit=False,
                enable_controlled_loss_exit=False,
                urgent_time_exit_after_minutes=0.0,
                hard_time_exit_after_minutes=1.0,
                hard_time_exit_close_fraction=1.0,
                hard_time_exit_step_minutes=0.0,
                hard_time_exit_fraction_step=0.0,
                hard_time_exit_bypass_profit_bank=True,
            )
            with override_config(RUNTIME=runtime, STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.reject_reduce_only_closeable_amount = True
                bot.exchange.ticker = {"bid": 90.0, "ask": 90.1, "last": 90.0}
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.entry_price = 100.0
                state.cycle_opened_at = time.time() - 2.0 * 60.0

                self.assertTrue(bot._maybe_apply_controlled_loss_exit(SYMBOL, None))
                state.pending_exit_ladder_since = time.time() - 5.0
                self.assertTrue(bot._maybe_apply_controlled_loss_exit(SYMBOL, None))

                self.assertEqual(bot.exchange.created_orders, [])
                self.assertEqual(bot.exchange.create_order_calls, 0)
                self.assertEqual(state.sell_ladder_orders, [])
                self.assertEqual(state.sell_ladder_mode, "controlled_loss_exit")
                self.assertTrue(
                    state.sell_ladder_signature.startswith(
                        "pending_closeable:controlled_loss_exit|"
                    )
                )

    def test_unknown_non_reduce_only_exit_order_blocks_duplicate_ladder(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                self.assertTrue(state.frozen_no_more_buys)
                self.assertEqual(
                    state.pending_exit_ladder_reason,
                    "unknown_exit_order_not_reduce_only",
                )

    def test_stale_unknown_non_reduce_only_exit_order_is_canceled(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, order_timeout_sec=1, poll_interval_sec=1)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                order = {
                    "id": "manual_sell",
                    "symbol": SYMBOL,
                    "side": "sell",
                    "price": 101.0,
                    "amount": 3.0,
                    "remaining": 3.0,
                    "reduceOnly": False,
                }

                self.assertFalse(bot._validate_sell_orders(SYMBOL, [order]))
                state.pending_exit_ladder_since = time.time() - 120.0
                self.assertFalse(bot._validate_sell_orders(SYMBOL, [order]))

                self.assertIn(
                    ("manual_sell", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                self.assertIsNone(state.pending_exit_ladder_since)
                self.assertEqual(state.pending_exit_ladder_reason, "")

    def test_unknown_exit_orders_cancel_tracked_ladder_when_combined_amount_exceeds_position(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.entry_price = 100.0
                state.sell_ladder_orders = [
                    {"id": "sell_1", "side": "sell", "price": 101.0, "amount": 4.0}
                ]

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
                self.assertIn(
                    ("sell_1", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )

    def test_private_snapshots_are_bulk_cached_per_cycle(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                    {
                        "id": "sell_1",
                        "symbol": SYMBOL,
                        "side": "sell",
                        "price": 101.0,
                        "amount": 5.0,
                    }
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

    def test_bulk_private_snapshot_cache_is_singleflight_across_threads(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_positions_delay = 0.02
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
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    snapshots = list(
                        executor.map(
                            lambda _: bot._fetch_position_snapshot(SYMBOL), range(8)
                        )
                    )

                self.assertTrue(all(snapshot["ok"] for snapshot in snapshots))
                self.assertTrue(
                    all(snapshot["long_size"] == 5.0 for snapshot in snapshots)
                )
                self.assertEqual(bot.exchange.fetch_positions_calls, 1)

    def test_account_snapshot_is_cached_per_cycle_and_singleflight(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_balance_delay = 0.02

                bot._reset_private_caches()
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    snapshots = list(
                        executor.map(lambda _index: bot._account_snapshot(), range(8))
                    )

                self.assertTrue(
                    all(snapshot["free"] == 1000.0 for snapshot in snapshots)
                )
                self.assertEqual(bot.exchange.fetch_balance_calls, 1)

                second = bot._account_snapshot()
                self.assertEqual(second["total"], 1000.0)
                self.assertEqual(bot.exchange.fetch_balance_calls, 1)

                bot._reset_private_caches()
                bot._account_snapshot()
                self.assertEqual(bot.exchange.fetch_balance_calls, 2)

    def test_private_position_fetch_retries_transient_network_failure(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
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
                self.assertEqual(
                    bot.exchange.urls["hostnames"]["contract"], "api.two.test"
                )

    def test_private_position_fetch_honors_configured_retry_count(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=4,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
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
                self.assertEqual(
                    bot.exchange.urls["hostnames"]["contract"], "api.two.test"
                )

    def test_bulk_private_position_network_outage_skips_per_symbol_cascade(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_positions_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                ]

                bot._reset_private_caches()
                snapshot = bot._fetch_position_snapshot(SYMBOL)
                second_snapshot = bot._fetch_position_snapshot(SECOND_SYMBOL)

                self.assertFalse(snapshot["ok"])
                self.assertFalse(second_snapshot["ok"])
                self.assertEqual(bot.exchange.fetch_positions_calls, 2)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(
                        row["reason"] == "bulk_positions_fetch_failed_cycle_skipped"
                        for row in rows
                    )
                )
                self.assertFalse(
                    any(row["reason"] == "position_fetch_failed" for row in rows)
                )

    def test_bulk_private_open_orders_network_outage_skips_per_symbol_cascade(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_open_orders_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                ]

                bot._reset_private_caches()
                orders = bot._fetch_open_orders(SYMBOL)
                second_orders = bot._fetch_open_orders(SECOND_SYMBOL)

                self.assertIsNone(orders)
                self.assertIsNone(second_orders)
                self.assertEqual(bot.exchange.fetch_open_orders_calls, 2)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(
                        row["reason"] == "bulk_open_orders_fetch_failed_cycle_skipped"
                        for row in rows
                    )
                )
                self.assertFalse(
                    any(row["reason"] == "open_orders_fetch_failed" for row in rows)
                )

    def test_private_prefetch_stops_after_bulk_position_network_outage(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_positions_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                ]

                bot._reset_private_caches()
                bot._prefetch_private_snapshots()

                self.assertEqual(bot.exchange.fetch_positions_calls, 2)
                self.assertEqual(bot.exchange.fetch_open_orders_calls, 0)

    def test_private_position_fetch_returns_not_ok_on_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            # Set up failures for mock exchange
            bot.exchange.fetch_positions_failures = [
                RuntimeError("bulk fail"),
                RuntimeError("symbol fail"),
            ]

            bot._reset_private_caches()

            snapshot = bot._fetch_position_snapshot(SYMBOL)

            self.assertFalse(snapshot["ok"])
            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["level"], "ERROR")
            self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
            self.assertEqual(rows[-1]["reason"], "position_fetch_failed")

    def test_exhausted_private_network_fetch_logs_warning_not_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.has["fetchPositions"] = False
                bot.exchange.fetch_positions_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                ]

                bot._reset_private_caches()
                snapshot = bot._fetch_position_snapshot(SYMBOL)
                second_snapshot = bot._fetch_position_snapshot(SECOND_SYMBOL)

                self.assertFalse(snapshot["ok"])
                self.assertFalse(second_snapshot["ok"])
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
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.has["fetchOpenOrders"] = False
                bot.exchange.fetch_open_orders_failures = [
                    ccxt.RequestTimeout("timeout-1"),
                    ccxt.RequestTimeout("timeout-2"),
                ]

                bot._reset_private_caches()
                orders = bot._fetch_open_orders(SYMBOL)
                second_orders = bot._fetch_open_orders(SECOND_SYMBOL)

                self.assertIsNone(orders)
                self.assertIsNone(second_orders)
                self.assertEqual(bot.exchange.fetch_open_orders_calls, 2)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "WARNING")
                self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
                self.assertEqual(rows[-1]["reason"], "open_orders_fetch_failed")

    def test_private_position_dict_response_is_logged_without_step_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.has["fetchPositions"] = False
            bot.exchange.fetch_positions_response_override = {
                "status": "error",
                "err_code": "500",
                "err_msg": "unexpected payload",
            }

            bot._run_step_symbol_safe(SYMBOL)

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            position_rows = [
                row for row in rows if row["reason"] == "position_fetch_failed"
            ]
            self.assertTrue(position_rows)
            self.assertEqual(position_rows[-1]["level"], "ERROR")
            self.assertEqual(
                position_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(position_rows[-1]["error_code"], "500")
            self.assertIn("fetch_positions returned dict", position_rows[-1]["message"])
            self.assertFalse(any(row["reason"] == "step_error" for row in rows))

    def test_per_symbol_open_orders_success(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.has["fetchOpenOrders"] = False
            mock_order = {"id": "123", "symbol": SYMBOL, "side": "buy"}
            bot.exchange.open_orders = [mock_order]

            orders = bot._fetch_open_orders(SYMBOL)

            self.assertIsNotNone(orders)
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["id"], "123")
            self.assertEqual(bot.exchange.fetch_open_orders_calls, 1)

    def test_open_orders_dict_response_is_logged_without_step_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.has["fetchOpenOrders"] = False
            bot.exchange.fetch_open_orders_response_override = {
                "status": "error",
                "err_code": "501",
                "err_msg": "unexpected payload",
            }

            bot._run_step_symbol_safe(SYMBOL)

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            order_rows = [
                row for row in rows if row["reason"] == "open_orders_fetch_failed"
            ]
            self.assertTrue(order_rows)
            self.assertEqual(order_rows[-1]["level"], "ERROR")
            self.assertEqual(
                order_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(order_rows[-1]["error_code"], "501")
            self.assertIn("fetch_open_orders returned dict", order_rows[-1]["message"])
            self.assertFalse(any(row["reason"] == "step_error" for row in rows))

    def test_private_position_error_dict_item_is_logged_without_step_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.has["fetchPositions"] = False
            bot.exchange.fetch_positions_response_override = [
                {
                    "status": "error",
                    "err_code": "500",
                    "err_msg": "unexpected payload",
                }
            ]

            bot._run_step_symbol_safe(SYMBOL)

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            position_rows = [
                row for row in rows if row["reason"] == "position_fetch_failed"
            ]
            self.assertTrue(position_rows)
            self.assertEqual(position_rows[-1]["level"], "ERROR")
            self.assertEqual(
                position_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(position_rows[-1]["error_code"], "500")
            self.assertIn(
                "fetch_positions returned list with error dict item",
                position_rows[-1]["message"],
            )
            self.assertFalse(any(row["reason"] == "step_error" for row in rows))

    def test_open_orders_error_dict_item_is_logged_without_step_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.has["fetchOpenOrders"] = False
            bot.exchange.fetch_open_orders_response_override = [
                {
                    "status": "error",
                    "err_code": "501",
                    "err_msg": "unexpected payload",
                }
            ]

            bot._run_step_symbol_safe(SYMBOL)

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            order_rows = [
                row for row in rows if row["reason"] == "open_orders_fetch_failed"
            ]
            self.assertTrue(order_rows)
            self.assertEqual(order_rows[-1]["level"], "ERROR")
            self.assertEqual(
                order_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(order_rows[-1]["error_code"], "501")
            self.assertIn(
                "fetch_open_orders returned list with error dict item",
                order_rows[-1]["message"],
            )
            self.assertFalse(any(row["reason"] == "step_error" for row in rows))

    def test_bulk_position_dict_response_falls_back_without_step_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.fetch_positions_response_override = {
                "status": "error",
                "err_code": "500",
                "err_msg": "unexpected bulk payload",
            }

            snapshot = bot._fetch_position_snapshot(SYMBOL)

            self.assertFalse(snapshot["ok"])
            self.assertEqual(bot.exchange.fetch_positions_calls, 2)
            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            bulk_rows = [
                row for row in rows if row["reason"] == "bulk_positions_fetch_failed"
            ]
            position_rows = [
                row for row in rows if row["reason"] == "position_fetch_failed"
            ]
            self.assertTrue(bulk_rows)
            self.assertTrue(position_rows)
            self.assertEqual(
                bulk_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(
                position_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(position_rows[-1]["error_code"], "500")
            self.assertIn("fetch_positions returned dict", position_rows[-1]["message"])
            self.assertFalse(any(row["reason"] == "step_error" for row in rows))

    def test_bulk_open_orders_success(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.has["fetchOpenOrders"] = True
            mock_order_1 = {"id": "123", "symbol": SYMBOL, "side": "buy"}
            mock_order_2 = {"id": "124", "symbol": SECOND_SYMBOL, "side": "sell"}
            bot.exchange.open_orders = [mock_order_1, mock_order_2]

            orders = bot._bulk_open_orders_by_symbol()

            self.assertIsNotNone(orders)
            self.assertEqual(len(orders), 2)
            self.assertEqual(len(orders[SYMBOL]), 1)
            self.assertEqual(orders[SYMBOL][0]["id"], "123")
            self.assertEqual(len(orders[SECOND_SYMBOL]), 1)
            self.assertEqual(orders[SECOND_SYMBOL][0]["id"], "124")
            self.assertEqual(bot.exchange.fetch_open_orders_calls, 1)

    def test_bulk_open_orders_dict_response_falls_back_without_step_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.fetch_open_orders_response_override = {
                "status": "error",
                "err_code": "501",
                "err_msg": "unexpected bulk payload",
            }

            orders = bot._fetch_open_orders(SYMBOL)

            self.assertIsNone(orders)
            self.assertEqual(bot.exchange.fetch_open_orders_calls, 2)
            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            bulk_rows = [
                row for row in rows if row["reason"] == "bulk_open_orders_fetch_failed"
            ]
            order_rows = [
                row for row in rows if row["reason"] == "open_orders_fetch_failed"
            ]
            self.assertTrue(bulk_rows)
            self.assertTrue(order_rows)
            self.assertEqual(
                bulk_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(
                order_rows[-1]["exception_type"], "UnexpectedExchangeResponse"
            )
            self.assertEqual(order_rows[-1]["error_code"], "501")
            self.assertIn("fetch_open_orders returned dict", order_rows[-1]["message"])
            self.assertFalse(any(row["reason"] == "step_error" for row in rows))

    def test_open_orders_params_type_error_is_not_retried_without_params(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.open_orders = [
                {
                    "id": "visible_exit",
                    "symbol": SYMBOL,
                    "side": config.EXIT_SIDE,
                    "amount": 1.0,
                }
            ]
            bot.exchange.fetch_open_orders_type_error_on_params = True

            orders = bot._fetch_open_orders(SYMBOL)

            self.assertIsNone(orders)
            self.assertEqual(bot.exchange.fetch_open_orders_calls, 2)
            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            order_rows = [
                row for row in rows if row["reason"] == "open_orders_fetch_failed"
            ]
            self.assertTrue(order_rows)
            self.assertEqual(order_rows[-1]["level"], "ERROR")
            self.assertEqual(order_rows[-1]["exception_type"], "TypeError")
            self.assertIn(
                "unexpected keyword argument 'params'", order_rows[-1]["message"]
            )

    def test_public_ohlcv_dict_response_raises_typed_exchange_response_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.exchange.fetch_ohlcv_response_override = {
                "status": "error",
                "err_code": "502",
                "err_msg": "unexpected payload",
            }

            with self.assertRaisesRegex(
                UnexpectedExchangeResponse, "fetch_ohlcv returned dict"
            ):
                bot._closed_candles(SYMBOL, 2, timeframe="1m")

    def test_position_mode_already_one_way_skips_write_endpoint(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))

                self.assertTrue(bot._ensure_one_way_position_mode(force=True))

                self.assertTrue(bot.one_way_mode_checked)
                self.assertEqual(bot.exchange.set_position_mode_calls, [])
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "INFO")
                self.assertEqual(rows[-1]["event"], "futures_setup")
                self.assertIn("position_mode_one_way_confirmed", rows[-1]["reason"])

    def test_position_mode_locked_by_existing_positions_logs_info(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                responses = iter(["", "single_side"])

                def fake_position_info(request):
                    mode = next(responses)
                    data = {"positions": [], "contract_detail": []}
                    if mode:
                        data["position_mode"] = mode
                    return {"status": "ok", "data": data}

                bot.exchange.contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo = fake_position_info
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
                self.assertIn("position_mode_existing_positions", rows[-1]["reason"])
                self.assertIn("position_mode=single_side", rows[-1]["reason"])

    def test_position_mode_access_limit_continues_when_one_way_is_confirmed(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                responses = iter(["", "single_side"])

                def fake_position_info(request):
                    mode = next(responses)
                    data = {"positions": [], "contract_detail": []}
                    if mode:
                        data["position_mode"] = mode
                    return {"status": "ok", "data": data}

                bot.exchange.contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo = fake_position_info
                bot.exchange.set_position_mode_error = RuntimeError(
                    'htx {"status":"error","err_code":1032,'
                    '"err_msg":"Maximum number of access attempts exceeded."}'
                )

                self.assertTrue(bot._ensure_one_way_position_mode(force=True))
                self.assertTrue(bot.one_way_mode_checked)
                self.assertEqual(len(bot.exchange.set_position_mode_calls), 1)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                row = next(
                    row
                    for row in rows
                    if row["reason"].startswith(
                        "position_mode_switch_limited_confirmed"
                    )
                )
                self.assertEqual(row["level"], "WARNING")
                self.assertEqual(row["event"], "futures_setup")
                self.assertEqual(row["exception_type"], "RuntimeError")
                self.assertEqual(row["error_code"], "1032")
                self.assertEqual(row["retryable"], "1")

    def test_position_mode_access_limit_without_confirmation_blocks_live_start(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.account_position_mode = ""
                bot.exchange.set_position_mode_error = RuntimeError(
                    'htx {"status":"error","err_code":1032,'
                    '"err_msg":"Maximum number of access attempts exceeded."}'
                )

                self.assertFalse(bot._ensure_one_way_position_mode(force=True))
                self.assertFalse(bot.one_way_mode_checked)
                self.assertEqual(len(bot.exchange.set_position_mode_calls), 1)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "ERROR")
                self.assertEqual(rows[-1]["event"], "futures_setup")
                self.assertEqual(
                    rows[-1]["reason"], "position_mode_switch_limited_unverified"
                )
                self.assertEqual(rows[-1]["error_code"], "1032")
                self.assertEqual(rows[-1]["retryable"], "1")

    def test_position_mode_locked_in_hedge_mode_blocks_live_start(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.account_position_mode = "dual_side"
                bot.exchange.set_position_mode_error = RuntimeError(
                    'htx {"status":"error","err_code":1494,'
                    '"err_msg":"Position mode cannot be adjusted for existing positions."}'
                )

                self.assertFalse(bot._ensure_one_way_position_mode(force=True))
                self.assertFalse(bot.one_way_mode_checked)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "ERROR")
                self.assertEqual(rows[-1]["event"], "futures_setup")
                self.assertEqual(rows[-1]["reason"], "position_mode_locked_hedge_mode")

    def test_position_mode_locked_without_mode_confirmation_blocks_live_start(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.account_position_mode = ""
                bot.exchange.set_position_mode_error = RuntimeError(
                    'htx {"status":"error","err_code":1494,'
                    '"err_msg":"Position mode cannot be adjusted for open orders."}'
                )

                self.assertFalse(bot._ensure_one_way_position_mode(force=True))
                self.assertFalse(bot.one_way_mode_checked)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "ERROR")
                self.assertEqual(rows[-1]["event"], "futures_setup")
                self.assertEqual(rows[-1]["reason"], "position_mode_locked_unverified")

    def test_setup_sets_leverage_when_enabled(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                set_position_mode_on_start=False,
                set_leverage_on_start=True,
            )
            risk = replace(config.RISK, leverage=7)
            with override_config(
                RUNTIME=config.RUNTIME, EXCHANGE=exchange_config, RISK=risk
            ):
                bot = self.make_bot(Path(raw_tmp))
                bot.symbols = [SYMBOL]

                bot._setup_futures_account()

                self.assertEqual(
                    bot.exchange.set_leverage_calls,
                    [(7, SYMBOL, {"marginMode": config.RISK.margin_mode})],
                )
                self.assertEqual(bot.order_leverage_cache[SYMBOL], 7.0)

    def test_setup_continues_when_enabled_leverage_cannot_be_set_for_one_symbol(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                set_position_mode_on_start=False,
                set_leverage_on_start=True,
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.symbols = [SYMBOL, SECOND_SYMBOL]
                bot.exchange.set_leverage_errors_by_symbol[SYMBOL] = RuntimeError(
                    "leverage rejected"
                )

                bot._setup_futures_account()

                self.assertEqual(
                    bot.exchange.set_leverage_calls,
                    [
                        (
                            config.RISK.leverage,
                            SYMBOL,
                            {"marginMode": config.RISK.margin_mode},
                        ),
                        (
                            config.RISK.leverage,
                            SECOND_SYMBOL,
                            {"marginMode": config.RISK.margin_mode},
                        ),
                    ],
                )
                self.assertNotIn(SYMBOL, bot.order_leverage_cache)
                self.assertEqual(
                    bot.order_leverage_cache[SECOND_SYMBOL], float(config.RISK.leverage)
                )
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                reasons = [row["reason"] for row in rows]
                self.assertIn("set_leverage_failed", reasons)
                self.assertIn("set_leverage_partial_failure", reasons)
                failed_row = next(
                    row for row in rows if row["reason"] == "set_leverage_failed"
                )
                self.assertEqual(failed_row["level"], "WARNING")
                self.assertEqual(failed_row["symbol"], SYMBOL)
                self.assertEqual(failed_row["exception_type"], "RuntimeError")
                partial_row = next(
                    row
                    for row in rows
                    if row["reason"] == "set_leverage_partial_failure"
                )
                self.assertEqual(partial_row["level"], "WARNING")

    def test_setup_continues_when_startup_leverage_fails_for_all_symbols(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                set_position_mode_on_start=False,
                set_leverage_on_start=True,
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.symbols = [SYMBOL, SECOND_SYMBOL]
                bot.exchange.set_leverage_errors_by_symbol[SYMBOL] = RuntimeError(
                    "symbol leverage rejected"
                )
                bot.exchange.set_leverage_errors_by_symbol[SECOND_SYMBOL] = (
                    RuntimeError("alt leverage rejected")
                )

                bot._setup_futures_account()

                self.assertEqual(
                    bot.exchange.set_leverage_calls,
                    [
                        (
                            config.RISK.leverage,
                            SYMBOL,
                            {"marginMode": config.RISK.margin_mode},
                        ),
                        (
                            config.RISK.leverage,
                            SECOND_SYMBOL,
                            {"marginMode": config.RISK.margin_mode},
                        ),
                    ],
                )
                self.assertEqual(bot.order_leverage_cache, {})
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                failed_rows = [
                    row for row in rows if row["reason"] == "set_leverage_failed"
                ]
                self.assertEqual(
                    [row["symbol"] for row in failed_rows], [SYMBOL, SECOND_SYMBOL]
                )
                self.assertTrue(all(row["level"] == "WARNING" for row in failed_rows))
                partial_row = next(
                    row
                    for row in rows
                    if row["reason"] == "set_leverage_partial_failure"
                )
                self.assertEqual(partial_row["level"], "WARNING")

    def test_funding_context_rejects_empty_payload_without_neutral_full_ttl_cache(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_funding_aware_exit=True,
                funding_cache_ttl_sec=300,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.has["fetchFundingRate"] = True
                bot.exchange.funding_rate_response = {}

                context = bot._funding_rate_context(SYMBOL)

                self.assertFalse(context["valid"])
                self.assertEqual(context["reason"], "funding_rate_unavailable")
                self.assertEqual(context["markup_multiplier"], 1.0)
                self.assertLessEqual(context["expires_at"] - context["ts"], 30.0)

    def test_funding_context_parses_info_funding_rate_after_invalid_cache_expires(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_funding_aware_exit=True,
                funding_cache_ttl_sec=300,
                funding_positive_threshold=0.0001,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.has["fetchFundingRate"] = True
                bot.exchange.funding_rate_response = {}
                invalid = bot._funding_rate_context(SYMBOL)
                invalid["expires_at"] = time.time() - 1.0
                bot.funding_cache[SYMBOL] = invalid
                bot.exchange.funding_rate_response = {
                    "info": {"funding_rate": "0.0002"}
                }

                context = bot._funding_rate_context(SYMBOL)

                self.assertTrue(context["valid"])
                self.assertAlmostEqual(context["rate"], 0.0002)
                self.assertEqual(context["reason"], "positive_funding_long_pays")
                self.assertEqual(bot.exchange.fetch_funding_rate_calls, 2)

    def test_funding_context_cache_is_singleflight_across_threads(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = replace(
                config.STRATEGY,
                enable_funding_aware_exit=True,
                funding_cache_ttl_sec=300,
                funding_positive_threshold=0.0001,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.has["fetchFundingRate"] = True
                calls = {"count": 0}
                call_lock = threading.Lock()

                def slow_fetch_funding_rate(symbol, params=None):
                    with call_lock:
                        calls["count"] += 1
                    time.sleep(0.02)
                    return {"fundingRate": "0.0002"}

                bot.exchange.fetch_funding_rate = slow_fetch_funding_rate

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    contexts = list(
                        executor.map(
                            lambda _index: bot._funding_rate_context(SYMBOL), range(8)
                        )
                    )

                self.assertEqual(calls["count"], 1)
                self.assertTrue(all(context["valid"] for context in contexts))
                self.assertTrue(
                    all(
                        context["reason"] == "positive_funding_long_pays"
                        for context in contexts
                    )
                )

    def test_shared_exchange_funding_rate_cache_is_singleflight_across_threads(self):
        class SlowFundingExchange:
            def __init__(self):
                self.calls = 0
                self.lock = threading.Lock()

            def fetch_funding_rate(self, symbol, params=None):
                with self.lock:
                    self.calls += 1
                time.sleep(0.02)
                return {"symbol": symbol, "fundingRate": 0.0001}

        exchange = SlowFundingExchange()
        cached = CachedMarketDataExchange(exchange, funding_ttl_sec=60)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _index: cached.fetch_funding_rate(SYMBOL), range(8))
            )

        self.assertEqual(exchange.calls, 1)
        self.assertTrue(all(result["symbol"] == SYMBOL for result in results))

    def test_shared_exchange_serializes_distinct_funding_fetches(self):
        class SlowFundingExchange:
            def __init__(self):
                self.calls = []
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def fetch_funding_rate(self, symbol, params=None):
                with self.lock:
                    self.calls.append(symbol)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.03)
                    return {"symbol": symbol, "fundingRate": 0.0001}
                finally:
                    with self.lock:
                        self.active -= 1

        exchange = SlowFundingExchange()
        cached = CachedMarketDataExchange(exchange, funding_ttl_sec=60)
        symbols = [SYMBOL, SECOND_SYMBOL, BTC_SYMBOL, XAUT_SYMBOL]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(cached.fetch_funding_rate, symbols))

        self.assertEqual(len(results), len(symbols))
        self.assertEqual(exchange.max_active, 1)
        self.assertEqual(set(exchange.calls), set(symbols))

    def test_shared_exchange_does_not_cache_invalid_funding_payload(self):
        exchange = FakeExchange()
        cached = CachedMarketDataExchange(exchange, funding_ttl_sec=300)
        exchange.funding_rate_response = {}

        self.assertEqual(cached.fetch_funding_rate(SYMBOL), {})
        exchange.funding_rate_response = {"fundingRate": "0.0002"}
        self.assertEqual(cached.fetch_funding_rate(SYMBOL), {"fundingRate": "0.0002"})
        self.assertEqual(exchange.fetch_funding_rate_calls, 2)

    def test_shared_exchange_does_not_cache_invalid_ohlcv_payload(self):
        exchange = FakeExchange()
        cached = CachedMarketDataExchange(exchange)
        exchange.fetch_ohlcv_response_override = {"status": "error", "err_code": "502"}

        self.assertEqual(
            cached.fetch_ohlcv(SYMBOL, timeframe="1m", limit=1),
            {"status": "error", "err_code": "502"},
        )

        exchange.fetch_ohlcv_response_override = None
        exchange.ohlcv[(SYMBOL, "1m")] = [[1, 100.0, 101.0, 99.0, 100.5, 10.0]]
        self.assertEqual(
            cached.fetch_ohlcv(SYMBOL, timeframe="1m", limit=1),
            exchange.ohlcv[(SYMBOL, "1m")],
        )
        self.assertEqual(len(exchange.ohlcv_calls), 2)

    def test_shared_exchange_ohlcv_cache_serializes_different_symbol_fetches(self):
        class SlowOhlcvExchange:
            def __init__(self):
                self.calls = []
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def fetch_ohlcv(
                self, symbol, timeframe="1m", since=None, limit=None, params=None
            ):
                with self.lock:
                    self.calls.append(symbol)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.03)
                    return [[1, 100.0, 101.0, 99.0, 100.5, 10.0]]
                finally:
                    with self.lock:
                        self.active -= 1

        exchange = SlowOhlcvExchange()
        cached = CachedMarketDataExchange(exchange)
        symbols = [SYMBOL, SECOND_SYMBOL, BTC_SYMBOL, XAUT_SYMBOL]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda item: cached.fetch_ohlcv(item, timeframe="1m", limit=1),
                    symbols,
                )
            )

        self.assertEqual(len(results), len(symbols))
        self.assertEqual(exchange.max_active, 1)
        self.assertEqual(set(exchange.calls), set(symbols))

    def test_shared_exchange_ticker_cache_is_singleflight_across_threads(self):
        class SlowTickerExchange:
            def __init__(self):
                self.calls = 0
                self.lock = threading.Lock()

            def fetch_ticker(self, symbol, params=None):
                with self.lock:
                    self.calls += 1
                time.sleep(0.02)
                return {"symbol": symbol, "last": 10.0}

        exchange = SlowTickerExchange()
        cached = CachedMarketDataExchange(exchange, ticker_ttl_sec=60)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _index: cached.fetch_ticker(SYMBOL), range(8))
            )

        self.assertEqual(exchange.calls, 1)
        self.assertTrue(all(result["symbol"] == SYMBOL for result in results))

    def test_shared_exchange_serializes_distinct_ticker_fetches(self):
        class SlowTickerExchange:
            def __init__(self):
                self.calls = []
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def fetch_ticker(self, symbol, params=None):
                with self.lock:
                    self.calls.append(symbol)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.03)
                    return {"symbol": symbol, "last": 10.0}
                finally:
                    with self.lock:
                        self.active -= 1

        exchange = SlowTickerExchange()
        cached = CachedMarketDataExchange(exchange, ticker_ttl_sec=60)
        symbols = [SYMBOL, SECOND_SYMBOL, BTC_SYMBOL, XAUT_SYMBOL]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(cached.fetch_ticker, symbols))

        self.assertEqual(len(results), len(symbols))
        self.assertEqual(exchange.max_active, 1)
        self.assertEqual(set(exchange.calls), set(symbols))

    def test_shared_exchange_order_book_cache_is_singleflight_across_threads(self):
        class SlowOrderBookExchange:
            def __init__(self):
                self.calls = 0
                self.lock = threading.Lock()

            def fetch_order_book(self, symbol, limit=None, params=None):
                with self.lock:
                    self.calls += 1
                time.sleep(0.02)
                return {
                    "bids": [[9.99, 100.0]],
                    "asks": [[10.01, 100.0]],
                    "symbol": symbol,
                }

        exchange = SlowOrderBookExchange()
        cached = CachedMarketDataExchange(exchange, order_book_ttl_sec=60)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _index: cached.fetch_order_book(SYMBOL, limit=5), range(8)
                )
            )

        self.assertEqual(exchange.calls, 1)
        self.assertTrue(all(result["symbol"] == SYMBOL for result in results))

    def test_shared_exchange_serializes_distinct_order_book_fetches(self):
        class SlowOrderBookExchange:
            def __init__(self):
                self.calls = []
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def fetch_order_book(self, symbol, limit=None, params=None):
                with self.lock:
                    self.calls.append(symbol)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.03)
                    return {
                        "bids": [[9.99, 100.0]],
                        "asks": [[10.01, 100.0]],
                        "symbol": symbol,
                    }
                finally:
                    with self.lock:
                        self.active -= 1

        exchange = SlowOrderBookExchange()
        cached = CachedMarketDataExchange(exchange, order_book_ttl_sec=60)
        symbols = [SYMBOL, SECOND_SYMBOL, BTC_SYMBOL, XAUT_SYMBOL]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda item: cached.fetch_order_book(item, limit=5), symbols
                )
            )

        self.assertEqual(len(results), len(symbols))
        self.assertEqual(exchange.max_active, 1)
        self.assertEqual(set(exchange.calls), set(symbols))

    def test_thread_safe_exchange_serializes_delegated_calls(self):
        class SlowExchange:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.calls = []
                self.lock = threading.Lock()

            def fetch_positions(self, symbols=None, params=None):
                return self._record("fetch_positions", symbols, params)

            def create_order(self, symbol, type, side, amount, price, params=None):
                return self._record("create_order", symbol, side, amount, price, params)

            def _record(self, name, *payload):
                with self.lock:
                    self.calls.append(name)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.03)
                    return {"name": name, "payload": payload}
                finally:
                    with self.lock:
                        self.active -= 1

        exchange = SlowExchange()
        safe = ThreadSafeExchange(exchange)

        def call(index):
            if index % 2:
                return safe.create_order(SYMBOL, "limit", "buy", 1.0, 10.0, params={})
            return safe.fetch_positions([SYMBOL], params={})

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(call, range(6)))

        self.assertEqual(len(results), 6)
        self.assertEqual(exchange.max_active, 1)
        self.assertEqual(exchange.calls.count("fetch_positions"), 3)
        self.assertEqual(exchange.calls.count("create_order"), 3)

    def test_multi_account_exchange_routes_symbol_private_calls(self):
        primary = FakeExchange("primary")
        secondary = FakeExchange("secondary")
        primary.balance_free = primary.balance_total = 100.0
        secondary.balance_free = secondary.balance_total = 250.0
        primary.positions = [{"symbol": SYMBOL, "side": "long", "contracts": 1.0}]
        secondary.positions = [
            {"symbol": SECOND_SYMBOL, "side": "long", "contracts": 2.0}
        ]
        secondary.open_orders = [
            {"id": "alt_open", "symbol": SECOND_SYMBOL, "side": "buy", "amount": 1.0}
        ]

        exchange = MultiAccountExchange(
            {"primary": primary, "secondary": secondary},
            {"alt2": "secondary"},
        )

        exchange.create_order(
            SECOND_SYMBOL, "limit", "buy", 2.0, 10.0, params={"reduceOnly": False}
        )
        exchange.create_order(SYMBOL, "limit", "buy", 1.0, 10.0, params={})
        positions = exchange.fetch_positions([SYMBOL, SECOND_SYMBOL], params={})
        orders = exchange.fetch_open_orders(SECOND_SYMBOL, params={})
        exchange.cancel_order("alt_open", SECOND_SYMBOL, params={})
        exchange.set_leverage(9, SECOND_SYMBOL, params={})
        exchange.set_position_mode(False, None, params={})
        secondary_balance = exchange.fetch_balance_for_symbol(SECOND_SYMBOL, params={})
        merged_balance = exchange.fetch_balance(params={})

        self.assertEqual(
            [order["symbol"] for order in primary.created_orders], [SYMBOL]
        )
        self.assertEqual(
            [order["symbol"] for order in secondary.created_orders], [SECOND_SYMBOL]
        )
        self.assertEqual(
            {position["symbol"] for position in positions}, {SYMBOL, SECOND_SYMBOL}
        )
        self.assertEqual([order["id"] for order in orders], ["alt_open"])
        self.assertEqual(primary.fetch_positions_calls, 1)
        self.assertEqual(secondary.fetch_positions_calls, 1)
        self.assertEqual(primary.fetch_open_orders_calls, 0)
        self.assertEqual(secondary.fetch_open_orders_calls, 1)
        self.assertEqual(secondary.canceled_orders, [("alt_open", SECOND_SYMBOL, {})])
        self.assertEqual(secondary.set_leverage_calls, [(9, SECOND_SYMBOL, {})])
        self.assertEqual(len(primary.set_position_mode_calls), 1)
        self.assertEqual(len(secondary.set_position_mode_calls), 1)
        self.assertEqual(
            secondary_balance["free"][config.EXCHANGE.quote_currency], 100.0
        )
        self.assertEqual(merged_balance["free"][config.EXCHANGE.quote_currency], 100.0)
        self.assertEqual(primary.fetch_balance_calls, 2)
        self.assertEqual(secondary.fetch_balance_calls, 0)

    def test_multi_account_exchange_deduplicates_aggregate_private_reads(self):
        primary = FakeExchange("primary")
        secondary = FakeExchange("secondary")
        duplicate_order = {
            "id": "same_order",
            "symbol": SYMBOL,
            "side": "sell",
            "amount": 1.0,
        }
        duplicate_position = {"symbol": SYMBOL, "side": "long", "contracts": 1.0}
        primary.open_orders = [dict(duplicate_order)]
        secondary.open_orders = [dict(duplicate_order)]
        primary.positions = [dict(duplicate_position)]
        secondary.positions = [dict(duplicate_position)]
        exchange = MultiAccountExchange(
            {"primary": primary, "secondary": secondary},
            {"alt2": "secondary"},
        )

        orders = exchange.fetch_open_orders(None, params={})
        positions = exchange.fetch_positions(None, params={})

        self.assertEqual(orders, [duplicate_order])
        self.assertEqual(positions, [duplicate_position])

    def test_bot_orders_and_balance_use_routed_api_account_for_symbol(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            primary = FakeExchange("primary")
            secondary = FakeExchange("secondary")
            primary.balance_free = primary.balance_total = 100.0
            secondary.balance_free = secondary.balance_total = 300.0
            bot.exchange = MultiAccountExchange(
                {"primary": primary, "secondary": secondary},
                {"alt2": "secondary"},
            )
            bot.market_by_symbol = {SYMBOL: MARKET, SECOND_SYMBOL: SECOND_MARKET}
            bot.symbols = [SYMBOL, SECOND_SYMBOL]
            bot._reset_private_caches()

            primary_snapshot = bot._account_snapshot(SYMBOL)
            secondary_snapshot = bot._account_snapshot(SECOND_SYMBOL)
            bot._create_one_way_order(
                symbol=SECOND_SYMBOL,
                order_type="limit",
                side=config.ENTRY_SIDE,
                amount=2.0,
                price=10.0,
                leverage=5,
            )

            self.assertEqual(primary_snapshot["free"], 100.0)
            self.assertEqual(secondary_snapshot["free"], 100.0)
            self.assertEqual(primary.fetch_balance_calls, 1)
            self.assertEqual(secondary.fetch_balance_calls, 0)
            self.assertEqual(primary.created_orders, [])
            self.assertEqual(secondary.created_orders[-1]["symbol"], SECOND_SYMBOL)

    def test_public_ohlcv_fetch_retries_transient_gateway_failure(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=2,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_ohlcv_failures = [
                    ccxt.ExchangeNotAvailable(
                        "USDT 504 Gateway Timeout <!DOCTYPE html><html>too much html"
                    )
                ]
                bot.exchange.ohlcv[(SYMBOL, "1m")] = [
                    [1, 100.0, 101.0, 99.0, 100.5, 10.0],
                    [60_000, 100.5, 102.0, 100.0, 101.5, 12.0],
                ]

                candles = bot._closed_candles(SYMBOL, 2, timeframe="1m")

                self.assertEqual(len(candles), 2)
                self.assertEqual(len(bot.exchange.ohlcv_calls), 2)
                self.assertEqual(
                    bot.exchange.urls["hostnames"]["contract"], "api.two.test"
                )
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["level"], "WARNING")
                self.assertEqual(rows[-1]["event"], "signal_invalid")
                self.assertEqual(rows[-1]["reason"], "ohlcv_network_retry")

    def test_public_ohlcv_fetch_does_not_retry_htx_invalid_parameter(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            exchange_config = replace(
                config.EXCHANGE,
                market_load_retries=4,
                contract_hostnames=("api.one.test", "api.two.test"),
            )
            with override_config(RUNTIME=config.RUNTIME, EXCHANGE=exchange_config):
                bot = self.make_bot(Path(raw_tmp))
                bot.exchange.fetch_ohlcv_failures = [
                    ccxt.NetworkError(
                        'htx {"status":"error","err-code":"invalid-parameter",'
                        '"err-msg":"invalid parameter"}'
                    )
                ]

                with self.assertRaises(ccxt.NetworkError):
                    bot._closed_candles(SYMBOL, 2, timeframe="1m")

                self.assertEqual(len(bot.exchange.ohlcv_calls), 1)
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertFalse(
                    any(row["reason"] == "ohlcv_network_retry" for row in rows)
                )

    def test_signal_candle_failure_logs_exception_type_and_htx_error_code(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            bot.benchmark_symbol = BTC_SYMBOL
            bot.symbols = [SECOND_SYMBOL]
            bot.entry_symbols = {SECOND_SYMBOL}
            bot.market_by_symbol = {
                BTC_SYMBOL: BTC_MARKET,
                SECOND_SYMBOL: SECOND_MARKET,
            }
            benchmark_candles = ohlcv_series([100.0 + index for index in range(120)])

            def fake_closed_candles(
                symbol, limit, max_ts=None, timeframe=None, exchange=None
            ):
                if symbol == BTC_SYMBOL:
                    return benchmark_candles[-int(limit) :]
                raise ccxt.NetworkError(
                    'htx {"status":"error","err-code":"invalid-parameter",'
                    '"err-msg":"invalid parameter"}'
                )

            bot._closed_candles = fake_closed_candles

            bot._update_signal_cache_if_needed()

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = [
                    row
                    for row in csv.DictReader(handle)
                    if row["reason"] == "symbol_candles_unavailable"
                ]
            self.assertTrue(rows)
            self.assertEqual(rows[-1]["exception_type"], "NetworkError")
            self.assertEqual(rows[-1]["error_code"], "invalid-parameter")
            self.assertEqual(rows[-1]["retryable"], "0")

    def test_signal_cache_retries_same_candle_after_retryable_symbol_fetch_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            strategy = self.ema_test_strategy(
                ema_use_rs_confirmation=False,
                ema_use_btc_risk_filter=False,
                ema_chop_filter_enabled=False,
                ema_volume_confirmation_enabled=False,
            )
            with override_config(STRATEGY=strategy):
                bot = self.make_bot(Path(raw_tmp))
                bot.benchmark_symbol = BTC_SYMBOL
                bot.symbols = [SYMBOL]
                bot.entry_symbols = {SYMBOL}
                bot.market_by_symbol = {BTC_SYMBOL: BTC_MARKET, SYMBOL: MARKET}
                benchmark_candles = ohlcv_series([100.0] * 120)
                symbol_candles = ohlcv_series(
                    [100.0 + index * 0.1 for index in range(120)]
                )
                calls = {"symbol": 0}

                def fake_closed_candles(
                    symbol, limit, max_ts=None, timeframe=None, exchange=None
                ):
                    if symbol == BTC_SYMBOL:
                        return benchmark_candles[-int(limit) :]
                    calls["symbol"] += 1
                    if calls["symbol"] == 1:
                        raise ccxt.RequestTimeout("temporary ohlcv timeout")
                    return symbol_candles[-int(limit) :]

                bot._closed_candles = fake_closed_candles

                first_updated = bot._update_signal_cache_if_needed()
                second_updated = bot._update_signal_cache_if_needed()

                self.assertTrue(first_updated)
                self.assertTrue(second_updated)
                self.assertEqual(calls["symbol"], 2)
                self.assertEqual(
                    bot.signal_cache["closed_candle_ts"], int(benchmark_candles[-1][0])
                )
                self.assertIn(SYMBOL, bot.signal_cache["symbols"])

    def test_log_message_omits_html_gateway_body(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            message = bot._compact_log_message(
                "USDT 504 Gateway Timeout <!DOCTYPE html><html><body>cloudflare body</body></html>"
            )

            self.assertEqual(
                message, "USDT 504 Gateway Timeout [html response omitted]"
            )

    def test_step_network_exception_logs_warning_not_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            bot._log_step_exception(SYMBOL, ccxt.RequestTimeout("timeout"))

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["level"], "WARNING")
            self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
            self.assertEqual(rows[-1]["reason"], "step_network_error")

    def test_step_non_network_exception_logs_fault(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))

            bot._log_step_exception(SYMBOL, RuntimeError("logic failed"))

            with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["level"], "FAULT")
            self.assertEqual(rows[-1]["event"], "state_exchange_mismatch")
            self.assertEqual(rows[-1]["reason"], "step_error")

    def test_runner_sleep_subtracts_cycle_elapsed_time(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, poll_interval_sec=3)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                with (
                    patch("htxbot.runner.time.time", return_value=101.25),
                    patch("htxbot.runner.time.sleep") as sleep,
                ):
                    bot._sleep_after_poll(100.0)

                sleep.assert_called_once_with(1.75)

    def test_bulk_private_snapshots_fall_back_when_some_payload_symbols_are_missing(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            with override_config(RUNTIME=config.RUNTIME):
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
                    {
                        "id": "with_symbol",
                        "symbol": SYMBOL,
                        "side": "sell",
                        "price": 101.0,
                        "amount": 2.0,
                    },
                    {
                        "id": "without_symbol",
                        "side": "sell",
                        "price": 102.0,
                        "amount": 3.0,
                    },
                ]

                bot._reset_private_caches()
                snapshot = bot._fetch_position_snapshot(SYMBOL)
                orders = bot._fetch_open_orders(SYMBOL)

                self.assertEqual(snapshot["long_size"], 5.0)
                self.assertEqual(
                    {order["id"] for order in orders}, {"with_symbol", "without_symbol"}
                )
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

    def test_enabled_profile_names_reads_global_htxbot_prefix(self):
        previous_bot_profiles = os.environ.get("BOT_PROFILES")
        previous_prefixed = os.environ.get("HTXBOT_BOT_PROFILES")
        os.environ.pop("BOT_PROFILES", None)
        os.environ["HTXBOT_BOT_PROFILES"] = "short"
        try:
            self.assertEqual(config.enabled_profile_names(), ("short",))
        finally:
            if previous_bot_profiles is None:
                os.environ.pop("BOT_PROFILES", None)
            else:
                os.environ["BOT_PROFILES"] = previous_bot_profiles
            if previous_prefixed is None:
                os.environ.pop("HTXBOT_BOT_PROFILES", None)
            else:
                os.environ["HTXBOT_BOT_PROFILES"] = previous_prefixed

    def test_long_short_direction_invariants_are_mirrored(self):
        with config.use_profile("long"):
            self.assertEqual(config.POSITION_SIDE, "long")
            self.assertEqual(config.ENTRY_SIDE, "buy")
            self.assertEqual(config.EXIT_SIDE, "sell")
        with config.use_profile("short"):
            self.assertEqual(config.POSITION_SIDE, "short")
            self.assertEqual(config.ENTRY_SIDE, "sell")
            self.assertEqual(config.EXIT_SIDE, "buy")

    def test_combined_updates_signal_caches_in_parallel_for_profiles(self):
        active = {"count": 0, "max": 0}
        seen_sides = []
        lock = threading.Lock()

        class SignalBot:
            def __init__(self, profile):
                self.profile = profile

            def _market_data_max_workers(self):
                return self.profile.runtime.market_data_max_workers

            def _update_signal_cache_if_needed(self):
                with lock:
                    seen_sides.append(config.POSITION_SIDE)
                    active["count"] += 1
                    active["max"] = max(active["max"], active["count"])
                try:
                    time.sleep(0.03)
                finally:
                    with lock:
                        active["count"] -= 1

        long_profile = replace(
            config.resolve_profile("long"),
            runtime=replace(
                config.resolve_profile("long").runtime, market_data_max_workers=2
            ),
        )
        short_profile = replace(
            config.resolve_profile("short"),
            runtime=replace(
                config.resolve_profile("short").runtime, market_data_max_workers=2
            ),
        )
        combined = object.__new__(CombinedHtxFuturesBot)
        combined.bots = [SignalBot(long_profile), SignalBot(short_profile)]

        CombinedHtxFuturesBot._update_signal_caches(combined)

        self.assertGreater(active["max"], 1)
        self.assertEqual(set(seen_sides), {"long", "short"})

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

    def test_combined_run_checks_runtime_lock_before_live_cycle(self):
        class LockBot:
            def __init__(self):
                self.profile = config.resolve_profile("long")
                self.assert_calls = 0
                self.release_calls = 0

            def _log_event(self, *args, **kwargs):
                pass

            def _assert_runtime_lock_owned(self):
                self.assert_calls += 1
                raise RuntimeError("lock ownership lost")

            def _release_runtime_lock(self):
                self.release_calls += 1

        combined = object.__new__(CombinedHtxFuturesBot)
        lock_bot = LockBot()
        combined.bots = [lock_bot]
        combined.setup = lambda: None
        run_once_calls = []
        combined.run_once = lambda: run_once_calls.append(True)

        with self.assertRaisesRegex(RuntimeError, "lock ownership lost"):
            CombinedHtxFuturesBot.run(combined)

        self.assertEqual(lock_bot.assert_calls, 1)
        self.assertEqual(lock_bot.release_calls, 1)
        self.assertEqual(run_once_calls, [])

    def test_reserved_opposite_position_does_not_disable_combined_profile(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("short"):
            with override_config(RUNTIME=config.RUNTIME):
                bot = self.make_bot(Path(raw_tmp))
                bot.external_reserved_symbols = {SYMBOL}
                state = bot._get_state(SYMBOL)
                state.entry_orders = [
                    {"id": "short_entry", "side": "sell", "price": 10.1, "amount": 1.0}
                ]

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
                self.assertFalse(state.entry_orders)
                self.assertIn(
                    ("short_entry", SYMBOL, {"marginMode": config.RISK.margin_mode}),
                    bot.exchange.canceled_orders,
                )
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(
                        row["event"] == "entry_order_canceled"
                        and row["order_id"] == "short_entry"
                        and row["side"] == "sell"
                        and row["reason"] == "reserved_by_other_profile"
                        for row in rows
                    )
                )
                self.assertFalse(
                    any(
                        row["event"] == "sell_order_canceled"
                        and row["order_id"] == "short_entry"
                        for row in rows
                    )
                )
                self.assertTrue(any(row["event"] == "profile_reserved" for row in rows))
                self.assertFalse(
                    any(
                        row["event"] == "state_exchange_mismatch"
                        and row["reason"] == "reserved_by_other_profile"
                        for row in rows
                    )
                )

    def test_stale_profile_state_closes_before_opposite_combined_reservation(self):
        for profile_name, opposite_side, opposite_entry in (
            ("long", "short", 90.0),
            ("short", "long", 110.0),
        ):
            with self.subTest(profile=profile_name):
                with (
                    tempfile.TemporaryDirectory() as raw_tmp,
                    config.use_profile(profile_name),
                ):
                    bot = self.make_bot(Path(raw_tmp))
                    bot.external_reserved_symbols = {SYMBOL}
                    state = bot._get_state(SYMBOL)
                    state.position_size = 5.0
                    state.position_available = 0.0
                    state.position_frozen = 5.0
                    state.position_side = profile_name
                    state.entry_price = 100.0
                    state.cycle_opened_at = time.time() - 60.0
                    if profile_name == "long":
                        state.total_bought_amount = 5.0
                        state.total_bought_quote = 500.0
                    else:
                        state.total_sold_amount = 5.0
                        state.total_sold_quote = 500.0

                    snapshot = {
                        f"{profile_name}_size": 0.0,
                        f"{profile_name}_available": 0.0,
                        f"{opposite_side}_size": 5.0,
                        f"{opposite_side}_available": 5.0,
                        f"{opposite_side}_entry_price": opposite_entry,
                    }

                    closed_status = bot._sync_state_with_position(
                        SYMBOL, snapshot, open_orders=[]
                    )

                    self.assertEqual(closed_status, "closed")
                    self.assertNotIn(SYMBOL, bot.disabled_symbols)
                    self.assertEqual(bot._get_state(SYMBOL).position_size, 0.0)
                    with bot.cycle_stats_path.open(
                        newline="", encoding="utf-8"
                    ) as handle:
                        cycle_rows = list(csv.DictReader(handle))
                    self.assertTrue(cycle_rows)
                    self.assertEqual(
                        cycle_rows[-1]["close_reason"],
                        f"position_replaced_by_{opposite_side}",
                    )
                    self.assertAlmostEqual(
                        float(cycle_rows[-1]["average_exit_price"]), opposite_entry
                    )
                    self.assertLess(float(cycle_rows[-1]["realized_pnl_quote"]), 0.0)

                    reserved_status = bot._sync_state_with_position(
                        SYMBOL, snapshot, open_orders=[]
                    )

                    self.assertEqual(reserved_status, "reserved")
                    self.assertNotIn(SYMBOL, bot.disabled_symbols)

    def test_position_gone_without_fill_details_uses_neutral_entry_price(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 5.0
            state.position_available = 5.0
            state.position_side = "long"
            state.entry_price = 100.0
            state.total_bought_amount = 5.0
            state.total_bought_quote = 500.0

            status = bot._sync_state_with_position(
                SYMBOL,
                {
                    "long_size": 0.0,
                    "long_available": 0.0,
                    "short_size": 0.0,
                    "short_available": 0.0,
                },
                open_orders=[],
            )

            self.assertEqual(status, "closed")
            with bot.cycle_stats_path.open(newline="", encoding="utf-8") as handle:
                cycle_rows = list(csv.DictReader(handle))
            self.assertTrue(cycle_rows)
            self.assertAlmostEqual(float(cycle_rows[-1]["average_exit_price"]), 100.0)

    def test_exit_ladder_cycle_stats_keep_take_profit_close_reason(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            bot = self.make_bot(Path(raw_tmp))
            state = bot._get_state(SYMBOL)
            state.position_size = 5.0
            state.position_available = 0.0
            state.position_frozen = 5.0
            state.position_side = "long"
            state.entry_price = 100.0
            state.total_bought_amount = 5.0
            state.total_bought_quote = 500.0
            state.sell_ladder_orders = [
                {
                    "id": "tp_1",
                    "side": "sell",
                    "amount": 5.0,
                    "price": 105.0,
                    "mode": "normal",
                }
            ]

            status = bot._sync_state_with_position(
                SYMBOL,
                {
                    "long_size": 0.0,
                    "long_available": 0.0,
                    "short_size": 0.0,
                    "short_available": 0.0,
                },
                open_orders=[],
            )

            self.assertEqual(status, "closed")
            with bot.cycle_stats_path.open(newline="", encoding="utf-8") as handle:
                cycle_rows = list(csv.DictReader(handle))
            self.assertTrue(cycle_rows)
            self.assertEqual(cycle_rows[-1]["close_reason"], "exit_ladder_filled")

    def test_position_gone_uses_pending_hard_stop_market_close_fill_price(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(config.RUNTIME, fetch_fill_details_on_sync=True)
            with override_config(RUNTIME=runtime):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 0.0
                state.position_frozen = 5.0
                state.position_side = "long"
                state.entry_price = 100.0
                state.total_bought_amount = 5.0
                state.total_bought_quote = 500.0
                state.hard_stop_order = {
                    "id": "market_close_1",
                    "side": "sell",
                    "amount": 5.0,
                    "created_at": time.time() - 5.0,
                    "hard_stop_loss": True,
                    "market_close": True,
                    "reduce_only": True,
                    "reason": "hard_stop_loss_market_close",
                }
                bot.exchange.has["fetchOrder"] = True
                bot.exchange.fetch_order_responses["market_close_1"] = {
                    "id": "market_close_1",
                    "symbol": SYMBOL,
                    "side": "sell",
                    "status": "closed",
                    "amount": 5.0,
                    "filled": 5.0,
                    "remaining": 0.0,
                    "average": 95.0,
                    "cost": 475.0,
                    "fee": {"cost": 0.2, "currency": "USDT"},
                }

                status = bot._sync_state_with_position(
                    SYMBOL,
                    {
                        "long_size": 0.0,
                        "long_available": 0.0,
                        "short_size": 0.0,
                        "short_available": 0.0,
                    },
                    open_orders=[],
                )

                self.assertEqual(status, "closed")
                self.assertEqual(
                    bot.exchange.fetch_order_calls[-1][0], "market_close_1"
                )
                with bot.csv_path.open(newline="", encoding="utf-8") as handle:
                    trade_rows = list(csv.DictReader(handle))
                fills = [
                    row for row in trade_rows if row["event"] == "sell_order_filled"
                ]
                self.assertTrue(fills)
                self.assertEqual(fills[-1]["order_id"], "market_close_1")
                self.assertAlmostEqual(float(fills[-1]["price"]), 95.0)
                self.assertEqual(fills[-1]["fill_source"], "order")
                with bot.cycle_stats_path.open(newline="", encoding="utf-8") as handle:
                    cycle_rows = list(csv.DictReader(handle))
                self.assertTrue(cycle_rows)
                self.assertAlmostEqual(
                    float(cycle_rows[-1]["average_exit_price"]), 95.0
                )
                self.assertEqual(
                    cycle_rows[-1]["close_reason"], "hard_stop_loss_market_close"
                )
                self.assertLess(float(cycle_rows[-1]["realized_pnl_quote"]), 0.0)

    def test_dust_close_cycle_stats_keep_dust_close_reason(self):
        with tempfile.TemporaryDirectory() as raw_tmp, config.use_profile("long"):
            runtime = replace(
                config.RUNTIME,
                reduce_only_enabled=True,
                fetch_fill_details_on_sync=True,
            )
            risk = replace(
                config.RISK, dust_position_notional=100.0, dust_close_enabled=True
            )
            with override_config(RUNTIME=runtime, RISK=risk):
                bot = self.make_bot(Path(raw_tmp))
                state = bot._get_state(SYMBOL)
                state.position_size = 5.0
                state.position_available = 5.0
                state.position_side = "long"
                state.entry_price = 10.0
                state.total_bought_amount = 5.0
                state.total_bought_quote = 50.0

                placed = bot._maybe_close_dust_position(SYMBOL, [])

                self.assertTrue(placed)
                self.assertEqual(state.pending_close_reason, "dust_position_close")
                self.assertEqual(
                    state.pending_close_order["reason"], "dust_position_close"
                )
                order_id = state.pending_close_order["id"]
                bot.exchange.has["fetchOrder"] = True
                bot.exchange.fetch_order_responses[order_id] = {
                    "id": order_id,
                    "symbol": SYMBOL,
                    "side": "sell",
                    "status": "closed",
                    "amount": 5.0,
                    "filled": 5.0,
                    "remaining": 0.0,
                    "average": 9.5,
                    "cost": 47.5,
                    "fee": {"cost": 0.02, "currency": "USDT"},
                }

                status = bot._sync_state_with_position(
                    SYMBOL,
                    {
                        "long_size": 0.0,
                        "long_available": 0.0,
                        "short_size": 0.0,
                        "short_available": 0.0,
                    },
                    open_orders=[],
                )

                self.assertEqual(status, "closed")
                with bot.cycle_stats_path.open(newline="", encoding="utf-8") as handle:
                    cycle_rows = list(csv.DictReader(handle))
                self.assertTrue(cycle_rows)
                self.assertEqual(cycle_rows[-1]["close_reason"], "dust_position_close")
                self.assertAlmostEqual(float(cycle_rows[-1]["average_exit_price"]), 9.5)
                self.assertEqual(bot._get_state(SYMBOL).pending_close_order, {})

    def test_combined_reserved_symbols_include_exchange_side_opposite_profile_activity(
        self,
    ):
        class ReservationBot:
            def __init__(self, profile, positions=None, orders=None):
                self.profile = profile
                self.states = {}
                self.symbols = [SYMBOL]
                self.positions = list(positions or [])
                self.orders = list(orders or [])
                self.events = []

            def _bulk_positions_by_symbol(self):
                return {SYMBOL: list(self.positions)}

            def _bulk_open_orders_by_symbol(self):
                return {SYMBOL: list(self.orders)}

            def _safe_float(self, value, default=0.0):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return default

            def _order_remaining_amount(self, order):
                return self._safe_float(
                    order.get("remaining"), self._safe_float(order.get("amount"), 0.0)
                )

            def _get_min_contracts(self, symbol):
                return 1.0

            def _log_event(self, *args, **kwargs):
                self.events.append((args, kwargs))

        long_profile = replace(
            config.resolve_profile("long"),
            runtime=replace(config.resolve_profile("long").runtime),
        )
        short_profile = replace(
            config.resolve_profile("short"),
            runtime=replace(config.resolve_profile("short").runtime),
        )
        long_bot = ReservationBot(long_profile)
        short_bot = ReservationBot(
            short_profile,
            positions=[{"symbol": SYMBOL, "side": "short", "contracts": 3.0}],
        )
        combined = object.__new__(CombinedHtxFuturesBot)
        combined.bots = [long_bot, short_bot]
        short_bot.symbols = []

        reserved = CombinedHtxFuturesBot._reserved_symbols(combined, exclude=long_bot)
        self.assertIn(SYMBOL, reserved)

        short_bot.positions = []
        short_bot.orders = [
            {"symbol": SYMBOL, "side": "sell", "amount": 2.0, "remaining": 2.0}
        ]

        reserved = CombinedHtxFuturesBot._reserved_symbols(combined, exclude=long_bot)
        self.assertIn(SYMBOL, reserved)

    def make_btc_hedge_combined(
        self, tmp_path: Path, positions=None, open_orders=None, ticker=None
    ):
        with config.use_profile("long"):
            long_bot = self.make_bot(tmp_path / "long")
        with config.use_profile("short"):
            short_bot = self.make_bot(tmp_path / "short")
        shared_exchange = FakeExchange()
        shared_exchange.positions = list(positions or [])
        shared_exchange.open_orders = list(open_orders or [])
        if ticker is not None:
            shared_exchange.ticker = dict(ticker)

        for profile_name, bot in (("long", long_bot), ("short", short_bot)):
            profile = config.resolve_profile(profile_name)
            bot.profile = profile
            bot.profile_name = profile.name
            bot.exchange = shared_exchange
            bot.symbols = [SYMBOL, SECOND_SYMBOL]
            bot.market_by_symbol = {
                SYMBOL: MARKET,
                SECOND_SYMBOL: SECOND_MARKET,
                BTC_SYMBOL: BTC_MARKET,
            }
            bot.benchmark_symbol = BTC_SYMBOL

        combined = object.__new__(CombinedHtxFuturesBot)
        combined.bots = [long_bot, short_bot]
        combined._last_btc_hedge_action_at = 0.0
        combined._btc_hedge_log_at = {}
        return combined, shared_exchange

    def test_btc_hedge_opens_short_for_net_long_exposure(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 10.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": SECOND_SYMBOL,
                    "side": "short",
                    "contracts": 5.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 99.9, "ask": 100.1, "last": 100.0},
                )

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(len(exchange.created_orders), 1)
            order = exchange.created_orders[-1]
            self.assertEqual(order["symbol"], BTC_SYMBOL)
            self.assertEqual(order["type"], "market")
            self.assertEqual(order["side"], "sell")
            self.assertEqual(order["amount"], 5.0)
            self.assertFalse(order["params"].get("reduceOnly", False))

    def test_btc_hedge_open_orders_params_type_error_blocks_rebalance(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 10.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": SECOND_SYMBOL,
                    "side": "short",
                    "contracts": 5.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 99.9, "ask": 100.1, "last": 100.0},
                )
                exchange.fetch_open_orders_type_error_on_params = True

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(exchange.created_orders, [])
            self.assertEqual(exchange.fetch_open_orders_calls, 1)
            with combined.bots[0].csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            order_rows = [
                row for row in rows if row["reason"] == "open_orders_fetch_failed"
            ]
            self.assertTrue(order_rows)
            self.assertEqual(order_rows[-1]["event"], "btc_hedge")
            self.assertEqual(order_rows[-1]["exception_type"], "TypeError")

    def test_btc_hedge_reduces_existing_same_side_with_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 10.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": SECOND_SYMBOL,
                    "side": "short",
                    "contracts": 5.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": BTC_SYMBOL,
                    "side": "short",
                    "contracts": 8.0,
                    "entryPrice": 100.0,
                    "available": 8.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 99.0, "ask": 101.0, "last": 100.0},
                )

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(len(exchange.created_orders), 1)
            order = exchange.created_orders[-1]
            self.assertEqual(order["symbol"], BTC_SYMBOL)
            self.assertEqual(order["side"], "buy")
            self.assertEqual(order["amount"], 3.0)
            self.assertTrue(order["params"].get("reduceOnly"))

    def test_btc_hedge_flip_closes_current_side_before_opening_opposite(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 1.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": SECOND_SYMBOL,
                    "side": "short",
                    "contracts": 6.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": BTC_SYMBOL,
                    "side": "short",
                    "contracts": 3.0,
                    "entryPrice": 100.0,
                    "available": 3.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 99.0, "ask": 101.0, "last": 100.0},
                )

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(len(exchange.created_orders), 1)
            order = exchange.created_orders[-1]
            self.assertEqual(order["symbol"], BTC_SYMBOL)
            self.assertEqual(order["side"], "buy")
            self.assertEqual(order["amount"], 3.0)
            self.assertTrue(order["params"].get("reduceOnly"))

    def test_btc_hedge_waits_when_btc_open_orders_exist(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 10.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            open_orders = [
                {
                    "id": "btc_pending",
                    "symbol": BTC_SYMBOL,
                    "side": "sell",
                    "amount": 1.0,
                    "remaining": 1.0,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    open_orders=open_orders,
                    ticker={"bid": 99.0, "ask": 101.0, "last": 100.0},
                )

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(exchange.created_orders, [])

    def test_btc_hedge_waits_when_reduce_closeable_is_zero(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 10.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": SECOND_SYMBOL,
                    "side": "short",
                    "contracts": 5.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
                {
                    "symbol": BTC_SYMBOL,
                    "side": "short",
                    "contracts": 8.0,
                    "entryPrice": 100.0,
                    "available": 0.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 99.0, "ask": 101.0, "last": 100.0},
                )

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(exchange.created_orders, [])

    def test_btc_hedge_default_does_not_open_with_single_profile(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 10.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 99.0, "ask": 101.0, "last": 100.0},
                )
                combined.bots = [combined.bots[0]]

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(exchange.created_orders, [])

    def test_btc_hedge_single_profile_closes_existing_hedge_reduce_only(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": BTC_SYMBOL,
                    "side": "short",
                    "contracts": 3.0,
                    "entryPrice": 100.0,
                    "available": 3.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 80.0, "ask": 120.0, "last": 100.0},
                )
                combined.bots = [combined.bots[0]]

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(len(exchange.created_orders), 1)
            order = exchange.created_orders[-1]
            self.assertEqual(order["symbol"], BTC_SYMBOL)
            self.assertEqual(order["side"], "buy")
            self.assertEqual(order["amount"], 3.0)
            self.assertTrue(order["params"].get("reduceOnly"))

    def test_btc_hedge_open_waits_when_btc_spread_is_too_wide(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_max_spread_bps=30.0,
                btc_hedge_cooldown_sec=0.0,
            )
            positions = [
                {
                    "symbol": SYMBOL,
                    "side": "long",
                    "contracts": 10.0,
                    "entryPrice": 100.0,
                    "marginMode": config.RISK.margin_mode,
                },
            ]
            with override_config(HEDGE=hedge):
                combined, exchange = self.make_btc_hedge_combined(
                    Path(raw_tmp),
                    positions=positions,
                    ticker={"bid": 90.0, "ask": 110.0, "last": 100.0},
                )

                CombinedHtxFuturesBot._rebalance_btc_hedge(combined)

            self.assertEqual(exchange.created_orders, [])

    def test_combined_rejects_mismatched_api_credentials(self):
        long_profile = replace(
            config.resolve_profile("long"),
            api_credentials=replace(
                config.resolve_profile("long").api_credentials, api_key="long_key"
            ),
        )
        short_profile = replace(
            config.resolve_profile("short"),
            api_credentials=replace(
                config.resolve_profile("short").api_credentials, api_key="short_key"
            ),
        )
        combined = object.__new__(CombinedHtxFuturesBot)
        combined.profiles = [long_profile, short_profile]

        with self.assertRaisesRegex(RuntimeError, "same primary HTX API credentials"):
            CombinedHtxFuturesBot._validate_shared_exchange_profiles(combined)

    def test_combined_rejects_mismatched_api_account_routing(self):
        credentials = config.resolve_profile("long").api_credentials
        long_profile = replace(
            config.resolve_profile("long"),
            api_accounts=(
                config.ApiAccountSettings("primary", credentials, ("test",)),
                config.ApiAccountSettings("secondary", credentials, ("alt2",)),
            ),
        )
        short_profile = replace(
            config.resolve_profile("short"),
            api_accounts=(
                config.ApiAccountSettings("primary", credentials, ("test", "alt2")),
            ),
        )
        combined = object.__new__(CombinedHtxFuturesBot)
        combined.profiles = [long_profile, short_profile]

        with self.assertRaisesRegex(RuntimeError, "same HTX API account routing"):
            CombinedHtxFuturesBot._validate_shared_exchange_profiles(combined)

    def test_combined_uses_separate_external_feeds_for_different_profile_settings(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)

            def isolated_profile(name, timeout):
                profile = config.resolve_profile(name)
                return replace(
                    profile,
                    api_credentials=replace(
                        profile.api_credentials,
                        api_key="test_key",
                        api_secret="test_secret",
                    ),
                    runtime=replace(
                        profile.runtime,
                        dry_run=True,
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
                    external_price_feed=replace(
                        profile.external_price_feed, rest_timeout_sec=timeout
                    ),
                )

            combined = CombinedHtxFuturesBot(
                profiles=(
                    isolated_profile("long", 1.0),
                    isolated_profile("short", 9.0),
                )
            )

            self.assertIsNot(
                combined.bots[0].external_price_feed,
                combined.bots[1].external_price_feed,
            )
            self.assertEqual(
                combined.bots[0].external_price_feed.settings.rest_timeout_sec, 1.0
            )
            self.assertEqual(
                combined.bots[1].external_price_feed.settings.rest_timeout_sec, 9.0
            )

    def test_secondary_api_coin_universe_is_loaded_from_env(self):
        with patch.dict(
            os.environ,
            {
                "COINS": "doge,bonk",
                "HTXBOT_COINS": "doge,bonk",
                "COINS_2": "1inch,aixbt,zro",
                "HTXBOT_COINS_2": "1inch,aixbt,zro",
            },
            clear=False,
        ):
            profile = config._make_profile("alias", "short", ())

        self.assertEqual(profile.coins, ("doge", "bonk", "1inch", "aixbt", "zro"))
        self.assertEqual(profile.api_accounts[1].coins, ("1inch", "aixbt", "zro"))


if __name__ == "__main__":
    unittest.main()
