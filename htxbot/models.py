# -*- coding: utf-8 -*-

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

import config


@dataclass
class DiagnosticEvent:
    severity: str
    category: str
    event: str
    message: str
    symbol: str = ""
    operation_id: str = ""
    signal_id: str = ""
    order_id: str = ""
    reason: str = ""
    exception: Optional[Exception] = None
    retryable: Optional[bool] = None
    attempt: Any = ""
    hostname: str = ""
    context: Optional[dict] = None


@dataclass
class OrderRequest:
    symbol: str
    order_type: str
    side: str
    amount: float
    price: Optional[float] = None
    reduce_only: bool = False
    post_only: bool = False
    leverage: Optional[float] = None
    extra_params: dict = field(default_factory=dict)


@dataclass
class SignalContext:
    closes: List[float]
    benchmark_closes: List[float]
    btc_risk: dict
    latest_ts: int
    candles: Optional[List[list]] = None
    cache_key: str = ""
    macro_context: Optional[dict] = None
    macro_closes: Optional[List[float]] = None
    macro_latest_ts: Optional[int] = None
    pullback_closes: Optional[List[float]] = None
    pullback_latest_ts: Optional[int] = None


class PositionLifecycle(str, Enum):
    FLAT = "flat"
    ENTERING = "entering"
    OPEN = "open"
    EXITING = "exiting"
    BREAKEVEN = "breakeven"
    PENDING_CLOSEABLE = "pending_closeable"
    ZOMBIE = "zombie"
    FORCE_EXIT = "force_exit"


@dataclass
class ExitLadderConfig:
    symbol: str
    total_contracts: float
    avg_entry_price: float
    rebuild: bool
    closeable_contracts: Optional[float] = None
    mode: str = "normal"
    exit_scope: Optional[str] = None
    signature_override: str = ""
    use_trailing_exit: bool = True
    signal: Optional[dict] = None


@dataclass
class ExitLadderPreflight:
    ok: bool
    requested_contracts: float
    position_contracts: float
    closeable_contracts: float
    planned_contracts: float
    existing_tracked_contracts: float = 0.0
    reason: str = "ok"


@dataclass
class SellLadderParams:
    symbol: str
    total_contracts: float
    avg_entry_price: float
    rebuild: bool
    closeable_contracts: Optional[float] = None
    mode: str = "normal"
    exit_scope: Optional[str] = None
    signature_override: str = ""
    use_trailing_exit: bool = True
    signal: Optional[dict] = None


@dataclass
class TradeState:
    symbol: str = ""
    market_symbol: str = ""
    active_side: Optional[str] = None
    lifecycle: str = PositionLifecycle.FLAT.value
    position_side: str = ""
    position_size: float = 0.0
    position_available: float = 0.0
    position_frozen: float = 0.0
    entry_price: float = 0.0
    cycle_id: str = ""
    last_buy_price: float = 0.0
    last_buy_amount: float = 0.0
    buy_stage: int = 0
    planned_quote_budget: float = 0.0
    initial_entry_notional: float = 0.0
    entry_orders: list = field(default_factory=list)
    sell_ladder_orders: list = field(default_factory=list)
    sell_ladder_mode: str = "normal"
    sell_ladder_signature: str = ""
    hard_stop_order: dict = field(default_factory=dict)
    hard_stop_signature: str = ""
    pending_exit_ladder_since: Optional[float] = None
    pending_exit_ladder_reason: str = ""
    pending_close_order: dict = field(default_factory=dict)
    pending_close_reason: str = ""
    frozen_no_more_buys: bool = False
    cycle_opened_at: Optional[float] = None
    cooldown_until: Optional[float] = None
    time_exit_activated_at: Optional[float] = None
    zombie_position: bool = False
    zombie_marked_at: Optional[float] = None
    paid_buy_fees_quote: float = 0.0
    paid_sell_fees_quote: float = 0.0
    total_bought_amount: float = 0.0
    total_bought_quote: float = 0.0
    total_sold_amount: float = 0.0
    total_sold_quote: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    remaining_entry_quote: float = 0.0
    remaining_buy_fees_quote: float = 0.0
    net_open_pnl: float = 0.0
    base_entry_amount: float = 0.0
    base_entry_quote: float = 0.0
    base_entry_fees_quote: float = 0.0
    base_entry_price: float = 0.0
    averaging_entry_amount: float = 0.0
    averaging_entry_quote: float = 0.0
    averaging_entry_fees_quote: float = 0.0
    leverage: float = field(default_factory=lambda: float(config.RISK.leverage))
    margin_mode: str = field(default_factory=lambda: config.RISK.margin_mode)
    last_signal_timestamp: Optional[float] = None
    last_rs30: float = 0.0
    last_rs60: float = 0.0
    last_ema30: float = 0.0
    last_ema60: float = 0.0
    last_ema25d: float = 0.0
    last_ema50d: float = 0.0
    last_ema1d: float = 0.0
    last_ema2d: float = 0.0
    last_ema50: float = 0.0
    last_ema100: float = 0.0
    last_btc_return_30m: float = 0.0
    last_entry_ladder_signal_timestamp: Optional[float] = None
    last_average_signal_timestamp: Optional[float] = None
    last_average_at: Optional[float] = None
    average_stage: int = 0
    strategy_name: str = "ema_pullback"
    last_ema_strategy_signal_timestamp: Optional[float] = None
    breakeven_activated_at: Optional[float] = None
    exit_runner_active: bool = False
    exit_runner_activated_at: Optional[float] = None
    exit_runner_peak_price: float = 0.0
    exit_runner_bottom_price: float = 0.0
    exit_runner_contracts: float = 0.0
    soft_defensive_last_signal_timestamp: Optional[float] = None
    soft_defensive_consecutive_signals: int = 0
    soft_defensive_exit_activated_at: Optional[float] = None
    soft_defensive_exit_last_rebuild_at: Optional[float] = None
    soft_defensive_exit_fraction: float = 0.0
    last_account_unload_at: Optional[float] = None
    account_unload_count: int = 0
    entry_rs30: float = 0.0
    entry_rs60: float = 0.0
    entry_ema30: float = 0.0
    entry_ema60: float = 0.0
    entry_ema25d: float = 0.0
    entry_ema50d: float = 0.0
    entry_ema1d: float = 0.0
    entry_ema2d: float = 0.0
    entry_ema50: float = 0.0
    entry_ema100: float = 0.0
    entry_btc_return_30m: float = 0.0


__all__ = [
    "OrderRequest",
    "ExitLadderConfig",
    "ExitLadderPreflight",
    "OrderRequest",
    "PositionLifecycle",
    "SellLadderParams",
    "SignalContext",
    "TradeState",
]
