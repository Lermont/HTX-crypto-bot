# -*- coding: utf-8 -*-

from pathlib import Path
import threading
from typing import Dict, List, Optional

import config

from .concurrency import ensure_runtime_locks
from .exchange import ExchangeMixin
from .external_price import ExternalPriceFeed
from .models import TradeState
from .monitoring import MonitoringMixin
from .runner import RunnerMixin
from .signal_engine import SignalMixin
from .shared_exchange import ensure_thread_safe_exchange
from .state import StateMixin
from .strategy import StrategyMixin


class HtxFuturesBot(
    MonitoringMixin,
    StateMixin,
    ExchangeMixin,
    SignalMixin,
    StrategyMixin,
    RunnerMixin,
):
    CSV_HEADER = (
        "ts", "level", "event", "symbol", "side", "order_id",
        "price", "amount", "filled", "remaining", "position_size",
        "entry_price", "notional", "fee_quote", "fee_currency",
        "fill_source", "rs30", "rs60", "ema50", "ema100", "reason",
        "message", "exception_type", "error_code", "retryable",
    )
    CYCLE_STATS_HEADER = (
        "symbol", "opened_at", "closed_at", "leverage", "margin_mode",
        "planned_budget", "total_entry_notional", "total_exit_notional",
        "average_entry_price", "average_exit_price", "buy_fees", "sell_fees",
        "realized_pnl_quote", "realized_pnl_percent_on_notional",
        "realized_pnl_percent_on_margin", "holding_minutes",
        "max_buy_stage", "frozen_no_more_buys", "close_reason",
        "entry_rs30", "entry_rs60", "entry_ema30", "entry_ema60",
        "strategy_name", "entry_ema25d", "entry_ema50d", "entry_ema1d",
        "entry_ema2d", "entry_ema50", "entry_ema100", "entry_btc_return_30m",
        "max_averaging_stage", "breakeven_activated",
    )
    MACRO_CSV_HEADER = (
        "ts", "profile", "regime", "gold_symbol", "btc_symbol",
        "gold_rsi", "btc_rsi", "rsi_spread", "gold_btc_ratio_return",
        "gold_return", "btc_return", "macro_direction_score",
        "long_budget_multiplier", "short_budget_multiplier", "ladder_multiplier",
        "disable_new_entries", "disable_averaging", "reason",
    )
    EXTERNAL_PRICE_CSV_HEADER = (
        "ts", "profile", "symbol", "mexc_symbol", "valid", "stale",
        "htx_bid", "htx_ask", "htx_mid", "mexc_bid", "mexc_ask", "mexc_mid",
        "mexc_bid_qty", "mexc_ask_qty", "mexc_bid_notional", "mexc_ask_notional",
        "spread_bps", "spread_bps_30s_avg", "spread_bps_2m_avg", "spread_bps_10m_avg",
        "spread_bps_zscore", "htx_change_30s_bps", "mexc_change_30s_bps",
        "htx_change_1m_bps", "mexc_change_1m_bps", "age_ms", "reason",
    )
    ACCOUNT_PNL_CSV_HEADER = [
        "ts", "profile", "open_pnl", "unrealized_pnl", "realized_open_pnl",
        "open_notional", "open_pnl_rate", "position_count", "history_samples",
        "min_open_pnl", "p25_open_pnl", "median_open_pnl", "p75_open_pnl",
        "max_open_pnl", "previous_open_pnl", "delta_open_pnl", "reason",
    ]
    SIGNAL_ANALYTICS_CSV_HEADER = [
        "ts", "profile", "symbol", "side", "signal_id", "signal_ts",
        "strategy_name", "valid", "entry_valid", "add_valid", "decision",
        "block_reason", "score", "rs30", "rs60", "ema50", "ema100",
        "ema1d", "ema2d", "ema25d", "ema50d", "macro_gap", "trigger_gap",
        "pullback_depth", "btc_return_30m", "volatility", "budget_multiplier",
        "ladder_multiplier", "volume_valid", "volume_ratio", "volume_spike_ratio",
        "volume_spike_direction", "volume_profile_valid", "volume_profile_break",
        "volume_profile_poc", "volume_profile_value_area_low", "volume_profile_value_area_high",
        "volume_reason", "macro_regime",
        "external_valid", "external_stale", "external_spread_bps",
        "planned_budget", "planned_orders", "planned_notional",
        "placed_orders", "filled_notional", "realized_pnl_quote",
    ]
    DIAGNOSTICS_CSV_HEADER = [
        "ts", "profile", "severity", "category", "event", "symbol",
        "operation_id", "signal_id", "order_id", "exception_type",
        "error_code", "message", "reason", "retryable", "attempt",
        "hostname",
    ]

    def __init__(self, profile=None, exchange=None, external_price_feed=None):
        import threading
        self._funding_lock = threading.Lock()
        self._account_pnl_lock = threading.Lock()
        self._signal_lock = threading.Lock()
        self.profile = config.resolve_profile(profile)
        with config.use_profile(self.profile):
            self.profile_name = self.profile.name
            ensure_runtime_locks(self)
            self.log = self._build_logger()
            self.exchange = ensure_thread_safe_exchange(exchange) if exchange is not None else self._create_exchange()
            self.external_price_feed = external_price_feed or ExternalPriceFeed(config.EXTERNAL_PRICE_FEED)
            self.state_path = Path(config.RUNTIME.state_file)
            self.lock_path = self.state_path.with_suffix(".lock")
            markets_cache_file = getattr(config.RUNTIME, "markets_cache_file", "")
            if markets_cache_file:
                self.markets_cache_path = Path(markets_cache_file)
            else:
                self.markets_cache_path = self.state_path.with_name(f"{self.state_path.stem}_markets_cache.json")
            self.csv_path = Path(config.MONITORING.csv_log_file)
            self.cycle_stats_path = Path(config.MONITORING.cycle_stats_csv_file)
            self.account_pnl_csv_path = Path(config.MONITORING.account_pnl_csv_file)
            self.macro_csv_path = Path(config.MONITORING.macro_csv_file)
            self.external_price_csv_path = Path(config.MONITORING.external_price_csv_file)
            self.signal_analytics_csv_path = Path(config.MONITORING.signal_analytics_csv_file)
            self.signal_analytics_jsonl_path = Path(config.MONITORING.signal_analytics_jsonl_file)
            self.diagnostics_csv_path = Path(config.MONITORING.diagnostics_csv_file)
            self.diagnostics_jsonl_path = Path(config.MONITORING.diagnostics_jsonl_file)
            self.timeframe_sec = self._timeframe_to_seconds(config.SIGNALS.timeframe)
            self._ensure_csv_file()
            self._ensure_cycle_stats_file()
            self._ensure_macro_csv_file()
            self._ensure_external_price_csv_file()
            self._ensure_account_pnl_csv_file()
            self._ensure_signal_analytics_files()
            self._ensure_diagnostics_files()
            self.symbols: List[str] = []
            self.market_by_symbol: Dict[str, dict] = {}
            self.disabled_symbols = set()
            self.benchmark_symbol: Optional[str] = None
            self.macro_gold_symbol: Optional[str] = None
            self.macro_gold_is_spot = False
            self._macro_gold_lookup_done = False
            self.macro_direct_gold_btc_symbol: Optional[str] = None
            self.macro_direct_gold_btc_is_spot = False
            self._macro_direct_gold_btc_lookup_done = False
            self.macro_spot_exchange = None
            self.one_way_mode_checked = False
            self.skip_futures_account_setup = False
            self.funding_cache: Dict[str, dict] = {}
            self.order_leverage_cache: Dict[str, float] = {}
            self._account_pnl_lock = threading.RLock()
            self._funding_cache_lock = threading.RLock()
            self._private_cache_lock = threading.RLock()
            self._reset_private_caches()
            self.states = self._load_state()
            self.signal_cache = {
                "closed_candle_ts": None,
                "benchmark_ok": False,
                "btc_risk": {
                    "return": 0.0,
                    "volatility": 0.0,
                    "budget_multiplier": 1.0,
                    "ladder_multiplier": 1.0,
                    "reason": "neutral",
                },
                "macro": {
                    "gold_btc_rsi": {
                        "ok": False,
                        "regime": "macro_unavailable",
                        "reason": "not_loaded",
                    },
                },
                "symbols": {},
            }
            self.entry_symbols = set()
            self.account_pnl_runtime = {"history": [], "last_sample_at": 0.0}
            self.account_pnl_bots = [self]
            self._record_config_warnings()

    def run(self):
        profile = getattr(self, "profile", None) or config.current_profile()
        with config.use_profile(profile):
            return RunnerMixin.run(self)


__all__ = ["HtxFuturesBot", "TradeState"]
