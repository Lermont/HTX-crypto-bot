# -*- coding: utf-8 -*-

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple, Union


BASE_DIR = Path(__file__).resolve().parent
CONFIG_WARNINGS = []
_DOTENV_VARS: Dict[str, str] = {}


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
            if profile and not key.upper().startswith(
                (f"{profile.upper()}_", "HTXBOT_")
            ):
                formatted_key = f"{profile.upper()}_{key}"
                if formatted_key not in _DOTENV_VARS:
                    _DOTENV_VARS[formatted_key] = clean_value
            else:
                if key not in _DOTENV_VARS:
                    _DOTENV_VARS[key] = clean_value


_load_dotenv_if_present(BASE_DIR / ".env")


def _env(name: str, profile: str = "") -> str:
    candidates = []
    if profile:
        prefix = profile.upper()
        candidates.extend((f"{prefix}_{name}", f"HTXBOT_{prefix}_{name}"))
    candidates.append(f"HTXBOT_{name}")
    candidates.append(name)
    candidates.append(f"HTXBOT_{name}")
    for candidate in candidates:
        value = os.environ.get(candidate, _DOTENV_VARS.get(candidate, "")).strip()
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


def _env_csv_optional(*names: str, profile: str = "") -> Optional[Tuple[str, ...]]:
    for name in names:
        value = _env(name, profile=profile)
        if not value:
            continue
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return None


def _normalize_coin(coin: str) -> str:
    return str(coin or "").strip().lower()


def _normalize_coins(coins: Tuple[str, ...]) -> Tuple[str, ...]:
    seen = set()
    normalized = []
    for coin in coins or ():
        item = _normalize_coin(coin)
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return tuple(normalized)


def _account_coin_env_names(suffix: str = "") -> Tuple[str, ...]:
    suffix = str(suffix or "").strip()
    if not suffix:
        return ("HTX_COINS", "COINS")
    return (
        f"HTX_COINS_{suffix}",
        f"COINS_{suffix}",
        f"HTX_API_{suffix}_COINS",
        f"API_{suffix}_COINS",
        f"HTX_{suffix}_COINS",
    )


def _configured_account_coins(
    profile: str, suffix: str = "", default: Tuple[str, ...] = ()
) -> Tuple[str, ...]:
    configured = _env_csv_optional(*_account_coin_env_names(suffix), profile=profile)
    if configured is None:
        return _normalize_coins(default)
    return _normalize_coins(configured)


def _configured_profile_coins(profile: str) -> Tuple[str, ...]:
    return _normalize_coins(
        _configured_account_coins(profile, "", ())
        + _configured_account_coins(profile, "2", ())
    )


def _env_float_tuple(
    name: str, default: Tuple[float, ...], profile: str = ""
) -> Tuple[float, ...]:
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


def _env_optional_float_tuple(
    name: str, default: Tuple[Optional[float], ...], profile: str = ""
) -> Tuple[Optional[float], ...]:
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


LONG_COINS = _configured_profile_coins("long")

SHORT_COINS = _configured_profile_coins("short")


@dataclass(frozen=True)
class ApiCredentials:
    api_key: str
    api_secret: str


@dataclass(frozen=True)
class ApiAccountSettings:
    name: str
    api_credentials: ApiCredentials
    coins: Tuple[str, ...]


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
    ema_entry_require_pullback_recovery: bool
    ema_chop_filter_enabled: bool
    ema_chop_period: int
    ema_chop_max: float
    ema_volume_confirmation_enabled: bool
    ema_volume_short_window: int
    ema_volume_long_window: int
    ema_volume_min_ratio: float
    ema_volume_min_directional_fraction: float
    ema_volume_spike_filter_enabled: bool
    ema_volume_spike_window: int
    ema_volume_spike_min_ratio: float
    ema_volume_adverse_spike_min_ratio: float
    ema_volume_profile_filter_enabled: bool
    ema_volume_profile_window: int
    ema_volume_profile_bins: int
    ema_volume_profile_value_area: float
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
    ema_exit_trailing_atr_multiplier: float
    ema_exit_trailing_min_pullback: float
    ema_exit_trailing_max_pullback: float
    ema_exit_trailing_take_profit_markup: float
    ema_exit_runner_profit_lock_enabled: bool
    ema_exit_runner_use_aggressive_limit: bool
    ema_averaging_enabled: bool
    ema_averaging_drawdown_step: float
    ema_averaging_min_drawdown_step: float
    ema_averaging_base_fraction: float
    ema_averaging_power: float
    ema_averaging_interval_hours: float
    ema_averaging_atr_enabled: bool
    ema_averaging_atr_period: int
    ema_averaging_atr_multiplier: float
    ema_averaging_min_atr_multiplier: float
    ema_averaging_min_daily_volatility_fraction: float
    ema_averaging_require_pullback_recovery: bool
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
    entry_macro_invalid_penalty: float
    entry_pullback_invalid_penalty: float
    entry_trigger_invalid_penalty: float
    entry_btc_invalid_penalty: float
    entry_btc_return_penalty_multiplier: float
    entry_market_structure_invalid_penalty: float
    entry_volume_invalid_penalty: float
    entry_chop_invalid_penalty: float
    entry_rs60_shortfall_penalty_multiplier: float
    entry_rs30_shortfall_penalty_multiplier: float
    entry_quality_budget_min_multiplier: float
    entry_quality_budget_reference: float
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
    hard_stop_loss_min_emergency_pct: float
    hard_stop_loss_atr_enabled: bool
    hard_stop_loss_atr_multiplier: float
    hard_stop_loss_atr_max_pct: float
    soft_defensive_exit_enabled: bool
    soft_defensive_exit_min_drawdown: float
    soft_defensive_exit_btc_against_return: float
    soft_defensive_exit_confirmations: int
    soft_defensive_exit_initial_fraction: float
    soft_defensive_exit_step_fraction: float
    soft_defensive_exit_max_fraction: float
    soft_defensive_exit_reprice_minutes: float
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
    controlled_loss_min_move_fraction: float
    controlled_loss_ramp_minutes: float
    controlled_loss_reprice_minutes: float
    controlled_loss_macro_gap_reference: float
    controlled_loss_macro_max_speed_multiplier: float
    controlled_loss_volatility_speed_enabled: bool
    controlled_loss_volatility_reference: float
    controlled_loss_volatility_trigger_multiplier: float
    controlled_loss_volatility_max_speed_multiplier: float
    controlled_loss_volatility_exponent: float
    controlled_loss_volatility_reprice_min_move_delta: float
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
    risk_off_time_exit_multiplier: float

    enable_gold_directional_bias: bool
    gold_directional_bias_strength: float
    gold_directional_bias_min_multiplier: float
    gold_directional_bias_max_multiplier: float
    gold_btc_ratio_return_reference: float

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
    score_penalty_multiplier: float
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
    dry_run: bool
    dry_run_equity: float
    order_timeout_sec: int
    poll_interval_sec: int
    market_data_max_workers: int
    post_only_enabled: bool
    reduce_only_enabled: bool
    fetch_fill_details_on_sync: bool
    fill_detail_lookback_sec: int
    state_file: str
    markets_cache_file: str


def _make_hedge_settings() -> HedgeSettings:
    return HedgeSettings(
        btc_hedge_enabled=False,
        btc_hedge_coin=(_env("BTC_HEDGE_COIN") or "btc").strip().lower(),
        btc_hedge_ratio=max(0.0, 1.0),
        btc_hedge_min_rebalance_notional=max(
            0.0, 10.0
        ),
        btc_hedge_max_notional=max(0.0, 0.0),
        btc_hedge_max_spread_bps=max(0.0, 30.0),
        btc_hedge_cooldown_sec=max(0.0, 30.0),
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
    api_accounts: Tuple[ApiAccountSettings, ...]
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
    def API_ACCOUNTS(self) -> Tuple[ApiAccountSettings, ...]:
        return self.api_accounts

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


def _validate_fraction_tuple(
    name: str, values: Tuple[float, ...], eps: float = 1e-9
) -> None:
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
    _validate_fraction_tuple(
        f"{profile.name}.BUYING.ladder_fractions", profile.buying.ladder_fractions
    )
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
        raise ValueError(
            f"{profile.name}.STRATEGY.ema_exit_trailing_fixed_fraction must be between 0 and 1"
        )
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
        "ema_exit_trailing_min_pullback",
        "ema_exit_trailing_max_pullback",
        "hard_stop_loss_pct",
        "hard_stop_loss_min_emergency_pct",
        "hard_stop_loss_atr_max_pct",
        "soft_defensive_exit_min_drawdown",
        "soft_defensive_exit_btc_against_return",
        "soft_defensive_exit_initial_fraction",
        "soft_defensive_exit_step_fraction",
        "soft_defensive_exit_max_fraction",
        "controlled_loss_max_position_fraction",
        "controlled_loss_min_move_fraction",
        "controlled_loss_volatility_reprice_min_move_delta",
    ):
        value = getattr(profile.strategy, setting_name)
        if value < 0.0 or value > 1.0:
            raise ValueError(
                f"{profile.name}.STRATEGY.{setting_name} must be between 0 and 1"
            )
    if profile.strategy.ema_exit_trailing_atr_multiplier < 0:
        raise ValueError(
            f"{profile.name}.STRATEGY.ema_exit_trailing_atr_multiplier must be non-negative"
        )
    if (
        profile.strategy.ema_exit_trailing_max_pullback > 0.0
        and profile.strategy.ema_exit_trailing_min_pullback
        > profile.strategy.ema_exit_trailing_max_pullback
    ):
        raise ValueError(
            f"{profile.name}.STRATEGY.ema_exit_trailing_min_pullback "
            "must be <= ema_exit_trailing_max_pullback"
        )

    if (
        profile.strategy.account_pnl_trailing_enabled
        and profile.strategy.account_pnl_trailing_stop_rate
        > profile.strategy.account_pnl_trailing_activation_rate
    ):
        _add_config_warning(
            f"{profile.name}: account_pnl_trailing_stop_rate is above activation_rate; "
            "global trailing may close immediately after activation"
        )
    if (
        profile.strategy.hard_stop_loss_enabled
        and profile.strategy.hard_stop_loss_pct <= 0
    ):
        raise ValueError(
            f"{profile.name}.STRATEGY.hard_stop_loss_pct must be positive when hard stop is enabled"
        )
    if profile.strategy.soft_defensive_exit_confirmations < 1:
        raise ValueError(
            f"{profile.name}.STRATEGY.soft_defensive_exit_confirmations must be at least 1"
        )
    if profile.strategy.hard_stop_loss_atr_multiplier < 0:
        raise ValueError(
            f"{profile.name}.STRATEGY.hard_stop_loss_atr_multiplier must be non-negative"
        )
    if profile.strategy.controlled_loss_volatility_reference < 0:
        raise ValueError(
            f"{profile.name}.STRATEGY.controlled_loss_volatility_reference must be non-negative"
        )
    if profile.strategy.controlled_loss_volatility_trigger_multiplier < 0:
        raise ValueError(
            f"{profile.name}.STRATEGY.controlled_loss_volatility_trigger_multiplier must be non-negative"
        )
    if profile.strategy.controlled_loss_volatility_max_speed_multiplier < 1:
        raise ValueError(
            f"{profile.name}.STRATEGY.controlled_loss_volatility_max_speed_multiplier must be at least 1"
        )
    if profile.strategy.controlled_loss_volatility_exponent < 1:
        raise ValueError(
            f"{profile.name}.STRATEGY.controlled_loss_volatility_exponent must be at least 1"
        )

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


def _api_credentials_for_account(profile: str, suffix: str = "") -> ApiCredentials:
    suffix = str(suffix or "").strip()
    if not suffix:
        return ApiCredentials(
            api_key=_first_env("HTX_API_KEY", "API_KEY", profile=profile),
            api_secret=_first_env("HTX_API_SECRET", "API_SECRET", profile=profile),
        )

    return ApiCredentials(
        api_key=_first_env(
            f"HTX_API_KEY_{suffix}",
            f"HTX_API{suffix}_KEY",
            f"HTX_API_{suffix}_KEY",
            f"HTX_{suffix}_API_KEY",
            "HTX_SECONDARY_API_KEY",
            f"API_KEY_{suffix}",
            "SECONDARY_API_KEY",
            profile=profile,
        ),
        api_secret=_first_env(
            f"HTX_API_SECRET_{suffix}",
            f"HTX_API{suffix}_SECRET",
            f"HTX_API_{suffix}_SECRET",
            f"HTX_{suffix}_API_SECRET",
            "HTX_SECONDARY_API_SECRET",
            f"API_SECRET_{suffix}",
            "SECONDARY_API_SECRET",
            profile=profile,
        ),
    )


def _validate_api_account_coins(
    accounts: Tuple[ApiAccountSettings, ...], profile: str
) -> None:
    owners = {}
    for account in accounts:
        for coin in account.coins:
            previous = owners.get(coin)
            if previous and previous != account.name:
                raise ValueError(
                    f"{profile}: coin {coin!r} is assigned to multiple HTX API accounts "
                    f"({previous}, {account.name})"
                )
            owners[coin] = account.name


def _make_api_accounts(
    profile: str, primary_credentials: ApiCredentials, fallback_coins: Tuple[str, ...]
) -> Tuple[ApiAccountSettings, ...]:
    primary_coins = _configured_account_coins(profile, "", fallback_coins)
    accounts = [
        ApiAccountSettings(
            name="primary",
            api_credentials=primary_credentials,
            coins=primary_coins,
        )
    ]

    secondary_credentials = _api_credentials_for_account(profile, "2")
    secondary_coins = _configured_account_coins(profile, "2", ())
    if (
        secondary_coins
        or secondary_credentials.api_key
        or secondary_credentials.api_secret
    ):
        accounts.append(
            ApiAccountSettings(
                name="secondary",
                api_credentials=secondary_credentials,
                coins=secondary_coins,
            )
        )

    resolved = tuple(accounts)
    _validate_api_account_coins(resolved, profile)
    return resolved


def _coins_from_api_accounts(
    accounts: Tuple[ApiAccountSettings, ...],
) -> Tuple[str, ...]:
    return _normalize_coins(
        tuple(coin for account in accounts for coin in account.coins)
    )


@dataclass(frozen=True)
class ProfileDirectionContext:
    trade_direction: str
    position_side: str
    opposite_position_side: str
    entry_side: str
    exit_side: str


@dataclass(frozen=True)
class ProfileStrategyContext:
    ema_trigger_timeframe: str
    ema_macro_timeframe: str
    ema_pullback_timeframe: str
    ema_entry_fractions: Tuple[float, ...]
    ema_entry_offsets: Tuple[float, ...]
    ema_max_averaging_stages: int
    dust_position_notional: float


def _make_direction_context(direction: str) -> ProfileDirectionContext:
    direction = direction.lower()
    if direction not in {"long", "short"}:
        raise ValueError(f"Unsupported trade direction: {direction}")
    return ProfileDirectionContext(
        trade_direction=direction,
        position_side=direction,
        opposite_position_side="short" if direction == "long" else "long",
        entry_side="buy" if direction == "long" else "sell",
        exit_side="sell" if direction == "long" else "buy",
    )


def _make_strategy_context(
    name: str, direction_context: ProfileDirectionContext
) -> ProfileStrategyContext:
    direction = direction_context.trade_direction
    ema_trigger_timeframe = _env("EMA_TRIGGER_TIMEFRAME", profile=name) or "5m"
    ema_macro_timeframe = _env("EMA_MACRO_TIMEFRAME", profile=name) or "1h"
    ema_pullback_timeframe = (
        _env("EMA_PULLBACK_TIMEFRAME", profile=name) or ema_trigger_timeframe
    )

    ema_entry_offsets_default = (0.0, 0.01)
    ema_entry_offsets = ema_entry_offsets_default
    ema_entry_fractions = (0.50, 0.50)

    configured_ema_max_averaging_stages = 2
    if configured_ema_max_averaging_stages > 2:
        _add_config_warning(
            f"{name}: live ema_max_averaging_stages={configured_ema_max_averaging_stages} "
            "is capped at the conservative launch maximum of 2"
        )
    ema_max_averaging_stages = min(max(0, configured_ema_max_averaging_stages), 2)
    dust_position_notional = 10.0

    return ProfileStrategyContext(
        ema_trigger_timeframe=ema_trigger_timeframe,
        ema_macro_timeframe=ema_macro_timeframe,
        ema_pullback_timeframe=ema_pullback_timeframe,
        ema_entry_fractions=ema_entry_fractions,
        ema_entry_offsets=ema_entry_offsets,
        ema_max_averaging_stages=ema_max_averaging_stages,
        dust_position_notional=dust_position_notional,
    )


def _make_exchange_settings(name: str) -> ExchangeSettings:
    return ExchangeSettings(
        quote_currency="USDT",
        enable_rate_limit=True,
        timeout_ms=30000,
        default_type="swap",
        set_position_mode_on_start=True,
        set_leverage_on_start=False,
        contract_hostnames=("api.hbdm.com", "api.hbdm.vn"),
        market_load_retries=4,
        markets_cache_max_age_sec=7 * 24 * 60 * 60,
    )


def _make_signal_settings(
    name: str, strategy_context: ProfileStrategyContext
) -> SignalSettings:
    return SignalSettings(
        timeframe=strategy_context.ema_trigger_timeframe,
        rs_fast_window=30,
        rs_slow_window=60,
    )


def _make_buy_settings(
    name: str, strategy_context: ProfileStrategyContext
) -> BuySettings:
    return BuySettings(
        position_budget_fraction=0.02,
        ladder_fractions=strategy_context.ema_entry_fractions,
        ladder_offsets=strategy_context.ema_entry_offsets,
    )


def _make_sell_settings(name: str) -> SellSettings:
    return SellSettings(
        buy_fee_rate=0.0001,
        sell_fee_rate=0.0001,
        min_gross_profit_floor=0.0,
    )


def _make_risk_settings(
    name: str, strategy_context: ProfileStrategyContext
) -> RiskSettings:
    return RiskSettings(
        min_quote_reserve=15.0,
        max_active_positions=50,
        max_position_notional_fraction=0.03,
        max_total_notional_fraction=0.50,
        active_position_min_notional_for_slot=strategy_context.dust_position_notional,
        dust_position_notional=strategy_context.dust_position_notional,
        dust_close_enabled=True,
        tiny_entry_close_enabled=True,
        tiny_entry_max_notional=strategy_context.dust_position_notional,
        tiny_entry_max_planned_fraction=0.10,
        leverage=30,
        account_leverage=0,
        margin_mode="cross",
        position_mode="one-way",
        cooldown_minutes_after_close=10.0,
        post_win_cooldown_minutes_after_close=90.0,
    )


def _make_strategy_settings(
    name: str, strategy_context: ProfileStrategyContext
) -> StrategySettings:
    ema_exit_fractions = (1.0,)
    ema_take_profit_markup = 0.01
    ema_exit_normal_fractions = (0.35, 0.25, 0.25, 0.15)
    ema_exit_normal_markups = (0.008, 0.016, 0.030, 0.050)
    ema_exit_medium_fractions = (0.45, 0.30, 0.15, 0.10)
    ema_exit_medium_markups = (0.004, 0.010, 0.020, 0.035)
    ema_exit_heavy_fractions = (0.60, 0.25, 0.15)
    ema_exit_heavy_markups = (0.003, 0.008, 0.015)

    ema_averaging_min_drawdown_step = max(
        0.001,
        0.01,
    )
    ema_averaging_drawdown_step = 0.01
    ema_averaging_base_fraction = 0.50
    ema_averaging_power = 1.0
    averaging_stage_count = max(0, strategy_context.ema_max_averaging_stages)
    hard_stop_loss_pct = 0.02

    return StrategySettings(
        ema_strategy_enabled=True,
        ema_macro_timeframe=strategy_context.ema_macro_timeframe,
        ema_pullback_timeframe=strategy_context.ema_pullback_timeframe,
        ema_trigger_timeframe=strategy_context.ema_trigger_timeframe,
        ema_macro_fast_minutes=2880,
        ema_macro_slow_minutes=7200,
        ema_pullback_fast_minutes=120,
        ema_pullback_slow_minutes=360,
        ema_pullback_recovery_lookback_minutes=720,
        ema_pullback_recovery_max_cross_age_minutes=180,
        ema_pullback_recovery_gap=0.001,
        ema_entry_require_pullback_recovery=False,
        ema_chop_filter_enabled=True,
        ema_chop_period=max(2, 14),
        ema_chop_max=61.8,
        ema_volume_confirmation_enabled=True,
        ema_volume_short_window=max(
            1, 5
        ),
        ema_volume_long_window=max(
            1, 20
        ),
        ema_volume_min_ratio=max(
            0.0, 1.05
        ),
        ema_volume_min_directional_fraction=max(
            0.0,
            min(
                1.0,
                0.0,
            ),
        ),
        ema_volume_spike_filter_enabled=True,
        ema_volume_spike_window=max(
            1, 5
        ),
        ema_volume_spike_min_ratio=max(
            0.0, 1.80
        ),
        ema_volume_adverse_spike_min_ratio=max(
            0.0, 2.00
        ),
        ema_volume_profile_filter_enabled=True,
        ema_volume_profile_window=max(
            1, 60
        ),
        ema_volume_profile_bins=max(
            2, 12
        ),
        ema_volume_profile_value_area=max(
            0.10,
            min(1.0, 0.70),
        ),
        ema_trigger_fast_minutes=120,
        ema_trigger_slow_minutes=360,
        ema_use_rs_confirmation=True,
        ema_long_min_rs60=0.0,
        ema_short_max_rs60=0.0,
        ema_use_btc_risk_filter=True,
        ema_btc_long_min_return_30m=-0.0025,
        ema_btc_short_max_return_30m=0.0025,
        ema_take_profit_markup=ema_take_profit_markup,
        ema_exit_ladder_fractions=ema_exit_fractions,
        ema_adaptive_exit_enabled=True,
        ema_exit_normal_ladder_fractions=ema_exit_normal_fractions,
        ema_exit_normal_ladder_markups=ema_exit_normal_markups,
        ema_exit_medium_ladder_fractions=ema_exit_medium_fractions,
        ema_exit_medium_ladder_markups=ema_exit_medium_markups,
        ema_exit_heavy_ladder_fractions=ema_exit_heavy_fractions,
        ema_exit_heavy_ladder_markups=ema_exit_heavy_markups,
        ema_exit_medium_position_ratio=1.30,
        ema_exit_heavy_position_ratio=1.80,
        ema_exit_decay_first_markup_after_hours=2.0,
        ema_exit_decay_first_markup_cap=0.008,
        ema_exit_decay_max_markup_after_hours=6.0,
        ema_exit_decay_max_markup=0.030,
        ema_exit_runner_enabled=True,
        ema_exit_runner_activation_markup=0.020,
        ema_exit_runner_trailing_pullback=0.010,
        ema_exit_runner_take_profit_markup=0.050,
        ema_exit_trailing_enabled=True,
        ema_exit_trailing_fixed_fraction=0.30,
        ema_exit_trailing_activation_markup=0.020,
        ema_exit_trailing_pullback=0.010,
        ema_exit_trailing_atr_multiplier=1.5,
        ema_exit_trailing_min_pullback=0.006,
        ema_exit_trailing_max_pullback=0.030,
        ema_exit_trailing_take_profit_markup=0.050,
        ema_exit_runner_profit_lock_enabled=True,
        ema_exit_runner_use_aggressive_limit=True,
        ema_averaging_enabled=True,
        ema_averaging_drawdown_step=ema_averaging_drawdown_step,
        ema_averaging_min_drawdown_step=ema_averaging_min_drawdown_step,
        ema_averaging_base_fraction=ema_averaging_base_fraction,
        ema_averaging_power=ema_averaging_power,
        ema_averaging_interval_hours=8.0,
        ema_averaging_atr_enabled=False,
        ema_averaging_atr_period=max(
            1, 14
        ),
        ema_averaging_atr_multiplier=max(
            0.0, 1.0
        ),
        ema_averaging_min_atr_multiplier=max(
            0.0,
            1.0,
        ),
        ema_averaging_min_daily_volatility_fraction=max(
            0.0,
            0.18,
        ),
        ema_averaging_require_pullback_recovery=True,
        ema_max_averaging_stages=strategy_context.ema_max_averaging_stages,
        account_pnl_enabled=True,
        account_pnl_window_minutes=360.0,
        account_pnl_sample_interval_sec=30.0,
        account_profit_unload_enabled=False,
        account_profit_unload_min_pnl_quote=5.0,
        account_profit_unload_min_pnl_rate=0.002,
        account_profit_unload_percentile=0.75,
        account_profit_unload_fraction=0.25,
        account_profit_unload_drawdown_fraction=0.50,
        account_profit_unload_peak_drawdown_fraction=0.25,
        account_profit_unload_full_pnl_quote=0.0,
        account_profit_unload_min_position_pnl_quote=0.50,
        account_profit_unload_min_position_pnl_rate=0.001,
        account_profit_unload_cooldown_sec=300.0,
        account_pnl_trailing_enabled=False,
        account_pnl_trailing_activation_rate=0.050,
        account_pnl_trailing_stop_rate=0.035,
        account_pnl_trailing_min_pnl_quote=0.0,
        account_averaging_enabled=False,
        account_averaging_min_samples=6,
        account_averaging_percentile=0.25,
        account_averaging_near_trough_quote=3.0,
        account_averaging_near_trough_fraction=0.10,
        account_averaging_bounce_quote=1.0,
        account_averaging_falling_guard_quote=1.0,
        account_averaging_falling_guard_fraction=0.05,
        account_averaging_budget_scale=0.50,
        ema_breakeven_enabled=True,
        ema_breakeven_after_hours=48.0,
        ema_breakeven_reprice_minutes=15.0,
        ema_breakeven_fee_buffer=0.0002,
        ema_breakeven_exit_fractions=(1.0,),
        enable_signal_size_scaling=False,
        signal_budget_min_multiplier=1.0,
        signal_budget_max_multiplier=1.0,
        signal_score_reference=1.0,
        signal_ema_gap_weight=1.0,
        entry_min_score=0.03,
        entry_min_rs60_abs=0.002,
        entry_min_rs30_abs=0.001,
        entry_macro_invalid_penalty=0.018,
        entry_pullback_invalid_penalty=0.006,
        entry_trigger_invalid_penalty=0.018,
        entry_btc_invalid_penalty=0.012,
        entry_btc_return_penalty_multiplier=4.0,
        entry_market_structure_invalid_penalty=0.020,
        entry_volume_invalid_penalty=0.012,
        entry_chop_invalid_penalty=0.012,
        entry_rs60_shortfall_penalty_multiplier=12.0,
        entry_rs30_shortfall_penalty_multiplier=24.0,
        entry_quality_budget_min_multiplier=0.35,
        entry_quality_budget_reference=0.06,
        entry_max_new_ladders_per_signal=5,
        entry_rate_limit_ladders=10,
        entry_rate_limit_window_minutes=60.0,
        entry_crowded_signal_fraction=0.30,
        entry_crowded_min_signals=12,
        entry_crowded_max_new_ladders_per_signal=3,
        entry_crowded_min_score=0.04,
        entry_crowded_min_rs60_abs=0.003,
        entry_crowded_min_rs30_abs=0.0015,
        entry_spread_filter_enabled=True,
        entry_spread_filter_max_bps=30.0,
        entry_spread_filter_block_if_unavailable=False,
        max_buy_stages=strategy_context.ema_max_averaging_stages + 1,
        averaging_drawdown_steps=tuple(
            max(
                ema_averaging_min_drawdown_step * index,
                ema_averaging_drawdown_step * index,
            )
            for index in range(1, averaging_stage_count + 1)
        ),
        averaging_budget_fractions=tuple(
            ema_averaging_base_fraction
            for index in range(1, averaging_stage_count + 1)
        ),
        no_more_averaging_after_minutes=48.0
        * 60.0,
        time_exit_after_minutes=48.0
        * 60.0,
        urgent_time_exit_after_minutes=0.0,
        hard_time_exit_after_minutes=96.0 * 60.0,
        hard_time_exit_close_fraction=0.25,
        hard_time_exit_step_minutes=12.0 * 60.0,
        hard_time_exit_fraction_step=0.25,
        hard_time_exit_max_loss_on_notional=0.03,
        hard_time_exit_bypass_profit_bank=True,
        hard_stop_loss_enabled=False,
        hard_stop_loss_pct=hard_stop_loss_pct,
        hard_stop_loss_min_emergency_pct=0.04,
        hard_stop_loss_atr_enabled=True,
        hard_stop_loss_atr_multiplier=max(
            0.0, 2.0
        ),
        hard_stop_loss_atr_max_pct=0.03,
        soft_defensive_exit_enabled=True,
        soft_defensive_exit_min_drawdown=hard_stop_loss_pct,
        soft_defensive_exit_btc_against_return=0.003,
        soft_defensive_exit_confirmations=max(
            1, 2
        ),
        soft_defensive_exit_initial_fraction=0.33,
        soft_defensive_exit_step_fraction=0.33,
        soft_defensive_exit_max_fraction=1.0,
        soft_defensive_exit_reprice_minutes=6.0,
        enable_absolute_force_exit=False,
        absolute_force_exit_after_minutes=0.0,
        enable_controlled_loss_exit=True,
        controlled_loss_after_zombie_minutes=180.0,
        controlled_loss_min_drawdown=0.025,
        controlled_loss_max_loss_on_notional=0.06,
        controlled_loss_max_position_fraction=0.20,
        controlled_loss_profit_bank_today_fraction=0.75,
        controlled_loss_profit_bank_7d_fraction=0.12,
        controlled_loss_min_bank_usdt=5.0,
        controlled_loss_min_move_fraction=0.10,
        controlled_loss_ramp_minutes=24.0 * 60.0,
        controlled_loss_reprice_minutes=60.0,
        controlled_loss_macro_gap_reference=0.02,
        controlled_loss_macro_max_speed_multiplier=2.0,
        controlled_loss_volatility_speed_enabled=True,
        controlled_loss_volatility_reference=max(
            0.0, 0.0
        ),
        controlled_loss_volatility_trigger_multiplier=max(
            0.0,
            1.5,
        ),
        controlled_loss_volatility_max_speed_multiplier=max(
            1.0,
            3.0,
        ),
        controlled_loss_volatility_exponent=max(
            1.0, 2.0
        ),
        controlled_loss_volatility_reprice_min_move_delta=0.05,
        max_unhealthy_positions_for_new_entries=2,
        cancel_unsafe_hidden_close_orders=True,
        enable_volatility_adjusted_ladders=False,
        volatility_window=60,
        volatility_reference=0.0012,
        daily_volatility_window=1440,
        daily_volatility_reference=0.035,
        enable_volatility_targeted_sizing=False,
        min_volatility_budget_multiplier=0.65,
        max_volatility_budget_multiplier=1.50,
        enable_volatility_recovery_stages=False,
        averaging_drawdown_daily_volatility_fraction=0.18,
        min_ladder_volatility_multiplier=0.75,
        max_ladder_volatility_multiplier=2.5,
        min_profit_fee_multiplier=1.0,
        enable_dynamic_profit_floor=False,
        dynamic_profit_floor_volatility_multiplier_threshold=1.5,
        dynamic_profit_floor_high_vol_multiplier=0.70,
        dynamic_profit_floor_adverse_funding_multiplier=0.60,
        dynamic_profit_floor_urgent_multiplier=0.40,
        dynamic_profit_floor_min_rate=0.0,
        enable_btc_risk_multiplier=False,
        btc_risk_return_window=30,
        btc_risk_drop_threshold=-0.004,
        btc_risk_high_vol_threshold=0.0018,
        btc_risk_drop_budget_multiplier=0.70,
        btc_risk_vol_budget_multiplier=0.80,
        btc_risk_min_budget_multiplier=0.55,
        btc_risk_max_ladder_multiplier=1.8,
        enable_funding_aware_exit=False,
        funding_cache_ttl_sec=300,
        funding_positive_threshold=0.0001,
        funding_negative_threshold=-0.0001,
        funding_positive_markup_multiplier=0.75,
        funding_negative_markup_multiplier=1.15,
    )


def _make_macro_settings(name: str) -> MacroSettings:
    return MacroSettings(
        enable_gold_btc_rsi_overlay=True,
        gold_coins=("xaut",),
        gold_timeframe=_env("GOLD_TIMEFRAME", profile=name) or "4h",
        gold_rsi_period=14,
        gold_min_candles=80,
        gold_cache_ttl_sec=900,
        use_direct_gold_btc_pair=False,
        direct_gold_btc_symbol=_env("DIRECT_GOLD_BTC_SYMBOL", profile=name),
        gold_strong_rsi=60.0,
        gold_weak_rsi=40.0,
        btc_strong_rsi=60.0,
        btc_weak_rsi=40.0,
        rsi_spread_threshold=15.0,
        risk_off_long_budget_multiplier=0.55,
        risk_off_short_budget_multiplier=0.85,
        risk_off_ladder_multiplier=1.25,
        risk_off_disable_averaging=True,
        risk_off_time_exit_multiplier=0.75,
        enable_gold_directional_bias=True,
        gold_directional_bias_strength=0.30,
        gold_directional_bias_min_multiplier=0.50,
        gold_directional_bias_max_multiplier=1.25,
        gold_btc_ratio_return_reference=0.03,
        panic_disable_new_entries=True,
        stale_macro_max_age_sec=3600,
    )


def _make_monitoring_settings(name: str) -> MonitoringSettings:
    archive_dir = _env("CSV_ARCHIVE_DIR", profile=name) or "csv_archive"
    archive_path = Path(archive_dir)
    if not archive_path.is_absolute():
        archive_dir = str(BASE_DIR / name / archive_dir)

    return MonitoringSettings(
        log_level=_env("LOG_LEVEL", profile=name) or "INFO",
        cycle_stats_csv_file=_path(
            name, f"bot_futures{'_short' if name == 'short' else ''}_cycle_stats.csv"
        ),
        csv_log_file=_path(
            name, f"bot_futures{'_short' if name == 'short' else ''}_trades.csv"
        ),
        macro_csv_file=_path(name, "bot_futures_macro.csv"),
        external_price_csv_file=_path(name, "external_price_feed.csv"),
        account_pnl_csv_file=_path(name, "account_pnl.csv"),
        signal_analytics_csv_file=_path(name, "signal_analytics.csv"),
        signal_analytics_jsonl_file=_path(name, "signal_analytics.jsonl"),
        diagnostics_csv_file=_path(name, "diagnostics.csv"),
        diagnostics_jsonl_file=_path(name, "diagnostics.jsonl"),
        csv_archive_dir=archive_dir,
        csv_rotate_max_bytes=1 * 1024 * 1024,
    )


def _make_external_price_feed_settings(name: str) -> ExternalPriceFeedSettings:
    return ExternalPriceFeedSettings(
        enabled=True,
        primary_exchange=_env("EXTERNAL_PRICE_PRIMARY_EXCHANGE", profile=name) or "htx",
        reference_exchanges=("mexc",),
        rest_poll_interval_sec=1.0,
        rest_timeout_sec=3.0,
        max_price_age_ms=3000,
        min_valid_bid_qty_usdt=50.0,
        min_valid_ask_qty_usdt=50.0,
        max_internal_spread_bps=30.0,
        entry_filter_enabled=False,
        score_penalty_multiplier=0.5,
        max_htx_premium_for_long_bps=15.0,
        max_htx_discount_for_short_bps=15.0,
        block_if_exchange_divergence_1m_bps=50.0,
        block_duration_sec=300,
        directional_1m_gate_enabled=True,
        directional_entry_1m_block_bps=50.0,
        directional_averaging_1m_block_bps=50.0,
        impulse_confirmation_enabled=True,
        mexc_lead_threshold_bps_30s=5.0,
        impulse_score_bonus=0.02,
        require_same_direction=True,
        exit_adjustment_enabled=False,
        long_take_profit_tighten_if_htx_premium_bps=20.0,
        short_take_profit_tighten_if_htx_discount_bps=20.0,
        tightened_ladder_fractions=(0.40, 0.30, 0.20, 0.10),
        tightened_ladder_markups=(0.005, 0.010, 0.020, None),
        disable_trading_if_reference_stale=False,
        ignore_reference_if_stale=True,
        stale_after_ms=3000,
    )


def _make_runtime_settings(name: str) -> RuntimeSettings:
    return RuntimeSettings(
        dry_run=False,
        dry_run_equity=1000.0,
        order_timeout_sec=90,
        poll_interval_sec=3,
        market_data_max_workers=max(
            1, 8
        ),
        post_only_enabled=True,
        reduce_only_enabled=True,
        fetch_fill_details_on_sync=True,
        fill_detail_lookback_sec=6 * 60 * 60,
        state_file=_path(
            name, f"bot_futures{'_short' if name == 'short' else ''}_state.json"
        ),
        markets_cache_file=_path(
            name, f"bot_futures{'_short' if name == 'short' else ''}_markets_cache.json"
        ),
    )


def _make_profile(name: str, direction: str, coins: Tuple[str, ...]) -> BotProfile:
    coins = _normalize_coins(coins)

    direction_context = _make_direction_context(direction)
    strategy_context = _make_strategy_context(name, direction_context)

    api_credentials = _api_credentials_for_account(name)
    api_accounts = _make_api_accounts(name, api_credentials, coins)
    account_coins = _coins_from_api_accounts(api_accounts)
    if account_coins:
        coins = account_coins

    profile = BotProfile(
        name=name,
        coins=coins,
        trade_direction=direction_context.trade_direction,
        position_side=direction_context.position_side,
        opposite_position_side=direction_context.opposite_position_side,
        entry_side=direction_context.entry_side,
        exit_side=direction_context.exit_side,
        api_credentials=api_credentials,
        api_accounts=api_accounts,
        exchange=_make_exchange_settings(name),
        signals=_make_signal_settings(name, strategy_context),
        buying=_make_buy_settings(name, strategy_context),
        selling=_make_sell_settings(name),
        risk=_make_risk_settings(name, strategy_context),
        strategy=_make_strategy_settings(name, strategy_context),
        macro=_make_macro_settings(name),
        monitoring=_make_monitoring_settings(name),
        runtime=_make_runtime_settings(name),
        external_price_feed=_make_external_price_feed_settings(name),
    )
    _validate_profile(profile)
    return profile


PROFILES: Dict[str, BotProfile] = {
    "long": _make_profile("long", "long", LONG_COINS),
    "short": _make_profile("short", "short", SHORT_COINS),
}

DEFAULT_PROFILE_NAME = "long"
PROFILE_NAMES = tuple(PROFILES.keys())
_CURRENT_PROFILE: ContextVar[BotProfile] = ContextVar(
    "htxbot_profile", default=PROFILES[DEFAULT_PROFILE_NAME]
)


def resolve_profile(profile: Union[str, BotProfile, None] = None) -> BotProfile:
    if profile is None:
        return _CURRENT_PROFILE.get()
    if isinstance(profile, BotProfile):
        return profile
    key = str(profile).strip().lower()
    if key not in PROFILES:
        raise KeyError(
            f"Unknown bot profile {profile!r}. Available profiles: {', '.join(PROFILE_NAMES)}"
        )
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
