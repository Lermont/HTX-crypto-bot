# -*- coding: utf-8 -*-

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple, Union


BASE_DIR = Path(__file__).resolve().parent
CONFIG_WARNINGS = []


def _load_dotenv_if_present(path: Path, profile: str = "") -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            clean_value = value.strip().strip("\"'")
            if profile and not key.upper().startswith((f"{profile.upper()}_", "HTXBOT_")):
                os.environ.setdefault(f"{profile.upper()}_{key}", clean_value)
            else:
                os.environ.setdefault(key, clean_value)


_load_dotenv_if_present(BASE_DIR / ".env")
_load_dotenv_if_present(BASE_DIR / "long" / ".env", profile="long")
_load_dotenv_if_present(BASE_DIR / "short" / ".env", profile="short")


def _env(name: str, profile: str = "") -> str:
    candidates = []
    if profile:
        prefix = profile.upper()
        candidates.extend((f"{prefix}_{name}", f"HTXBOT_{prefix}_{name}"))
    candidates.append(f"HTXBOT_{name}")
    candidates.append(name)
    candidates.append(f"HTXBOT_{name}")
    for candidate in candidates:
        value = os.getenv(candidate, "").strip()
        if value:
            return value
    return ""


def _first_env(*names: str, profile: str = "") -> str:
    for name in names:
        value = _env(name, profile=profile)
        if value:
            return value
    return ""


def _env_bool(name: str, default: bool, profile: str = "") -> bool:
    value = _env(name, profile=profile).lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_float(name: str, default: float, profile: str = "") -> float:
    value = _env(name, profile=profile)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int, profile: str = "") -> int:
    value = _env(name, profile=profile)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_csv(name: str, default: Tuple[str, ...], profile: str = "") -> Tuple[str, ...]:
    value = _env(name, profile=profile)
    if not value:
        return default
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or default


def _env_float_tuple(name: str, default: Tuple[float, ...], profile: str = "") -> Tuple[float, ...]:
    value = _env(name, profile=profile)
    if not value:
        return default
    parsed = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            parsed.append(float(item))
        except ValueError:
            return default
    return tuple(parsed) or default


def _env_optional_float_tuple(name: str, default: Tuple[Optional[float], ...], profile: str = "") -> Tuple[Optional[float], ...]:
    value = _env(name, profile=profile)
    if not value:
        return default
    parsed = []
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item in {"runner", "none", "null"}:
            parsed.append(None)
            continue
        try:
            parsed.append(float(item))
        except ValueError:
            return default
    return tuple(parsed) or default


def _add_config_warning(message: str) -> None:
    if message not in CONFIG_WARNINGS:
        CONFIG_WARNINGS.append(message)

LONG_COINS = (
    "eth", "sol", "bnb", "xrp", "ada", "avax", "link", "dot", "ltc", "bch",
    "etc", "trx", "ton", "sui", "apt", "op", "near", "sei",
    "inj", "fil", "atom", "algo", "pol", "tao", "icp", "wld", "grt",
    "tia", "hbar", "kas", "xlm", "kaito", "ssv", "lpt", "pendle", "ena",
    "ondo", "jup", "aave", "uni", "ldo", "ethfi", "zro", "zk", "1inch",
    "crv", "orca", "hype", "zec", "xmr", "dydx", "ens", "cake", "comp",
    "gala", "axs", "sand",
)

SHORT_COINS = (
    "eth", "sol", "bnb", "xrp", "ada", "avax", "link", "dot", "ltc", "bch",
    "doge", "etc", "trx", "ton", "sui", "apt", "arb", "op", "near", "sei",
    "inj", "fil", "atom", "algo", "pol", "tao", "icp", "wld", "grt", "pyth",
    "tia", "hbar", "xlm", "kaito", "ssv", "lpt", "pendle", "ena",
    "jup", "uni", "ldo", "ethfi", "zro", "zk", "1inch",
    "crv", "orca", "zec", "xmr", "dydx", "ens", "cake", "comp",
    "gala", "axs", "cfx", "sand",
)


@dataclass(frozen=True)
class ApiCredentials:
    api_key: str
    api_secret: str


@dataclass(frozen=True)
class ExchangeSettings:
    quote_currency: str
    enable_rate_limit: bool
    timeout_ms: int
    default_type: str
    set_position_mode_on_start: bool
    set_leverage_on_start: bool
    contract_hostnames: Tuple[str, ...]
    market_load_retries: int
    markets_cache_max_age_sec: int


@dataclass(frozen=True)
class SignalSettings:
    timeframe: str
    rs_fast_window: int
    rs_slow_window: int


@dataclass(frozen=True)
class BuySettings:
    position_budget_fraction: float
    ladder_fractions: Tuple[float, ...]
    ladder_offsets: Tuple[float, ...]


@dataclass(frozen=True)
class SellSettings:
    buy_fee_rate: float
    sell_fee_rate: float
    min_gross_profit_floor: float


@dataclass(frozen=True)
class RiskSettings:
    min_quote_reserve: float
    max_active_positions: int
    max_position_notional_fraction: float
    max_total_notional_fraction: float
    active_position_min_notional_for_slot: float
    dust_position_notional: float
    dust_close_enabled: bool
    tiny_entry_close_enabled: bool
    tiny_entry_max_notional: float
    tiny_entry_max_planned_fraction: float
    leverage: int
    account_leverage: int
    margin_mode: str
    position_mode: str
    cooldown_minutes_after_close: float
    post_win_cooldown_minutes_after_close: float


@dataclass(frozen=True)
class StrategySettings:
    ema_strategy_enabled: bool
    ema_macro_timeframe: str
    ema_pullback_timeframe: str
    ema_trigger_timeframe: str
    ema_macro_fast_minutes: int
    ema_macro_slow_minutes: int
    ema_pullback_fast_minutes: int
    ema_pullback_slow_minutes: int
    ema_pullback_recovery_lookback_minutes: int
    ema_pullback_recovery_max_cross_age_minutes: int
    ema_pullback_recovery_gap: float
    ema_trigger_fast_minutes: int
    ema_trigger_slow_minutes: int
    ema_use_rs_confirmation: bool
    ema_long_min_rs60: float
    ema_short_max_rs60: float
    ema_use_btc_risk_filter: bool
    ema_btc_long_min_return_30m: float
    ema_btc_short_max_return_30m: float
    ema_take_profit_markup: float
    ema_exit_ladder_fractions: Tuple[float, ...]
    ema_adaptive_exit_enabled: bool
    ema_exit_normal_ladder_fractions: Tuple[float, ...]
    ema_exit_normal_ladder_markups: Tuple[float, ...]
    ema_exit_medium_ladder_fractions: Tuple[float, ...]
    ema_exit_medium_ladder_markups: Tuple[float, ...]
    ema_exit_heavy_ladder_fractions: Tuple[float, ...]
    ema_exit_heavy_ladder_markups: Tuple[float, ...]
    ema_exit_medium_position_ratio: float
    ema_exit_heavy_position_ratio: float
    ema_exit_decay_first_markup_after_hours: float
    ema_exit_decay_first_markup_cap: float
    ema_exit_decay_max_markup_after_hours: float
    ema_exit_decay_max_markup: float
    ema_exit_runner_enabled: bool
    ema_exit_runner_activation_markup: float
    ema_exit_runner_trailing_pullback: float
    ema_exit_runner_take_profit_markup: float
    ema_exit_trailing_enabled: bool
    ema_exit_trailing_fixed_fraction: float
    ema_exit_trailing_activation_markup: float
    ema_exit_trailing_pullback: float
    ema_exit_trailing_take_profit_markup: float
    ema_averaging_enabled: bool
    ema_averaging_drawdown_step: float
    ema_averaging_base_fraction: float
    ema_averaging_power: float
    ema_averaging_interval_hours: float
    ema_averaging_atr_enabled: bool
    ema_averaging_atr_period: int
    ema_averaging_atr_multiplier: float
    ema_max_averaging_stages: int
    account_pnl_enabled: bool
    account_pnl_window_minutes: float
    account_pnl_sample_interval_sec: float
    account_profit_unload_enabled: bool
    account_profit_unload_min_pnl_quote: float
    account_profit_unload_min_pnl_rate: float
    account_profit_unload_percentile: float
    account_profit_unload_fraction: float
    account_profit_unload_drawdown_fraction: float
    account_profit_unload_peak_drawdown_fraction: float
    account_profit_unload_full_pnl_quote: float
    account_profit_unload_min_position_pnl_quote: float
    account_profit_unload_min_position_pnl_rate: float
    account_profit_unload_cooldown_sec: float
    account_pnl_trailing_enabled: bool
    account_pnl_trailing_activation_rate: float
    account_pnl_trailing_stop_rate: float
    account_pnl_trailing_min_pnl_quote: float
    account_averaging_enabled: bool
    account_averaging_min_samples: int
    account_averaging_percentile: float
    account_averaging_near_trough_quote: float
    account_averaging_near_trough_fraction: float
    account_averaging_bounce_quote: float
    account_averaging_falling_guard_quote: float
    account_averaging_falling_guard_fraction: float
    account_averaging_budget_scale: float
    ema_breakeven_enabled: bool
    ema_breakeven_after_hours: float
    ema_breakeven_reprice_minutes: float
    ema_breakeven_fee_buffer: float
    ema_breakeven_exit_fractions: Tuple[float, ...]
    enable_signal_size_scaling: bool
    signal_budget_min_multiplier: float
    signal_budget_max_multiplier: float
    signal_score_reference: float
    signal_ema_gap_weight: float
    entry_min_score: float
    entry_min_rs60_abs: float
    entry_min_rs30_abs: float
    entry_max_new_ladders_per_signal: int
    entry_rate_limit_ladders: int
    entry_rate_limit_window_minutes: float
    entry_crowded_signal_fraction: float
    entry_crowded_min_signals: int
    entry_crowded_max_new_ladders_per_signal: int
    entry_crowded_min_score: float
    entry_crowded_min_rs60_abs: float
    entry_crowded_min_rs30_abs: float
    entry_spread_filter_enabled: bool
    entry_spread_filter_max_bps: float
    entry_spread_filter_block_if_unavailable: bool
    max_buy_stages: int
    averaging_drawdown_steps: Tuple[float, ...]
    averaging_budget_fractions: Tuple[float, ...]
    no_more_averaging_after_minutes: float
    time_exit_after_minutes: float
    urgent_time_exit_after_minutes: float
    hard_time_exit_after_minutes: float
    hard_time_exit_close_fraction: float
    hard_time_exit_step_minutes: float
    hard_time_exit_fraction_step: float
    hard_time_exit_max_loss_on_notional: float
    hard_time_exit_bypass_profit_bank: bool
    hard_stop_loss_enabled: bool
    hard_stop_loss_pct: float
    enable_absolute_force_exit: bool
    absolute_force_exit_after_minutes: float
    enable_controlled_loss_exit: bool
    controlled_loss_after_zombie_minutes: float
    controlled_loss_min_drawdown: float
    controlled_loss_max_loss_on_notional: float
    controlled_loss_max_position_fraction: float
    controlled_loss_profit_bank_today_fraction: float
    controlled_loss_profit_bank_7d_fraction: float
    controlled_loss_min_bank_usdt: float
    max_unhealthy_positions_for_new_entries: int
    cancel_unsafe_hidden_close_orders: bool
    enable_volatility_adjusted_ladders: bool
    volatility_window: int
    volatility_reference: float
    daily_volatility_window: int
    daily_volatility_reference: float
    enable_volatility_targeted_sizing: bool
    min_volatility_budget_multiplier: float
    max_volatility_budget_multiplier: float
    enable_volatility_recovery_stages: bool
    averaging_drawdown_daily_volatility_fraction: float
    min_ladder_volatility_multiplier: float
    max_ladder_volatility_multiplier: float
    min_profit_fee_multiplier: float
    enable_dynamic_profit_floor: bool
    dynamic_profit_floor_volatility_multiplier_threshold: float
    dynamic_profit_floor_high_vol_multiplier: float
    dynamic_profit_floor_adverse_funding_multiplier: float
    dynamic_profit_floor_urgent_multiplier: float
    dynamic_profit_floor_min_rate: float
    enable_btc_risk_multiplier: bool
    btc_risk_return_window: int
    btc_risk_drop_threshold: float
    btc_risk_high_vol_threshold: float
    btc_risk_drop_budget_multiplier: float
    btc_risk_vol_budget_multiplier: float
    btc_risk_min_budget_multiplier: float
    btc_risk_max_ladder_multiplier: float
    enable_funding_aware_exit: bool
    funding_cache_ttl_sec: int
    funding_positive_threshold: float
    funding_negative_threshold: float
    funding_positive_markup_multiplier: float
    funding_negative_markup_multiplier: float


@dataclass(frozen=True)
class MacroSettings:
    enable_gold_btc_rsi_overlay: bool
    gold_coins: Tuple[str, ...]
    gold_timeframe: str
    gold_rsi_period: int
    gold_min_candles: int
    gold_cache_ttl_sec: int
    use_direct_gold_btc_pair: bool
    direct_gold_btc_symbol: str

    gold_strong_rsi: float
    gold_weak_rsi: float
    btc_strong_rsi: float
    btc_weak_rsi: float
    rsi_spread_threshold: float

    risk_off_long_budget_multiplier: float
    risk_off_short_budget_multiplier: float
    risk_off_ladder_multiplier: float
    risk_off_disable_averaging: bool
    risk_off_disable_recovery: bool
    risk_off_time_exit_multiplier: float

    panic_disable_new_entries: bool
    stale_macro_max_age_sec: int


@dataclass(frozen=True)
class ExternalPriceFeedSettings:
    enabled: bool
    primary_exchange: str
    reference_exchanges: Tuple[str, ...]
    rest_poll_interval_sec: float
    rest_timeout_sec: float
    max_price_age_ms: int
    min_valid_bid_qty_usdt: float
    min_valid_ask_qty_usdt: float
    max_internal_spread_bps: float
    entry_filter_enabled: bool
    max_htx_premium_for_long_bps: float
    max_htx_discount_for_short_bps: float
    block_if_exchange_divergence_1m_bps: float
    block_duration_sec: int
    directional_1m_gate_enabled: bool
    directional_entry_1m_block_bps: float
    directional_averaging_1m_block_bps: float
    impulse_confirmation_enabled: bool
    mexc_lead_threshold_bps_30s: float
    impulse_score_bonus: float
    require_same_direction: bool
    exit_adjustment_enabled: bool
    long_take_profit_tighten_if_htx_premium_bps: float
    short_take_profit_tighten_if_htx_discount_bps: float
    tightened_ladder_fractions: Tuple[float, ...]
    tightened_ladder_markups: Tuple[Optional[float], ...]
    disable_trading_if_reference_stale: bool
    ignore_reference_if_stale: bool
    stale_after_ms: int


@dataclass(frozen=True)
class HedgeSettings:
    btc_hedge_enabled: bool
    btc_hedge_coin: str
    btc_hedge_ratio: float
    btc_hedge_min_rebalance_notional: float
    btc_hedge_max_notional: float
    btc_hedge_max_spread_bps: float
    btc_hedge_cooldown_sec: float


@dataclass(frozen=True)
class MonitoringSettings:
    log_level: str
    cycle_stats_csv_file: str
    csv_log_file: str
    macro_csv_file: str
    external_price_csv_file: str
    account_pnl_csv_file: str
    signal_analytics_csv_file: str
    signal_analytics_jsonl_file: str
    diagnostics_csv_file: str
    diagnostics_jsonl_file: str
    csv_archive_dir: str
    csv_rotate_max_bytes: int


@dataclass(frozen=True)
class RuntimeSettings:
    order_timeout_sec: int
    poll_interval_sec: int
    post_only_enabled: bool
    reduce_only_enabled: bool
    fetch_fill_details_on_sync: bool
    fill_detail_lookback_sec: int
    state_file: str
    markets_cache_file: str


def _make_hedge_settings() -> HedgeSettings:
    return HedgeSettings(
        btc_hedge_enabled=_env_bool("BTC_HEDGE_ENABLED", True),
        btc_hedge_coin=(_env("BTC_HEDGE_COIN") or "btc").strip().lower(),
        btc_hedge_ratio=max(0.0, _env_float("BTC_HEDGE_RATIO", 1.0)),
        btc_hedge_min_rebalance_notional=max(0.0, _env_float("BTC_HEDGE_MIN_REBALANCE_NOTIONAL", 10.0)),
        btc_hedge_max_notional=max(0.0, _env_float("BTC_HEDGE_MAX_NOTIONAL", 0.0)),
        btc_hedge_max_spread_bps=max(0.0, _env_float("BTC_HEDGE_MAX_SPREAD_BPS", 30.0)),
        btc_hedge_cooldown_sec=max(0.0, _env_float("BTC_HEDGE_COOLDOWN_SEC", 30.0)),
    )


HEDGE = _make_hedge_settings()


@dataclass(frozen=True)
class BotProfile:
    name: str
    coins: Tuple[str, ...]
    trade_direction: str
    position_side: str
    opposite_position_side: str
    entry_side: str
    exit_side: str
    api_credentials: ApiCredentials
    exchange: ExchangeSettings
    signals: SignalSettings
    buying: BuySettings
    selling: SellSettings
    risk: RiskSettings
    strategy: StrategySettings
    macro: MacroSettings
    monitoring: MonitoringSettings
    runtime: RuntimeSettings
    external_price_feed: ExternalPriceFeedSettings

    @property
    def COINS(self) -> Tuple[str, ...]:
        return self.coins

    @property
    def TRADE_DIRECTION(self) -> str:
        return self.trade_direction

    @property
    def POSITION_SIDE(self) -> str:
        return self.position_side

    @property
    def OPPOSITE_POSITION_SIDE(self) -> str:
        return self.opposite_position_side

    @property
    def ENTRY_SIDE(self) -> str:
        return self.entry_side

    @property
    def EXIT_SIDE(self) -> str:
        return self.exit_side

    @property
    def API_CREDENTIALS(self) -> ApiCredentials:
        return self.api_credentials

    @property
    def EXCHANGE(self) -> ExchangeSettings:
        return self.exchange

    @property
    def SIGNALS(self) -> SignalSettings:
        return self.signals

    @property
    def BUYING(self) -> BuySettings:
        return self.buying

    @property
    def SELLING(self) -> SellSettings:
        return self.selling

    @property
    def RISK(self) -> RiskSettings:
        return self.risk

    @property
    def STRATEGY(self) -> StrategySettings:
        return self.strategy

    @property
    def MACRO(self) -> MacroSettings:
        return self.macro

    @property
    def MONITORING(self) -> MonitoringSettings:
        return self.monitoring

    @property
    def RUNTIME(self) -> RuntimeSettings:
        return self.runtime

    @property
    def EXTERNAL_PRICE_FEED(self) -> ExternalPriceFeedSettings:
        return self.external_price_feed

    @property
    def BOT_NAME(self) -> str:
        return self.name


def _path(profile: str, filename: str) -> str:
    return str(BASE_DIR / profile / filename)


def _validate_fraction_tuple(name: str, values: Tuple[float, ...], eps: float = 1e-9) -> None:
    if not values:
        raise ValueError(f"{name} must not be empty")
    if any(item < 0 for item in values):
        raise ValueError(f"{name} must contain non-negative fractions")
    total = sum(values)
    if total > 1.0 + eps:
        raise ValueError(f"{name} sum must be <= 1.0, got {total:.12f}")


def _validate_tuple_lengths(
    name: str,
    left_name: str,
    left: Tuple[object, ...],
    right_name: str,
    right: Tuple[object, ...],
) -> None:
    if len(left) != len(right):
        raise ValueError(
            f"{name} {left_name} and {right_name} must have the same length, "
            f"got {len(left)} and {len(right)}"
        )


def _validate_profile(profile: "BotProfile") -> None:
    _validate_fraction_tuple(f"{profile.name}.BUYING.ladder_fractions", profile.buying.ladder_fractions)
    _validate_fraction_tuple(
        f"{profile.name}.STRATEGY.ema_exit_ladder_fractions",
        profile.strategy.ema_exit_ladder_fractions,
    )
    _validate_fraction_tuple(
        f"{profile.name}.STRATEGY.ema_exit_normal_ladder_fractions",
        profile.strategy.ema_exit_normal_ladder_fractions,
    )
    _validate_fraction_tuple(
        f"{profile.name}.STRATEGY.ema_exit_medium_ladder_fractions",
        profile.strategy.ema_exit_medium_ladder_fractions,
    )
    _validate_fraction_tuple(
        f"{profile.name}.STRATEGY.ema_exit_heavy_ladder_fractions",
        profile.strategy.ema_exit_heavy_ladder_fractions,
    )
    _validate_fraction_tuple(
        f"{profile.name}.STRATEGY.ema_breakeven_exit_fractions",
        profile.strategy.ema_breakeven_exit_fractions,
    )
    _validate_tuple_lengths(
        f"{profile.name}.BUYING",
        "ladder_fractions",
        profile.buying.ladder_fractions,
        "ladder_offsets",
        profile.buying.ladder_offsets,
    )
    _validate_tuple_lengths(
        f"{profile.name}.STRATEGY.ema_exit_normal",
        "ladder_fractions",
        profile.strategy.ema_exit_normal_ladder_fractions,
        "ladder_markups",
        profile.strategy.ema_exit_normal_ladder_markups,
    )
    _validate_tuple_lengths(
        f"{profile.name}.STRATEGY.ema_exit_medium",
        "ladder_fractions",
        profile.strategy.ema_exit_medium_ladder_fractions,
        "ladder_markups",
        profile.strategy.ema_exit_medium_ladder_markups,
    )
    _validate_tuple_lengths(
        f"{profile.name}.STRATEGY.ema_exit_heavy",
        "ladder_fractions",
        profile.strategy.ema_exit_heavy_ladder_fractions,
        "ladder_markups",
        profile.strategy.ema_exit_heavy_ladder_markups,
    )
    _validate_tuple_lengths(
        f"{profile.name}.EXTERNAL_PRICE_FEED.tightened_ladder",
        "fractions",
        profile.external_price_feed.tightened_ladder_fractions,
        "markups",
        profile.external_price_feed.tightened_ladder_markups,
    )
    _validate_fraction_tuple(
        f"{profile.name}.EXTERNAL_PRICE_FEED.tightened_ladder_fractions",
        profile.external_price_feed.tightened_ladder_fractions,
    )
    if not 0.0 <= profile.strategy.ema_exit_trailing_fixed_fraction <= 1.0:
        raise ValueError(f"{profile.name}.STRATEGY.ema_exit_trailing_fixed_fraction must be between 0 and 1")
    for setting_name in (
        "account_profit_unload_percentile",
        "account_profit_unload_fraction",
        "account_profit_unload_drawdown_fraction",
        "account_profit_unload_peak_drawdown_fraction",
        "account_pnl_trailing_activation_rate",
        "account_pnl_trailing_stop_rate",
        "account_averaging_percentile",
        "account_averaging_near_trough_fraction",
        "account_averaging_falling_guard_fraction",
        "account_averaging_budget_scale",
        "hard_stop_loss_pct",
    ):
        value = getattr(profile.strategy, setting_name)
        if value < 0.0 or value > 1.0:
            raise ValueError(f"{profile.name}.STRATEGY.{setting_name} must be between 0 and 1")

    if (
        profile.strategy.account_pnl_trailing_enabled
        and profile.strategy.account_pnl_trailing_stop_rate
        > profile.strategy.account_pnl_trailing_activation_rate
    ):
        _add_config_warning(
            f"{profile.name}: account_pnl_trailing_stop_rate is above activation_rate; "
            "global trailing may close immediately after activation"
        )
    if profile.strategy.hard_stop_loss_enabled and profile.strategy.hard_stop_loss_pct <= 0:
        raise ValueError(f"{profile.name}.STRATEGY.hard_stop_loss_pct must be positive when hard stop is enabled")

    if profile.risk.max_position_notional_fraction > 0.03 + 1e-12:
        _add_config_warning(
            f"{profile.name}: live max_position_notional_fraction="
            f"{profile.risk.max_position_notional_fraction:.4f} is above the conservative 0.0300 launch cap"
        )
    if profile.risk.max_total_notional_fraction > 0.50 + 1e-12:
        _add_config_warning(
            f"{profile.name}: live max_total_notional_fraction="
            f"{profile.risk.max_total_notional_fraction:.4f} is above the conservative 0.5000 launch cap"
        )
    if profile.strategy.ema_max_averaging_stages > 2:
        _add_config_warning(
            f"{profile.name}: live ema_max_averaging_stages="
            f"{profile.strategy.ema_max_averaging_stages} is above the conservative launch cap of 2"
        )


def _make_profile(name: str, direction: str, coins: Tuple[str, ...]) -> BotProfile:
    direction = direction.lower()
    if direction not in {"long", "short"}:
        raise ValueError(f"Unsupported trade direction: {direction}")

    position_side = direction
    opposite_position_side = "short" if position_side == "long" else "long"
    entry_side = "buy" if position_side == "long" else "sell"
    exit_side = "sell" if position_side == "long" else "buy"

    api_credentials = ApiCredentials(
        api_key=_first_env("HTX_API_KEY", "API_KEY", profile=name),
        api_secret=_first_env("HTX_API_SECRET", "API_SECRET", profile=name),
    )
    exchange = ExchangeSettings(
        quote_currency="USDT",
        enable_rate_limit=True,
        timeout_ms=_env_int("TIMEOUT_MS", 30000, profile=name),
        default_type="swap",
        set_position_mode_on_start=_env_bool("SET_POSITION_MODE_ON_START", True, profile=name),
        set_leverage_on_start=False,
        contract_hostnames=_env_csv("CONTRACT_HOSTNAMES", ("api.hbdm.com", "api.hbdm.vn"), profile=name),
        market_load_retries=_env_int("MARKET_LOAD_RETRIES", 4, profile=name),
        markets_cache_max_age_sec=_env_int("MARKETS_CACHE_MAX_AGE_SEC", 7 * 24 * 60 * 60, profile=name),
    )
    ema_trigger_timeframe = _env("EMA_TRIGGER_TIMEFRAME", profile=name) or "1m"
    ema_macro_timeframe = _env("EMA_MACRO_TIMEFRAME", profile=name) or "1d"
    ema_pullback_timeframe = _env("EMA_PULLBACK_TIMEFRAME", profile=name) or "4h"

    signals = SignalSettings(
        timeframe=ema_trigger_timeframe,
        rs_fast_window=30,
        rs_slow_window=60,
    )
    ema_entry_offsets_default = _env_float_tuple("EMA_ENTRY_LADDER_OFFSETS", (0.0, 0.01), profile=name)
    ema_entry_offsets = _env_float_tuple(
        f"EMA_ENTRY_LADDER_OFFSETS_{direction.upper()}",
        ema_entry_offsets_default,
        profile=name,
    )
    ema_entry_fractions = _env_float_tuple("EMA_ENTRY_LADDER_FRACTIONS", (0.50, 0.50), profile=name)
    ema_exit_fractions = _env_float_tuple("EMA_EXIT_LADDER_FRACTIONS", (1.0,), profile=name)
    ema_take_profit_markup = _env_float("EMA_TAKE_PROFIT_MARKUP", 0.01, profile=name)
    ema_exit_normal_fractions = _env_float_tuple(
        "EMA_EXIT_NORMAL_LADDER_FRACTIONS",
        (0.35, 0.25, 0.25, 0.15),
        profile=name,
    )
    ema_exit_normal_markups = _env_float_tuple(
        "EMA_EXIT_NORMAL_LADDER_MARKUPS",
        (0.008, 0.016, 0.030, 0.050),
        profile=name,
    )
    ema_exit_medium_fractions = _env_float_tuple(
        "EMA_EXIT_MEDIUM_LADDER_FRACTIONS",
        (0.45, 0.30, 0.15, 0.10),
        profile=name,
    )
    ema_exit_medium_markups = _env_float_tuple(
        "EMA_EXIT_MEDIUM_LADDER_MARKUPS",
        (0.004, 0.010, 0.020, 0.035),
        profile=name,
    )
    ema_exit_heavy_fractions = _env_float_tuple(
        "EMA_EXIT_HEAVY_LADDER_FRACTIONS",
        (0.60, 0.25, 0.15),
        profile=name,
    )
    ema_exit_heavy_markups = _env_float_tuple(
        "EMA_EXIT_HEAVY_LADDER_MARKUPS",
        (0.003, 0.008, 0.015),
        profile=name,
    )
    dust_position_notional = _env_float("DUST_POSITION_NOTIONAL", 10.0, profile=name)
    buying = BuySettings(
        position_budget_fraction=_env_float(
            "EMA_POSITION_BUDGET_FRACTION",
            _env_float("POSITION_BUDGET_FRACTION", 0.02, profile=name),
            profile=name,
        ),
        ladder_fractions=ema_entry_fractions,
        ladder_offsets=ema_entry_offsets,
    )
    selling = SellSettings(
        buy_fee_rate=0.0001,
        sell_fee_rate=0.0001,
        min_gross_profit_floor=0.0,
    )
    risk = RiskSettings(
        min_quote_reserve=_env_float("MIN_QUOTE_RESERVE", 15.0, profile=name),
        max_active_positions=_env_int("MAX_ACTIVE_POSITIONS", 50, profile=name),
        max_position_notional_fraction=_env_float(
            "EMA_MAX_POSITION_MARGIN_FRACTION",
            _env_float("MAX_POSITION_NOTIONAL_FRACTION", 0.03, profile=name),
            profile=name,
        ),
        max_total_notional_fraction=_env_float(
            "EMA_MAX_TOTAL_MARGIN_FRACTION",
            _env_float("MAX_TOTAL_NOTIONAL_FRACTION", 0.50, profile=name),
            profile=name,
        ),
        active_position_min_notional_for_slot=_env_float("ACTIVE_POSITION_MIN_NOTIONAL_FOR_SLOT", dust_position_notional, profile=name),
        dust_position_notional=dust_position_notional,
        dust_close_enabled=_env_bool("DUST_CLOSE_ENABLED", True, profile=name),
        tiny_entry_close_enabled=_env_bool("TINY_ENTRY_CLOSE_ENABLED", True, profile=name),
        tiny_entry_max_notional=_env_float("TINY_ENTRY_MAX_NOTIONAL", dust_position_notional, profile=name),
        tiny_entry_max_planned_fraction=_env_float("TINY_ENTRY_MAX_PLANNED_FRACTION", 0.10, profile=name),
        leverage=_env_int("LEVERAGE", 30, profile=name),
        account_leverage=_env_int("ACCOUNT_LEVERAGE", _env_int("ORDER_LEVERAGE", 0, profile=name), profile=name),
        margin_mode="cross",
        position_mode="one-way",
        cooldown_minutes_after_close=_env_float("COOLDOWN_MINUTES_AFTER_CLOSE", 10.0, profile=name),
        post_win_cooldown_minutes_after_close=_env_float(
            "POST_WIN_COOLDOWN_MINUTES_AFTER_CLOSE",
            _env_float("WIN_COOLDOWN_MINUTES_AFTER_CLOSE", 90.0, profile=name),
            profile=name,
        ),
    )
    ema_averaging_drawdown_step = _env_float("EMA_AVERAGING_DRAWDOWN_STEP", 0.01, profile=name)
    ema_averaging_base_fraction = _env_float(
        "EMA_AVERAGING_BASE_FRACTION",
        _env_float("EMA_AVERAGING_POSITION_FRACTION", 0.50, profile=name),
        profile=name,
    )
    ema_averaging_power = _env_float("EMA_AVERAGING_POWER", 1.0, profile=name)
    ema_max_averaging_stages = _env_int("EMA_MAX_AVERAGING_STAGES", 2, profile=name)
    averaging_stage_count = max(0, ema_max_averaging_stages)
    strategy = StrategySettings(
        ema_strategy_enabled=_env_bool("EMA_STRATEGY_ENABLED", True, profile=name),
        ema_macro_timeframe=ema_macro_timeframe,
        ema_pullback_timeframe=ema_pullback_timeframe,
        ema_trigger_timeframe=ema_trigger_timeframe,
        ema_macro_fast_minutes=_env_int("EMA_MACRO_FAST_MINUTES", 36000, profile=name),
        ema_macro_slow_minutes=_env_int("EMA_MACRO_SLOW_MINUTES", 72000, profile=name),
        ema_pullback_fast_minutes=_env_int("EMA_PULLBACK_FAST_MINUTES", 1440, profile=name),
        ema_pullback_slow_minutes=_env_int("EMA_PULLBACK_SLOW_MINUTES", 2880, profile=name),
        ema_pullback_recovery_lookback_minutes=_env_int("EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES", 2880, profile=name),
        ema_pullback_recovery_max_cross_age_minutes=_env_int("EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES", 1440, profile=name),
        ema_pullback_recovery_gap=_env_float("EMA_PULLBACK_RECOVERY_GAP", 0.001, profile=name),
        ema_trigger_fast_minutes=_env_int("EMA_TRIGGER_FAST_MINUTES", 50, profile=name),
        ema_trigger_slow_minutes=_env_int("EMA_TRIGGER_SLOW_MINUTES", 100, profile=name),
        ema_use_rs_confirmation=_env_bool("EMA_USE_RS_CONFIRMATION", True, profile=name),
        ema_long_min_rs60=_env_float("EMA_LONG_MIN_RS60", 0.0, profile=name),
        ema_short_max_rs60=_env_float("EMA_SHORT_MAX_RS60", 0.0, profile=name),
        ema_use_btc_risk_filter=_env_bool("EMA_USE_BTC_RISK_FILTER", True, profile=name),
        ema_btc_long_min_return_30m=_env_float("EMA_BTC_LONG_MIN_RETURN_30M", -0.0025, profile=name),
        ema_btc_short_max_return_30m=_env_float("EMA_BTC_SHORT_MAX_RETURN_30M", 0.0025, profile=name),
        ema_take_profit_markup=ema_take_profit_markup,
        ema_exit_ladder_fractions=ema_exit_fractions,
        ema_adaptive_exit_enabled=_env_bool("EMA_ADAPTIVE_EXIT_ENABLED", True, profile=name),
        ema_exit_normal_ladder_fractions=ema_exit_normal_fractions,
        ema_exit_normal_ladder_markups=ema_exit_normal_markups,
        ema_exit_medium_ladder_fractions=ema_exit_medium_fractions,
        ema_exit_medium_ladder_markups=ema_exit_medium_markups,
        ema_exit_heavy_ladder_fractions=ema_exit_heavy_fractions,
        ema_exit_heavy_ladder_markups=ema_exit_heavy_markups,
        ema_exit_medium_position_ratio=_env_float("EMA_EXIT_MEDIUM_POSITION_RATIO", 1.30, profile=name),
        ema_exit_heavy_position_ratio=_env_float("EMA_EXIT_HEAVY_POSITION_RATIO", 1.80, profile=name),
        ema_exit_decay_first_markup_after_hours=_env_float("EMA_EXIT_DECAY_FIRST_MARKUP_AFTER_HOURS", 2.0, profile=name),
        ema_exit_decay_first_markup_cap=_env_float("EMA_EXIT_DECAY_FIRST_MARKUP_CAP", 0.008, profile=name),
        ema_exit_decay_max_markup_after_hours=_env_float("EMA_EXIT_DECAY_MAX_MARKUP_AFTER_HOURS", 6.0, profile=name),
        ema_exit_decay_max_markup=_env_float("EMA_EXIT_DECAY_MAX_MARKUP", 0.030, profile=name),
        ema_exit_runner_enabled=_env_bool("EMA_EXIT_RUNNER_ENABLED", False, profile=name),
        ema_exit_runner_activation_markup=_env_float("EMA_EXIT_RUNNER_ACTIVATION_MARKUP", 0.020, profile=name),
        ema_exit_runner_trailing_pullback=_env_float("EMA_EXIT_RUNNER_TRAILING_PULLBACK", 0.010, profile=name),
        ema_exit_runner_take_profit_markup=_env_float("EMA_EXIT_RUNNER_TAKE_PROFIT_MARKUP", 0.050, profile=name),
        ema_exit_trailing_enabled=_env_bool("EMA_EXIT_TRAILING_ENABLED", False, profile=name),
        ema_exit_trailing_fixed_fraction=_env_float("EMA_EXIT_TRAILING_FIXED_FRACTION", 0.35, profile=name),
        ema_exit_trailing_activation_markup=_env_float(
            "EMA_EXIT_TRAILING_ACTIVATION_MARKUP",
            _env_float("EMA_EXIT_RUNNER_ACTIVATION_MARKUP", 0.020, profile=name),
            profile=name,
        ),
        ema_exit_trailing_pullback=_env_float(
            "EMA_EXIT_TRAILING_PULLBACK",
            _env_float("EMA_EXIT_RUNNER_TRAILING_PULLBACK", 0.010, profile=name),
            profile=name,
        ),
        ema_exit_trailing_take_profit_markup=_env_float(
            "EMA_EXIT_TRAILING_TAKE_PROFIT_MARKUP",
            _env_float("EMA_EXIT_RUNNER_TAKE_PROFIT_MARKUP", 0.050, profile=name),
            profile=name,
        ),
        ema_averaging_enabled=_env_bool("EMA_AVERAGING_ENABLED", True, profile=name),
        ema_averaging_drawdown_step=ema_averaging_drawdown_step,
        ema_averaging_base_fraction=ema_averaging_base_fraction,
        ema_averaging_power=ema_averaging_power,
        ema_averaging_interval_hours=_env_float("EMA_AVERAGING_INTERVAL_HOURS", 8.0, profile=name),
        ema_averaging_atr_enabled=_env_bool("EMA_AVERAGING_ATR_ENABLED", False, profile=name),
        ema_averaging_atr_period=max(1, _env_int("EMA_AVERAGING_ATR_PERIOD", 14, profile=name)),
        ema_averaging_atr_multiplier=max(0.0, _env_float("EMA_AVERAGING_ATR_MULTIPLIER", 1.0, profile=name)),
        ema_max_averaging_stages=ema_max_averaging_stages,
        account_pnl_enabled=_env_bool("ACCOUNT_PNL_ENABLED", True, profile=name),
        account_pnl_window_minutes=_env_float("ACCOUNT_PNL_WINDOW_MINUTES", 360.0, profile=name),
        account_pnl_sample_interval_sec=_env_float("ACCOUNT_PNL_SAMPLE_INTERVAL_SEC", 30.0, profile=name),
        account_profit_unload_enabled=_env_bool("ACCOUNT_PROFIT_UNLOAD_ENABLED", False, profile=name),
        account_profit_unload_min_pnl_quote=_env_float("ACCOUNT_PROFIT_UNLOAD_MIN_PNL_QUOTE", 5.0, profile=name),
        account_profit_unload_min_pnl_rate=_env_float("ACCOUNT_PROFIT_UNLOAD_MIN_PNL_RATE", 0.002, profile=name),
        account_profit_unload_percentile=_env_float("ACCOUNT_PROFIT_UNLOAD_PERCENTILE", 0.75, profile=name),
        account_profit_unload_fraction=_env_float("ACCOUNT_PROFIT_UNLOAD_FRACTION", 0.25, profile=name),
        account_profit_unload_drawdown_fraction=_env_float("ACCOUNT_PROFIT_UNLOAD_DRAWDOWN_FRACTION", 0.50, profile=name),
        account_profit_unload_peak_drawdown_fraction=_env_float("ACCOUNT_PROFIT_UNLOAD_PEAK_DRAWDOWN_FRACTION", 0.25, profile=name),
        account_profit_unload_full_pnl_quote=_env_float("ACCOUNT_PROFIT_UNLOAD_FULL_PNL_QUOTE", 0.0, profile=name),
        account_profit_unload_min_position_pnl_quote=_env_float("ACCOUNT_PROFIT_UNLOAD_MIN_POSITION_PNL_QUOTE", 0.50, profile=name),
        account_profit_unload_min_position_pnl_rate=_env_float("ACCOUNT_PROFIT_UNLOAD_MIN_POSITION_PNL_RATE", 0.001, profile=name),
        account_profit_unload_cooldown_sec=_env_float("ACCOUNT_PROFIT_UNLOAD_COOLDOWN_SEC", 300.0, profile=name),
        account_pnl_trailing_enabled=_env_bool("ACCOUNT_PNL_TRAILING_ENABLED", False, profile=name),
        account_pnl_trailing_activation_rate=_env_float("ACCOUNT_PNL_TRAILING_ACTIVATION_RATE", 0.050, profile=name),
        account_pnl_trailing_stop_rate=_env_float("ACCOUNT_PNL_TRAILING_STOP_RATE", 0.035, profile=name),
        account_pnl_trailing_min_pnl_quote=_env_float("ACCOUNT_PNL_TRAILING_MIN_PNL_QUOTE", 0.0, profile=name),
        account_averaging_enabled=_env_bool("ACCOUNT_AVERAGING_ENABLED", False, profile=name),
        account_averaging_min_samples=_env_int("ACCOUNT_AVERAGING_MIN_SAMPLES", 6, profile=name),
        account_averaging_percentile=_env_float("ACCOUNT_AVERAGING_PERCENTILE", 0.25, profile=name),
        account_averaging_near_trough_quote=_env_float("ACCOUNT_AVERAGING_NEAR_TROUGH_QUOTE", 3.0, profile=name),
        account_averaging_near_trough_fraction=_env_float("ACCOUNT_AVERAGING_NEAR_TROUGH_FRACTION", 0.10, profile=name),
        account_averaging_bounce_quote=_env_float("ACCOUNT_AVERAGING_BOUNCE_QUOTE", 1.0, profile=name),
        account_averaging_falling_guard_quote=_env_float("ACCOUNT_AVERAGING_FALLING_GUARD_QUOTE", 1.0, profile=name),
        account_averaging_falling_guard_fraction=_env_float("ACCOUNT_AVERAGING_FALLING_GUARD_FRACTION", 0.05, profile=name),
        account_averaging_budget_scale=_env_float("ACCOUNT_AVERAGING_BUDGET_SCALE", 0.50, profile=name),
        ema_breakeven_enabled=_env_bool("EMA_BREAKEVEN_ENABLED", True, profile=name),
        ema_breakeven_after_hours=_env_float("EMA_BREAKEVEN_AFTER_HOURS", 48.0, profile=name),
        ema_breakeven_reprice_minutes=_env_float("EMA_BREAKEVEN_REPRICE_MINUTES", 15.0, profile=name),
        ema_breakeven_fee_buffer=_env_float("EMA_BREAKEVEN_FEE_BUFFER", 0.0002, profile=name),
        ema_breakeven_exit_fractions=_env_float_tuple("EMA_BREAKEVEN_EXIT_FRACTIONS", (1.0,), profile=name),
        enable_signal_size_scaling=False,
        signal_budget_min_multiplier=1.0,
        signal_budget_max_multiplier=1.0,
        signal_score_reference=1.0,
        signal_ema_gap_weight=1.0,
        entry_min_score=_env_float("ENTRY_MIN_SCORE", 0.03, profile=name),
        entry_min_rs60_abs=_env_float("ENTRY_MIN_RS60_ABS", 0.002, profile=name),
        entry_min_rs30_abs=_env_float("ENTRY_MIN_RS30_ABS", 0.001, profile=name),
        entry_max_new_ladders_per_signal=_env_int("ENTRY_MAX_NEW_LADDERS_PER_SIGNAL", 5, profile=name),
        entry_rate_limit_ladders=_env_int("ENTRY_RATE_LIMIT_LADDERS", 10, profile=name),
        entry_rate_limit_window_minutes=_env_float("ENTRY_RATE_LIMIT_WINDOW_MINUTES", 60.0, profile=name),
        entry_crowded_signal_fraction=_env_float("ENTRY_CROWDED_SIGNAL_FRACTION", 0.30, profile=name),
        entry_crowded_min_signals=_env_int("ENTRY_CROWDED_MIN_SIGNALS", 12, profile=name),
        entry_crowded_max_new_ladders_per_signal=_env_int("ENTRY_CROWDED_MAX_NEW_LADDERS_PER_SIGNAL", 3, profile=name),
        entry_crowded_min_score=_env_float("ENTRY_CROWDED_MIN_SCORE", 0.04, profile=name),
        entry_crowded_min_rs60_abs=_env_float("ENTRY_CROWDED_MIN_RS60_ABS", 0.003, profile=name),
        entry_crowded_min_rs30_abs=_env_float("ENTRY_CROWDED_MIN_RS30_ABS", 0.0015, profile=name),
        entry_spread_filter_enabled=_env_bool("ENTRY_SPREAD_FILTER_ENABLED", True, profile=name),
        entry_spread_filter_max_bps=_env_float(
            "ENTRY_SPREAD_FILTER_MAX_BPS",
            _env_float("EXTERNAL_PRICE_MAX_INTERNAL_SPREAD_BPS", 30.0, profile=name),
            profile=name,
        ),
        entry_spread_filter_block_if_unavailable=_env_bool("ENTRY_SPREAD_FILTER_BLOCK_IF_UNAVAILABLE", False, profile=name),
        max_buy_stages=_env_int("MAX_BUY_STAGES", ema_max_averaging_stages + 1, profile=name),
        averaging_drawdown_steps=tuple(
            _env_float(
                f"AVERAGING_DRAWDOWN_STEP_{index}",
                ema_averaging_drawdown_step * index,
                profile=name,
            )
            for index in range(1, averaging_stage_count + 1)
        ),
        averaging_budget_fractions=tuple(
            _env_float(
                f"AVERAGING_BUDGET_FRACTION_{index}",
                ema_averaging_base_fraction,
                profile=name,
            )
            for index in range(1, averaging_stage_count + 1)
        ),
        no_more_averaging_after_minutes=_env_float("EMA_BREAKEVEN_AFTER_HOURS", 48.0, profile=name) * 60.0,
        time_exit_after_minutes=_env_float("EMA_BREAKEVEN_AFTER_HOURS", 48.0, profile=name) * 60.0,
        urgent_time_exit_after_minutes=_env_float("URGENT_TIME_EXIT_AFTER_MINUTES", 0.0, profile=name),
        hard_time_exit_after_minutes=_env_float(
            "HARD_TIME_EXIT_AFTER_MINUTES",
            _env_float("HARD_TIME_EXIT_AFTER_HOURS", 96.0, profile=name) * 60.0,
            profile=name,
        ),
        hard_time_exit_close_fraction=_env_float("HARD_TIME_EXIT_CLOSE_FRACTION", 0.25, profile=name),
        hard_time_exit_step_minutes=_env_float("HARD_TIME_EXIT_STEP_MINUTES", 12.0 * 60.0, profile=name),
        hard_time_exit_fraction_step=_env_float("HARD_TIME_EXIT_FRACTION_STEP", 0.25, profile=name),
        hard_time_exit_max_loss_on_notional=_env_float("HARD_TIME_EXIT_MAX_LOSS_ON_NOTIONAL", 0.03, profile=name),
        hard_time_exit_bypass_profit_bank=_env_bool("HARD_TIME_EXIT_BYPASS_PROFIT_BANK", True, profile=name),
        hard_stop_loss_enabled=_env_bool("HARD_STOP_LOSS_ENABLED", False, profile=name),
        hard_stop_loss_pct=_env_float("HARD_STOP_LOSS_PCT", 0.0, profile=name),
        enable_absolute_force_exit=False,
        absolute_force_exit_after_minutes=0.0,
        enable_controlled_loss_exit=False,
        controlled_loss_after_zombie_minutes=0.0,
        controlled_loss_min_drawdown=0.0,
        controlled_loss_max_loss_on_notional=0.0,
        controlled_loss_max_position_fraction=0.0,
        controlled_loss_profit_bank_today_fraction=0.0,
        controlled_loss_profit_bank_7d_fraction=0.0,
        controlled_loss_min_bank_usdt=0.0,
        max_unhealthy_positions_for_new_entries=_env_int("MAX_UNHEALTHY_POSITIONS_FOR_NEW_ENTRIES", 2, profile=name),
        cancel_unsafe_hidden_close_orders=_env_bool("CANCEL_UNSAFE_HIDDEN_CLOSE_ORDERS", True, profile=name),
        enable_volatility_adjusted_ladders=False,
        volatility_window=_env_int("VOLATILITY_WINDOW", 60, profile=name),
        volatility_reference=_env_float("VOLATILITY_REFERENCE", 0.0012, profile=name),
        daily_volatility_window=_env_int("DAILY_VOLATILITY_WINDOW", 1440, profile=name),
        daily_volatility_reference=_env_float("DAILY_VOLATILITY_REFERENCE", 0.035, profile=name),
        enable_volatility_targeted_sizing=False,
        min_volatility_budget_multiplier=_env_float("MIN_VOLATILITY_BUDGET_MULTIPLIER", 0.65, profile=name),
        max_volatility_budget_multiplier=_env_float("MAX_VOLATILITY_BUDGET_MULTIPLIER", 1.50, profile=name),
        enable_volatility_recovery_stages=False,
        averaging_drawdown_daily_volatility_fraction=_env_float("AVERAGING_DRAWDOWN_DAILY_VOLATILITY_FRACTION", 0.18, profile=name),
        min_ladder_volatility_multiplier=_env_float("MIN_LADDER_VOLATILITY_MULTIPLIER", 0.75, profile=name),
        max_ladder_volatility_multiplier=_env_float("MAX_LADDER_VOLATILITY_MULTIPLIER", 2.5, profile=name),
        min_profit_fee_multiplier=1.0,
        enable_dynamic_profit_floor=False,
        dynamic_profit_floor_volatility_multiplier_threshold=_env_float("DYNAMIC_PROFIT_FLOOR_VOLATILITY_MULTIPLIER_THRESHOLD", 1.5, profile=name),
        dynamic_profit_floor_high_vol_multiplier=_env_float("DYNAMIC_PROFIT_FLOOR_HIGH_VOL_MULTIPLIER", 0.70, profile=name),
        dynamic_profit_floor_adverse_funding_multiplier=_env_float("DYNAMIC_PROFIT_FLOOR_ADVERSE_FUNDING_MULTIPLIER", 0.60, profile=name),
        dynamic_profit_floor_urgent_multiplier=_env_float("DYNAMIC_PROFIT_FLOOR_URGENT_MULTIPLIER", 0.40, profile=name),
        dynamic_profit_floor_min_rate=_env_float("DYNAMIC_PROFIT_FLOOR_MIN_RATE", 0.0, profile=name),
        enable_btc_risk_multiplier=False,
        btc_risk_return_window=_env_int("BTC_RISK_RETURN_WINDOW", 30, profile=name),
        btc_risk_drop_threshold=_env_float("BTC_RISK_DROP_THRESHOLD", -0.004, profile=name),
        btc_risk_high_vol_threshold=_env_float("BTC_RISK_HIGH_VOL_THRESHOLD", 0.0018, profile=name),
        btc_risk_drop_budget_multiplier=_env_float("BTC_RISK_DROP_BUDGET_MULTIPLIER", 0.70, profile=name),
        btc_risk_vol_budget_multiplier=_env_float("BTC_RISK_VOL_BUDGET_MULTIPLIER", 0.80, profile=name),
        btc_risk_min_budget_multiplier=_env_float("BTC_RISK_MIN_BUDGET_MULTIPLIER", 0.55, profile=name),
        btc_risk_max_ladder_multiplier=_env_float("BTC_RISK_MAX_LADDER_MULTIPLIER", 1.8, profile=name),
        enable_funding_aware_exit=False,
        funding_cache_ttl_sec=_env_int("FUNDING_CACHE_TTL_SEC", 300, profile=name),
        funding_positive_threshold=_env_float("FUNDING_POSITIVE_THRESHOLD", 0.0001, profile=name),
        funding_negative_threshold=_env_float("FUNDING_NEGATIVE_THRESHOLD", -0.0001, profile=name),
        funding_positive_markup_multiplier=_env_float("FUNDING_POSITIVE_MARKUP_MULTIPLIER", 0.75, profile=name),
        funding_negative_markup_multiplier=_env_float("FUNDING_NEGATIVE_MARKUP_MULTIPLIER", 1.15, profile=name),
    )
    macro = MacroSettings(
        enable_gold_btc_rsi_overlay=_env_bool("ENABLE_GOLD_BTC_RSI_OVERLAY", True, profile=name),
        gold_coins=_env_csv("MACRO_GOLD_COINS", ("xaut",), profile=name),
        gold_timeframe=_env("GOLD_TIMEFRAME", profile=name) or "4h",
        gold_rsi_period=_env_int("GOLD_RSI_PERIOD", 14, profile=name),
        gold_min_candles=_env_int("GOLD_MIN_CANDLES", 80, profile=name),
        gold_cache_ttl_sec=_env_int("GOLD_CACHE_TTL_SEC", 900, profile=name),
        use_direct_gold_btc_pair=_env_bool("USE_DIRECT_GOLD_BTC_PAIR", False, profile=name),
        direct_gold_btc_symbol=_env("DIRECT_GOLD_BTC_SYMBOL", profile=name),
        gold_strong_rsi=_env_float("GOLD_STRONG_RSI", 60.0, profile=name),
        gold_weak_rsi=_env_float("GOLD_WEAK_RSI", 40.0, profile=name),
        btc_strong_rsi=_env_float("BTC_STRONG_RSI", 60.0, profile=name),
        btc_weak_rsi=_env_float("BTC_WEAK_RSI", 40.0, profile=name),
        rsi_spread_threshold=_env_float("RSI_SPREAD_THRESHOLD", 15.0, profile=name),
        risk_off_long_budget_multiplier=_env_float("RISK_OFF_LONG_BUDGET_MULTIPLIER", 0.55, profile=name),
        risk_off_short_budget_multiplier=_env_float("RISK_OFF_SHORT_BUDGET_MULTIPLIER", 0.85, profile=name),
        risk_off_ladder_multiplier=_env_float("RISK_OFF_LADDER_MULTIPLIER", 1.25, profile=name),
        risk_off_disable_averaging=_env_bool("RISK_OFF_DISABLE_AVERAGING", True, profile=name),
        risk_off_disable_recovery=_env_bool("RISK_OFF_DISABLE_RECOVERY", True, profile=name),
        risk_off_time_exit_multiplier=_env_float("RISK_OFF_TIME_EXIT_MULTIPLIER", 0.75, profile=name),
        panic_disable_new_entries=_env_bool("PANIC_DISABLE_NEW_ENTRIES", True, profile=name),
        stale_macro_max_age_sec=_env_int("STALE_MACRO_MAX_AGE_SEC", 3600, profile=name),
    )
    archive_dir = _env("CSV_ARCHIVE_DIR", profile=name) or "csv_archive"
    archive_path = Path(archive_dir)
    if not archive_path.is_absolute():
        archive_dir = str(BASE_DIR / name / archive_dir)

    monitoring = MonitoringSettings(
        log_level=_env("LOG_LEVEL", profile=name) or "INFO",
        cycle_stats_csv_file=_path(name, f"bot_futures{'_short' if name == 'short' else ''}_cycle_stats.csv"),
        csv_log_file=_path(name, f"bot_futures{'_short' if name == 'short' else ''}_trades.csv"),
        macro_csv_file=_path(name, "bot_futures_macro.csv"),
        external_price_csv_file=_path(name, "external_price_feed.csv"),
        account_pnl_csv_file=_path(name, "account_pnl.csv"),
        signal_analytics_csv_file=_path(name, "signal_analytics.csv"),
        signal_analytics_jsonl_file=_path(name, "signal_analytics.jsonl"),
        diagnostics_csv_file=_path(name, "diagnostics.csv"),
        diagnostics_jsonl_file=_path(name, "diagnostics.jsonl"),
        csv_archive_dir=archive_dir,
        csv_rotate_max_bytes=_env_int("CSV_ROTATE_MAX_BYTES", 1 * 1024 * 1024, profile=name),
    )

    external_price_feed = ExternalPriceFeedSettings(
        enabled=_env_bool("EXTERNAL_PRICE_FEED_ENABLED", True, profile=name),
        primary_exchange=_env("EXTERNAL_PRICE_PRIMARY_EXCHANGE", profile=name) or "htx",
        reference_exchanges=_env_csv("EXTERNAL_PRICE_REFERENCE_EXCHANGES", ("mexc",), profile=name),
        rest_poll_interval_sec=_env_float("EXTERNAL_PRICE_REST_POLL_INTERVAL_SEC", 1.0, profile=name),
        rest_timeout_sec=_env_float("EXTERNAL_PRICE_REST_TIMEOUT_SEC", 3.0, profile=name),
        max_price_age_ms=_env_int("EXTERNAL_PRICE_MAX_PRICE_AGE_MS", 3000, profile=name),
        min_valid_bid_qty_usdt=_env_float("EXTERNAL_PRICE_MIN_VALID_BID_QTY_USDT", 50.0, profile=name),
        min_valid_ask_qty_usdt=_env_float("EXTERNAL_PRICE_MIN_VALID_ASK_QTY_USDT", 50.0, profile=name),
        max_internal_spread_bps=_env_float("EXTERNAL_PRICE_MAX_INTERNAL_SPREAD_BPS", 30.0, profile=name),
        entry_filter_enabled=_env_bool("EXTERNAL_PRICE_ENTRY_FILTER_ENABLED", True, profile=name),
        max_htx_premium_for_long_bps=_env_float("EXTERNAL_PRICE_MAX_HTX_PREMIUM_FOR_LONG_BPS", 15.0, profile=name),
        max_htx_discount_for_short_bps=_env_float("EXTERNAL_PRICE_MAX_HTX_DISCOUNT_FOR_SHORT_BPS", 15.0, profile=name),
        block_if_exchange_divergence_1m_bps=_env_float("EXTERNAL_PRICE_BLOCK_IF_DIVERGENCE_1M_BPS", 50.0, profile=name),
        block_duration_sec=_env_int("EXTERNAL_PRICE_BLOCK_DURATION_SEC", 300, profile=name),
        directional_1m_gate_enabled=_env_bool("EXTERNAL_PRICE_DIRECTIONAL_1M_GATE_ENABLED", True, profile=name),
        directional_entry_1m_block_bps=_env_float("EXTERNAL_PRICE_DIRECTIONAL_ENTRY_1M_BLOCK_BPS", 50.0, profile=name),
        directional_averaging_1m_block_bps=_env_float(
            "EXTERNAL_PRICE_DIRECTIONAL_AVERAGING_1M_BLOCK_BPS",
            _env_float("EXTERNAL_PRICE_DIRECTIONAL_ENTRY_1M_BLOCK_BPS", 50.0, profile=name),
            profile=name,
        ),
        impulse_confirmation_enabled=_env_bool("EXTERNAL_PRICE_IMPULSE_CONFIRMATION_ENABLED", True, profile=name),
        mexc_lead_threshold_bps_30s=_env_float("EXTERNAL_PRICE_MEXC_LEAD_THRESHOLD_BPS_30S", 5.0, profile=name),
        impulse_score_bonus=_env_float("EXTERNAL_PRICE_IMPULSE_SCORE_BONUS", 0.02, profile=name),
        require_same_direction=_env_bool("EXTERNAL_PRICE_REQUIRE_SAME_DIRECTION", True, profile=name),
        exit_adjustment_enabled=_env_bool("EXTERNAL_PRICE_EXIT_ADJUSTMENT_ENABLED", False, profile=name),
        long_take_profit_tighten_if_htx_premium_bps=_env_float("EXTERNAL_PRICE_LONG_TP_TIGHTEN_PREMIUM_BPS", 20.0, profile=name),
        short_take_profit_tighten_if_htx_discount_bps=_env_float("EXTERNAL_PRICE_SHORT_TP_TIGHTEN_DISCOUNT_BPS", 20.0, profile=name),
        tightened_ladder_fractions=_env_float_tuple("EXTERNAL_PRICE_TIGHTENED_LADDER_FRACTIONS", (0.40, 0.30, 0.20, 0.10), profile=name),
        tightened_ladder_markups=_env_optional_float_tuple("EXTERNAL_PRICE_TIGHTENED_LADDER_MARKUPS", (0.005, 0.010, 0.020, None), profile=name),
        disable_trading_if_reference_stale=_env_bool("EXTERNAL_PRICE_DISABLE_TRADING_IF_REFERENCE_STALE", False, profile=name),
        ignore_reference_if_stale=_env_bool("EXTERNAL_PRICE_IGNORE_REFERENCE_IF_STALE", True, profile=name),
        stale_after_ms=_env_int("EXTERNAL_PRICE_STALE_AFTER_MS", 3000, profile=name),
    )

    runtime = RuntimeSettings(
        order_timeout_sec=_env_int("ORDER_TIMEOUT_SEC", 90, profile=name),
        poll_interval_sec=_env_int("POLL_INTERVAL_SEC", 3, profile=name),
        post_only_enabled=_env_bool("POST_ONLY_ENABLED", True, profile=name),
        reduce_only_enabled=_env_bool("REDUCE_ONLY_ENABLED", True, profile=name),
        fetch_fill_details_on_sync=_env_bool("FETCH_FILL_DETAILS_ON_SYNC", True, profile=name),
        fill_detail_lookback_sec=_env_int("FILL_DETAIL_LOOKBACK_SEC", 6 * 60 * 60, profile=name),
        state_file=_path(name, f"bot_futures{'_short' if name == 'short' else ''}_state.json"),
        markets_cache_file=_path(name, f"bot_futures{'_short' if name == 'short' else ''}_markets_cache.json"),
    )

    profile = BotProfile(
        name=name,
        coins=coins,
        trade_direction=direction,
        position_side=position_side,
        opposite_position_side=opposite_position_side,
        entry_side=entry_side,
        exit_side=exit_side,
        api_credentials=api_credentials,
        exchange=exchange,
        signals=signals,
        buying=buying,
        selling=selling,
        risk=risk,
        strategy=strategy,
        macro=macro,
        monitoring=monitoring,
        runtime=runtime,
        external_price_feed=external_price_feed,
    )
    _validate_profile(profile)
    return profile


PROFILES: Dict[str, BotProfile] = {
    "long": _make_profile("long", "long", LONG_COINS),
    "short": _make_profile("short", "short", SHORT_COINS),
}

DEFAULT_PROFILE_NAME = "long"
PROFILE_NAMES = tuple(PROFILES.keys())
_CURRENT_PROFILE: ContextVar[BotProfile] = ContextVar("htxbot_profile", default=PROFILES[DEFAULT_PROFILE_NAME])


def resolve_profile(profile: Union[str, BotProfile, None] = None) -> BotProfile:
    if profile is None:
        return _CURRENT_PROFILE.get()
    if isinstance(profile, BotProfile):
        return profile
    key = str(profile).strip().lower()
    if key not in PROFILES:
        raise KeyError(f"Unknown bot profile {profile!r}. Available profiles: {', '.join(PROFILE_NAMES)}")
    return PROFILES[key]


def current_profile() -> BotProfile:
    return _CURRENT_PROFILE.get()


def set_current_profile(profile: Union[str, BotProfile]) -> BotProfile:
    resolved = resolve_profile(profile)
    _CURRENT_PROFILE.set(resolved)
    return resolved


@contextmanager
def use_profile(profile: Union[str, BotProfile]) -> Iterator[BotProfile]:
    resolved = resolve_profile(profile)
    token = _CURRENT_PROFILE.set(resolved)
    try:
        yield resolved
    finally:
        _CURRENT_PROFILE.reset(token)


def enabled_profile_names() -> Tuple[str, ...]:
    raw = _env("BOT_PROFILES")
    if not raw:
        return ("long", "short")
    names = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    unknown = tuple(name for name in names if name not in PROFILES)
    if unknown:
        raise KeyError(
            f"Unknown bot profile(s) in BOT_PROFILES: {', '.join(unknown)}. "
            f"Available profiles: {', '.join(PROFILE_NAMES)}"
        )
    if not names:
        return ("long", "short")
    return names


def __getattr__(name: str):
    profile = current_profile()
    if hasattr(profile, name):
        return getattr(profile, name)
    raise AttributeError(name)
