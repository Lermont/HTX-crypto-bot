# -*- coding: utf-8 -*-

import concurrent.futures
import math
import time
from typing import List, Optional, Tuple

import config

from .indicators import (
    average_true_range,
    calculate_ema,
    calculate_ema_series,
    calculate_rsi,
    clamp,
    compute_log_return,
    realized_volatility,
)
from .models import SignalContext
from .signal_math import (
    btc_risk_context,
    choppiness_context,
    daily_volatility_context,
    ema_pullback_recovery_context,
    ema_signal_direction_metrics,
    gold_btc_ratio_return,
    local_reversion_context,
    relative_strength_context,
    signal_budget_multiplier,
    signal_score,
    volume_confirmation_context,
    volatility_multiplier,
)


class SignalMixin:
    def _calculate_ema(self, prices: List[float], period: int) -> float:
        return calculate_ema(prices, period)

    def _compute_log_return(self, price_now: float, price_then: float) -> float:
        return compute_log_return(price_now, price_then)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return clamp(value, lower, upper)

    def _realized_volatility(self, closes: List[float], window: int) -> float:
        return realized_volatility(closes, window)

    def _average_true_range(self, candles: List[list], period: int) -> float:
        return average_true_range(candles, period)

    def _average_true_range_rate(
        self, candles: Optional[List[list]], close_price: float, period: int
    ) -> tuple[float, float]:
        if not candles or close_price <= 0:
            return 0.0, 0.0
        atr = self._average_true_range(candles, period)
        if atr <= 0:
            return 0.0, 0.0
        return atr, atr / close_price

    def _ema_market_structure_required_history(self) -> int:
        strategy = config.STRATEGY
        required = 0
        if getattr(strategy, "ema_chop_filter_enabled", False):
            required = max(required, max(2, int(strategy.ema_chop_period)) + 1)
        if getattr(strategy, "ema_volume_confirmation_enabled", False):
            required = max(
                required,
                max(1, int(strategy.ema_volume_short_window)),
                max(1, int(strategy.ema_volume_long_window)),
            )
            if getattr(strategy, "ema_volume_spike_filter_enabled", False):
                required = max(
                    required,
                    max(
                        1,
                        int(
                            getattr(
                                strategy,
                                "ema_volume_spike_window",
                                strategy.ema_volume_short_window,
                            )
                        ),
                    ),
                )
            if getattr(strategy, "ema_volume_profile_filter_enabled", False):
                required = max(
                    required,
                    max(
                        1,
                        int(
                            getattr(
                                strategy,
                                "ema_volume_profile_window",
                                strategy.ema_volume_long_window,
                            )
                        ),
                    ),
                )
        return required

    def _ema_market_structure_context(self, candles: Optional[List[list]]) -> dict:
        strategy = config.STRATEGY
        candles = candles or []
        if getattr(strategy, "ema_chop_filter_enabled", False) and candles:
            chop = choppiness_context(
                candles,
                strategy.ema_chop_period,
                strategy.ema_chop_max,
            )
        else:
            chop = {
                "chop_valid": True,
                "chop": 0.0,
                "chop_max": self._safe_float(
                    getattr(strategy, "ema_chop_max", 0.0), 0.0
                ),
                "chop_period": max(2, int(getattr(strategy, "ema_chop_period", 14))),
                "chop_reason": "disabled"
                if not getattr(strategy, "ema_chop_filter_enabled", False)
                else "candles_unavailable",
            }

        if getattr(strategy, "ema_volume_confirmation_enabled", False) and candles:
            volume = volume_confirmation_context(
                candles,
                strategy.ema_volume_short_window,
                strategy.ema_volume_long_window,
                strategy.ema_volume_min_ratio,
                strategy.ema_volume_min_directional_fraction,
                config.POSITION_SIDE,
                getattr(
                    strategy,
                    "ema_volume_spike_window",
                    strategy.ema_volume_short_window,
                ),
                (
                    getattr(strategy, "ema_volume_spike_min_ratio", 0.0)
                    if getattr(strategy, "ema_volume_spike_filter_enabled", False)
                    else 0.0
                ),
                (
                    getattr(strategy, "ema_volume_adverse_spike_min_ratio", 0.0)
                    if getattr(strategy, "ema_volume_spike_filter_enabled", False)
                    else 0.0
                ),
                getattr(strategy, "ema_volume_profile_filter_enabled", False),
                getattr(
                    strategy,
                    "ema_volume_profile_window",
                    strategy.ema_volume_long_window,
                ),
                getattr(strategy, "ema_volume_profile_bins", 12),
                getattr(strategy, "ema_volume_profile_value_area", 0.70),
            )
        else:
            volume = {
                "volume_valid": True,
                "volume_average_valid": True,
                "volume_ratio": 0.0,
                "volume_recent": 0.0,
                "volume_baseline": 0.0,
                "volume_directional_fraction": 0.0,
                "volume_spike_valid": True,
                "volume_spike_ratio": 0.0,
                "volume_spike_volume": 0.0,
                "volume_spike_baseline": 0.0,
                "volume_spike_direction": "unknown",
                "volume_spike_reason": "disabled",
                "volume_profile_valid": True,
                "volume_profile_poc": 0.0,
                "volume_profile_value_area_low": 0.0,
                "volume_profile_value_area_high": 0.0,
                "volume_profile_break": False,
                "volume_profile_reason": "disabled",
                "volume_required_candles": max(
                    max(1, int(getattr(strategy, "ema_volume_short_window", 5))),
                    max(1, int(getattr(strategy, "ema_volume_long_window", 20))),
                    max(1, int(getattr(strategy, "ema_volume_profile_window", 20))),
                ),
                "volume_reason": (
                    "disabled"
                    if not getattr(strategy, "ema_volume_confirmation_enabled", False)
                    else "candles_unavailable"
                ),
            }

        return {
            **volume,
            **chop,
            "market_structure_valid": bool(
                volume["volume_valid"] and chop["chop_valid"]
            ),
        }

    def _signal_market_structure_block_reason(
        self, signal: Optional[dict], prefix: str = "entry_market_structure_invalid"
    ) -> str:
        signal = signal or {}
        return (
            f"{prefix};"
            f"volume_valid={int(bool(signal.get('volume_valid', True)))};"
            f"volume_ratio={self._safe_float(signal.get('volume_ratio'), 0.0):.6f};"
            f"volume_min_ratio={self._safe_float(getattr(config.STRATEGY, 'ema_volume_min_ratio', 0.0), 0.0):.6f};"
            f"volume_spike_valid={int(bool(signal.get('volume_spike_valid', True)))};"
            f"volume_spike_ratio={self._safe_float(signal.get('volume_spike_ratio'), 0.0):.6f};"
            f"volume_spike_direction={signal.get('volume_spike_direction', '')};"
            f"volume_spike_reason={signal.get('volume_spike_reason', '')};"
            f"volume_profile_valid={int(bool(signal.get('volume_profile_valid', True)))};"
            f"volume_profile_break={int(bool(signal.get('volume_profile_break', False)))};"
            f"volume_profile_poc={self._safe_float(signal.get('volume_profile_poc'), 0.0):.12f};"
            f"volume_profile_reason={signal.get('volume_profile_reason', '')};"
            f"volume_reason={signal.get('volume_reason', '')};"
            f"chop_valid={int(bool(signal.get('chop_valid', True)))};"
            f"chop={self._safe_float(signal.get('chop'), 0.0):.6f};"
            f"chop_max={self._safe_float(signal.get('chop_max'), 0.0):.6f};"
            f"chop_reason={signal.get('chop_reason', '')}"
        )

    def _entry_raw_signal_block_reason(
        self, signal: Optional[dict], prefix: str = "entry_signal_invalid"
    ) -> str:
        if not signal:
            return f"{prefix};signal_missing=1"
        if not signal.get("entry_valid", False):
            context = self._entry_signal_quality_context(signal, external_bonus=0.0)
            if context.get("has_data"):
                return self._entry_weighted_score_block_reason(context)

        def flag(name: str, default: bool = True) -> int:
            return int(bool(signal.get(name, default)))

        data_default = bool(signal.get("valid", False))
        return (
            f"{prefix};"
            f"valid={flag('valid', False)};"
            f"data_valid={flag('data_valid', data_default)};"
            f"direction_valid={flag('direction_valid', data_default)};"
            f"entry_valid={flag('entry_valid', False)};"
            f"entry_setup_valid={flag('entry_setup_valid', False)};"
            f"entry_side_valid={flag('entry_side_valid', False)};"
            f"entry_signal_source={signal.get('entry_signal_source', '')};"
            f"macro_valid={flag('macro_valid')};"
            f"pullback_valid={flag('pullback_valid')};"
            f"trigger_valid={flag('trigger_valid')};"
            f"rs_confirm_valid={flag('rs_confirm_valid')};"
            f"btc_entry_valid={flag('btc_entry_valid')};"
            f"market_structure_valid={flag('market_structure_valid')};"
            f"volume_valid={flag('volume_valid')};"
            f"chop_valid={flag('chop_valid')};"
            f"score={self._safe_float(signal.get('score'), 0.0):.6f};"
            f"rs30={self._safe_float(signal.get('rs30'), 0.0):.6f};"
            f"rs60={self._safe_float(signal.get('rs60'), 0.0):.6f};"
            f"volume_reason={signal.get('volume_reason', '')};"
            f"chop_reason={signal.get('chop_reason', '')}"
        )

    def _signal_score(
        self, rs30: float, rs60: float, ema50: float, ema100: float, price: float
    ) -> float:
        return signal_score(
            rs30,
            rs60,
            ema50,
            ema100,
            price,
            config.POSITION_SIDE,
            config.STRATEGY.signal_ema_gap_weight,
        )

    def _local_reversion_context(
        self, closes: List[float], current_close: float
    ) -> dict:
        return local_reversion_context(closes, current_close, config.POSITION_SIDE)

    def _signal_budget_multiplier(self, score: float) -> float:
        strategy = config.STRATEGY
        return signal_budget_multiplier(
            score,
            strategy.enable_signal_size_scaling,
            strategy.signal_score_reference,
            strategy.signal_budget_min_multiplier,
            strategy.signal_budget_max_multiplier,
        )

    def _is_raw_entry_signal_valid(self, signal: Optional[dict]) -> bool:
        if (
            not signal
            or not self._signal_data_valid(signal)
            or not self.signal_cache.get("benchmark_ok")
        ):
            return False
        return bool(signal.get("entry_valid", False))

    def _signal_data_valid(self, signal: Optional[dict]) -> bool:
        if not signal:
            return False
        if "data_valid" in signal:
            return bool(signal.get("data_valid"))
        return bool(signal.get("valid"))

    def _signal_direction_valid(self, signal: Optional[dict]) -> bool:
        if not self._signal_data_valid(signal):
            return False
        if "direction_valid" in signal:
            return bool(signal.get("direction_valid"))
        return bool(signal.get("valid"))

    def _directional_entry_value(self, value: float) -> float:
        return -value if config.POSITION_SIDE == "short" else value

    def _entry_thresholds(self, crowded: bool = False) -> dict:
        strategy = config.STRATEGY
        thresholds = {
            "score": max(0.0, self._safe_float(strategy.entry_min_score, 0.0)),
            "rs60": self._safe_float(strategy.entry_min_rs60_abs, 0.0),
            "rs30": self._safe_float(strategy.entry_min_rs30_abs, 0.0),
        }
        if crowded:
            thresholds["score"] = max(
                thresholds["score"],
                max(0.0, self._safe_float(strategy.entry_crowded_min_score, 0.0)),
            )
            thresholds["rs60"] = max(
                thresholds["rs60"],
                self._safe_float(strategy.entry_crowded_min_rs60_abs, 0.0),
            )
            thresholds["rs30"] = max(
                thresholds["rs30"],
                self._safe_float(strategy.entry_crowded_min_rs30_abs, 0.0),
            )
        return thresholds

    def _entry_signal_quality_context(
        self, signal: Optional[dict], crowded: bool = False, external_bonus: float = 0.0
    ) -> dict:
        signal = signal or {}
        thresholds = self._entry_thresholds(crowded=crowded)
        min_score = max(0.0, self._safe_float(thresholds.get("score"), 0.0))
        strategy = config.STRATEGY
        raw_score = self._safe_float(signal.get("score"), 0.0)
        external_bonus = self._safe_float(external_bonus, 0.0)
        base_score = raw_score + external_bonus
        penalties = {}
        flags = {
            "valid": bool(signal.get("valid", False)),
            "data_valid": bool(signal.get("data_valid", signal.get("valid", False))),
            "direction_valid": bool(
                signal.get("direction_valid", signal.get("valid", False))
            ),
            "entry_valid": bool(signal.get("entry_valid", False)),
            "ema_entry_valid": bool(
                signal.get("ema_entry_valid", signal.get("entry_valid", False))
            ),
            "entry_setup_valid": bool(
                signal.get(
                    "entry_setup_valid",
                    bool(
                        signal.get("trigger_valid", False)
                        or signal.get("pullback_valid", False)
                    ),
                )
            ),
            "entry_side_valid": bool(
                signal.get(
                    "entry_side_valid",
                    bool(
                        signal.get("macro_valid", signal.get("direction_valid", False))
                        and signal.get(
                            "entry_setup_valid",
                            bool(
                                signal.get("trigger_valid", False)
                                or signal.get("pullback_valid", False)
                            ),
                        )
                    ),
                )
            ),
            "macro_valid": bool(
                signal.get("macro_valid", signal.get("direction_valid", False))
            ),
            "pullback_valid": bool(signal.get("pullback_valid", True)),
            "trigger_valid": bool(signal.get("trigger_valid", True)),
            "rs_confirm_valid": bool(signal.get("rs_confirm_valid", True)),
            "btc_entry_valid": bool(signal.get("btc_entry_valid", True)),
            "market_structure_valid": bool(signal.get("market_structure_valid", True)),
            "volume_valid": bool(signal.get("volume_valid", True)),
            "chop_valid": bool(signal.get("chop_valid", True)),
        }

        def add_penalty(name: str, amount: float) -> None:
            amount = max(0.0, self._safe_float(amount, 0.0))
            if amount > 1e-12:
                penalties[name] = penalties.get(name, 0.0) + amount

        has_data = bool(signal and self._signal_data_valid(signal))
        if not has_data:
            add_penalty("data", min_score + abs(base_score) + 1.0)
        if not flags["ema_entry_valid"]:
            add_penalty("ema_entry", min_score + abs(base_score) + 1.0)

        if not flags["macro_valid"]:
            add_penalty("macro", getattr(strategy, "entry_macro_invalid_penalty", 0.0))
        if not flags["pullback_valid"]:
            add_penalty(
                "pullback", getattr(strategy, "entry_pullback_invalid_penalty", 0.0)
            )
        if not flags["trigger_valid"]:
            add_penalty(
                "trigger", getattr(strategy, "entry_trigger_invalid_penalty", 0.0)
            )

        btc_valid = flags["btc_entry_valid"]
        if not btc_valid:
            btc_penalty = self._safe_float(
                getattr(strategy, "entry_btc_invalid_penalty", 0.0), 0.0
            )
            btc_return = self._safe_float(
                signal.get("btc_return_30m", signal.get("btc_entry_return")), 0.0
            )
            if config.POSITION_SIDE == "short":
                adverse_return = btc_return - self._safe_float(
                    getattr(strategy, "ema_btc_short_max_return_30m", 0.0), 0.0
                )
            else:
                adverse_return = (
                    self._safe_float(
                        getattr(strategy, "ema_btc_long_min_return_30m", 0.0), 0.0
                    )
                    - btc_return
                )
            btc_penalty += max(0.0, adverse_return) * self._safe_float(
                getattr(strategy, "entry_btc_return_penalty_multiplier", 0.0),
                0.0,
            )
            add_penalty("btc", btc_penalty)

        if not flags["market_structure_valid"]:
            add_penalty(
                "market_structure",
                getattr(strategy, "entry_market_structure_invalid_penalty", 0.0),
            )
        if not flags["volume_valid"]:
            add_penalty(
                "volume", getattr(strategy, "entry_volume_invalid_penalty", 0.0)
            )
        if not flags["chop_valid"]:
            add_penalty("chop", getattr(strategy, "entry_chop_invalid_penalty", 0.0))

        rs60 = self._safe_float(signal.get("rs60"), 0.0)
        rs30 = self._safe_float(signal.get("rs30"), 0.0)
        directional_rs60 = self._directional_entry_value(rs60)
        directional_rs30 = self._directional_entry_value(rs30)
        rs60_shortfall = max(
            0.0, self._safe_float(thresholds.get("rs60"), 0.0) - directional_rs60
        )
        rs30_shortfall = max(
            0.0, self._safe_float(thresholds.get("rs30"), 0.0) - directional_rs30
        )
        add_penalty(
            "rs60",
            rs60_shortfall
            * self._safe_float(
                getattr(strategy, "entry_rs60_shortfall_penalty_multiplier", 0.0), 0.0
            ),
        )
        add_penalty(
            "rs30",
            rs30_shortfall
            * self._safe_float(
                getattr(strategy, "entry_rs30_shortfall_penalty_multiplier", 0.0), 0.0
            ),
        )

        penalty_total = sum(penalties.values())
        weighted_score = base_score - penalty_total
        reference = max(
            min_score,
            self._safe_float(
                getattr(strategy, "entry_quality_budget_reference", 0.0), 0.0
            ),
            1e-9,
        )
        min_budget_multiplier = min(
            1.0,
            max(
                0.0,
                self._safe_float(
                    getattr(strategy, "entry_quality_budget_min_multiplier", 1.0), 1.0
                ),
            ),
        )
        quality_budget_multiplier = min(
            1.0, max(min_budget_multiplier, weighted_score / reference)
        )
        passed = bool(has_data and weighted_score + 1e-12 >= min_score)
        return {
            "has_data": has_data,
            "passed": passed,
            "thresholds": thresholds,
            "min_score": min_score,
            "raw_score": raw_score,
            "external_bonus": external_bonus,
            "base_score": base_score,
            "weighted_score": weighted_score,
            "penalty_total": penalty_total,
            "penalties": penalties,
            "flags": flags,
            "directional_rs60": directional_rs60,
            "directional_rs30": directional_rs30,
            "rs60_shortfall": rs60_shortfall,
            "rs30_shortfall": rs30_shortfall,
            "quality_budget_multiplier": quality_budget_multiplier,
            "volume_reason": signal.get("volume_reason", ""),
            "volume_profile_break": bool(signal.get("volume_profile_break", False)),
            "volume_spike_direction": signal.get("volume_spike_direction", ""),
            "volume_spike_reason": signal.get("volume_spike_reason", ""),
            "chop_reason": signal.get("chop_reason", ""),
            "btc_return_30m": self._safe_float(
                signal.get("btc_return_30m", signal.get("btc_entry_return")), 0.0
            ),
        }

    def _entry_weighted_score_block_reason(self, context: dict) -> str:
        penalties = (
            context.get("penalties")
            if isinstance(context.get("penalties"), dict)
            else {}
        )
        penalty_text = ";".join(
            f"penalty_{name}={value:.6f}" for name, value in sorted(penalties.items())
        )
        if penalty_text:
            penalty_text += ";"
        flags = context.get("flags") if isinstance(context.get("flags"), dict) else {}
        flag_text = ";".join(
            f"{name}={int(bool(value))}" for name, value in sorted(flags.items())
        )
        if flag_text:
            flag_text += ";"
        return (
            "entry_weighted_score_below_min;"
            f"weighted_score={self._safe_float(context.get('weighted_score'), 0.0):.6f};"
            f"raw_score={self._safe_float(context.get('raw_score'), 0.0):.6f};"
            f"external_bonus={self._safe_float(context.get('external_bonus'), 0.0):.6f};"
            f"penalty_total={self._safe_float(context.get('penalty_total'), 0.0):.6f};"
            f"{penalty_text}"
            f"{flag_text}"
            f"min={self._safe_float(context.get('min_score'), 0.0):.6f};"
            f"directional_rs30={self._safe_float(context.get('directional_rs30'), 0.0):.6f};"
            f"directional_rs60={self._safe_float(context.get('directional_rs60'), 0.0):.6f};"
            f"rs30_shortfall={self._safe_float(context.get('rs30_shortfall'), 0.0):.6f};"
            f"rs60_shortfall={self._safe_float(context.get('rs60_shortfall'), 0.0):.6f};"
            f"btc_return_30m={self._safe_float(context.get('btc_return_30m'), 0.0):.6f};"
            f"quality_budget_multiplier={self._safe_float(context.get('quality_budget_multiplier'), 0.0):.6f};"
            f"volume_profile_break={int(bool(context.get('volume_profile_break', False)))};"
            f"volume_spike_direction={context.get('volume_spike_direction', '')};"
            f"volume_spike_reason={context.get('volume_spike_reason', '')};"
            f"volume_reason={context.get('volume_reason', '')};"
            f"chop_reason={context.get('chop_reason', '')}"
        )

    def _entry_signal_quality_block_reason(
        self, signal: Optional[dict], crowded: bool = False
    ) -> str:
        external_bonus = self._safe_float(
            getattr(self, "_external_entry_score_bonus", lambda _signal: 0.0)(signal),
            0.0,
        )
        context = self._entry_signal_quality_context(
            signal, crowded=crowded, external_bonus=external_bonus
        )
        if context.get("passed"):
            return ""
        return self._entry_weighted_score_block_reason(context)

    def _is_entry_signal_valid(self, signal: Optional[dict]) -> bool:
        return not self._entry_signal_quality_block_reason(signal, crowded=False)

    def _is_entry_expansion_signal_valid(self, signal: Optional[dict]) -> bool:
        return False

    def _is_add_signal_valid(self, signal: Optional[dict]) -> bool:
        return self._signal_add_valid(signal)

    def _signal_add_valid(self, signal: Optional[dict]) -> bool:
        if (
            not signal
            or not self._signal_direction_valid(signal)
            or not self.signal_cache.get("benchmark_ok")
        ):
            return False
        return bool(signal.get("add_valid", False))

    def _volatility_multiplier(self, volatility: float) -> float:
        strategy = config.STRATEGY
        return volatility_multiplier(
            volatility,
            strategy.enable_volatility_adjusted_ladders,
            strategy.volatility_reference,
            strategy.min_ladder_volatility_multiplier,
            strategy.max_ladder_volatility_multiplier,
        )

    def _daily_volatility_context(self, closes: List[float]) -> dict:
        strategy = config.STRATEGY
        window = max(0, int(strategy.daily_volatility_window))
        return daily_volatility_context(
            closes,
            window,
            strategy.daily_volatility_reference,
            strategy.enable_volatility_targeted_sizing,
            strategy.min_volatility_budget_multiplier,
            strategy.max_volatility_budget_multiplier,
        )

    def _btc_risk_context(self, benchmark_closes: List[float]) -> dict:
        strategy = config.STRATEGY
        return btc_risk_context(
            benchmark_closes,
            config.POSITION_SIDE,
            strategy.enable_btc_risk_multiplier,
            strategy.btc_risk_return_window,
            strategy.volatility_window,
            strategy.btc_risk_drop_threshold,
            strategy.btc_risk_drop_budget_multiplier,
            strategy.btc_risk_high_vol_threshold,
            strategy.btc_risk_vol_budget_multiplier,
            strategy.btc_risk_min_budget_multiplier,
            strategy.btc_risk_max_ladder_multiplier,
        )

    def _calculate_rsi(self, closes: List[float], period: int) -> float:
        return calculate_rsi(closes, period)

    def _neutral_macro_context(
        self, reason: str = "neutral", regime: str = "neutral", ok: bool = True
    ) -> dict:
        return {
            "ok": ok,
            "ts": int(time.time()),
            "gold_symbol": getattr(self, "macro_gold_symbol", "") or "",
            "btc_symbol": getattr(self, "benchmark_symbol", "") or "",
            "timeframe": str(getattr(config.MACRO, "gold_timeframe", "4h") or "4h"),
            "gold_rsi": 0.0,
            "btc_rsi": 0.0,
            "rsi_spread": 0.0,
            "gold_btc_ratio_return": 0.0,
            "gold_return": 0.0,
            "btc_return": 0.0,
            "macro_direction_score": 0.0,
            "regime": regime,
            "long_budget_multiplier": 1.0,
            "short_budget_multiplier": 1.0,
            "directional_long_multiplier": 1.0,
            "directional_short_multiplier": 1.0,
            "ladder_multiplier": 1.0,
            "disable_new_entries": False,
            "disable_averaging": False,
            "time_exit_multiplier": 1.0,
            "reason": reason,
        }

    def _macro_cache_root(self) -> dict:
        return self.signal_cache.setdefault("macro", {})

    def _cached_gold_btc_rsi_context(self) -> dict:
        context = self._macro_cache_root().get("gold_btc_rsi")
        if isinstance(context, dict):
            return context
        context = self._neutral_macro_context(
            "not_loaded", regime="macro_unavailable", ok=False
        )
        self._macro_cache_root()["gold_btc_rsi"] = context
        return context

    def _macro_context_is_stale(self, context: dict) -> bool:
        max_age = max(0, int(config.MACRO.stale_macro_max_age_sec))
        if max_age <= 0:
            return False
        ts = self._safe_float((context or {}).get("ts"), 0.0)
        return bool(ts > 0 and time.time() - ts > max_age)

    def _macro_context_for_trading(self, context: Optional[dict] = None) -> dict:
        context = (
            context
            if isinstance(context, dict)
            else self._cached_gold_btc_rsi_context()
        )
        if not config.MACRO.enable_gold_btc_rsi_overlay:
            return self._neutral_macro_context(
                "disabled", regime="macro_disabled", ok=False
            )
        if self._macro_context_is_stale(context):
            stale = self._neutral_macro_context(
                "macro_context_stale", regime="neutral", ok=False
            )
            stale["gold_symbol"] = context.get("gold_symbol", stale["gold_symbol"])
            stale["btc_symbol"] = context.get("btc_symbol", stale["btc_symbol"])
            key = (context.get("ts"), context.get("regime"), context.get("reason"))
            if getattr(self, "_last_macro_stale_log_key", None) != key:
                self._last_macro_stale_log_key = key
                self._record_macro_context(
                    stale, event="macro_context_stale", level="WARNING"
                )
            return stale
        return context

    def _record_macro_context(self, context: dict, event: str, level: str = "INFO"):
        regime = context.get("regime", "")
        reason = context.get("reason", "")
        message = f"Gold/BTC macro context {regime}: {reason}"
        self._log_event(
            level,
            message,
            event=event,
            symbol=context.get("gold_symbol", ""),
            reason=(
                f"regime={regime};gold_rsi={self._safe_float(context.get('gold_rsi'), 0.0):.4f};"
                f"btc_rsi={self._safe_float(context.get('btc_rsi'), 0.0):.4f};"
                f"rsi_spread={self._safe_float(context.get('rsi_spread'), 0.0):.4f};"
                f"macro_direction_score={self._safe_float(context.get('macro_direction_score'), 0.0):.4f};"
                f"reason={reason}"
            ),
        )
        append_macro = getattr(self, "_append_macro_csv", None)
        if append_macro:
            try:
                append_macro(context)
            except Exception as exc:
                self._log_event(
                    "WARNING",
                    f"Could not append macro context CSV: {exc}",
                    event="macro_context_csv_failed",
                    symbol=context.get("gold_symbol", ""),
                    reason="macro_csv_failed",
                )

    def _gold_btc_ratio_return(
        self,
        gold_closes: List[float],
        btc_closes: List[float],
        direct_closes: Optional[List[float]] = None,
    ) -> float:
        return gold_btc_ratio_return(
            gold_closes, btc_closes, config.MACRO.gold_rsi_period, direct_closes
        )

    def _macro_window_return(self, closes: List[float], window: int) -> float:
        window = max(1, int(window))
        values = []
        for price in closes or []:
            try:
                value = float(price)
            except (TypeError, ValueError):
                continue
            if value > 0:
                values.append(value)
        if len(values) <= window:
            return 0.0
        return compute_log_return(values[-1], values[-window - 1])

    def _gold_directional_bias_score(
        self,
        gold_rsi: float,
        btc_rsi: float,
        ratio_return: float,
        gold_return: float,
        btc_return: float,
        regime_bias: float = 0.0,
    ) -> float:
        macro = config.MACRO
        if not getattr(macro, "enable_gold_directional_bias", False):
            return 0.0

        spread_ref = max(
            abs(self._safe_float(getattr(macro, "rsi_spread_threshold", 15.0), 15.0)),
            1.0,
        )
        ratio_ref = max(
            abs(
                self._safe_float(
                    getattr(macro, "gold_btc_ratio_return_reference", 0.03), 0.03
                )
            ),
            1e-9,
        )
        rsi_spread = btc_rsi - gold_rsi
        spread_score = clamp(rsi_spread / spread_ref, -1.0, 1.0)
        ratio_score = clamp(-ratio_return / ratio_ref, -1.0, 1.0)

        trend_score = 0.0
        if gold_return > 0 and btc_return < 0:
            trend_score = -1.0
        elif gold_return > 0 and btc_return >= 0:
            trend_score = 0.5 if btc_return >= gold_return else -0.5
        elif gold_return < 0 and btc_return > 0:
            trend_score = 1.0
        elif gold_return < 0 and btc_return < 0:
            trend_score = 0.25 if btc_return >= gold_return else -0.25

        continuous_score = clamp(
            0.45 * spread_score + 0.35 * ratio_score + 0.20 * trend_score, -1.0, 1.0
        )
        regime_score = clamp(self._safe_float(regime_bias, 0.0), -1.0, 1.0)
        if abs(regime_score) > abs(continuous_score):
            return regime_score
        return continuous_score

    def _apply_gold_directional_bias(
        self,
        context: dict,
        gold_rsi: float,
        btc_rsi: float,
        ratio_return: float,
        gold_return: float,
        btc_return: float,
        regime_bias: float = 0.0,
    ) -> dict:
        macro = config.MACRO
        context.update(
            {
                "gold_return": gold_return,
                "btc_return": btc_return,
                "macro_direction_score": 0.0,
                "directional_long_multiplier": 1.0,
                "directional_short_multiplier": 1.0,
            }
        )
        if not getattr(macro, "enable_gold_directional_bias", False):
            return context

        score = self._gold_directional_bias_score(
            gold_rsi,
            btc_rsi,
            ratio_return,
            gold_return,
            btc_return,
            regime_bias=regime_bias,
        )
        strength = max(
            0.0,
            self._safe_float(
                getattr(macro, "gold_directional_bias_strength", 0.30), 0.30
            ),
        )
        min_multiplier = self._clamp(
            self._safe_float(
                getattr(macro, "gold_directional_bias_min_multiplier", 0.50), 0.50
            ),
            0.0,
            1.0,
        )
        max_multiplier = max(
            1.0,
            self._safe_float(
                getattr(macro, "gold_directional_bias_max_multiplier", 1.25), 1.25
            ),
        )
        directional_long = clamp(1.0 + strength * score, min_multiplier, max_multiplier)
        directional_short = clamp(
            1.0 - strength * score, min_multiplier, max_multiplier
        )

        long_budget = max(
            0.0, self._safe_float(context.get("long_budget_multiplier"), 1.0)
        )
        short_budget = max(
            0.0, self._safe_float(context.get("short_budget_multiplier"), 1.0)
        )
        if score > 1e-12:
            long_budget = max(long_budget, directional_long)
            short_budget = min(short_budget, directional_short)
        elif score < -1e-12:
            long_budget = min(long_budget, directional_long)
            short_budget = max(short_budget, directional_short)

        context.update(
            {
                "macro_direction_score": score,
                "directional_long_multiplier": directional_long,
                "directional_short_multiplier": directional_short,
                "long_budget_multiplier": long_budget,
                "short_budget_multiplier": short_budget,
            }
        )
        if context.get("regime") == "neutral" and abs(score) > 1e-12:
            context["regime"] = "gold_directional_bias"
            context["reason"] = "btc_gold_relative_bias"
        return context

    def _classify_gold_btc_rsi_context(
        self,
        gold_symbol: str,
        btc_symbol: str,
        gold_rsi: float,
        btc_rsi: float,
        ratio_return: float,
        gold_return: float = 0.0,
        btc_return: float = 0.0,
    ) -> dict:
        macro = config.MACRO
        rsi_spread = btc_rsi - gold_rsi
        context = self._neutral_macro_context("neutral", regime="neutral", ok=True)
        context.update(
            {
                "gold_symbol": gold_symbol,
                "btc_symbol": btc_symbol,
                "gold_rsi": gold_rsi,
                "btc_rsi": btc_rsi,
                "rsi_spread": rsi_spread,
                "gold_btc_ratio_return": ratio_return,
                "gold_return": gold_return,
                "btc_return": btc_return,
            }
        )

        if btc_rsi <= macro.btc_weak_rsi and gold_rsi <= macro.gold_weak_rsi:
            context.update(
                {
                    "regime": "deleveraging",
                    "ladder_multiplier": 1.4,
                    "long_budget_multiplier": 0.0
                    if macro.panic_disable_new_entries
                    else 1.0,
                    "short_budget_multiplier": 0.25
                    if macro.panic_disable_new_entries
                    else 1.0,
                    "disable_averaging": True,
                    "time_exit_multiplier": 0.65,
                    "reason": "btc_weak_gold_weak",
                }
            )
            return self._apply_gold_directional_bias(
                context,
                gold_rsi,
                btc_rsi,
                ratio_return,
                gold_return,
                btc_return,
                regime_bias=0.0,
            )

        btc_defensive_rsi = macro.btc_weak_rsi + 5.0
        if (gold_rsi >= macro.gold_strong_rsi and btc_rsi <= btc_defensive_rsi) or (
            gold_rsi - btc_rsi >= macro.rsi_spread_threshold
        ):
            context.update(
                {
                    "regime": "crypto_underperforms_gold",
                    "long_budget_multiplier": min(
                        max(0.0, macro.risk_off_long_budget_multiplier), 1.0
                    ),
                    "short_budget_multiplier": max(
                        0.0, macro.risk_off_short_budget_multiplier
                    ),
                    "ladder_multiplier": max(0.0, macro.risk_off_ladder_multiplier),
                    "disable_averaging": bool(macro.risk_off_disable_averaging),
                    "time_exit_multiplier": max(
                        0.0, macro.risk_off_time_exit_multiplier
                    ),
                    "reason": "gold_strong_btc_weak",
                }
            )
            return self._apply_gold_directional_bias(
                context,
                gold_rsi,
                btc_rsi,
                ratio_return,
                gold_return,
                btc_return,
                regime_bias=-1.0,
            )

        if (
            btc_rsi >= macro.btc_strong_rsi
            and gold_rsi < macro.gold_strong_rsi - 5.0
            and btc_rsi - gold_rsi >= 10.0
        ):
            context.update(
                {
                    "regime": "crypto_risk_on",
                    "long_budget_multiplier": 1.0,
                    "short_budget_multiplier": 0.75,
                    "reason": "btc_strong_gold_lagging",
                }
            )
            return self._apply_gold_directional_bias(
                context,
                gold_rsi,
                btc_rsi,
                ratio_return,
                gold_return,
                btc_return,
                regime_bias=1.0,
            )

        if btc_rsi >= macro.btc_strong_rsi and gold_rsi >= macro.gold_strong_rsi:
            context.update(
                {
                    "regime": "broad_liquidity_risk_on",
                    "long_budget_multiplier": 1.0,
                    "short_budget_multiplier": 0.85,
                    "reason": "btc_strong_gold_strong",
                }
            )
            return self._apply_gold_directional_bias(
                context,
                gold_rsi,
                btc_rsi,
                ratio_return,
                gold_return,
                btc_return,
                regime_bias=0.5,
            )

        return self._apply_gold_directional_bias(
            context,
            gold_rsi,
            btc_rsi,
            ratio_return,
            gold_return,
            btc_return,
            regime_bias=0.0,
        )

    def _macro_fetch_exchange(self, is_spot: bool):
        return self._spot_exchange() if is_spot else self.exchange

    def _gold_btc_rsi_context(self) -> dict:
        if not config.MACRO.enable_gold_btc_rsi_overlay:
            context = self._neutral_macro_context(
                "disabled", regime="macro_disabled", ok=False
            )
            self._macro_cache_root()["gold_btc_rsi"] = context
            return context

        cached = self._cached_gold_btc_rsi_context()
        ttl = max(0, int(config.MACRO.gold_cache_ttl_sec))
        if (
            ttl > 0
            and cached.get("ts")
            and cached.get("reason") != "not_loaded"
            and time.time() - self._safe_float(cached.get("ts"), 0.0) < ttl
        ):
            return self._macro_context_for_trading(cached)

        gold_symbol = getattr(self, "macro_gold_symbol", None)
        if not gold_symbol and not getattr(self, "_macro_gold_lookup_done", False):
            finder = getattr(self, "_find_macro_gold_symbol", None)
            gold_symbol = finder() if finder else None
            self.macro_gold_symbol = gold_symbol
            self._macro_gold_lookup_done = True
        if not gold_symbol:
            context = self._neutral_macro_context(
                "gold_symbol_not_found", regime="macro_unavailable", ok=False
            )
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(
                context, event="macro_context_unavailable", level="WARNING"
            )
            return context

        btc_symbol = getattr(self, "benchmark_symbol", None)
        if not btc_symbol:
            context = self._neutral_macro_context(
                "btc_symbol_not_found", regime="macro_unavailable", ok=False
            )
            context["gold_symbol"] = gold_symbol
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(
                context, event="macro_context_unavailable", level="WARNING"
            )
            return context

        timeframe = str(config.MACRO.gold_timeframe or "4h")
        limit = max(
            int(config.MACRO.gold_min_candles), int(config.MACRO.gold_rsi_period) + 2
        )
        fetch_limit = limit + 1
        try:
            gold_candles = self._closed_candles(
                gold_symbol,
                fetch_limit,
                timeframe=timeframe,
                exchange=self._macro_fetch_exchange(
                    bool(getattr(self, "macro_gold_is_spot", False))
                ),
            )
            btc_candles = self._closed_candles(
                btc_symbol, fetch_limit, timeframe=timeframe
            )
        except Exception as exc:
            context = self._neutral_macro_context(
                "macro_candles_unavailable", regime="macro_unavailable", ok=False
            )
            context.update(
                {
                    "gold_symbol": gold_symbol,
                    "btc_symbol": btc_symbol,
                    "timeframe": timeframe,
                }
            )
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(
                context, event="macro_context_unavailable", level="WARNING"
            )
            self._log_event(
                "DEBUG",
                f"Gold/BTC macro candles unavailable: {exc}",
                event="macro_context_unavailable",
                symbol=gold_symbol,
                reason="macro_candles_fetch_failed",
            )
            return context

        if len(gold_candles) < limit or len(btc_candles) < limit:
            context = self._neutral_macro_context(
                "macro_history_short", regime="macro_unavailable", ok=False
            )
            context.update(
                {
                    "gold_symbol": gold_symbol,
                    "btc_symbol": btc_symbol,
                    "timeframe": timeframe,
                }
            )
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(
                context, event="macro_context_unavailable", level="WARNING"
            )
            return context

        gold_closes = [self._safe_float(row[4], 0.0) for row in gold_candles]
        btc_closes = [self._safe_float(row[4], 0.0) for row in btc_candles]
        gold_rsi = self._calculate_rsi(gold_closes, config.MACRO.gold_rsi_period)
        btc_rsi = self._calculate_rsi(btc_closes, config.MACRO.gold_rsi_period)
        if config.MACRO.gold_rsi_period <= 0:
            context = self._neutral_macro_context(
                "macro_rsi_unavailable", regime="macro_unavailable", ok=False
            )
            context.update(
                {
                    "gold_symbol": gold_symbol,
                    "btc_symbol": btc_symbol,
                    "timeframe": timeframe,
                }
            )
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(
                context, event="macro_context_unavailable", level="WARNING"
            )
            return context

        direct_closes = None
        direct_symbol = getattr(self, "macro_direct_gold_btc_symbol", None)
        if (
            config.MACRO.use_direct_gold_btc_pair
            and not direct_symbol
            and not getattr(self, "_macro_direct_gold_btc_lookup_done", False)
        ):
            finder = getattr(self, "_find_direct_gold_btc_symbol", None)
            direct_symbol = finder() if finder else None
            self.macro_direct_gold_btc_symbol = direct_symbol
            self._macro_direct_gold_btc_lookup_done = True
        if direct_symbol:
            try:
                direct_candles = self._closed_candles(
                    direct_symbol,
                    fetch_limit,
                    timeframe=timeframe,
                    exchange=self._macro_fetch_exchange(
                        bool(getattr(self, "macro_direct_gold_btc_is_spot", False))
                    ),
                )
                direct_closes = [
                    self._safe_float(row[4], 0.0) for row in direct_candles
                ]
            except Exception:
                direct_closes = None

        ratio_window = max(1, int(config.MACRO.gold_rsi_period))
        gold_return = self._macro_window_return(gold_closes, ratio_window)
        btc_return = self._macro_window_return(btc_closes, ratio_window)
        ratio_return = self._gold_btc_ratio_return(
            gold_closes, btc_closes, direct_closes=direct_closes
        )
        context = self._classify_gold_btc_rsi_context(
            gold_symbol,
            btc_symbol,
            gold_rsi,
            btc_rsi,
            ratio_return,
            gold_return=gold_return,
            btc_return=btc_return,
        )
        context["timeframe"] = timeframe
        self._macro_cache_root()["gold_btc_rsi"] = context
        self._record_macro_context(context, event="macro_context_updated", level="INFO")
        if (
            abs(self._safe_float(context.get("long_budget_multiplier"), 1.0) - 1.0)
            > 1e-12
            or abs(self._safe_float(context.get("short_budget_multiplier"), 1.0) - 1.0)
            > 1e-12
            or abs(self._safe_float(context.get("ladder_multiplier"), 1.0) - 1.0)
            > 1e-12
            or context.get("disable_new_entries")
            or context.get("disable_averaging")
        ):
            self._log_event(
                "INFO",
                f"Macro budget scaled: {context.get('regime', '')}",
                event="macro_budget_scaled",
                symbol=gold_symbol,
                reason=(
                    f"regime={context.get('regime', '')};"
                    f"long_mult={self._safe_float(context.get('long_budget_multiplier'), 1.0):.3f};"
                    f"short_mult={self._safe_float(context.get('short_budget_multiplier'), 1.0):.3f};"
                    f"ladder_mult={self._safe_float(context.get('ladder_multiplier'), 1.0):.3f};"
                    f"direction_score={self._safe_float(context.get('macro_direction_score'), 0.0):.3f};"
                    f"reason={context.get('reason', '')}"
                ),
            )
        return context

    def _signal_timeframe_seconds(self, timeframe: str) -> int:
        timeframe = str(timeframe or "1m").strip().lower()
        try:
            return max(1, int(self.exchange.parse_timeframe(timeframe)))
        except Exception:
            pass

        unit = timeframe[-1:] or "m"
        try:
            value = int(timeframe[:-1] or "1")
        except ValueError:
            return 60
        if unit == "s":
            return max(1, value)
        if unit == "m":
            return max(1, value * 60)
        if unit == "h":
            return max(1, value * 60 * 60)
        if unit == "d":
            return max(1, value * 24 * 60 * 60)
        return 60

    def _ema_timeframes(self) -> dict:
        strategy = config.STRATEGY
        return {
            "macro": str(getattr(strategy, "ema_macro_timeframe", "1d") or "1d"),
            "pullback": str(getattr(strategy, "ema_pullback_timeframe", "4h") or "4h"),
            "trigger": str(
                getattr(strategy, "ema_trigger_timeframe", config.SIGNALS.timeframe)
                or config.SIGNALS.timeframe
            ),
        }

    def _period_minutes_to_candles(self, minutes: int, timeframe: str) -> int:
        timeframe_minutes = max(self._signal_timeframe_seconds(timeframe) / 60.0, 1e-9)
        return max(1, int(math.ceil(max(1, int(minutes)) / timeframe_minutes)))

    def _trigger_window_candles(
        self, minutes: int, timeframe: Optional[str] = None
    ) -> int:
        return self._period_minutes_to_candles(
            minutes, timeframe or self._ema_timeframes()["trigger"]
        )

    def _ema_periods(self, converted: bool = False) -> dict:
        strategy = config.STRATEGY
        if converted:
            timeframes = self._ema_timeframes()
            return {
                "ema_macro_fast": self._period_minutes_to_candles(
                    strategy.ema_macro_fast_minutes, timeframes["macro"]
                ),
                "ema_macro_slow": self._period_minutes_to_candles(
                    strategy.ema_macro_slow_minutes, timeframes["macro"]
                ),
                "ema_pullback_fast": self._period_minutes_to_candles(
                    strategy.ema_pullback_fast_minutes, timeframes["pullback"]
                ),
                "ema_pullback_slow": self._period_minutes_to_candles(
                    strategy.ema_pullback_slow_minutes, timeframes["pullback"]
                ),
                "ema_trigger_fast": self._period_minutes_to_candles(
                    strategy.ema_trigger_fast_minutes, timeframes["trigger"]
                ),
                "ema_trigger_slow": self._period_minutes_to_candles(
                    strategy.ema_trigger_slow_minutes, timeframes["trigger"]
                ),
            }
        return {
            "ema_macro_fast": max(1, int(strategy.ema_macro_fast_minutes)),
            "ema_macro_slow": max(1, int(strategy.ema_macro_slow_minutes)),
            "ema_pullback_fast": max(1, int(strategy.ema_pullback_fast_minutes)),
            "ema_pullback_slow": max(1, int(strategy.ema_pullback_slow_minutes)),
            "ema_trigger_fast": max(1, int(strategy.ema_trigger_fast_minutes)),
            "ema_trigger_slow": max(1, int(strategy.ema_trigger_slow_minutes)),
        }

    def _ema_pullback_recovery_windows(self, converted: bool = False) -> tuple:
        strategy = config.STRATEGY
        if converted:
            timeframe = self._ema_timeframes()["pullback"]
            return (
                self._period_minutes_to_candles(
                    strategy.ema_pullback_recovery_lookback_minutes, timeframe
                ),
                self._period_minutes_to_candles(
                    strategy.ema_pullback_recovery_max_cross_age_minutes, timeframe
                ),
            )
        return (
            max(1, int(strategy.ema_pullback_recovery_lookback_minutes)),
            max(1, int(strategy.ema_pullback_recovery_max_cross_age_minutes)),
        )

    def _ema_required_history(self, group: str = "all", converted: bool = False) -> int:
        periods = self._ema_periods(converted=converted)
        rs_slow_window = config.SIGNALS.rs_slow_window
        btc_fast_window = config.SIGNALS.rs_fast_window
        if converted:
            trigger_timeframe = self._ema_timeframes()["trigger"]
            rs_slow_window = self._trigger_window_candles(
                config.SIGNALS.rs_slow_window, trigger_timeframe
            )
            btc_fast_window = self._trigger_window_candles(
                config.SIGNALS.rs_fast_window, trigger_timeframe
            )
        if group == "macro":
            return max(periods["ema_macro_fast"], periods["ema_macro_slow"])
        if group == "pullback":
            pullback_lookback, _ = self._ema_pullback_recovery_windows(
                converted=converted
            )
            return (
                max(periods["ema_pullback_fast"], periods["ema_pullback_slow"])
                + pullback_lookback
            )
        if group == "trigger":
            return max(
                periods["ema_trigger_fast"],
                periods["ema_trigger_slow"],
                rs_slow_window + 1,
                btc_fast_window + 1,
            )
        return max(
            max(periods.values()),
            rs_slow_window + 1,
            btc_fast_window + 1,
        )

    def _ema_series_from_closes(self, closes: List[float], period: int) -> List[float]:
        return calculate_ema_series(closes, period)

    def _ema_values_from_closes(
        self,
        closes: List[float],
        latest_ts: int,
        cache_key: str = "",
        periods: Optional[dict] = None,
        cache_namespace: str = "ema",
        timeframe_sec: Optional[int] = None,
    ) -> Optional[dict]:
        periods = periods or self._ema_periods()
        required = max(periods.values())
        if len(closes) < required:
            return None

        values = {}
        period_signature = tuple(sorted(periods.items()))
        cache_root = None
        cache = None
        if cache_key and hasattr(self, "signal_cache"):
            cache_root = self.signal_cache.setdefault("ema_cache", {})
            full_cache_key = (
                f"{cache_namespace}:{cache_key}" if cache_namespace else cache_key
            )
            cache = cache_root.get(full_cache_key)
        else:
            full_cache_key = ""

        timeframe_ms = (
            max(1, int(timeframe_sec or getattr(self, "timeframe_sec", 60))) * 1000
        )
        if (
            cache
            and cache.get("period_signature") == period_signature
            and int(cache.get("latest_ts", 0)) == int(latest_ts)
            and isinstance(cache.get("values"), dict)
        ):
            return dict(cache["values"])

        can_update = (
            cache
            and cache.get("period_signature") == period_signature
            and int(cache.get("latest_ts", 0)) + timeframe_ms == int(latest_ts)
            and isinstance(cache.get("values"), dict)
        )
        latest_close = closes[-1]

        if can_update:
            previous_values = cache["values"]
            for name, period in periods.items():
                previous = self._safe_float(previous_values.get(name), 0.0)
                if previous <= 0:
                    can_update = False
                    break
                alpha = 2.0 / (period + 1.0)
                values[name] = latest_close * alpha + previous * (1.0 - alpha)

        if not can_update:
            values = {
                name: self._calculate_ema(closes, period)
                for name, period in periods.items()
            }

        if cache_root is not None:
            cache_root[full_cache_key] = {
                "latest_ts": int(latest_ts),
                "period_signature": period_signature,
                "values": dict(values),
            }

        return values

    def _ema_pullback_recovery_context(
        self,
        closes: List[float],
        fast_period: int,
        slow_period: int,
        converted: bool = False,
    ) -> dict:
        lookback, max_cross_age = self._ema_pullback_recovery_windows(
            converted=converted
        )
        gap_threshold = max(
            0.0, self._safe_float(config.STRATEGY.ema_pullback_recovery_gap, 0.0)
        )
        return ema_pullback_recovery_context(
            closes,
            fast_period,
            slow_period,
            lookback,
            max_cross_age,
            gap_threshold,
            config.POSITION_SIDE,
        )

    def _empty_ema_signal(
        self, latest_ts: int, reason: str, price: float = 0.0
    ) -> dict:
        return {
            "strategy_name": "ema_pullback",
            "price": price,
            "rs30": 0.0,
            "rs60": 0.0,
            "rs_edge": 0.0,
            "macro_gap": 0.0,
            "trigger_gap": 0.0,
            "pullback_depth": 0.0,
            "ema_macro_fast": 0.0,
            "ema_macro_slow": 0.0,
            "ema_pullback_fast": 0.0,
            "ema_pullback_slow": 0.0,
            "ema_trigger_fast": 0.0,
            "ema_trigger_slow": 0.0,
            "ema_macro_side": "neutral",
            "ema_trigger_side": "neutral",
            "ema_side": "neutral",
            "ema_side_valid": False,
            "ema_entry_valid": False,
            "entry_setup_valid": False,
            "entry_side_valid": False,
            "entry_signal_source": "none",
            "ema25d": 0.0,
            "ema50d": 0.0,
            "ema1d": 0.0,
            "ema2d": 0.0,
            "ema50": 0.0,
            "ema100": 0.0,
            "ema_macro_timeframe": self._ema_timeframes()["macro"],
            "ema_pullback_timeframe": self._ema_timeframes()["pullback"],
            "ema_trigger_timeframe": self._ema_timeframes()["trigger"],
            "macro_valid": False,
            "pullback_valid": False,
            "pullback_recovered": False,
            "pullback_had_pullback": False,
            "pullback_cross_age_candles": -1,
            "pullback_recovery_lookback_candles": self._ema_pullback_recovery_windows(
                converted=True
            )[0],
            "pullback_recovery_max_cross_age_candles": self._ema_pullback_recovery_windows(
                converted=True
            )[1],
            "pullback_recovery_gap": 0.0,
            "pullback_recovery_min_gap": max(
                0.0, self._safe_float(config.STRATEGY.ema_pullback_recovery_gap, 0.0)
            ),
            "entry_pullback_required": bool(
                getattr(config.STRATEGY, "ema_entry_require_pullback_recovery", False)
            ),
            "entry_pullback_gate_valid": False,
            "trigger_valid": False,
            "rs_confirm_valid": False,
            "btc_entry_valid": False,
            "market_structure_valid": False,
            "volume_valid": False,
            "volume_average_valid": False,
            "volume_ratio": 0.0,
            "volume_recent": 0.0,
            "volume_baseline": 0.0,
            "volume_directional_fraction": 0.0,
            "volume_spike_valid": False,
            "volume_spike_ratio": 0.0,
            "volume_spike_volume": 0.0,
            "volume_spike_baseline": 0.0,
            "volume_spike_direction": "unknown",
            "volume_spike_reason": "empty_signal",
            "volume_profile_valid": False,
            "volume_profile_poc": 0.0,
            "volume_profile_value_area_low": 0.0,
            "volume_profile_value_area_high": 0.0,
            "volume_profile_break": False,
            "volume_profile_reason": "empty_signal",
            "volume_reason": "empty_signal",
            "chop_valid": False,
            "chop": 0.0,
            "chop_max": self._safe_float(
                getattr(config.STRATEGY, "ema_chop_max", 0.0), 0.0
            ),
            "chop_reason": "empty_signal",
            "btc_entry_return": 0.0,
            "btc_return_30m": 0.0,
            "score": 0.0,
            "data_valid": False,
            "direction_valid": False,
            "valid": False,
            "entry_valid": False,
            "add_valid": False,
            "budget_multiplier": 1.0,
            "volatility_budget_multiplier": 1.0,
            "ladder_multiplier": 1.0,
            "volatility": 0.0,
            "volatility_multiplier": 1.0,
            "atr": 0.0,
            "atr_rate": 0.0,
            "daily_volatility": 0.0,
            "daily_volatility_multiplier": 1.0,
            "signal_budget_multiplier": 1.0,
            "btc_budget_multiplier": 1.0,
            "macro_budget_multiplier": 1.0,
            "macro_ladder_multiplier": 1.0,
            "macro_regime": "neutral",
            "macro_direction_score": 0.0,
            "macro_disable_new_entries": False,
            "macro_disable_averaging": False,
            "macro_time_exit_multiplier": 1.0,
            "btc_risk_reason": "ema_filter",
            "reason": reason,
            "ts": latest_ts,
        }

    def _build_signal_from_closes(
        self,
        ctx: SignalContext | List[float],
        benchmark_closes: Optional[List[float]] = None,
        btc_risk: Optional[dict] = None,
        latest_ts: Optional[int] = None,
        cache_key: str = "",
        macro_context: Optional[dict] = None,
        macro_closes: Optional[List[float]] = None,
        macro_latest_ts: Optional[int] = None,
        pullback_closes: Optional[List[float]] = None,
        pullback_latest_ts: Optional[int] = None,
    ) -> Optional[dict]:
        if not isinstance(ctx, SignalContext):
            if benchmark_closes is None or latest_ts is None:
                raise TypeError(
                    "_build_signal_from_closes requires benchmark_closes and latest_ts"
                )
            ctx = SignalContext(
                closes=list(ctx or []),
                benchmark_closes=list(benchmark_closes or []),
                btc_risk=dict(btc_risk or {}),
                latest_ts=int(latest_ts),
                candles=None,
                cache_key=cache_key,
                macro_context=macro_context,
                macro_closes=macro_closes,
                macro_latest_ts=macro_latest_ts,
                pullback_closes=pullback_closes,
                pullback_latest_ts=pullback_latest_ts,
            )
        closes = ctx.closes
        benchmark_closes = ctx.benchmark_closes
        btc_risk = ctx.btc_risk
        latest_ts = ctx.latest_ts
        candles = ctx.candles
        cache_key = ctx.cache_key
        macro_context = ctx.macro_context
        macro_closes = ctx.macro_closes
        macro_latest_ts = ctx.macro_latest_ts
        pullback_closes = ctx.pullback_closes
        pullback_latest_ts = ctx.pullback_latest_ts

        if not closes or not benchmark_closes:
            return None

        current_close = closes[-1]
        current_btc = benchmark_closes[-1]
        if current_close <= 0 or current_btc <= 0:
            return None

        strategy = config.STRATEGY
        if not getattr(strategy, "ema_strategy_enabled", True):
            return self._empty_ema_signal(
                latest_ts, "ema_strategy_disabled", price=current_close
            )

        use_timeframe_ema = macro_closes is not None or pullback_closes is not None
        periods = self._ema_periods(converted=use_timeframe_ema)
        macro_closes = macro_closes if macro_closes is not None else closes
        pullback_closes = pullback_closes if pullback_closes is not None else closes
        macro_latest_ts = int(
            macro_latest_ts if macro_latest_ts is not None else latest_ts
        )
        pullback_latest_ts = int(
            pullback_latest_ts if pullback_latest_ts is not None else latest_ts
        )
        timeframes = self._ema_timeframes()
        rs_fast_window = self._trigger_window_candles(
            config.SIGNALS.rs_fast_window, timeframes["trigger"]
        )
        rs_slow_window = self._trigger_window_candles(
            config.SIGNALS.rs_slow_window, timeframes["trigger"]
        )
        btc_return_window = rs_fast_window
        benchmark_required = max(rs_slow_window, btc_return_window) + 1

        trigger_required = self._ema_required_history(
            "trigger", converted=use_timeframe_ema
        )
        macro_required = self._ema_required_history(
            "macro", converted=use_timeframe_ema
        )
        pullback_required = self._ema_required_history(
            "pullback", converted=use_timeframe_ema
        )
        if (
            len(closes) < trigger_required
            or len(benchmark_closes) < benchmark_required
            or len(macro_closes) < macro_required
            or len(pullback_closes) < pullback_required
        ):
            return self._empty_ema_signal(
                latest_ts,
                (
                    f"ema_history_short;trigger_candles={len(closes)};trigger_required={trigger_required};"
                    f"macro_candles={len(macro_closes)};macro_required={macro_required};macro_tf={timeframes['macro']};"
                    f"pullback_candles={len(pullback_closes)};pullback_required={pullback_required};pullback_tf={timeframes['pullback']};"
                    f"benchmark_candles={len(benchmark_closes)};benchmark_required={benchmark_required}"
                ),
                price=current_close,
            )

        rs_context = relative_strength_context(
            closes, benchmark_closes, rs_fast_window, rs_slow_window
        )
        rs30 = rs_context["rs30"]
        rs60 = rs_context["rs60"]
        btc_return_30m = rs_context["btc_return_30m"]

        trigger_periods = {
            "ema_trigger_fast": periods["ema_trigger_fast"],
            "ema_trigger_slow": periods["ema_trigger_slow"],
        }
        pullback_periods = {
            "ema_pullback_fast": periods["ema_pullback_fast"],
            "ema_pullback_slow": periods["ema_pullback_slow"],
        }
        macro_periods = {
            "ema_macro_fast": periods["ema_macro_fast"],
            "ema_macro_slow": periods["ema_macro_slow"],
        }
        trigger_values = self._ema_values_from_closes(
            closes,
            latest_ts,
            cache_key=cache_key,
            periods=trigger_periods,
            cache_namespace="ema_trigger",
            timeframe_sec=self._signal_timeframe_seconds(timeframes["trigger"])
            if use_timeframe_ema
            else None,
        )
        pullback_values = self._ema_values_from_closes(
            pullback_closes,
            pullback_latest_ts,
            cache_key=cache_key,
            periods=pullback_periods,
            cache_namespace="ema_pullback",
            timeframe_sec=self._signal_timeframe_seconds(timeframes["pullback"])
            if use_timeframe_ema
            else None,
        )
        macro_values = self._ema_values_from_closes(
            macro_closes,
            macro_latest_ts,
            cache_key=cache_key,
            periods=macro_periods,
            cache_namespace="ema_macro",
            timeframe_sec=self._signal_timeframe_seconds(timeframes["macro"])
            if use_timeframe_ema
            else None,
        )
        if not trigger_values or not pullback_values or not macro_values:
            return self._empty_ema_signal(
                latest_ts,
                (
                    f"ema_history_short;trigger_candles={len(closes)};trigger_required={trigger_required};"
                    f"macro_candles={len(macro_closes)};macro_required={macro_required};"
                    f"pullback_candles={len(pullback_closes)};pullback_required={pullback_required}"
                ),
                price=current_close,
            )

        ema_macro_fast = macro_values["ema_macro_fast"]
        ema_macro_slow = macro_values["ema_macro_slow"]
        ema_pullback_fast = pullback_values["ema_pullback_fast"]
        ema_pullback_slow = pullback_values["ema_pullback_slow"]
        ema_trigger_fast = trigger_values["ema_trigger_fast"]
        ema_trigger_slow = trigger_values["ema_trigger_slow"]
        pullback_context = self._ema_pullback_recovery_context(
            pullback_closes,
            periods["ema_pullback_fast"],
            periods["ema_pullback_slow"],
            converted=use_timeframe_ema,
        )

        direction = ema_signal_direction_metrics(
            config.POSITION_SIDE,
            current_close,
            ema_macro_fast,
            ema_macro_slow,
            ema_pullback_fast,
            ema_pullback_slow,
            ema_trigger_fast,
            ema_trigger_slow,
            bool(pullback_context["pullback_valid"]),
            rs60,
            btc_return_30m,
            strategy.ema_use_rs_confirmation,
            strategy.ema_long_min_rs60,
            strategy.ema_short_max_rs60,
            strategy.ema_use_btc_risk_filter,
            strategy.ema_btc_long_min_return_30m,
            strategy.ema_btc_short_max_return_30m,
        )
        macro_valid = direction["macro_valid"]
        pullback_valid = direction["pullback_valid"]
        trigger_valid = direction["trigger_valid"]
        rs_confirm_valid = direction["rs_confirm_valid"]
        btc_entry_valid = direction["btc_entry_valid"]
        ema_macro_side = direction.get("ema_macro_side", "neutral")
        ema_trigger_side = direction.get("ema_trigger_side", "neutral")
        ema_side = direction.get("ema_side", "neutral")
        ema_side_valid = bool(direction.get("ema_side_valid", False))
        entry_setup_valid = bool(
            direction.get("entry_setup_valid", bool(trigger_valid or pullback_valid))
        )
        entry_side_valid = bool(
            direction.get("entry_side_valid", bool(macro_valid and entry_setup_valid))
        )
        entry_signal_source = str(
            direction.get("entry_signal_source", "none") or "none"
        )
        macro_gap = direction["macro_gap"]
        trigger_gap = direction["trigger_gap"]
        pullback_depth = direction["pullback_depth"]
        rs_edge = direction["rs_edge"]
        score = direction["score"]

        macro_context = self._macro_context_for_trading(macro_context)
        market_structure = self._ema_market_structure_context(candles)
        data_valid = True
        direction_valid = bool(entry_side_valid and score > 0)
        market_structure_valid = bool(market_structure["market_structure_valid"])
        entry_pullback_required = bool(
            getattr(strategy, "ema_entry_require_pullback_recovery", False)
        )
        entry_pullback_gate_valid = bool(pullback_valid or not entry_pullback_required)
        ema_entry_valid = bool(
            macro_valid and entry_setup_valid and entry_pullback_gate_valid
        )
        raw_entry_valid = bool(ema_entry_valid and rs_confirm_valid and btc_entry_valid)
        raw_add_valid = bool(direction["add_valid"])
        add_valid = bool(raw_add_valid and market_structure_valid)
        volatility = self._realized_volatility(closes, strategy.volatility_window)
        volatility_multiplier = self._volatility_multiplier(volatility)
        atr, atr_rate = self._average_true_range_rate(
            candles, current_close, strategy.ema_averaging_atr_period
        )
        daily_volatility = self._daily_volatility_context(closes)
        signal_budget_multiplier = self._signal_budget_multiplier(score)
        btc_budget_multiplier = max(
            0.0, self._safe_float(btc_risk.get("budget_multiplier"), 1.0)
        )
        btc_ladder_multiplier = max(
            0.0, self._safe_float(btc_risk.get("ladder_multiplier"), 1.0)
        )
        if config.POSITION_SIDE == "short":
            macro_budget_multiplier = max(
                0.0, self._safe_float(macro_context.get("short_budget_multiplier"), 1.0)
            )
        else:
            macro_budget_multiplier = max(
                0.0, self._safe_float(macro_context.get("long_budget_multiplier"), 1.0)
            )
        macro_ladder_multiplier = max(
            0.0, self._safe_float(macro_context.get("ladder_multiplier"), 1.0)
        )
        entry_quality_signal = {
            "valid": data_valid,
            "data_valid": data_valid,
            "direction_valid": direction_valid,
            "ema_entry_valid": ema_entry_valid,
            "entry_setup_valid": entry_setup_valid,
            "entry_side_valid": entry_side_valid,
            "entry_signal_source": entry_signal_source,
            "entry_valid": raw_entry_valid,
            "macro_valid": macro_valid,
            "pullback_valid": pullback_valid,
            "trigger_valid": trigger_valid,
            "rs_confirm_valid": rs_confirm_valid,
            "btc_entry_valid": btc_entry_valid,
            "market_structure_valid": market_structure_valid,
            "volume_valid": bool(market_structure["volume_valid"]),
            "chop_valid": bool(market_structure["chop_valid"]),
            "score": score,
            "rs30": rs30,
            "rs60": rs60,
            "btc_return_30m": btc_return_30m,
            "volume_reason": market_structure["volume_reason"],
            "chop_reason": market_structure["chop_reason"],
        }
        entry_quality = self._entry_signal_quality_context(
            entry_quality_signal, external_bonus=0.0
        )
        entry_valid = bool(ema_entry_valid and entry_quality["passed"])
        entry_quality_budget_multiplier = self._safe_float(
            entry_quality.get("quality_budget_multiplier"), 1.0
        )
        budget_multiplier = (
            signal_budget_multiplier
            * btc_budget_multiplier
            * macro_budget_multiplier
            * entry_quality_budget_multiplier
        )
        ladder_multiplier = (
            volatility_multiplier * btc_ladder_multiplier * macro_ladder_multiplier
        )
        reason = (
            f"strategy=ema_pullback;macro_tf={timeframes['macro']};pullback_tf={timeframes['pullback']};trigger_tf={timeframes['trigger']};"
            f"ema_side={ema_side};ema_macro_side={ema_macro_side};ema_trigger_side={ema_trigger_side};"
            f"ema_side_valid={int(ema_side_valid)};"
            f"entry_side_valid={int(entry_side_valid)};entry_signal_source={entry_signal_source};"
            f"ema25d={ema_macro_fast:.12f};ema50d={ema_macro_slow:.12f};"
            f"ema1d={ema_pullback_fast:.12f};ema2d={ema_pullback_slow:.12f};"
            f"ema50={ema_trigger_fast:.12f};ema100={ema_trigger_slow:.12f};"
            f"rs30={rs30:.6f};rs60={rs60:.6f};btc_return_30m={btc_return_30m:.6f};"
            f"pullback_recovered={int(pullback_context['pullback_recovered'])};"
            f"pullback_had_pullback={int(pullback_context['pullback_had_pullback'])};"
            f"pullback_cross_age={int(pullback_context['pullback_cross_age_candles'])};"
            f"pullback_gap={pullback_context['pullback_recovery_gap']:.6f};"
            f"pullback_min_gap={pullback_context['pullback_recovery_min_gap']:.6f};"
            f"entry_pullback_required={int(entry_pullback_required)};"
            f"entry_pullback_gate_valid={int(entry_pullback_gate_valid)};"
            f"entry_setup_valid={int(entry_setup_valid)};"
            f"ema_entry_valid={int(ema_entry_valid)};"
            f"macro_valid={int(macro_valid)};pullback_valid={int(pullback_valid)};"
            f"trigger_valid={int(trigger_valid)};rs_confirm_valid={int(rs_confirm_valid)};"
            f"btc_entry_valid={int(btc_entry_valid)};"
            f"market_structure_valid={int(market_structure_valid)};"
            f"volume_valid={int(bool(market_structure['volume_valid']))};"
            f"volume_ratio={market_structure['volume_ratio']:.6f};"
            f"volume_average_valid={int(bool(market_structure.get('volume_average_valid', False)))};"
            f"volume_spike_valid={int(bool(market_structure.get('volume_spike_valid', True)))};"
            f"volume_spike_ratio={market_structure.get('volume_spike_ratio', 0.0):.6f};"
            f"volume_spike_direction={market_structure.get('volume_spike_direction', '')};"
            f"volume_spike_reason={market_structure.get('volume_spike_reason', '')};"
            f"volume_profile_valid={int(bool(market_structure.get('volume_profile_valid', True)))};"
            f"volume_profile_break={int(bool(market_structure.get('volume_profile_break', False)))};"
            f"volume_profile_poc={market_structure.get('volume_profile_poc', 0.0):.12f};"
            f"volume_profile_va_low={market_structure.get('volume_profile_value_area_low', 0.0):.12f};"
            f"volume_profile_va_high={market_structure.get('volume_profile_value_area_high', 0.0):.12f};"
            f"volume_profile_reason={market_structure.get('volume_profile_reason', '')};"
            f"volume_reason={market_structure['volume_reason']};"
            f"chop_valid={int(bool(market_structure['chop_valid']))};"
            f"chop={market_structure['chop']:.6f};"
            f"chop_reason={market_structure['chop_reason']};"
            f"raw_entry_valid={int(raw_entry_valid)};"
            f"entry_valid={int(entry_valid)};"
            f"add_valid={int(add_valid)};score={score:.6f};"
            f"entry_weighted_score={entry_quality['weighted_score']:.6f};"
            f"entry_weighted_min={entry_quality['min_score']:.6f};"
            f"entry_penalty_total={entry_quality['penalty_total']:.6f};"
            f"entry_quality_budget_multiplier={entry_quality_budget_multiplier:.3f};"
            f"atr_rate={atr_rate:.6f};"
            f"signal_budget_multiplier={signal_budget_multiplier:.3f};"
            f"btc_budget_multiplier={btc_budget_multiplier:.3f};"
            f"macro_budget_multiplier={macro_budget_multiplier:.3f};"
            f"macro_direction_score={self._safe_float(macro_context.get('macro_direction_score'), 0.0):.3f};"
            f"macro_regime={macro_context.get('regime', 'neutral')}"
        )

        return {
            "strategy_name": "ema_pullback",
            "price": current_close,
            "rs30": rs30,
            "rs60": rs60,
            "rs_edge": rs_edge,
            "rs_abs_valid": rs_confirm_valid,
            "rs_overheated": False,
            "ema_gap": trigger_gap,
            "macro_gap": macro_gap,
            "trigger_gap": trigger_gap,
            "pullback_depth": pullback_depth,
            "ema_valid": trigger_valid,
            "ema_macro_fast": ema_macro_fast,
            "ema_macro_slow": ema_macro_slow,
            "ema_pullback_fast": ema_pullback_fast,
            "ema_pullback_slow": ema_pullback_slow,
            "ema_trigger_fast": ema_trigger_fast,
            "ema_trigger_slow": ema_trigger_slow,
            "ema_macro_side": ema_macro_side,
            "ema_trigger_side": ema_trigger_side,
            "ema_side": ema_side,
            "ema_side_valid": ema_side_valid,
            "ema_entry_valid": ema_entry_valid,
            "entry_setup_valid": entry_setup_valid,
            "entry_side_valid": entry_side_valid,
            "entry_signal_source": entry_signal_source,
            "ema25d": ema_macro_fast,
            "ema50d": ema_macro_slow,
            "ema1d": ema_pullback_fast,
            "ema2d": ema_pullback_slow,
            "ema50": ema_trigger_fast,
            "ema100": ema_trigger_slow,
            "ema_macro_timeframe": timeframes["macro"],
            "ema_pullback_timeframe": timeframes["pullback"],
            "ema_trigger_timeframe": timeframes["trigger"],
            "trend_ema_fast": ema_macro_fast,
            "trend_ema_slow": ema_macro_slow,
            "trend_ema_gap": macro_gap,
            "price_to_trend_ema": (current_close - ema_macro_slow) / current_close,
            "recent_return_5m": 0.0,
            "recent_return_15m": 0.0,
            "pullback_from_high": 0.0,
            "bounce_from_low": 0.0,
            "local_reversion": pullback_depth,
            "btc_entry_return": btc_return_30m,
            "btc_return_30m": btc_return_30m,
            "score": score,
            "data_valid": data_valid,
            "direction_valid": direction_valid,
            "macro_valid": macro_valid,
            "pullback_valid": pullback_valid,
            "pullback_recovered": bool(pullback_context["pullback_recovered"]),
            "pullback_had_pullback": bool(pullback_context["pullback_had_pullback"]),
            "pullback_cross_age_candles": int(
                pullback_context["pullback_cross_age_candles"]
            ),
            "pullback_recovery_lookback_candles": int(
                pullback_context["pullback_recovery_lookback_candles"]
            ),
            "pullback_recovery_max_cross_age_candles": int(
                pullback_context["pullback_recovery_max_cross_age_candles"]
            ),
            "pullback_recovery_gap": pullback_context["pullback_recovery_gap"],
            "pullback_recovery_min_gap": pullback_context["pullback_recovery_min_gap"],
            "entry_pullback_required": entry_pullback_required,
            "entry_pullback_gate_valid": entry_pullback_gate_valid,
            "trigger_valid": trigger_valid,
            "rs_confirm_valid": rs_confirm_valid,
            "trend_valid": macro_valid,
            "recent_valid": pullback_valid,
            "btc_entry_valid": btc_entry_valid,
            "raw_entry_valid": raw_entry_valid,
            "market_structure_valid": market_structure_valid,
            "volume_valid": bool(market_structure["volume_valid"]),
            "volume_average_valid": bool(
                market_structure.get("volume_average_valid", False)
            ),
            "volume_ratio": market_structure["volume_ratio"],
            "volume_recent": market_structure["volume_recent"],
            "volume_baseline": market_structure["volume_baseline"],
            "volume_directional_fraction": market_structure[
                "volume_directional_fraction"
            ],
            "volume_spike_valid": bool(
                market_structure.get("volume_spike_valid", True)
            ),
            "volume_spike_ratio": market_structure.get("volume_spike_ratio", 0.0),
            "volume_spike_volume": market_structure.get("volume_spike_volume", 0.0),
            "volume_spike_baseline": market_structure.get("volume_spike_baseline", 0.0),
            "volume_spike_direction": market_structure.get(
                "volume_spike_direction", ""
            ),
            "volume_spike_reason": market_structure.get("volume_spike_reason", ""),
            "volume_profile_valid": bool(
                market_structure.get("volume_profile_valid", True)
            ),
            "volume_profile_poc": market_structure.get("volume_profile_poc", 0.0),
            "volume_profile_value_area_low": market_structure.get(
                "volume_profile_value_area_low", 0.0
            ),
            "volume_profile_value_area_high": market_structure.get(
                "volume_profile_value_area_high", 0.0
            ),
            "volume_profile_break": bool(
                market_structure.get("volume_profile_break", False)
            ),
            "volume_profile_reason": market_structure.get("volume_profile_reason", ""),
            "volume_reason": market_structure["volume_reason"],
            "chop_valid": bool(market_structure["chop_valid"]),
            "chop": market_structure["chop"],
            "chop_max": market_structure["chop_max"],
            "chop_reason": market_structure["chop_reason"],
            "volatility": volatility,
            "volatility_multiplier": volatility_multiplier,
            "atr": atr,
            "atr_rate": atr_rate,
            "daily_volatility": daily_volatility["daily_volatility"],
            "daily_volatility_multiplier": daily_volatility[
                "daily_volatility_multiplier"
            ],
            "volatility_budget_multiplier": daily_volatility[
                "volatility_budget_multiplier"
            ],
            "signal_budget_multiplier": signal_budget_multiplier,
            "entry_weighted_score": entry_quality["weighted_score"],
            "entry_weighted_score_min": entry_quality["min_score"],
            "entry_weighted_penalty_total": entry_quality["penalty_total"],
            "entry_weighted_penalties": dict(entry_quality["penalties"]),
            "entry_quality_budget_multiplier": entry_quality_budget_multiplier,
            "btc_budget_multiplier": btc_budget_multiplier,
            "macro_budget_multiplier": macro_budget_multiplier,
            "macro_ladder_multiplier": macro_ladder_multiplier,
            "macro_regime": macro_context.get("regime", "neutral"),
            "macro_direction_score": self._safe_float(
                macro_context.get("macro_direction_score"), 0.0
            ),
            "macro_disable_new_entries": bool(
                macro_context.get("disable_new_entries", False)
            ),
            "macro_disable_averaging": bool(
                macro_context.get("disable_averaging", False)
            ),
            "macro_time_exit_multiplier": self._safe_float(
                macro_context.get("time_exit_multiplier"), 1.0
            ),
            "budget_multiplier": budget_multiplier,
            "ladder_multiplier": ladder_multiplier,
            "btc_risk_reason": btc_risk.get("reason", "ema_filter"),
            "valid": data_valid,
            "entry_valid": entry_valid,
            "add_valid": add_valid,
            "reason": reason,
            "ts": latest_ts,
        }

    def _closed_candles(
        self,
        symbol: str,
        limit: int,
        max_ts: Optional[int] = None,
        timeframe: Optional[str] = None,
        exchange=None,
    ) -> list:
        timeframe = timeframe or config.SIGNALS.timeframe
        client = exchange or self.exchange
        if exchange is None and hasattr(self, "_fetch_ohlcv_with_retry"):
            ohlcv = self._fetch_ohlcv_with_retry(
                symbol, timeframe=timeframe, limit=limit
            )
        else:
            ohlcv = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            ohlcv = self._expect_ccxt_list_response(
                ohlcv,
                "fetch_ohlcv",
                symbol=symbol,
                item_types=(list, tuple),
            )
        now_ms = int(time.time() * 1000)
        timeframe_ms = self._signal_timeframe_seconds(timeframe) * 1000
        current_bucket = (now_ms // timeframe_ms) * timeframe_ms
        closed = [row for row in (ohlcv or []) if row and int(row[0]) < current_bucket]
        if max_ts is not None:
            closed = [row for row in closed if int(row[0]) <= max_ts]
        return closed

    def _market_data_max_workers(self) -> int:
        try:
            workers = int(getattr(config.RUNTIME, "market_data_max_workers", 1) or 1)
        except (TypeError, ValueError):
            workers = 1
        return max(1, workers)

    def _update_signal_cache_if_needed(self) -> bool:
        if not getattr(config.STRATEGY, "ema_strategy_enabled", True):
            self.signal_cache["benchmark_ok"] = False
            self.signal_cache["symbols"] = {}
            return False

        if not self.benchmark_symbol:
            self.signal_cache["benchmark_ok"] = False
            self.signal_cache["symbols"] = {}
            return False

        timeframes = self._ema_timeframes()
        trigger_timeframe = timeframes["trigger"]
        macro_timeframe = timeframes["macro"]
        pullback_timeframe = timeframes["pullback"]
        now_ms = int(time.time() * 1000)
        timeframe_ms = self._signal_timeframe_seconds(trigger_timeframe) * 1000
        target_closed_ts = ((now_ms // timeframe_ms) - 1) * timeframe_ms
        if self.signal_cache.get("closed_candle_ts") == target_closed_ts:
            return False

        trigger_history_limit = max(
            self._ema_required_history("trigger", converted=True) + 5,
            self._ema_market_structure_required_history(),
            max(
                self._trigger_window_candles(
                    config.SIGNALS.rs_slow_window, trigger_timeframe
                ),
                self._trigger_window_candles(
                    config.SIGNALS.rs_fast_window, trigger_timeframe
                ),
            )
            + 1,
            config.STRATEGY.volatility_window + 1,
            config.STRATEGY.daily_volatility_window + 1,
        )
        macro_history_limit = self._ema_required_history("macro", converted=True) + 5
        pullback_history_limit = (
            self._ema_required_history("pullback", converted=True) + 5
        )
        symbol_trigger_history_limit = max(
            trigger_history_limit,
            macro_history_limit if macro_timeframe == trigger_timeframe else 0,
            pullback_history_limit if pullback_timeframe == trigger_timeframe else 0,
        )
        symbol_macro_history_limit = max(
            macro_history_limit,
            pullback_history_limit if pullback_timeframe == macro_timeframe else 0,
        )

        try:
            benchmark_candles = self._closed_candles(
                self.benchmark_symbol,
                trigger_history_limit,
                timeframe=trigger_timeframe,
            )
        except Exception as exc:
            self.signal_cache["benchmark_ok"] = False
            self.signal_cache["symbols"] = {}
            self._log_event(
                "WARNING",
                f"BTC benchmark candles unavailable: {exc}",
                event="signal_invalid",
                symbol=self.benchmark_symbol,
                reason="benchmark_unavailable",
                exception=exc,
                retryable=getattr(
                    self, "_is_transient_exchange_error", lambda _exc: False
                )(exc),
            )
            return True

        benchmark_required = (
            max(
                self._trigger_window_candles(
                    config.SIGNALS.rs_slow_window, trigger_timeframe
                ),
                self._trigger_window_candles(
                    config.SIGNALS.rs_fast_window, trigger_timeframe
                ),
            )
            + 1
        )
        if len(benchmark_candles) < benchmark_required:
            self.signal_cache["benchmark_ok"] = False
            self.signal_cache["symbols"] = {}
            self._log_event(
                "WARNING",
                "BTC benchmark history is too short",
                event="signal_invalid",
                symbol=self.benchmark_symbol,
                reason=f"benchmark_history_short;candles={len(benchmark_candles)};required={benchmark_required}",
            )
            return True

        latest_ts = int(benchmark_candles[-1][0])
        benchmark_closes = [self._safe_float(row[4]) for row in benchmark_candles]
        self.signal_cache["benchmark_ok"] = True
        btc_risk = self._btc_risk_context(benchmark_closes)
        self.signal_cache["btc_risk"] = btc_risk
        macro_context = self._gold_btc_rsi_context()
        self.signal_cache.setdefault("macro", {})["gold_btc_rsi"] = macro_context

        rows = {}
        had_retryable_symbol_error = False

        def fetch_symbol_candles(symbol):
            try:
                candles = self._closed_candles(
                    symbol,
                    symbol_trigger_history_limit,
                    max_ts=latest_ts,
                    timeframe=trigger_timeframe,
                )
            except Exception as exc:
                return (
                    symbol,
                    None,
                    (
                        "WARNING",
                        f"Signal candles unavailable for {symbol}: {exc}",
                        "signal_invalid",
                        "symbol_candles_unavailable",
                        exc,
                    ),
                )

            if len(candles) < 2:
                return (
                    symbol,
                    None,
                    (
                        "DEBUG",
                        f"Signal skipped for {symbol}: not enough closed candles",
                        "signal_invalid",
                        "symbol_history_short",
                    ),
                )

            if int(candles[-1][0]) != latest_ts:
                return (
                    symbol,
                    None,
                    (
                        "DEBUG",
                        f"Signal skipped for {symbol}: candle is not aligned with BTC",
                        "signal_invalid",
                        "symbol_not_aligned_with_btc",
                    ),
                )

            try:
                if macro_timeframe == trigger_timeframe:
                    macro_candles = candles
                else:
                    macro_candles = self._closed_candles(
                        symbol,
                        symbol_macro_history_limit,
                        max_ts=latest_ts,
                        timeframe=macro_timeframe,
                    )
                if pullback_timeframe == trigger_timeframe:
                    pullback_candles = candles
                elif pullback_timeframe == macro_timeframe:
                    pullback_candles = macro_candles
                else:
                    pullback_candles = self._closed_candles(
                        symbol,
                        pullback_history_limit,
                        max_ts=latest_ts,
                        timeframe=pullback_timeframe,
                    )
            except Exception as exc:
                return (
                    symbol,
                    None,
                    (
                        "WARNING",
                        f"EMA timeframe candles unavailable for {symbol}: {exc}",
                        "ema_signal_invalid",
                        f"ema_timeframe_candles_unavailable;macro_tf={macro_timeframe};pullback_tf={pullback_timeframe};trigger_tf={trigger_timeframe}",
                        exc,
                    ),
                )

            return symbol, (candles, macro_candles, pullback_candles), None

        profile = getattr(self, "profile", None) or config.current_profile()

        def fetch_symbol_candles_safe(symbol):
            try:
                with config.use_profile(profile):
                    return fetch_symbol_candles(symbol)
            except Exception as exc:
                return (
                    symbol,
                    None,
                    (
                        "WARNING",
                        f"Unhandled exception fetching candles for {symbol}: {exc}",
                        "signal_invalid",
                        "unhandled_fetch_exception",
                        exc,
                    ),
                )

        symbols = list(self.symbols)
        max_workers = min(self._market_data_max_workers(), max(1, len(symbols)))
        if max_workers <= 1 or len(symbols) <= 1:
            results = [fetch_symbol_candles_safe(symbol) for symbol in symbols]
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers
            ) as executor:
                results = list(executor.map(fetch_symbol_candles_safe, symbols))

        for symbol, data, log_info in results:
            if log_info:
                level, msg, event, reason = log_info[:4]
                exc = log_info[4] if len(log_info) > 4 else None
                retryable = (
                    getattr(self, "_is_transient_exchange_error", lambda _exc: False)(
                        exc
                    )
                    if exc
                    else None
                )
                if retryable:
                    had_retryable_symbol_error = True
                self._log_event(
                    level,
                    msg,
                    event=event,
                    symbol=symbol,
                    reason=reason,
                    exception=exc,
                    retryable=retryable,
                )
                continue

            if not data:
                continue

            candles, macro_candles, pullback_candles = data

            if not macro_candles or not pullback_candles:
                self._log_event(
                    "DEBUG",
                    f"EMA timeframe history missing for {symbol}",
                    event="ema_signal_invalid",
                    symbol=symbol,
                    reason=(
                        f"ema_timeframe_history_missing;macro_tf={macro_timeframe};macro_candles={len(macro_candles or [])};"
                        f"pullback_tf={pullback_timeframe};pullback_candles={len(pullback_candles or [])}"
                    ),
                )
                continue

            closes = [self._safe_float(row[4]) for row in candles]
            macro_closes = [self._safe_float(row[4]) for row in macro_candles]
            pullback_closes = [self._safe_float(row[4]) for row in pullback_candles]

            ctx = SignalContext(
                closes=closes,
                benchmark_closes=benchmark_closes,
                btc_risk=btc_risk,
                latest_ts=latest_ts,
                candles=candles,
                cache_key=symbol,
                macro_context=macro_context,
                macro_closes=macro_closes,
                macro_latest_ts=int(macro_candles[-1][0]),
                pullback_closes=pullback_closes,
                pullback_latest_ts=int(pullback_candles[-1][0]),
            )
            signal = self._build_signal_from_closes(ctx)
            if not signal:
                self._log_event(
                    "DEBUG",
                    f"Signal skipped for {symbol}: could not build signal",
                    event="signal_invalid",
                    symbol=symbol,
                    reason="signal_build_failed",
                )
                continue
            rows[symbol] = signal

            rs30 = self._safe_float(signal.get("rs30"), 0.0)
            rs60 = self._safe_float(signal.get("rs60"), 0.0)
            ema50 = self._safe_float(signal.get("ema_trigger_fast"), 0.0)
            ema100 = self._safe_float(signal.get("ema_trigger_slow"), 0.0)
            entry_valid = bool(signal.get("entry_valid"))
            macro_valid = bool(signal.get("macro_valid"))
            pullback_valid = bool(signal.get("pullback_valid"))
            trigger_valid = bool(signal.get("trigger_valid"))

            state = self._get_state(symbol)
            state.strategy_name = "ema_pullback"
            state.last_signal_timestamp = latest_ts
            state.last_ema_strategy_signal_timestamp = latest_ts
            state.last_rs30 = rs30
            state.last_rs60 = rs60
            state.last_ema30 = ema50
            state.last_ema60 = ema100
            state.last_ema25d = self._safe_float(signal.get("ema_macro_fast"), 0.0)
            state.last_ema50d = self._safe_float(signal.get("ema_macro_slow"), 0.0)
            state.last_ema1d = self._safe_float(signal.get("ema_pullback_fast"), 0.0)
            state.last_ema2d = self._safe_float(signal.get("ema_pullback_slow"), 0.0)
            state.last_ema50 = ema50
            state.last_ema100 = ema100
            state.last_btc_return_30m = self._safe_float(
                signal.get("btc_return_30m"), 0.0
            )

            self._log_event(
                "INFO" if entry_valid else "DEBUG",
                f"EMA signal {'valid' if entry_valid else 'invalid'} for {symbol}",
                event="ema_signal_valid" if entry_valid else "ema_signal_invalid",
                symbol=symbol,
                rs30=rs30,
                rs60=rs60,
                ema50=ema50,
                ema100=ema100,
                reason=f"{signal.get('reason', '')};macro={int(macro_valid)};pullback={int(pullback_valid)};trigger={int(trigger_valid)}",
            )

        self.signal_cache["symbols"] = rows
        self.signal_cache["closed_candle_ts"] = (
            None if had_retryable_symbol_error else latest_ts
        )
        self._log_event(
            "INFO",
            f"Signals updated for {len(rows)} futures symbols",
            event="signal_updated",
            reason=(
                f"closed_ts={latest_ts}"
                if not had_retryable_symbol_error
                else f"closed_ts={latest_ts};retryable_symbol_error=1;cache_retry_pending=1"
            ),
        )
        self._save_state()
        return True
