# -*- coding: utf-8 -*-

import math
import time
import concurrent.futures
from typing import List, Optional, Tuple

import config

from .indicators import average_true_range, calculate_ema, calculate_rsi, clamp, compute_log_return, realized_volatility
from .models import SignalContext


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

    def _average_true_range_rate(self, candles: Optional[List[list]], close_price: float, period: int) -> Tuple[float, float]:
        if not candles or close_price <= 0:
            return 0.0, 0.0
        atr = self._average_true_range(candles, period)
        if atr <= 0:
            return 0.0, 0.0
        return atr, atr / close_price

    def _signal_score(self, rs30: float, rs60: float, ema50: float, ema100: float, price: float) -> float:
        if config.POSITION_SIDE == "short":
            rs_edge = max(0.0, rs30 - rs60)
        else:
            rs_edge = max(0.0, rs60 - rs30)
        ema_edge = 0.0
        if price > 0:
            if config.POSITION_SIDE == "short":
                ema_gap = (ema50 - ema100) / price
            else:
                ema_gap = (ema100 - ema50) / price
            ema_edge = max(0.0, ema_gap) * config.STRATEGY.signal_ema_gap_weight
        return rs_edge + ema_edge

    def _local_reversion_context(self, closes: List[float], current_close: float) -> dict:
        window = 15
        recent = [price for price in closes[-window - 1:] if price > 0]
        if not recent or current_close <= 0:
            return {
                "pullback_from_high": 0.0,
                "bounce_from_low": 0.0,
                "local_reversion": 0.0,
            }

        recent_high = max(recent)
        recent_low = min(recent)
        pullback_from_high = (recent_high - current_close) / recent_high if recent_high > 0 else 0.0
        bounce_from_low = (current_close - recent_low) / recent_low if recent_low > 0 else 0.0
        return {
            "pullback_from_high": max(0.0, pullback_from_high),
            "bounce_from_low": max(0.0, bounce_from_low),
            "local_reversion": max(0.0, bounce_from_low if config.POSITION_SIDE == "short" else pullback_from_high),
        }

    def _signal_budget_multiplier(self, score: float) -> float:
        strategy = config.STRATEGY
        if not strategy.enable_signal_size_scaling:
            return 1.0
        reference = max(strategy.signal_score_reference, 1e-12)
        ratio = self._clamp(score / reference, 0.0, 1.0)
        return strategy.signal_budget_min_multiplier + (
            strategy.signal_budget_max_multiplier - strategy.signal_budget_min_multiplier
        ) * ratio

    def _is_raw_entry_signal_valid(self, signal: Optional[dict]) -> bool:
        if not signal or not self._signal_direction_valid(signal) or not self.signal_cache.get("benchmark_ok"):
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

    def _entry_signal_quality_block_reason(self, signal: Optional[dict], crowded: bool = False) -> str:
        if not self._is_raw_entry_signal_valid(signal):
            return "entry_signal_invalid"

        thresholds = self._entry_thresholds(crowded=crowded)
        raw_score = self._safe_float(signal.get("score"), 0.0)
        external_bonus = self._safe_float(getattr(self, "_external_entry_score_bonus", lambda _signal: 0.0)(signal), 0.0)
        score = raw_score + external_bonus
        if score + 1e-12 < thresholds["score"]:
            return f"entry_score_below_min;score={score:.6f};raw_score={raw_score:.6f};external_bonus={external_bonus:.6f};min={thresholds['score']:.6f}"

        rs60 = self._safe_float(signal.get("rs60"), 0.0)
        directional_rs60 = self._directional_entry_value(rs60)
        if directional_rs60 + 1e-12 < thresholds["rs60"]:
            return (
                "entry_rs60_below_min;"
                f"rs60={rs60:.6f};directional={directional_rs60:.6f};min={thresholds['rs60']:.6f}"
            )

        rs30 = self._safe_float(signal.get("rs30"), 0.0)
        directional_rs30 = self._directional_entry_value(rs30)
        if directional_rs30 + 1e-12 < thresholds["rs30"]:
            return (
                "entry_rs30_below_min;"
                f"rs30={rs30:.6f};directional={directional_rs30:.6f};min={thresholds['rs30']:.6f}"
            )

        return ""

    def _is_entry_signal_valid(self, signal: Optional[dict]) -> bool:
        return not self._entry_signal_quality_block_reason(signal, crowded=False)

    def _is_entry_expansion_signal_valid(self, signal: Optional[dict]) -> bool:
        return False

    def _is_add_signal_valid(self, signal: Optional[dict]) -> bool:
        return self._signal_add_valid(signal)

    def _signal_add_valid(self, signal: Optional[dict]) -> bool:
        if not signal or not self._signal_direction_valid(signal) or not self.signal_cache.get("benchmark_ok"):
            return False
        return bool(signal.get("add_valid", False))

    def _volatility_multiplier(self, volatility: float) -> float:
        strategy = config.STRATEGY
        if not strategy.enable_volatility_adjusted_ladders:
            return 1.0
        reference = max(strategy.volatility_reference, 1e-12)
        return self._clamp(
            volatility / reference,
            strategy.min_ladder_volatility_multiplier,
            strategy.max_ladder_volatility_multiplier,
        )

    def _daily_volatility_context(self, closes: List[float]) -> dict:
        strategy = config.STRATEGY
        window = max(0, int(strategy.daily_volatility_window))
        if window <= 1 or len(closes) < window + 1:
            return {
                "daily_volatility": 0.0,
                "daily_volatility_multiplier": 1.0,
                "volatility_budget_multiplier": 1.0,
            }

        daily_volatility = self._realized_volatility(closes, window) * math.sqrt(window)
        reference = max(strategy.daily_volatility_reference, 1e-12)
        daily_volatility_multiplier = daily_volatility / reference
        volatility_budget_multiplier = 1.0
        if strategy.enable_volatility_targeted_sizing:
            if daily_volatility_multiplier > 0:
                raw_budget_multiplier = 1.0 / daily_volatility_multiplier
            else:
                raw_budget_multiplier = strategy.max_volatility_budget_multiplier
            volatility_budget_multiplier = self._clamp(
                raw_budget_multiplier,
                strategy.min_volatility_budget_multiplier,
                strategy.max_volatility_budget_multiplier,
            )
        return {
            "daily_volatility": daily_volatility,
            "daily_volatility_multiplier": daily_volatility_multiplier,
            "volatility_budget_multiplier": volatility_budget_multiplier,
        }

    def _btc_risk_context(self, benchmark_closes: List[float]) -> dict:
        strategy = config.STRATEGY
        if not strategy.enable_btc_risk_multiplier:
            return {
                "return": 0.0,
                "volatility": 0.0,
                "budget_multiplier": 1.0,
                "ladder_multiplier": 1.0,
                "reason": "disabled",
            }

        window = strategy.btc_risk_return_window
        btc_return = 0.0
        if window > 0 and len(benchmark_closes) > window:
            btc_return = self._compute_log_return(benchmark_closes[-1], benchmark_closes[-window - 1])
        btc_volatility = self._realized_volatility(benchmark_closes, strategy.volatility_window)

        budget_multiplier = 1.0
        reasons = []
        if config.POSITION_SIDE == "short":
            btc_risk_move = btc_return >= -strategy.btc_risk_drop_threshold
            btc_risk_reason = "btc_rise"
        else:
            btc_risk_move = btc_return <= strategy.btc_risk_drop_threshold
            btc_risk_reason = "btc_drop"

        if btc_risk_move:
            budget_multiplier *= strategy.btc_risk_drop_budget_multiplier
            reasons.append(btc_risk_reason)
        if btc_volatility >= strategy.btc_risk_high_vol_threshold:
            budget_multiplier *= strategy.btc_risk_vol_budget_multiplier
            reasons.append("btc_high_vol")

        budget_multiplier = self._clamp(budget_multiplier, strategy.btc_risk_min_budget_multiplier, 1.0)
        ladder_multiplier = self._clamp(
            1.0 + (1.0 - budget_multiplier),
            1.0,
            strategy.btc_risk_max_ladder_multiplier,
        )
        return {
            "return": btc_return,
            "volatility": btc_volatility,
            "budget_multiplier": budget_multiplier,
            "ladder_multiplier": ladder_multiplier,
            "reason": "+".join(reasons) if reasons else "neutral",
        }

    def _calculate_rsi(self, closes: List[float], period: int) -> float:
        return calculate_rsi(closes, period)

    def _neutral_macro_context(self, reason: str = "neutral", regime: str = "neutral", ok: bool = True) -> dict:
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
            "regime": regime,
            "long_budget_multiplier": 1.0,
            "short_budget_multiplier": 1.0,
            "ladder_multiplier": 1.0,
            "disable_new_entries": False,
            "disable_averaging": False,
            "disable_recovery": False,
            "time_exit_multiplier": 1.0,
            "reason": reason,
        }

    def _macro_cache_root(self) -> dict:
        return self.signal_cache.setdefault("macro", {})

    def _cached_gold_btc_rsi_context(self) -> dict:
        context = self._macro_cache_root().get("gold_btc_rsi")
        if isinstance(context, dict):
            return context
        context = self._neutral_macro_context("not_loaded", regime="macro_unavailable", ok=False)
        self._macro_cache_root()["gold_btc_rsi"] = context
        return context

    def _macro_context_is_stale(self, context: dict) -> bool:
        max_age = max(0, int(config.MACRO.stale_macro_max_age_sec))
        if max_age <= 0:
            return False
        ts = self._safe_float((context or {}).get("ts"), 0.0)
        return bool(ts > 0 and time.time() - ts > max_age)

    def _macro_context_for_trading(self, context: Optional[dict] = None) -> dict:
        context = context if isinstance(context, dict) else self._cached_gold_btc_rsi_context()
        if not config.MACRO.enable_gold_btc_rsi_overlay:
            return self._neutral_macro_context("disabled", regime="macro_disabled", ok=False)
        if self._macro_context_is_stale(context):
            stale = self._neutral_macro_context("macro_context_stale", regime="neutral", ok=False)
            stale["gold_symbol"] = context.get("gold_symbol", stale["gold_symbol"])
            stale["btc_symbol"] = context.get("btc_symbol", stale["btc_symbol"])
            key = (context.get("ts"), context.get("regime"), context.get("reason"))
            if getattr(self, "_last_macro_stale_log_key", None) != key:
                self._last_macro_stale_log_key = key
                self._record_macro_context(stale, event="macro_context_stale", level="WARNING")
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
                f"rsi_spread={self._safe_float(context.get('rsi_spread'), 0.0):.4f};reason={reason}"
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
        window = max(1, int(config.MACRO.gold_rsi_period))
        if direct_closes:
            values = [price for price in direct_closes if price > 0]
            if len(values) > window:
                return self._compute_log_return(values[-1], values[-window - 1])
            return 0.0

        count = min(len(gold_closes), len(btc_closes))
        if count <= window:
            return 0.0
        ratios = []
        for gold, btc in zip(gold_closes[-count:], btc_closes[-count:]):
            if gold > 0 and btc > 0:
                ratios.append(gold / btc)
        if len(ratios) <= window:
            return 0.0
        return self._compute_log_return(ratios[-1], ratios[-window - 1])

    def _classify_gold_btc_rsi_context(
        self,
        gold_symbol: str,
        btc_symbol: str,
        gold_rsi: float,
        btc_rsi: float,
        ratio_return: float,
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
            }
        )

        if btc_rsi <= macro.btc_weak_rsi and gold_rsi <= macro.gold_weak_rsi:
            context.update(
                {
                    "regime": "deleveraging",
                    "ladder_multiplier": 1.4,
                    "disable_new_entries": bool(macro.panic_disable_new_entries),
                    "disable_averaging": True,
                    "disable_recovery": True,
                    "time_exit_multiplier": 0.65,
                    "reason": "btc_weak_gold_weak",
                }
            )
            return context

        btc_defensive_rsi = macro.btc_weak_rsi + 5.0
        if (
            gold_rsi >= macro.gold_strong_rsi
            and btc_rsi <= btc_defensive_rsi
        ) or (gold_rsi - btc_rsi >= macro.rsi_spread_threshold):
            context.update(
                {
                    "regime": "crypto_underperforms_gold",
                    "long_budget_multiplier": min(max(0.0, macro.risk_off_long_budget_multiplier), 1.0),
                    "short_budget_multiplier": max(0.0, macro.risk_off_short_budget_multiplier),
                    "ladder_multiplier": max(0.0, macro.risk_off_ladder_multiplier),
                    "disable_averaging": bool(macro.risk_off_disable_averaging),
                    "disable_recovery": bool(macro.risk_off_disable_recovery),
                    "time_exit_multiplier": max(0.0, macro.risk_off_time_exit_multiplier),
                    "reason": "gold_strong_btc_weak",
                }
            )
            return context

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
            return context

        if btc_rsi >= macro.btc_strong_rsi and gold_rsi >= macro.gold_strong_rsi:
            context.update(
                {
                    "regime": "broad_liquidity_risk_on",
                    "long_budget_multiplier": 1.0,
                    "short_budget_multiplier": 0.85,
                    "reason": "btc_strong_gold_strong",
                }
            )
            return context

        return context

    def _macro_fetch_exchange(self, is_spot: bool):
        return self._spot_exchange() if is_spot else self.exchange

    def _gold_btc_rsi_context(self) -> dict:
        if not config.MACRO.enable_gold_btc_rsi_overlay:
            context = self._neutral_macro_context("disabled", regime="macro_disabled", ok=False)
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
            context = self._neutral_macro_context("gold_symbol_not_found", regime="macro_unavailable", ok=False)
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(context, event="macro_context_unavailable", level="WARNING")
            return context

        btc_symbol = getattr(self, "benchmark_symbol", None)
        if not btc_symbol:
            context = self._neutral_macro_context("btc_symbol_not_found", regime="macro_unavailable", ok=False)
            context["gold_symbol"] = gold_symbol
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(context, event="macro_context_unavailable", level="WARNING")
            return context

        timeframe = str(config.MACRO.gold_timeframe or "4h")
        limit = max(int(config.MACRO.gold_min_candles), int(config.MACRO.gold_rsi_period) + 2)
        fetch_limit = limit + 1
        try:
            gold_candles = self._closed_candles(
                gold_symbol,
                fetch_limit,
                timeframe=timeframe,
                exchange=self._macro_fetch_exchange(bool(getattr(self, "macro_gold_is_spot", False))),
            )
            btc_candles = self._closed_candles(btc_symbol, fetch_limit, timeframe=timeframe)
        except Exception as exc:
            context = self._neutral_macro_context("macro_candles_unavailable", regime="macro_unavailable", ok=False)
            context.update({"gold_symbol": gold_symbol, "btc_symbol": btc_symbol, "timeframe": timeframe})
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(context, event="macro_context_unavailable", level="WARNING")
            self._log_event(
                "DEBUG",
                f"Gold/BTC macro candles unavailable: {exc}",
                event="macro_context_unavailable",
                symbol=gold_symbol,
                reason="macro_candles_fetch_failed",
            )
            return context

        if len(gold_candles) < limit or len(btc_candles) < limit:
            context = self._neutral_macro_context("macro_history_short", regime="macro_unavailable", ok=False)
            context.update({"gold_symbol": gold_symbol, "btc_symbol": btc_symbol, "timeframe": timeframe})
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(context, event="macro_context_unavailable", level="WARNING")
            return context

        gold_closes = [self._safe_float(row[4], 0.0) for row in gold_candles]
        btc_closes = [self._safe_float(row[4], 0.0) for row in btc_candles]
        gold_rsi = self._calculate_rsi(gold_closes, config.MACRO.gold_rsi_period)
        btc_rsi = self._calculate_rsi(btc_closes, config.MACRO.gold_rsi_period)
        if config.MACRO.gold_rsi_period <= 0:
            context = self._neutral_macro_context("macro_rsi_unavailable", regime="macro_unavailable", ok=False)
            context.update({"gold_symbol": gold_symbol, "btc_symbol": btc_symbol, "timeframe": timeframe})
            self._macro_cache_root()["gold_btc_rsi"] = context
            self._record_macro_context(context, event="macro_context_unavailable", level="WARNING")
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
                    exchange=self._macro_fetch_exchange(bool(getattr(self, "macro_direct_gold_btc_is_spot", False))),
                )
                direct_closes = [self._safe_float(row[4], 0.0) for row in direct_candles]
            except Exception:
                direct_closes = None

        ratio_return = self._gold_btc_ratio_return(gold_closes, btc_closes, direct_closes=direct_closes)
        context = self._classify_gold_btc_rsi_context(gold_symbol, btc_symbol, gold_rsi, btc_rsi, ratio_return)
        context["timeframe"] = timeframe
        self._macro_cache_root()["gold_btc_rsi"] = context
        self._record_macro_context(context, event="macro_context_updated", level="INFO")
        if (
            abs(self._safe_float(context.get("long_budget_multiplier"), 1.0) - 1.0) > 1e-12
            or abs(self._safe_float(context.get("short_budget_multiplier"), 1.0) - 1.0) > 1e-12
            or abs(self._safe_float(context.get("ladder_multiplier"), 1.0) - 1.0) > 1e-12
            or context.get("disable_new_entries")
            or context.get("disable_averaging")
            or context.get("disable_recovery")
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
            "trigger": str(getattr(strategy, "ema_trigger_timeframe", config.SIGNALS.timeframe) or config.SIGNALS.timeframe),
        }

    def _period_minutes_to_candles(self, minutes: int, timeframe: str) -> int:
        timeframe_minutes = max(self._signal_timeframe_seconds(timeframe) / 60.0, 1e-9)
        return max(1, int(math.ceil(max(1, int(minutes)) / timeframe_minutes)))

    def _trigger_window_candles(self, minutes: int, timeframe: Optional[str] = None) -> int:
        return self._period_minutes_to_candles(minutes, timeframe or self._ema_timeframes()["trigger"])

    def _ema_periods(self, converted: bool = False) -> dict:
        strategy = config.STRATEGY
        if converted:
            timeframes = self._ema_timeframes()
            return {
                "ema_macro_fast": self._period_minutes_to_candles(strategy.ema_macro_fast_minutes, timeframes["macro"]),
                "ema_macro_slow": self._period_minutes_to_candles(strategy.ema_macro_slow_minutes, timeframes["macro"]),
                "ema_pullback_fast": self._period_minutes_to_candles(strategy.ema_pullback_fast_minutes, timeframes["pullback"]),
                "ema_pullback_slow": self._period_minutes_to_candles(strategy.ema_pullback_slow_minutes, timeframes["pullback"]),
                "ema_trigger_fast": self._period_minutes_to_candles(strategy.ema_trigger_fast_minutes, timeframes["trigger"]),
                "ema_trigger_slow": self._period_minutes_to_candles(strategy.ema_trigger_slow_minutes, timeframes["trigger"]),
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
                self._period_minutes_to_candles(strategy.ema_pullback_recovery_lookback_minutes, timeframe),
                self._period_minutes_to_candles(strategy.ema_pullback_recovery_max_cross_age_minutes, timeframe),
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
            rs_slow_window = self._trigger_window_candles(config.SIGNALS.rs_slow_window, trigger_timeframe)
            btc_fast_window = self._trigger_window_candles(config.SIGNALS.rs_fast_window, trigger_timeframe)
        if group == "macro":
            return max(periods["ema_macro_fast"], periods["ema_macro_slow"])
        if group == "pullback":
            pullback_lookback, _ = self._ema_pullback_recovery_windows(converted=converted)
            return max(periods["ema_pullback_fast"], periods["ema_pullback_slow"]) + pullback_lookback
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
        if not closes:
            return []
        alpha = 2.0 / (max(1, int(period)) + 1.0)
        ema = float(closes[0])
        values = [ema]
        for price in closes[1:]:
            ema = float(price) * alpha + ema * (1.0 - alpha)
            values.append(ema)
        return values

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
            full_cache_key = f"{cache_namespace}:{cache_key}" if cache_namespace else cache_key
            cache = cache_root.get(full_cache_key)
        else:
            full_cache_key = ""

        timeframe_ms = max(1, int(timeframe_sec or getattr(self, "timeframe_sec", 60))) * 1000
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
            values = {name: self._calculate_ema(closes, period) for name, period in periods.items()}

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
        lookback, max_cross_age = self._ema_pullback_recovery_windows(converted=converted)
        gap_threshold = max(0.0, self._safe_float(config.STRATEGY.ema_pullback_recovery_gap, 0.0))
        fast_series = self._ema_series_from_closes(closes, fast_period)
        slow_series = self._ema_series_from_closes(closes, slow_period)
        signed_gaps: List[float] = []
        for fast, slow in zip(fast_series, slow_series):
            if slow <= 0:
                signed_gaps.append(0.0)
                continue
            if config.POSITION_SIDE == "short":
                signed_gaps.append((slow - fast) / slow)
            else:
                signed_gaps.append((fast - slow) / slow)

        current_gap = signed_gaps[-1] if signed_gaps else 0.0
        if gap_threshold > 0:
            recovered = current_gap + 1e-12 >= gap_threshold
        else:
            recovered = current_gap > 0

        history_start = max(0, len(signed_gaps) - lookback - 1)
        recent_gaps = signed_gaps[history_start:]
        had_pullback = any(gap <= 0 for gap in recent_gaps)

        last_cross_index = None
        for index in range(max(1, history_start), len(signed_gaps)):
            if signed_gaps[index] > 0 and signed_gaps[index - 1] <= 0:
                last_cross_index = index

        cross_age = len(signed_gaps) - 1 - last_cross_index if last_cross_index is not None else -1
        fresh_cross = cross_age >= 0 and cross_age <= max_cross_age
        valid = bool(recovered and had_pullback and fresh_cross)

        return {
            "pullback_valid": valid,
            "pullback_recovered": recovered,
            "pullback_had_pullback": had_pullback,
            "pullback_cross_age_candles": cross_age,
            "pullback_recovery_lookback_candles": lookback,
            "pullback_recovery_max_cross_age_candles": max_cross_age,
            "pullback_recovery_gap": current_gap,
            "pullback_recovery_min_gap": gap_threshold,
        }

    def _empty_ema_signal(self, latest_ts: int, reason: str, price: float = 0.0) -> dict:
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
            "pullback_recovery_lookback_candles": self._ema_pullback_recovery_windows(converted=True)[0],
            "pullback_recovery_max_cross_age_candles": self._ema_pullback_recovery_windows(converted=True)[1],
            "pullback_recovery_gap": 0.0,
            "pullback_recovery_min_gap": max(0.0, self._safe_float(config.STRATEGY.ema_pullback_recovery_gap, 0.0)),
            "trigger_valid": False,
            "rs_confirm_valid": False,
            "btc_entry_valid": False,
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
            "macro_disable_new_entries": False,
            "macro_disable_averaging": False,
            "macro_disable_recovery": False,
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
                raise TypeError("_build_signal_from_closes requires benchmark_closes and latest_ts")
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
            return self._empty_ema_signal(latest_ts, "ema_strategy_disabled", price=current_close)

        use_timeframe_ema = macro_closes is not None or pullback_closes is not None
        periods = self._ema_periods(converted=use_timeframe_ema)
        macro_closes = macro_closes if macro_closes is not None else closes
        pullback_closes = pullback_closes if pullback_closes is not None else closes
        macro_latest_ts = int(macro_latest_ts if macro_latest_ts is not None else latest_ts)
        pullback_latest_ts = int(pullback_latest_ts if pullback_latest_ts is not None else latest_ts)
        timeframes = self._ema_timeframes()
        rs_fast_window = self._trigger_window_candles(config.SIGNALS.rs_fast_window, timeframes["trigger"])
        rs_slow_window = self._trigger_window_candles(config.SIGNALS.rs_slow_window, timeframes["trigger"])
        btc_return_window = rs_fast_window
        benchmark_required = max(rs_slow_window, btc_return_window) + 1

        trigger_required = self._ema_required_history("trigger", converted=use_timeframe_ema)
        macro_required = self._ema_required_history("macro", converted=use_timeframe_ema)
        pullback_required = self._ema_required_history("pullback", converted=use_timeframe_ema)
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

        price_30_ago = closes[-rs_fast_window - 1]
        btc_30_ago = benchmark_closes[-rs_fast_window - 1]
        price_60_ago = closes[-rs_slow_window - 1]
        btc_60_ago = benchmark_closes[-rs_slow_window - 1]
        rs30 = self._compute_log_return(current_close, price_30_ago) - self._compute_log_return(current_btc, btc_30_ago)
        rs60 = self._compute_log_return(current_close, price_60_ago) - self._compute_log_return(current_btc, btc_60_ago)
        btc_return_30m = self._compute_log_return(current_btc, benchmark_closes[-btc_return_window - 1])

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
            timeframe_sec=self._signal_timeframe_seconds(timeframes["trigger"]) if use_timeframe_ema else None,
        )
        pullback_values = self._ema_values_from_closes(
            pullback_closes,
            pullback_latest_ts,
            cache_key=cache_key,
            periods=pullback_periods,
            cache_namespace="ema_pullback",
            timeframe_sec=self._signal_timeframe_seconds(timeframes["pullback"]) if use_timeframe_ema else None,
        )
        macro_values = self._ema_values_from_closes(
            macro_closes,
            macro_latest_ts,
            cache_key=cache_key,
            periods=macro_periods,
            cache_namespace="ema_macro",
            timeframe_sec=self._signal_timeframe_seconds(timeframes["macro"]) if use_timeframe_ema else None,
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

        if config.POSITION_SIDE == "short":
            macro_valid = ema_macro_fast < ema_macro_slow
            pullback_valid = bool(pullback_context["pullback_valid"])
            trigger_valid = ema_trigger_fast < ema_trigger_slow
            rs_confirm_valid = (not strategy.ema_use_rs_confirmation) or rs60 <= strategy.ema_short_max_rs60
            btc_entry_valid = (not strategy.ema_use_btc_risk_filter) or btc_return_30m <= strategy.ema_btc_short_max_return_30m
            macro_gap = (ema_macro_slow - ema_macro_fast) / current_close
            trigger_gap = (ema_trigger_slow - ema_trigger_fast) / current_close
            pullback_depth = (ema_pullback_slow - ema_pullback_fast) / current_close
            rs_edge = max(0.0, -rs60)
            score = macro_gap + trigger_gap + pullback_depth + rs_edge
        else:
            macro_valid = ema_macro_fast > ema_macro_slow
            pullback_valid = bool(pullback_context["pullback_valid"])
            trigger_valid = ema_trigger_fast > ema_trigger_slow
            rs_confirm_valid = (not strategy.ema_use_rs_confirmation) or rs60 >= strategy.ema_long_min_rs60
            btc_entry_valid = (not strategy.ema_use_btc_risk_filter) or btc_return_30m >= strategy.ema_btc_long_min_return_30m
            macro_gap = (ema_macro_fast - ema_macro_slow) / current_close
            trigger_gap = (ema_trigger_fast - ema_trigger_slow) / current_close
            pullback_depth = (ema_pullback_fast - ema_pullback_slow) / current_close
            rs_edge = max(0.0, rs60)
            score = macro_gap + trigger_gap + pullback_depth + rs_edge

        macro_context = self._macro_context_for_trading(macro_context)
        data_valid = True
        direction_valid = bool(macro_valid)
        entry_valid = bool(
            macro_valid
            and pullback_valid
            and trigger_valid
            and rs_confirm_valid
            and btc_entry_valid
        )
        add_valid = bool(macro_valid and (trigger_valid or pullback_valid))
        recovery_confirmation = self._frozen_recovery_confirmation(candles, benchmark_closes, btc_risk)
        frozen_recovery_confirmed = bool(
            add_valid
            and btc_entry_valid
            and recovery_confirmation.get("frozen_recovery_confirmed")
            and not macro_context.get("disable_recovery", False)
        )
        volatility = self._realized_volatility(closes, strategy.volatility_window)
        volatility_multiplier = self._volatility_multiplier(volatility)
        atr, atr_rate = self._average_true_range_rate(candles, current_close, strategy.ema_averaging_atr_period)
        daily_volatility = self._daily_volatility_context(closes)
        signal_budget_multiplier = self._signal_budget_multiplier(score)
        btc_budget_multiplier = max(0.0, self._safe_float(btc_risk.get("budget_multiplier"), 1.0))
        btc_ladder_multiplier = max(0.0, self._safe_float(btc_risk.get("ladder_multiplier"), 1.0))
        if config.POSITION_SIDE == "short":
            macro_budget_multiplier = max(0.0, self._safe_float(macro_context.get("short_budget_multiplier"), 1.0))
        else:
            macro_budget_multiplier = min(
                max(0.0, self._safe_float(macro_context.get("long_budget_multiplier"), 1.0)),
                1.0,
            )
        macro_ladder_multiplier = max(0.0, self._safe_float(macro_context.get("ladder_multiplier"), 1.0))
        budget_multiplier = signal_budget_multiplier * btc_budget_multiplier * macro_budget_multiplier
        ladder_multiplier = volatility_multiplier * btc_ladder_multiplier * macro_ladder_multiplier
        reason = (
            f"strategy=ema_pullback;macro_tf={timeframes['macro']};pullback_tf={timeframes['pullback']};trigger_tf={timeframes['trigger']};"
            f"ema25d={ema_macro_fast:.12f};ema50d={ema_macro_slow:.12f};"
            f"ema1d={ema_pullback_fast:.12f};ema2d={ema_pullback_slow:.12f};"
            f"ema50={ema_trigger_fast:.12f};ema100={ema_trigger_slow:.12f};"
            f"rs30={rs30:.6f};rs60={rs60:.6f};btc_return_30m={btc_return_30m:.6f};"
            f"pullback_recovered={int(pullback_context['pullback_recovered'])};"
            f"pullback_had_pullback={int(pullback_context['pullback_had_pullback'])};"
            f"pullback_cross_age={int(pullback_context['pullback_cross_age_candles'])};"
            f"pullback_gap={pullback_context['pullback_recovery_gap']:.6f};"
            f"pullback_min_gap={pullback_context['pullback_recovery_min_gap']:.6f};"
            f"macro_valid={int(macro_valid)};pullback_valid={int(pullback_valid)};"
            f"trigger_valid={int(trigger_valid)};rs_confirm_valid={int(rs_confirm_valid)};"
            f"btc_entry_valid={int(btc_entry_valid)};entry_valid={int(entry_valid)};"
            f"add_valid={int(add_valid)};score={score:.6f};"
            f"atr_rate={atr_rate:.6f};"
            f"signal_budget_multiplier={signal_budget_multiplier:.3f};"
            f"btc_budget_multiplier={btc_budget_multiplier:.3f};"
            f"macro_budget_multiplier={macro_budget_multiplier:.3f};"
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
            "pullback_cross_age_candles": int(pullback_context["pullback_cross_age_candles"]),
            "pullback_recovery_lookback_candles": int(pullback_context["pullback_recovery_lookback_candles"]),
            "pullback_recovery_max_cross_age_candles": int(pullback_context["pullback_recovery_max_cross_age_candles"]),
            "pullback_recovery_gap": pullback_context["pullback_recovery_gap"],
            "pullback_recovery_min_gap": pullback_context["pullback_recovery_min_gap"],
            "trigger_valid": trigger_valid,
            "rs_confirm_valid": rs_confirm_valid,
            "trend_valid": macro_valid,
            "recent_valid": pullback_valid,
            "btc_entry_valid": btc_entry_valid,
            "volatility": volatility,
            "volatility_multiplier": volatility_multiplier,
            "atr": atr,
            "atr_rate": atr_rate,
            "daily_volatility": daily_volatility["daily_volatility"],
            "daily_volatility_multiplier": daily_volatility["daily_volatility_multiplier"],
            "volatility_budget_multiplier": daily_volatility["volatility_budget_multiplier"],
            "signal_budget_multiplier": signal_budget_multiplier,
            "btc_budget_multiplier": btc_budget_multiplier,
            "macro_budget_multiplier": macro_budget_multiplier,
            "macro_ladder_multiplier": macro_ladder_multiplier,
            "macro_regime": macro_context.get("regime", "neutral"),
            "macro_disable_new_entries": bool(macro_context.get("disable_new_entries", False)),
            "macro_disable_averaging": bool(macro_context.get("disable_averaging", False)),
            "macro_disable_recovery": bool(macro_context.get("disable_recovery", False)),
            "macro_time_exit_multiplier": self._safe_float(macro_context.get("time_exit_multiplier"), 1.0),
            "budget_multiplier": budget_multiplier,
            "ladder_multiplier": ladder_multiplier,
            "btc_risk_reason": btc_risk.get("reason", "ema_filter"),
            "valid": direction_valid,
            "entry_valid": entry_valid,
            "add_valid": add_valid,
            "frozen_recovery_confirmed": frozen_recovery_confirmed,
            "frozen_recovery_confirmed_candles": int(
                recovery_confirmation.get("frozen_recovery_confirmed_candles", 0)
            ),
            "reason": reason,
            "ts": latest_ts,
        }

    def _frozen_recovery_confirmation(self, candles: list, benchmark_closes: List[float], btc_risk: dict) -> dict:
        closes = [self._safe_float(row[4], 0.0) for row in candles or [] if row and len(row) > 4]
        confirmed_candles = 0
        for index in range(len(closes) - 1, 0, -1):
            current = closes[index]
            previous = closes[index - 1]
            if current <= 0 or previous <= 0:
                break
            if config.POSITION_SIDE == "short":
                favorable = current < previous
            else:
                favorable = current > previous
            if not favorable:
                break
            confirmed_candles += 1

        btc_budget = self._safe_float((btc_risk or {}).get("budget_multiplier"), 1.0)
        return {
            "frozen_recovery_confirmed": confirmed_candles > 0 and btc_budget > 0,
            "frozen_recovery_confirmed_candles": confirmed_candles,
        }
        return {
            "frozen_recovery_confirmed": False,
            "frozen_recovery_confirmed_candles": 0,
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
            ohlcv = self._fetch_ohlcv_with_retry(symbol, timeframe=timeframe, limit=limit)
        else:
            ohlcv = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        now_ms = int(time.time() * 1000)
        timeframe_ms = self._signal_timeframe_seconds(timeframe) * 1000
        current_bucket = (now_ms // timeframe_ms) * timeframe_ms
        closed = [row for row in (ohlcv or []) if row and int(row[0]) < current_bucket]
        if max_ts is not None:
            closed = [row for row in closed if int(row[0]) <= max_ts]
        return closed

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
            max(
                self._trigger_window_candles(config.SIGNALS.rs_slow_window, trigger_timeframe),
                self._trigger_window_candles(config.SIGNALS.rs_fast_window, trigger_timeframe),
            )
            + 1,
            config.STRATEGY.volatility_window + 1,
            config.STRATEGY.daily_volatility_window + 1,
        )
        macro_history_limit = self._ema_required_history("macro", converted=True) + 5
        pullback_history_limit = self._ema_required_history("pullback", converted=True) + 5
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
            )
            return True

        benchmark_required = max(
            self._trigger_window_candles(config.SIGNALS.rs_slow_window, trigger_timeframe),
            self._trigger_window_candles(config.SIGNALS.rs_fast_window, trigger_timeframe),
        ) + 1
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
        self.signal_cache["closed_candle_ts"] = latest_ts
        self.signal_cache["benchmark_ok"] = True
        btc_risk = self._btc_risk_context(benchmark_closes)
        self.signal_cache["btc_risk"] = btc_risk
        macro_context = self._gold_btc_rsi_context()
        self.signal_cache.setdefault("macro", {})["gold_btc_rsi"] = macro_context

        rows = {}

        def fetch_symbol_candles(symbol):
            try:
                candles = self._closed_candles(
                    symbol,
                    symbol_trigger_history_limit,
                    max_ts=latest_ts,
                    timeframe=trigger_timeframe,
                )
            except Exception as exc:
                return symbol, None, ("WARNING", f"Signal candles unavailable for {symbol}: {exc}", "signal_invalid", "symbol_candles_unavailable")

            if len(candles) < 2:
                return symbol, None, ("DEBUG", f"Signal skipped for {symbol}: not enough closed candles", "signal_invalid", "symbol_history_short")

            if int(candles[-1][0]) != latest_ts:
                return symbol, None, ("DEBUG", f"Signal skipped for {symbol}: candle is not aligned with BTC", "signal_invalid", "symbol_not_aligned_with_btc")

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
                return symbol, None, (
                    "WARNING",
                    f"EMA timeframe candles unavailable for {symbol}: {exc}",
                    "ema_signal_invalid",
                    f"ema_timeframe_candles_unavailable;macro_tf={macro_timeframe};pullback_tf={pullback_timeframe};trigger_tf={trigger_timeframe}"
                )

            return symbol, (candles, macro_candles, pullback_candles), None

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, len(self.symbols) or 1)) as executor:
            futures = {executor.submit(fetch_symbol_candles, symbol): symbol for symbol in self.symbols}
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    symbol = futures[future]
                    self._log_event("WARNING", f"Unhandled exception fetching candles for {symbol}: {exc}", event="signal_invalid", symbol=symbol, reason="unhandled_fetch_exception")

        for symbol, data, log_info in results:
            if log_info:
                level, msg, event, reason = log_info
                self._log_event(level, msg, event=event, symbol=symbol, reason=reason)
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
            state.last_btc_return_30m = self._safe_float(signal.get("btc_return_30m"), 0.0)

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
        self._log_event(
            "INFO",
            f"Signals updated for {len(rows)} futures symbols",
            event="signal_updated",
            reason=f"closed_ts={latest_ts}",
        )
        self._save_state()
        return True
