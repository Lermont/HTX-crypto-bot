# -*- coding: utf-8 -*-

import csv
import math
import threading
import time
from typing import Dict, List, Optional, Tuple

import config

from .models import ExitLadderConfig, ExitLadderPreflight, SellLadderParams, TradeState


class ExitStrategy:
    def _aggressive_exit_limit_price(self, symbol: str) -> float:
        reference_price, last_price = self._fetch_reference_price(symbol)
        price = reference_price or last_price
        if price <= 0:
            return 0.0
        if config.EXIT_SIDE == "sell":
            return self._price_at_or_below(symbol, price)
        return self._price_at_or_above(symbol, price)

    def _hard_stop_loss_rate(self, signal: Optional[dict] = None) -> Tuple[float, str]:
        strategy = config.STRATEGY
        fixed_pct = max(0.0, self._safe_float(strategy.hard_stop_loss_pct, 0.0))
        effective_pct = fixed_pct
        parts = [f"fixed_pct={fixed_pct:.6f}"]
        if strategy.hard_stop_loss_atr_enabled:
            atr_rate = max(0.0, self._safe_float((signal or {}).get("atr_rate"), 0.0))
            atr_multiplier = max(0.0, self._safe_float(strategy.hard_stop_loss_atr_multiplier, 0.0))
            atr_pct = atr_rate * atr_multiplier
            atr_max_pct = max(0.0, self._safe_float(strategy.hard_stop_loss_atr_max_pct, 0.0))
            if atr_max_pct > 0:
                atr_pct = min(atr_pct, atr_max_pct)
            effective_pct = max(effective_pct, atr_pct)
            parts.append(
                f"atr_enabled=1;atr_rate={atr_rate:.6f};"
                f"atr_multiplier={atr_multiplier:.3f};atr_pct={atr_pct:.6f};"
                f"atr_max_pct={atr_max_pct:.6f}"
            )
        else:
            parts.append("atr_enabled=0")
        return effective_pct, ";".join(parts)

    def _hard_stop_loss_trigger_price(self, symbol: str, state: TradeState, loss_rate: float) -> float:
        pct = max(0.0, self._safe_float(loss_rate, 0.0))
        if pct <= 0 or state.entry_price <= 0:
            return 0.0
        if config.POSITION_SIDE == "short":
            return self._price_at_or_above(symbol, state.entry_price * (1.0 + pct))
        return self._price_at_or_below(symbol, state.entry_price * (1.0 - pct))

    def _hard_stop_loss_signature(self, symbol: str, state: TradeState, amount: float, trigger_price: float) -> str:
        return (
            f"hard_stop_loss|direction={config.POSITION_SIDE}|side={config.EXIT_SIDE}|"
            f"amount={amount:.12f}|entry={state.entry_price:.12f}|"
            f"trigger={trigger_price:.12f}"
        )

    def _create_hard_stop_loss_order(self, symbol: str, amount: float, trigger_price: float) -> dict:
        return self._create_one_way_order(
            symbol=symbol,
            order_type="market",
            side=config.EXIT_SIDE,
            amount=amount,
            price=None,
            reduce_only=True,
            extra_params={"stopLossPrice": trigger_price},
        )

    def _force_hard_stop_loss_market_close(
        self,
        symbol: str,
        trigger_price: float,
        loss_rate: float,
        loss_rate_reason: str,
        trigger_exception: Optional[Exception] = None,
    ) -> bool:
        state = self._get_state(symbol)
        first_breach = state.sell_ladder_mode != "hard_stop_loss"
        state.frozen_no_more_buys = True
        state.sell_ladder_mode = "hard_stop_loss"
        state.sell_ladder_signature = ""
        state.hard_stop_signature = ""
        self._refresh_active_side(state)
        self._save_state()

        if first_breach:
            self._log_event(
                "WARNING",
                f"Hard stop-loss trigger is already crossed for {symbol}; switching to reduce-only market close",
                event="hard_stop_loss_trigger_crossed",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=trigger_price,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"hard_stop_loss_trigger_crossed;loss_rate={loss_rate:.6f};{loss_rate_reason}",
                exception=trigger_exception,
            )

        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason="hard_stop_loss_trigger_crossed")
        if state.sell_ladder_orders:
            self._cancel_sell_orders(symbol, reason="hard_stop_loss_trigger_crossed")
        if state.hard_stop_order:
            self._cancel_hard_stop_order(symbol, reason="hard_stop_loss_trigger_crossed")
        state = self._get_state(symbol)
        state.frozen_no_more_buys = True
        state.sell_ladder_mode = "hard_stop_loss"
        state.sell_ladder_signature = ""
        state.hard_stop_signature = ""
        self._refresh_active_side(state)
        self._save_state()

        if state.entry_orders or state.sell_ladder_orders or state.hard_stop_order:
            self._log_event(
                "WARNING",
                f"Hard stop-loss market close delayed for {symbol}: tracked order cancel did not fully clear",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=trigger_price,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"hard_stop_loss_market_close_cancel_failed;{loss_rate_reason}",
            )
            return True

        if not config.RUNTIME.reduce_only_enabled:
            self._log_event(
                "ERROR",
                f"Hard stop-loss market close blocked for {symbol}: reduce-only disabled",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=trigger_price,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"hard_stop_loss_market_close_reduce_only_disabled;{loss_rate_reason}",
            )
            return True

        raw_amount = max(0.0, state.position_size)
        amount = min(raw_amount, max(0.0, self._amount_to_precision(symbol, raw_amount)))
        if amount <= 0:
            self._log_event(
                "WARNING",
                f"Hard stop-loss market close delayed for {symbol}: close amount is below exchange minimum",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=trigger_price,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"hard_stop_loss_market_close_amount_below_minimum;{loss_rate_reason}",
            )
            return True

        try:
            order = self._create_one_way_order(
                symbol=symbol,
                order_type="market",
                side=config.EXIT_SIDE,
                amount=amount,
                price=None,
                reduce_only=True,
            )
            order_id = str(order.get("id") or "")
        except Exception as exc:
            closeable_rejected = self._is_reduce_only_amount_exceeds_closeable_error(exc)
            if closeable_rejected:
                state.position_available = 0.0
                state.position_frozen = max(state.position_frozen, state.position_size)
                self._refresh_active_side(state)
                self._save_state()
            self._log_event(
                "WARNING" if closeable_rejected else "ERROR",
                (
                    f"Hard stop-loss market close delayed for {symbol}: HTX reports no closeable amount"
                    if closeable_rejected
                    else f"Hard stop-loss market close failed for {symbol}: {exc}"
                ),
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=trigger_price,
                amount=amount,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=(
                    f"hard_stop_loss_market_close_closeable_rejected;{loss_rate_reason}"
                    if closeable_rejected
                    else f"hard_stop_loss_market_close_order_rejected;{loss_rate_reason}"
                ),
                exception=exc,
            )
            return True

        self._log_event(
            "WARNING",
            f"Hard stop-loss reduce-only market close placed for {symbol}: contracts={amount}",
            event="hard_stop_loss_market_close_placed",
            symbol=symbol,
            side=config.EXIT_SIDE,
            order_id=order_id,
            price=trigger_price,
            amount=amount,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"hard_stop_loss_trigger_crossed;loss_rate={loss_rate:.6f};{loss_rate_reason}",
        )
        return True

    def _ensure_hard_stop_loss(self, symbol: str, signal: Optional[dict] = None) -> bool:
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            if state.hard_stop_order:
                self._cancel_hard_stop_order(symbol, reason="hard_stop_loss_flat_position")
            return False

        loss_rate, loss_rate_reason = self._hard_stop_loss_rate(signal)
        trigger_price = self._hard_stop_loss_trigger_price(symbol, state, loss_rate)
        if state.sell_ladder_mode == "hard_stop_loss":
            return self._force_hard_stop_loss_market_close(
                symbol,
                trigger_price,
                loss_rate,
                loss_rate_reason,
            )

        if not config.STRATEGY.hard_stop_loss_enabled:
            if state.hard_stop_order:
                self._cancel_hard_stop_order(symbol, reason="hard_stop_loss_disabled")
            return False

        if not config.RUNTIME.reduce_only_enabled:
            self._log_event(
                "ERROR",
                f"Hard stop-loss blocked for {symbol}: reduce-only disabled",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason="hard_stop_loss_reduce_only_disabled",
            )
            state.frozen_no_more_buys = True
            self._refresh_active_side(state)
            self._save_state()
            return False

        amount = self._amount_to_precision(symbol, min(max(0.0, state.position_size), state.position_size))
        if amount <= 0 or trigger_price <= 0:
            return False

        signature = self._hard_stop_loss_signature(symbol, state, amount, trigger_price)
        if state.hard_stop_order and state.hard_stop_signature == signature:
            return False
        if state.hard_stop_order:
            self._cancel_hard_stop_order(symbol, reason="hard_stop_loss_rebuild")
            state = self._get_state(symbol)
            if state.hard_stop_order:
                return True

        order = {}
        order_id = ""
        order_exc: Optional[Exception] = None
        try:
            order = self._create_hard_stop_loss_order(symbol, amount, trigger_price)
            order_id = str(order.get("id") or "")
        except Exception as exc:
            order_exc = exc
            if self._is_reduce_only_amount_exceeds_closeable_error(exc) and state.sell_ladder_orders:
                self._log_event(
                    "WARNING",
                    f"Hard stop-loss for {symbol} is blocked by reserved closeable amount; canceling TP ladder and retrying stop first",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=config.EXIT_SIDE,
                    amount=amount,
                    price=trigger_price,
                    position_size=state.position_size,
                    entry_price=state.entry_price,
                    reason=f"hard_stop_loss_closeable_reserved_by_exit_ladder;{loss_rate_reason}",
                    exception=exc,
                )
                self._cancel_sell_orders(symbol, reason="hard_stop_loss_priority")
                state = self._get_state(symbol)
                if state.sell_ladder_orders:
                    self._log_event(
                        "WARNING",
                        f"Hard stop-loss delayed for {symbol}: TP ladder cancel did not fully clear",
                        event="reduce_only_violation_prevented",
                        symbol=symbol,
                        side=config.EXIT_SIDE,
                        amount=amount,
                        price=trigger_price,
                        position_size=state.position_size,
                        entry_price=state.entry_price,
                        reason=f"hard_stop_loss_priority_cancel_failed;{loss_rate_reason}",
                    )
                    return True
                try:
                    order = self._create_hard_stop_loss_order(symbol, amount, trigger_price)
                    order_id = str(order.get("id") or "")
                except Exception as retry_exc:
                    order_exc = retry_exc

        if not order_id and order_exc is not None:
            if self._is_hard_stop_loss_trigger_reached_error(order_exc):
                return self._force_hard_stop_loss_market_close(
                    symbol,
                    trigger_price,
                    loss_rate,
                    loss_rate_reason,
                    trigger_exception=order_exc,
                )
            state.frozen_no_more_buys = True
            self._refresh_active_side(state)
            self._save_state()
            self._log_event(
                "ERROR",
                f"Hard stop-loss order failed for {symbol}: {order_exc}",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=amount,
                price=trigger_price,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"hard_stop_loss_order_rejected;{loss_rate_reason}",
                exception=order_exc,
            )
            return False

        if not order_id:
            self._log_event(
                "ERROR",
                f"Hard stop-loss order for {symbol} returned no order id",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=amount,
                price=trigger_price,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason="hard_stop_loss_order_id_missing",
            )
            return False

        state.hard_stop_order = {
            "id": order_id,
            "side": config.EXIT_SIDE,
            "price": 0.0,
            "trigger_price": trigger_price,
            "amount": amount,
            "created_at": time.time(),
            "hard_stop_loss": True,
            "reduce_only": True,
            "loss_rate": loss_rate,
            "cancel_params": {"stopLossTakeProfit": True},
            "reason": "hard_stop_loss",
        }
        state.hard_stop_signature = signature
        self._refresh_active_side(state)
        self._save_state()
        self._log_event(
            "WARNING",
            f"Hard stop-loss placed for {symbol}: contracts={amount} trigger={trigger_price}",
            event="hard_stop_loss_placed",
            symbol=symbol,
            side=config.EXIT_SIDE,
            order_id=order_id,
            amount=amount,
            price=trigger_price,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"hard_stop_loss;loss_rate={loss_rate:.6f};{loss_rate_reason}",
        )
        return True

    def _sell_ladder_markups(self, mode: str = "normal") -> Tuple[float, ...]:
        if mode == "breakeven":
            return tuple(0.0 for _ in config.STRATEGY.ema_breakeven_exit_fractions)
        return tuple(config.STRATEGY.ema_take_profit_markup for _ in config.STRATEGY.ema_exit_ladder_fractions)

    def _reset_exit_runner_state(self, state: TradeState):
        state.exit_runner_active = False
        state.exit_runner_activated_at = None
        state.exit_runner_peak_price = 0.0
        state.exit_runner_bottom_price = 0.0
        state.exit_runner_contracts = 0.0

    def _is_time_exit_mode(self, mode: str) -> bool:
        return mode == "breakeven"

    def _is_managed_exit_mode(self, mode: str) -> bool:
        return self._is_time_exit_mode(mode) or mode in {"account_unload", "controlled_loss_exit", "urgent_time_exit"}

    def _position_initial_notional(self, symbol: str, state: Optional[TradeState], fallback_contracts: float, fallback_price: float) -> float:
        if not state:
            return self._contracts_to_notional(symbol, fallback_contracts, fallback_price) if symbol else 0.0

        initial = self._safe_float(getattr(state, "initial_entry_notional", 0.0), 0.0)
        if initial > 0:
            return initial

        leverage = max(self._safe_float(getattr(state, "leverage", 0.0), 0.0), self._safe_float(config.RISK.leverage, 1.0), 1.0)
        planned = self._safe_float(getattr(state, "planned_quote_budget", 0.0), 0.0) * leverage
        if planned > 0:
            return planned

        current = self._contracts_to_notional(symbol, fallback_contracts, fallback_price) if symbol else 0.0
        return current

    def _position_ratio_for_exit_ladder(
        self,
        symbol: str,
        state: Optional[TradeState],
        total_contracts: float,
        avg_entry_price: float,
    ) -> float:
        current = self._contracts_to_notional(symbol, total_contracts, avg_entry_price) if symbol else 0.0
        initial = self._position_initial_notional(symbol, state, total_contracts, avg_entry_price)
        if current <= 0 or initial <= 0:
            return 1.0
        return max(0.0, current / initial)

    def _exit_ladder_age_hours(self, state: Optional[TradeState]) -> float:
        if not state:
            return 0.0
        return self._position_held_minutes(state) / 60.0

    def _adaptive_exit_ladder_name(self, position_ratio: float) -> str:
        strategy = config.STRATEGY
        medium_threshold = max(0.0, strategy.ema_exit_medium_position_ratio)
        heavy_threshold = max(medium_threshold, strategy.ema_exit_heavy_position_ratio)
        if position_ratio <= medium_threshold:
            return "normal"
        if position_ratio <= heavy_threshold:
            return "medium"
        return "heavy"

    def _base_adaptive_exit_ladder(self, ladder_name: str) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
        strategy = config.STRATEGY
        if ladder_name == "heavy":
            return tuple(strategy.ema_exit_heavy_ladder_fractions), tuple(strategy.ema_exit_heavy_ladder_markups)
        if ladder_name == "medium":
            return tuple(strategy.ema_exit_medium_ladder_fractions), tuple(strategy.ema_exit_medium_ladder_markups)
        return tuple(strategy.ema_exit_normal_ladder_fractions), tuple(strategy.ema_exit_normal_ladder_markups)

    def _merge_exit_ladder_steps(self, steps: List[dict]) -> List[dict]:
        merged: List[dict] = []
        for step in steps:
            if step.get("runner"):
                merged.append(dict(step))
                continue
            markup = self._safe_float(step.get("markup"), 0.0)
            if merged and not merged[-1].get("runner") and abs(self._safe_float(merged[-1].get("markup"), 0.0) - markup) <= 1e-12:
                merged[-1]["fraction"] = self._safe_float(merged[-1].get("fraction"), 0.0) + self._safe_float(step.get("fraction"), 0.0)
            else:
                merged.append(dict(step))
        return merged

    def _controlled_loss_cached_signal(self, symbol: str, signal: Optional[dict] = None) -> dict:
        if isinstance(signal, dict):
            return signal
        cache = getattr(self, "signal_cache", {}) or {}
        symbols = cache.get("symbols") if isinstance(cache, dict) else {}
        cached = symbols.get(symbol) if isinstance(symbols, dict) and symbol else None
        return cached if isinstance(cached, dict) else {}

    def _controlled_loss_signal_pressure_context(self, symbol: str = "", signal: Optional[dict] = None) -> dict:
        strategy = config.STRATEGY
        signal = self._controlled_loss_cached_signal(symbol, signal)
        directional_gap = 0.0
        signal_reason = "signal_unavailable"
        data_valid = False
        if signal and bool(signal.get("data_valid", True)):
            for key in ("trend_ema_gap", "macro_gap"):
                if key in signal:
                    directional_gap = self._safe_float(signal.get(key), 0.0)
                    signal_reason = key
                    data_valid = True
                    break

        adverse_gap = max(0.0, -directional_gap)
        reference = max(1e-12, self._safe_float(strategy.controlled_loss_macro_gap_reference, 0.0))
        adverse_intensity = self._clamp(adverse_gap / reference, 0.0, 1.0)
        return {
            "data_valid": data_valid,
            "adverse_gap": adverse_gap,
            "adverse_intensity": adverse_intensity,
            "directional_gap": directional_gap,
            "signal_reason": signal_reason,
        }

    def _controlled_loss_macro_pressure_context(self, symbol: str = "", signal: Optional[dict] = None) -> dict:
        strategy = config.STRATEGY
        signal_pressure = self._controlled_loss_signal_pressure_context(symbol, signal)
        signal_intensity = self._safe_float(signal_pressure.get("adverse_intensity"), 0.0)
        directional_gap = self._safe_float(signal_pressure.get("directional_gap"), 0.0)
        signal_reason = str(signal_pressure.get("signal_reason") or "signal_unavailable")

        macro_context = self._macro_guard_context()
        overlay_intensity = 0.0
        time_exit_multiplier = self._safe_float(macro_context.get("time_exit_multiplier"), 1.0)
        if time_exit_multiplier < 1.0:
            overlay_intensity = max(overlay_intensity, self._clamp(1.0 - time_exit_multiplier, 0.0, 1.0))
        budget_key = "short_budget_multiplier" if config.POSITION_SIDE == "short" else "long_budget_multiplier"
        budget_multiplier = self._safe_float(macro_context.get(budget_key), 1.0)
        if budget_multiplier < 1.0:
            overlay_intensity = max(overlay_intensity, self._clamp(1.0 - budget_multiplier, 0.0, 1.0))

        intensity = max(signal_intensity, overlay_intensity)
        max_speed = max(1.0, self._safe_float(strategy.controlled_loss_macro_max_speed_multiplier, 1.0))
        speed_multiplier = 1.0 + intensity * (max_speed - 1.0)
        return {
            "macro_intensity": intensity,
            "signal_intensity": signal_intensity,
            "overlay_intensity": overlay_intensity,
            "speed_multiplier": speed_multiplier,
            "directional_gap": directional_gap,
            "macro_regime": str(macro_context.get("regime") or "neutral"),
            "macro_reason": str(macro_context.get("reason") or signal_reason),
            "signal_reason": signal_reason,
            "time_exit_multiplier": time_exit_multiplier,
            "budget_multiplier": budget_multiplier,
        }

    def _controlled_loss_volatility_pressure_context(self, symbol: str = "", signal: Optional[dict] = None) -> dict:
        strategy = config.STRATEGY
        signal = self._controlled_loss_cached_signal(symbol, signal)
        signal_pressure = self._controlled_loss_signal_pressure_context(symbol, signal)
        adverse_intensity = self._safe_float(signal_pressure.get("adverse_intensity"), 0.0)
        reference = self._safe_float(getattr(strategy, "controlled_loss_volatility_reference", 0.0), 0.0)
        if reference <= 0:
            reference = self._safe_float(getattr(strategy, "volatility_reference", 0.0), 0.0)
        reference = max(reference, 1e-12)

        if not getattr(strategy, "controlled_loss_volatility_speed_enabled", True):
            return {
                "volatility_intensity": 0.0,
                "volatility_adverse_intensity": adverse_intensity,
                "volatility_speed_multiplier": 1.0,
                "volatility_exponential_curve": 1.0,
                "volatility_ratio": 0.0,
                "local_volatility": 0.0,
                "atr_rate": 0.0,
                "daily_volatility_multiplier": 1.0,
                "volatility_reference": reference,
                "volatility_trigger_multiplier": 0.0,
                "volatility_reason": "disabled",
            }

        volatility = max(0.0, self._safe_float(signal.get("volatility"), 0.0))
        atr_rate = max(0.0, self._safe_float(signal.get("atr_rate"), 0.0))
        local_volatility = max(volatility, atr_rate)
        volatility_ratio = local_volatility / reference if local_volatility > 0 else 0.0

        daily_volatility_multiplier = max(0.0, self._safe_float(signal.get("daily_volatility_multiplier"), 1.0))
        if daily_volatility_multiplier > 1.0:
            volatility_ratio = max(volatility_ratio, daily_volatility_multiplier)

        signal_volatility_multiplier = max(0.0, self._safe_float(signal.get("volatility_multiplier"), 1.0))
        if signal_volatility_multiplier > 1.0:
            volatility_ratio = max(volatility_ratio, signal_volatility_multiplier)

        trigger = max(0.0, self._safe_float(getattr(strategy, "controlled_loss_volatility_trigger_multiplier", 1.5), 1.5))
        if trigger <= 0:
            raw_intensity = volatility_ratio
        else:
            raw_intensity = max(0.0, (volatility_ratio / trigger) - 1.0)
        volatility_intensity = self._clamp(raw_intensity, 0.0, 1.0) * self._clamp(adverse_intensity, 0.0, 1.0)

        max_speed = max(1.0, self._safe_float(getattr(strategy, "controlled_loss_volatility_max_speed_multiplier", 1.0), 1.0))
        speed_multiplier = max_speed ** volatility_intensity
        max_curve = max(1.0, self._safe_float(getattr(strategy, "controlled_loss_volatility_exponent", 1.0), 1.0))
        exponential_curve = 1.0 + volatility_intensity * (max_curve - 1.0)
        if volatility_intensity <= 0:
            reason = (
                "volatility_not_adverse"
                if adverse_intensity <= 0
                else "volatility_below_trigger"
            )
        else:
            reason = "adverse_volatility_spike"

        return {
            "volatility_intensity": volatility_intensity,
            "volatility_adverse_intensity": adverse_intensity,
            "volatility_speed_multiplier": speed_multiplier,
            "volatility_exponential_curve": exponential_curve,
            "volatility_ratio": volatility_ratio,
            "local_volatility": local_volatility,
            "atr_rate": atr_rate,
            "daily_volatility_multiplier": daily_volatility_multiplier,
            "volatility_reference": reference,
            "volatility_trigger_multiplier": trigger,
            "volatility_reason": reason,
        }

    def _controlled_loss_ramp_context(
        self,
        state: Optional[TradeState],
        symbol: str = "",
        signal: Optional[dict] = None,
    ) -> dict:
        if state is None:
            return {
                "move_fraction": 0.0,
                "elapsed_minutes": 0.0,
                "ramp_minutes": 0.0,
                "min_move_fraction": 0.0,
                "macro_intensity": 0.0,
                "signal_intensity": 0.0,
                "overlay_intensity": 0.0,
                "speed_multiplier": 1.0,
                "macro_speed_multiplier": 1.0,
                "volatility_intensity": 0.0,
                "volatility_speed_multiplier": 1.0,
                "volatility_exponential_curve": 1.0,
                "volatility_ratio": 0.0,
                "local_volatility": 0.0,
                "atr_rate": 0.0,
                "daily_volatility_multiplier": 1.0,
                "volatility_reference": 0.0,
                "volatility_trigger_multiplier": 0.0,
                "volatility_reason": "state_unavailable",
                "ramp_profile": "linear",
                "linear_progress": 0.0,
                "effective_progress": 0.0,
                "directional_gap": 0.0,
                "hard_move_fraction": 0.0,
                "macro_regime": "unavailable",
                "macro_reason": "state_unavailable",
                "signal_reason": "state_unavailable",
            }

        strategy = config.STRATEGY
        min_move = self._clamp(
            self._safe_float(getattr(strategy, "controlled_loss_min_move_fraction", 0.1), 0.1),
            0.0,
            1.0,
        )
        ramp_minutes = max(0.0, self._safe_float(getattr(strategy, "controlled_loss_ramp_minutes", 24.0 * 60.0), 0.0))
        pressure = self._controlled_loss_macro_pressure_context(symbol, signal)
        volatility_pressure = self._controlled_loss_volatility_pressure_context(symbol, signal)
        macro_speed_multiplier = max(1.0, self._safe_float(pressure.get("speed_multiplier"), 1.0))
        volatility_speed_multiplier = max(1.0, self._safe_float(volatility_pressure.get("volatility_speed_multiplier"), 1.0))
        speed_multiplier = macro_speed_multiplier * volatility_speed_multiplier
        move_fraction = min_move
        elapsed_minutes = 0.0
        linear_progress = 0.0
        effective_progress = 0.0
        ramp_profile = "linear"

        if state.sell_ladder_mode == "controlled_loss_exit" and state.time_exit_activated_at:
            elapsed_minutes = max(0.0, (time.time() - state.time_exit_activated_at) / 60.0)
            linear_progress = 1.0 if ramp_minutes <= 0 else (elapsed_minutes * speed_multiplier) / ramp_minutes
            effective_progress = linear_progress
            volatility_intensity = self._safe_float(volatility_pressure.get("volatility_intensity"), 0.0)
            if volatility_intensity > 0.0 and linear_progress > 0.0:
                if linear_progress >= 1.0:
                    effective_progress = 1.0
                else:
                    curve = max(1.0, self._safe_float(volatility_pressure.get("volatility_exponential_curve"), 1.0))
                    exponential_progress = 1.0 - math.exp(-linear_progress * curve)
                    effective_progress = max(linear_progress, exponential_progress)
                ramp_profile = "exponential_volatility"
            time_move = min_move + (1.0 - min_move) * min(effective_progress, 1.0)
            move_fraction = max(move_fraction, time_move)

        hard_move = 0.0
        if self._hard_time_exit_elapsed(state):
            step_minutes = max(0.0, strategy.hard_time_exit_step_minutes)
            step_increase = max(0.0, strategy.hard_time_exit_fraction_step)
            if step_minutes > 0 and step_increase > 0:
                overdue_minutes = max(
                    0.0,
                    self._position_held_minutes(state) - max(0.0, strategy.hard_time_exit_after_minutes),
                )
                hard_move = min_move + (overdue_minutes // step_minutes) * step_increase
                move_fraction = max(move_fraction, hard_move)

        context = dict(pressure)
        context["macro_speed_multiplier"] = macro_speed_multiplier
        context["speed_multiplier"] = speed_multiplier
        context.update(volatility_pressure)
        context.update(
            {
                "move_fraction": self._clamp(move_fraction, 0.0, 1.0),
                "elapsed_minutes": elapsed_minutes,
                "ramp_minutes": ramp_minutes,
                "min_move_fraction": min_move,
                "hard_move_fraction": self._clamp(hard_move, 0.0, 1.0),
                "ramp_profile": ramp_profile,
                "linear_progress": self._clamp(linear_progress, 0.0, 1.0),
                "effective_progress": self._clamp(effective_progress, 0.0, 1.0),
            }
        )
        return context

    def _controlled_loss_move_fraction(
        self,
        state: Optional[TradeState],
        symbol: str = "",
        signal: Optional[dict] = None,
    ) -> float:
        return self._safe_float(
            self._controlled_loss_ramp_context(state, symbol=symbol, signal=signal).get("move_fraction"),
            0.0,
        )

    def _sell_ladder_plan(
        self,
        symbol: str,
        total_contracts: float,
        avg_entry_price: float,
        mode: str = "normal",
        state: Optional[TradeState] = None,
        use_trailing_exit: bool = True,
        signal: Optional[dict] = None,
    ) -> Tuple[List[dict], dict]:
        strategy = config.STRATEGY
        if state is None and symbol:
            state = self._get_state(symbol)

        age_hours = self._exit_ladder_age_hours(state)
        position_ratio = self._position_ratio_for_exit_ladder(symbol, state, total_contracts, avg_entry_price)
        context = {
            "ladder_name": mode,
            "position_ratio": position_ratio,
            "position_age_hours": age_hours,
            "runner_enabled": False,
            "runner_fraction": 0.0,
        }

        if mode == "breakeven":
            fractions = tuple(strategy.ema_breakeven_exit_fractions)
            steps = [{"fraction": fraction, "markup": 0.0, "runner": False} for fraction in fractions]
            context["ladder_name"] = "breakeven"
            return steps, context

        if mode == "account_unload":
            context["ladder_name"] = "account_unload"
            return [{"fraction": 1.0, "markup": 0.0, "runner": False}], context

        if mode == "controlled_loss_exit":
            fractions = tuple(strategy.ema_exit_ladder_fractions)
            ramp_context = self._controlled_loss_ramp_context(state, symbol=symbol, signal=signal)
            move_fraction = self._safe_float(ramp_context.get("move_fraction"), 0.0)
            steps = [
                {"fraction": fraction, "markup": move_fraction, "runner": False}
                for fraction in fractions
            ]
            context["ladder_name"] = "controlled_loss"
            context.update(
                {
                    "controlled_loss_move_fraction": move_fraction,
                    "controlled_loss_macro_intensity": self._safe_float(ramp_context.get("macro_intensity"), 0.0),
                    "controlled_loss_signal_intensity": self._safe_float(ramp_context.get("signal_intensity"), 0.0),
                    "controlled_loss_overlay_intensity": self._safe_float(ramp_context.get("overlay_intensity"), 0.0),
                    "controlled_loss_speed_multiplier": self._safe_float(ramp_context.get("speed_multiplier"), 1.0),
                    "controlled_loss_macro_speed_multiplier": self._safe_float(ramp_context.get("macro_speed_multiplier"), 1.0),
                    "controlled_loss_volatility_intensity": self._safe_float(ramp_context.get("volatility_intensity"), 0.0),
                    "controlled_loss_volatility_speed_multiplier": self._safe_float(ramp_context.get("volatility_speed_multiplier"), 1.0),
                    "controlled_loss_volatility_exponential_curve": self._safe_float(ramp_context.get("volatility_exponential_curve"), 1.0),
                    "controlled_loss_volatility_ratio": self._safe_float(ramp_context.get("volatility_ratio"), 0.0),
                    "controlled_loss_local_volatility": self._safe_float(ramp_context.get("local_volatility"), 0.0),
                    "controlled_loss_atr_rate": self._safe_float(ramp_context.get("atr_rate"), 0.0),
                    "controlled_loss_daily_volatility_multiplier": self._safe_float(ramp_context.get("daily_volatility_multiplier"), 1.0),
                    "controlled_loss_volatility_reference": self._safe_float(ramp_context.get("volatility_reference"), 0.0),
                    "controlled_loss_volatility_trigger_multiplier": self._safe_float(ramp_context.get("volatility_trigger_multiplier"), 0.0),
                    "controlled_loss_linear_progress": self._safe_float(ramp_context.get("linear_progress"), 0.0),
                    "controlled_loss_effective_progress": self._safe_float(ramp_context.get("effective_progress"), 0.0),
                    "controlled_loss_directional_gap": self._safe_float(ramp_context.get("directional_gap"), 0.0),
                    "controlled_loss_elapsed_minutes": self._safe_float(ramp_context.get("elapsed_minutes"), 0.0),
                    "controlled_loss_ramp_minutes": self._safe_float(ramp_context.get("ramp_minutes"), 0.0),
                    "controlled_loss_macro_regime": str(ramp_context.get("macro_regime") or "neutral"),
                    "controlled_loss_macro_reason": str(ramp_context.get("macro_reason") or "neutral"),
                    "controlled_loss_volatility_reason": str(ramp_context.get("volatility_reason") or "neutral"),
                    "controlled_loss_ramp_profile": str(ramp_context.get("ramp_profile") or "linear"),
                }
            )
            return steps, context

        if mode != "normal" or not strategy.ema_adaptive_exit_enabled:
            fractions = tuple(strategy.ema_exit_ladder_fractions)
            markups = self._sell_ladder_markups(mode)
            steps = [
                {"fraction": fraction, "markup": max(0.0, markup), "runner": False}
                for fraction, markup in zip(fractions, markups)
            ]
            context["ladder_name"] = "legacy"
            return steps, context

        external_exit = self._external_exit_tighten_context(symbol)
        if external_exit.get("tighten"):
            settings = config.EXTERNAL_PRICE_FEED
            steps = []
            runner_fraction = 0.0
            for fraction, markup in zip(settings.tightened_ladder_fractions, settings.tightened_ladder_markups):
                fraction = max(0.0, self._safe_float(fraction, 0.0))
                if markup is None:
                    runner_fraction += fraction
                    continue
                steps.append({"fraction": fraction, "markup": max(0.0, self._safe_float(markup, 0.0)), "runner": False})
            if runner_fraction > 0:
                steps.append({"fraction": runner_fraction, "markup": 0.0, "runner": True})
                context["runner_enabled"] = True
                context["runner_fraction"] = runner_fraction
            context["ladder_name"] = "external_tightened"
            context["external_spread_bps"] = external_exit.get("spread_bps", 0.0)
            context["external_reason"] = external_exit.get("reason", "external_exit_tightened")
            return self._merge_exit_ladder_steps(steps), context

        ladder_name = self._adaptive_exit_ladder_name(position_ratio)
        fractions, markups = self._base_adaptive_exit_ladder(ladder_name)
        context["ladder_name"] = ladder_name

        first_cap_after = max(0.0, strategy.ema_exit_decay_first_markup_after_hours)
        first_markup_cap = max(0.0, strategy.ema_exit_decay_first_markup_cap)
        max_markup_after = max(0.0, strategy.ema_exit_decay_max_markup_after_hours)
        max_markup = max(0.0, strategy.ema_exit_decay_max_markup)
        runner_allowed = bool(
            strategy.ema_exit_runner_enabled
            and ladder_name == "normal"
            and len(fractions) > 1
            and (max_markup_after <= 0 or age_hours < max_markup_after)
        )

        fixed_steps: List[dict] = []
        runner_fraction = 0.0
        trailing_enabled = bool(strategy.ema_exit_trailing_enabled and use_trailing_exit)
        if trailing_enabled and runner_allowed:
            fixed_fraction = self._clamp(strategy.ema_exit_trailing_fixed_fraction, 0.0, 1.0)
            fixed_markup = max(0.0, markups[0] if markups else strategy.ema_take_profit_markup)
            if first_cap_after > 0 and first_markup_cap > 0 and age_hours >= first_cap_after:
                fixed_markup = min(fixed_markup, first_markup_cap)
            fixed_steps.append({"fraction": fixed_fraction, "markup": fixed_markup, "runner": False})
            runner_fraction = max(0.0, 1.0 - fixed_fraction)
            if runner_fraction > 0:
                fixed_steps.append({"fraction": runner_fraction, "markup": 0.0, "runner": True})
                context["runner_enabled"] = True
                context["runner_fraction"] = runner_fraction
                context["trailing_exit"] = True
            return self._merge_exit_ladder_steps(fixed_steps), context

        for index, (fraction, markup) in enumerate(zip(fractions, markups)):
            fraction = max(0.0, fraction)
            markup = max(0.0, markup)
            is_runner_stage = runner_allowed and index == len(fractions) - 1
            if is_runner_stage:
                runner_fraction += fraction
                continue

            if first_cap_after > 0 and first_markup_cap > 0 and age_hours >= first_cap_after and fixed_steps == []:
                markup = min(markup, first_markup_cap)
            if max_markup_after > 0 and max_markup > 0 and age_hours >= max_markup_after:
                markup = min(markup, max_markup)
            fixed_steps.append({"fraction": fraction, "markup": markup, "runner": False})

        if runner_fraction > 0:
            fixed_steps.append({"fraction": runner_fraction, "markup": 0.0, "runner": True})
            context["runner_enabled"] = True
            context["runner_fraction"] = runner_fraction

        return self._merge_exit_ladder_steps(fixed_steps), context

    def _exit_ladder_contract_allocations(
        self,
        symbol: str,
        ladder_contracts: float,
        steps: List[dict],
        state: Optional[TradeState],
    ) -> Tuple[List[Tuple[int, dict, float]], float]:
        fixed_steps = [(index, step) for index, step in enumerate(steps, start=1) if not step.get("runner")]
        fixed_fraction_total = sum(self._safe_float(step.get("fraction"), 0.0) for _, step in fixed_steps)
        runner_fraction_total = sum(self._safe_float(step.get("fraction"), 0.0) for step in steps if step.get("runner"))

        fixed_contracts = ladder_contracts
        runner_contracts = 0.0
        if runner_fraction_total > 0 and fixed_fraction_total > 0:
            existing_runner = self._safe_float(getattr(state, "exit_runner_contracts", 0.0), 0.0) if state else 0.0
            if existing_runner > 0:
                runner_contracts = self._amount_to_precision(symbol, min(ladder_contracts, existing_runner))
                fixed_contracts = self._amount_to_precision(symbol, max(0.0, ladder_contracts - runner_contracts))
            else:
                fixed_target = self._amount_to_precision(symbol, ladder_contracts * min(1.0, fixed_fraction_total))
                if 0 < fixed_target < ladder_contracts:
                    fixed_contracts = fixed_target
                    runner_contracts = self._amount_to_precision(symbol, max(0.0, ladder_contracts - fixed_contracts))

        if fixed_contracts <= 0 and runner_contracts <= 0 and ladder_contracts > 0:
            fixed_contracts = ladder_contracts
            runner_contracts = 0.0

        allocations: List[Tuple[int, dict, float]] = []
        allocated = 0.0
        for item_index, (stage_index, step) in enumerate(fixed_steps):
            remaining = max(0.0, fixed_contracts - allocated)
            if remaining <= 0:
                break
            if item_index == len(fixed_steps) - 1 or fixed_fraction_total <= 0:
                contracts = self._amount_to_precision(symbol, remaining)
            else:
                stage_fraction = self._safe_float(step.get("fraction"), 0.0) / fixed_fraction_total
                contracts = self._amount_to_precision(symbol, min(fixed_contracts * stage_fraction, remaining))
            if contracts <= 0:
                continue
            if allocated + contracts > fixed_contracts:
                contracts = self._amount_to_precision(symbol, max(0.0, fixed_contracts - allocated))
            if contracts <= 0:
                continue
            allocated += contracts
            allocations.append((stage_index, step, contracts))

        return allocations, runner_contracts

    def _is_adverse_profit_floor_funding(self, funding_rate: float) -> bool:
        strategy = config.STRATEGY
        if config.POSITION_SIDE == "short":
            return funding_rate <= strategy.funding_negative_threshold
        return funding_rate >= strategy.funding_positive_threshold

    def _dynamic_profit_floor(
        self,
        base_profit_floor: float,
        daily_volatility_multiplier: float,
        funding_rate: float,
        mode: str,
    ) -> Tuple[float, float, str]:
        strategy = config.STRATEGY
        if not strategy.enable_dynamic_profit_floor:
            return base_profit_floor, 1.0, "disabled"

        multiplier = 1.0
        reasons = []
        threshold = max(0.0, strategy.dynamic_profit_floor_volatility_multiplier_threshold)
        if threshold > 0 and daily_volatility_multiplier >= threshold:
            multiplier *= self._clamp(strategy.dynamic_profit_floor_high_vol_multiplier, 0.0, 1.0)
            reasons.append("high_daily_volatility")
        if self._is_adverse_profit_floor_funding(funding_rate):
            multiplier *= self._clamp(strategy.dynamic_profit_floor_adverse_funding_multiplier, 0.0, 1.0)
            reasons.append("adverse_funding")
        if mode == "urgent_time_exit":
            multiplier *= self._clamp(strategy.dynamic_profit_floor_urgent_multiplier, 0.0, 1.0)
            reasons.append("urgent_time_exit")

        minimum = max(0.0, strategy.dynamic_profit_floor_min_rate)
        return max(minimum, base_profit_floor * multiplier), multiplier, "+".join(reasons) if reasons else "neutral"

    def _sell_ladder_signature(
        self,
        mode: str = "normal",
        symbol: str = "",
        state: Optional[TradeState] = None,
        total_contracts: Optional[float] = None,
        avg_entry_price: Optional[float] = None,
        use_trailing_exit: bool = True,
        signal: Optional[dict] = None,
    ) -> str:
        if state is None and symbol:
            state = self._get_state(symbol)
        if total_contracts is None:
            total_contracts = self._safe_float(getattr(state, "position_size", 0.0), 0.0) if state else 1.0
        if avg_entry_price is None:
            avg_entry_price = self._safe_float(getattr(state, "entry_price", 0.0), 0.0) if state else 1.0
        if total_contracts <= 0:
            total_contracts = 1.0
        if avg_entry_price <= 0:
            avg_entry_price = 1.0

        steps, plan_context = self._sell_ladder_plan(
            symbol,
            total_contracts,
            avg_entry_price,
            mode=mode,
            state=state,
            use_trailing_exit=use_trailing_exit,
            signal=signal,
        )
        plan = ",".join(
            f"{self._safe_float(step.get('fraction'), 0.0):.8f}@runner"
            if step.get("runner")
            else f"{self._safe_float(step.get('fraction'), 0.0):.8f}@{self._safe_float(step.get('markup'), 0.0):.8f}"
            for step in steps
        )
        strategy = config.STRATEGY
        trailing_enabled = bool(strategy.ema_exit_trailing_enabled and use_trailing_exit)
        controlled_loss_signature = ""
        if mode == "controlled_loss_exit":
            controlled_loss_signature = (
                f"|controlled_loss_ramp={strategy.controlled_loss_min_move_fraction:.8f}:"
                f"{strategy.controlled_loss_ramp_minutes:.4f}:"
                f"{strategy.controlled_loss_reprice_minutes:.4f}:"
                f"{strategy.controlled_loss_macro_gap_reference:.8f}:"
                f"{strategy.controlled_loss_macro_max_speed_multiplier:.4f}:"
                f"{int(strategy.controlled_loss_volatility_speed_enabled)}:"
                f"{strategy.controlled_loss_volatility_reference:.8f}:"
                f"{strategy.controlled_loss_volatility_trigger_multiplier:.4f}:"
                f"{strategy.controlled_loss_volatility_max_speed_multiplier:.4f}:"
                f"{strategy.controlled_loss_volatility_exponent:.4f}:"
                f"{strategy.controlled_loss_volatility_reprice_min_move_delta:.8f}"
            )
        return (
            f"{mode}|strategy=ema_pullback|direction={config.POSITION_SIDE}|exit_side={config.EXIT_SIDE}|"
            f"plan={plan}|ladder={plan_context.get('ladder_name', mode)}|"
            f"adaptive={int(strategy.ema_adaptive_exit_enabled)}|"
            f"ratio={plan_context.get('position_ratio', 1.0):.4f}|"
            f"runner={int(plan_context.get('runner_enabled', False))}|"
            f"trailing={int(trailing_enabled)}:"
            f"{strategy.ema_exit_trailing_fixed_fraction:.8f}:"
            f"{strategy.ema_exit_trailing_activation_markup:.8f}:"
            f"{strategy.ema_exit_trailing_pullback:.8f}:"
            f"{strategy.ema_exit_trailing_take_profit_markup:.8f}|"
            f"external_spread={plan_context.get('external_spread_bps', 0.0):.4f}|"
            f"tp={strategy.ema_take_profit_markup:.8f}|"
            f"breakeven_after={strategy.ema_breakeven_after_hours:.4f}|"
            f"breakeven_reprice={strategy.ema_breakeven_reprice_minutes:.4f}|"
            f"breakeven_buffer={strategy.ema_breakeven_fee_buffer:.8f}|"
            f"decay_first={strategy.ema_exit_decay_first_markup_after_hours:.4f}:"
            f"{strategy.ema_exit_decay_first_markup_cap:.8f}|"
            f"decay_max={strategy.ema_exit_decay_max_markup_after_hours:.4f}:"
            f"{strategy.ema_exit_decay_max_markup:.8f}"
            f"{controlled_loss_signature}"
        )

    def _should_use_split_exit_ladder(self, symbol: str, state: Optional[TradeState], mode: str) -> bool:
        if mode != "normal" or not state or state.position_size <= 0:
            return False
        self._ensure_entry_buckets_initialized(symbol, state)
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)
        base_contracts = max(0.0, min(state.base_entry_amount, state.position_size))
        recovery_contracts = max(0.0, min(state.averaging_entry_amount, max(0.0, state.position_size - base_contracts)))
        return (
            base_contracts > eps
            and recovery_contracts > eps
            and state.base_entry_price > 0
        )

    def _split_sell_ladder_signature(self, symbol: str, state: TradeState) -> str:
        self._ensure_entry_buckets_initialized(symbol, state)
        base_contracts = max(0.0, min(state.base_entry_amount, state.position_size))
        recovery_contracts = max(0.0, min(state.averaging_entry_amount, max(0.0, state.position_size - base_contracts)))
        base_price = state.base_entry_price or state.entry_price
        base_signature = self._sell_ladder_signature(
            "normal",
            symbol,
            state,
            total_contracts=base_contracts,
            avg_entry_price=base_price,
            use_trailing_exit=False,
        )
        recovery_price = self._sell_price_floor(
            symbol,
            base_price,
            0.0,
            context=self._sell_ladder_context(symbol, mode="normal"),
        )
        return (
            f"{base_signature}|split_exit=1|"
            f"base_contracts={base_contracts:.12f}|"
            f"average_recovery_contracts={recovery_contracts:.12f}|"
            f"base_entry_price={base_price:.12f}|"
            f"average_recovery_price={recovery_price:.12f}"
        )

    def _exit_ladder_signature(
        self,
        mode: str = "normal",
        symbol: str = "",
        state: Optional[TradeState] = None,
        total_contracts: Optional[float] = None,
        avg_entry_price: Optional[float] = None,
    ) -> str:
        if state is None and symbol:
            state = self._get_state(symbol)
        if symbol and self._should_use_split_exit_ladder(symbol, state, mode):
            return self._split_sell_ladder_signature(symbol, state)
        return self._sell_ladder_signature(mode, symbol, state, total_contracts, avg_entry_price)

    def _pending_exit_ladder_signature(
        self,
        mode: str = "normal",
        symbol: str = "",
        state: Optional[TradeState] = None,
        total_contracts: Optional[float] = None,
        avg_entry_price: Optional[float] = None,
    ) -> str:
        return f"pending_closeable:{self._exit_ladder_signature(mode, symbol, state, total_contracts, avg_entry_price)}"

    def _is_exit_ladder_waiting_for_closeable(
        self,
        symbol: str,
        mode: str = "normal",
        state: Optional[TradeState] = None,
    ) -> bool:
        if state is None:
            state = self._get_state(symbol)
        if state.sell_ladder_orders:
            return False
        if state.position_available > 0:
            return False
        current_signature = str(state.sell_ladder_signature or "")
        expected_signature = self._pending_exit_ladder_signature(mode, symbol, state)
        mode_pending_prefix = f"pending_closeable:{mode}|"
        if current_signature != expected_signature and not current_signature.startswith(mode_pending_prefix):
            return False

        now = time.time()
        pending_since = self._safe_float(getattr(state, "pending_exit_ladder_since", 0.0), 0.0)
        if pending_since <= 0:
            state.pending_exit_ladder_since = now
            self._save_state()
            return True

        retry_after = max(
            1.0,
            self._safe_float(getattr(config.RUNTIME, "order_timeout_sec", 0.0), 0.0),
            self._safe_float(getattr(config.RUNTIME, "poll_interval_sec", 0.0), 0.0),
        )
        # Give it up to 5 minutes to clear naturally, then force a "reset" by canceling all orders
        force_reset_after = 300.0
        elapsed = now - pending_since
        if elapsed < retry_after:
            return True

        if state.position_available <= 0 and state.position_frozen > 0:
            if elapsed > force_reset_after:
                self._log_event(
                    "WARNING",
                    f"Forcing exit ladder rebuild for {symbol}: position is still reserved after {elapsed:.1f}s; canceling all orders to clear state",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    reason="pending_closeable_force_reset",
                )
                self._cancel_all_orders(symbol, reason="pending_closeable_force_reset")
                state.sell_ladder_signature = ""
                self._clear_pending_exit_ladder(state)
                self._save_state()
                return False

            state.pending_exit_ladder_since = pending_since  # Keep original start time
            self._refresh_active_side(state)
            self._save_state()
            self._log_event(
                "WARNING",
                f"Delayed {config.EXIT_SIDE} exit ladder for {symbol} is still waiting ({elapsed:.1f}s): HTX reports the position as fully reserved",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                reason=f"{state.pending_exit_ladder_reason or 'closeable_amount_reserved_by_existing_exit_orders'};pending_closeable_still_reserved",
            )
            return True

        state.sell_ladder_signature = ""
        self._clear_pending_exit_ladder(state)
        self._refresh_active_side(state)
        self._save_state()
        self._log_event(
            "WARNING",
            f"Retrying delayed {config.EXIT_SIDE} exit ladder for {symbol}: closeable amount is still unavailable",
            event="reduce_only_violation_prevented",
            symbol=symbol,
            side=config.EXIT_SIDE,
            position_size=state.position_size,
            reason="pending_closeable_retry",
        )
        return False

    def _mark_exit_ladder_waiting_for_closeable(
        self,
        symbol: str,
        mode: str,
        reason: str,
        amount: float = 0.0,
        exception: Optional[Exception] = None,
    ):
        state = self._get_state(symbol)
        state.sell_ladder_orders = []
        state.sell_ladder_mode = mode
        state.sell_ladder_signature = self._pending_exit_ladder_signature(mode, symbol, state)
        if not state.pending_exit_ladder_since or state.pending_exit_ladder_reason != reason:
            state.pending_exit_ladder_since = time.time()
        state.pending_exit_ladder_reason = reason
        self._refresh_active_side(state)
        self._save_state()
        self._log_event(
            "WARNING",
            f"{config.EXIT_SIDE.title()} exit ladder delayed for {symbol}: closeable amount is currently unavailable",
            event="reduce_only_violation_prevented",
            symbol=symbol,
            side=config.EXIT_SIDE,
            amount=amount,
            position_size=state.position_size,
            reason=reason,
            exception=exception,
        )

    def _sell_ladder_context(self, symbol: str, mode: str = "normal") -> dict:
        roundtrip_fee = config.SELLING.buy_fee_rate + config.SELLING.sell_fee_rate
        fee_floor = roundtrip_fee + max(0.0, config.STRATEGY.ema_breakeven_fee_buffer)

        return {
            "mode": mode,
            "volatility": 0.0,
            "daily_volatility": 0.0,
            "daily_volatility_multiplier": 1.0,
            "spread_rate": 0.0,
            "markup_multiplier": 1.0,
            "base_profit_floor": fee_floor if mode == "breakeven" else 0.0,
            "profit_floor": fee_floor if mode == "breakeven" else 0.0,
            "profit_floor_multiplier": 1.0,
            "profit_floor_reason": "ema_breakeven" if mode == "breakeven" else "ema_take_profit",
            "fee_floor": fee_floor,
            "spread_floor": 0.0,
            "volatility_floor": 0.0,
            "funding_rate": 0.0,
            "funding_reason": "disabled",
            "controlled_reference_price": 0.0,
            "controlled_loss_budget": 0.0,
            "controlled_loss_macro_intensity": 0.0,
            "controlled_loss_signal_intensity": 0.0,
            "controlled_loss_overlay_intensity": 0.0,
            "controlled_loss_speed_multiplier": 1.0,
            "controlled_loss_macro_speed_multiplier": 1.0,
            "controlled_loss_volatility_intensity": 0.0,
            "controlled_loss_volatility_speed_multiplier": 1.0,
            "controlled_loss_volatility_exponential_curve": 1.0,
            "controlled_loss_volatility_ratio": 0.0,
            "controlled_loss_local_volatility": 0.0,
            "controlled_loss_atr_rate": 0.0,
            "controlled_loss_daily_volatility_multiplier": 1.0,
            "controlled_loss_volatility_reference": 0.0,
            "controlled_loss_volatility_trigger_multiplier": 0.0,
            "controlled_loss_linear_progress": 0.0,
            "controlled_loss_effective_progress": 0.0,
            "controlled_loss_directional_gap": 0.0,
            "controlled_loss_elapsed_minutes": 0.0,
            "controlled_loss_ramp_minutes": 0.0,
            "controlled_loss_macro_regime": "neutral",
            "controlled_loss_macro_reason": "neutral",
            "controlled_loss_volatility_reason": "neutral",
            "controlled_loss_ramp_profile": "linear",
            "external_spread_bps": 0.0,
            "external_reason": "unavailable",
        }

    def _breakeven_exit_price(self, avg_entry_price: float) -> float:
        if avg_entry_price <= 0:
            return 0.0
        fee_floor = (
            max(0.0, config.SELLING.buy_fee_rate)
            + max(0.0, config.SELLING.sell_fee_rate)
            + max(0.0, config.STRATEGY.ema_breakeven_fee_buffer)
        )
        if config.POSITION_SIDE == "short":
            return avg_entry_price * (1 - fee_floor)
        return avg_entry_price * (1 + fee_floor)

    def _sell_price_floor(self, symbol: str, avg_entry_price: float, markup: float, context: Optional[dict] = None) -> float:
        context = context or {}
        mode = str(context.get("mode") or "normal")
        if mode == "breakeven":
            raw_price = self._breakeven_exit_price(avg_entry_price)
            if config.POSITION_SIDE == "short":
                return self._price_at_or_below(symbol, raw_price)
            return self._price_at_or_above(symbol, raw_price)

        breakeven_price = self._breakeven_exit_price(avg_entry_price)
        if config.POSITION_SIDE == "short":
            raw_price = avg_entry_price * (1 - markup)
            if breakeven_price > 0:
                raw_price = min(raw_price, breakeven_price)
            return self._price_at_or_below(symbol, raw_price)

        raw_price = avg_entry_price * (1 + markup)
        if breakeven_price > 0:
            raw_price = max(raw_price, breakeven_price)
        return self._price_at_or_above(symbol, raw_price)

    def _controlled_loss_exit_price(
        self,
        symbol: str,
        avg_entry_price: float,
        move_fraction: float,
        context: Optional[dict] = None,
    ) -> float:
        context = context or {}
        breakeven = self._breakeven_exit_price(avg_entry_price)
        reference_price = self._safe_float(context.get("controlled_reference_price"), 0.0)
        if reference_price <= 0:
            reference_price, _ = self._fetch_reference_price(symbol)
        if breakeven <= 0 or reference_price <= 0:
            return self._sell_price_floor(symbol, avg_entry_price, 0.0, context=context)

        move = self._clamp(move_fraction, 0.0, 1.0)
        max_loss = self._controlled_loss_max_loss_on_notional(self._get_state(symbol))
        if config.POSITION_SIDE == "short":
            if reference_price <= breakeven:
                return self._price_at_or_below(symbol, breakeven)
            raw_price = breakeven + (reference_price - breakeven) * move
            max_loss_price = avg_entry_price * (1 + max_loss)
            raw_price = min(raw_price, max_loss_price)
            return self._price_at_or_below(symbol, raw_price)

        if reference_price >= breakeven:
            return self._price_at_or_above(symbol, breakeven)
        raw_price = breakeven - (breakeven - reference_price) * move
        max_loss_price = avg_entry_price * (1 - max_loss)
        raw_price = max(raw_price, max_loss_price)
        return self._price_at_or_above(symbol, raw_price)

    def _exit_ladder_preflight(
        self,
        symbol: str,
        total_contracts: float,
        closeable_contracts: Optional[float],
        rebuild: bool,
        signature_override: str = "",
    ) -> ExitLadderPreflight:
        state = self._get_state(symbol)
        requested = max(0.0, self._safe_float(total_contracts, 0.0))
        position_contracts = max(0.0, self._safe_float(state.position_size, 0.0))
        if position_contracts > 0:
            requested = min(requested, position_contracts)

        existing_tracked = 0.0
        if state.sell_ladder_orders and not rebuild and not signature_override:
            existing_tracked = sum(self._safe_float(ref.get("amount"), 0.0) for ref in state.sell_ladder_orders)
            return ExitLadderPreflight(
                ok=False,
                requested_contracts=requested,
                position_contracts=position_contracts,
                closeable_contracts=0.0,
                planned_contracts=0.0,
                existing_tracked_contracts=existing_tracked,
                reason="existing_exit_ladder_not_canceled",
            )

        closeable = requested
        if closeable_contracts is not None:
            closeable = min(requested, max(0.0, self._safe_float(closeable_contracts, 0.0)))
        planned = self._amount_to_precision(symbol, closeable)
        if planned <= 0:
            return ExitLadderPreflight(
                ok=False,
                requested_contracts=requested,
                position_contracts=position_contracts,
                closeable_contracts=max(0.0, closeable),
                planned_contracts=0.0,
                existing_tracked_contracts=existing_tracked,
                reason="no_closeable_position_available",
            )

        if position_contracts > 0 and planned > position_contracts:
            planned = self._amount_to_precision(symbol, position_contracts)
        if planned > requested:
            planned = self._amount_to_precision(symbol, requested)
        if planned <= 0:
            return ExitLadderPreflight(
                ok=False,
                requested_contracts=requested,
                position_contracts=position_contracts,
                closeable_contracts=max(0.0, closeable),
                planned_contracts=0.0,
                existing_tracked_contracts=existing_tracked,
                reason="planned_exit_amount_below_minimum",
            )

        return ExitLadderPreflight(
            ok=True,
            requested_contracts=requested,
            position_contracts=position_contracts,
            closeable_contracts=max(0.0, closeable),
            planned_contracts=planned,
            existing_tracked_contracts=existing_tracked,
            reason="ok",
        )

    def _place_sell_ladder(
        self,
        ladder_config: Optional[ExitLadderConfig | SellLadderParams | str] = None,
        *args,
        **kwargs,
    ):
        fields = (
            "symbol",
            "total_contracts",
            "avg_entry_price",
            "rebuild",
            "closeable_contracts",
            "mode",
            "exit_scope",
            "signature_override",
            "use_trailing_exit",
            "signal",
        )
        if isinstance(ladder_config, str) or ladder_config is None:
            values = {}
            if isinstance(ladder_config, str):
                values["symbol"] = ladder_config
                positional = args
            else:
                positional = args
            if len(positional) > len(fields) - len(values):
                raise TypeError("_place_sell_ladder received too many positional arguments")
            for name, value in zip(fields[len(values):], positional):
                values[name] = value
            unexpected = set(kwargs) - set(fields)
            if unexpected:
                raise TypeError(f"_place_sell_ladder got unexpected keyword argument {sorted(unexpected)[0]!r}")
            values.update(kwargs)
            missing = [name for name in fields[:4] if name not in values]
            if missing:
                raise TypeError(f"_place_sell_ladder missing required argument {missing[0]!r}")
            ladder_config = ExitLadderConfig(**values)
        elif isinstance(ladder_config, SellLadderParams):
            ladder_config = ExitLadderConfig(
                symbol=ladder_config.symbol,
                total_contracts=ladder_config.total_contracts,
                avg_entry_price=ladder_config.avg_entry_price,
                rebuild=ladder_config.rebuild,
                closeable_contracts=ladder_config.closeable_contracts,
                mode=ladder_config.mode,
                exit_scope=ladder_config.exit_scope,
                signature_override=ladder_config.signature_override,
                use_trailing_exit=ladder_config.use_trailing_exit,
                signal=ladder_config.signal,
            )
        elif args or kwargs:
            raise TypeError("_place_sell_ladder does not accept extra arguments with ExitLadderConfig")
        elif not isinstance(ladder_config, ExitLadderConfig):
            raise TypeError("_place_sell_ladder requires ExitLadderConfig or SellLadderParams")

        symbol = ladder_config.symbol
        total_contracts = ladder_config.total_contracts
        avg_entry_price = ladder_config.avg_entry_price
        rebuild = ladder_config.rebuild
        closeable_contracts = ladder_config.closeable_contracts
        mode = ladder_config.mode
        exit_scope = ladder_config.exit_scope
        signature_override = ladder_config.signature_override
        use_trailing_exit = ladder_config.use_trailing_exit
        signal = ladder_config.signal
        state = self._get_state(symbol)
        exit_side = config.EXIT_SIDE
        exit_label = "Buy" if exit_side == "buy" else "Sell"
        if total_contracts <= 0 or avg_entry_price <= 0:
            self._log_event(
                "WARNING",
                f"{exit_label} exit ladder blocked for {symbol}: no {config.POSITION_SIDE} position",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=exit_side,
                reason=f"{exit_side}_without_{config.POSITION_SIDE}_position",
            )
            return

        if not config.RUNTIME.reduce_only_enabled:
            self._log_event(
                "ERROR",
                f"{exit_label} exit ladder blocked for {symbol}: reduce-only disabled",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=exit_side,
                reason="reduce_only_disabled",
            )
            return

        preflight = self._exit_ladder_preflight(
            symbol,
            total_contracts,
            closeable_contracts,
            rebuild=rebuild,
            signature_override=signature_override,
        )
        if not preflight.ok:
            if preflight.reason == "existing_exit_ladder_not_canceled":
                self._log_event(
                    "WARNING",
                    f"{exit_label} exit ladder blocked for {symbol}: existing tracked exits must be canceled first",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=preflight.existing_tracked_contracts,
                    position_size=state.position_size,
                    reason=preflight.reason,
                )
                return
            self._mark_exit_ladder_waiting_for_closeable(
                symbol,
                mode,
                preflight.reason,
            )
            return
        ladder_contracts = preflight.planned_contracts

        if ladder_contracts + max(self._get_min_contracts(symbol) * 1e-9, 1e-12) < total_contracts:
            self._log_event(
                "INFO",
                f"{exit_label} exit ladder capped for {symbol}: closeable={ladder_contracts} position={total_contracts}",
                event="exit_ladder_rebuilt" if rebuild else "exit_ladder_placed",
                symbol=symbol,
                side=exit_side,
                amount=ladder_contracts,
                position_size=total_contracts,
                reason="closeable_amount_cap",
            )

        ref_exit_scope = exit_scope or ("base" if mode == "normal" else "position")
        state.sell_ladder_orders = []
        state.sell_ladder_mode = mode
        self._clear_pending_exit_ladder(state)
        steps, plan_context = self._sell_ladder_plan(
            symbol,
            total_contracts,
            avg_entry_price,
            mode=mode,
            state=state,
            use_trailing_exit=use_trailing_exit,
            signal=signal,
        )
        allocations, runner_contracts = self._exit_ladder_contract_allocations(symbol, ladder_contracts, steps, state)
        state.sell_ladder_signature = signature_override or self._sell_ladder_signature(
            mode,
            symbol,
            state,
            total_contracts=total_contracts,
            avg_entry_price=avg_entry_price,
            use_trailing_exit=use_trailing_exit,
            signal=signal,
        )
        if plan_context.get("runner_enabled") and runner_contracts > 0:
            state.exit_runner_contracts = runner_contracts
        else:
            self._reset_exit_runner_state(state)
        allocated = 0.0
        created_at = time.time()
        operation_id = self._operation_id("exit_ladder", symbol=symbol)
        exit_planned_orders = 0
        exit_planned_notional = 0.0
        sell_context = self._sell_ladder_context(symbol, mode=mode)
        sell_context["external_spread_bps"] = self._safe_float(plan_context.get("external_spread_bps"), 0.0)
        sell_context["external_reason"] = str(plan_context.get("external_reason") or sell_context.get("external_reason") or "unavailable")
        if mode == "controlled_loss_exit":
            for key in (
                "controlled_loss_macro_intensity",
                "controlled_loss_signal_intensity",
                "controlled_loss_overlay_intensity",
                "controlled_loss_speed_multiplier",
                "controlled_loss_macro_speed_multiplier",
                "controlled_loss_volatility_intensity",
                "controlled_loss_volatility_speed_multiplier",
                "controlled_loss_volatility_exponential_curve",
                "controlled_loss_volatility_ratio",
                "controlled_loss_local_volatility",
                "controlled_loss_atr_rate",
                "controlled_loss_daily_volatility_multiplier",
                "controlled_loss_volatility_reference",
                "controlled_loss_volatility_trigger_multiplier",
                "controlled_loss_linear_progress",
                "controlled_loss_effective_progress",
                "controlled_loss_directional_gap",
                "controlled_loss_elapsed_minutes",
                "controlled_loss_ramp_minutes",
            ):
                sell_context[key] = self._safe_float(plan_context.get(key), self._safe_float(sell_context.get(key), 0.0))
            sell_context["controlled_loss_macro_regime"] = str(
                plan_context.get("controlled_loss_macro_regime") or sell_context.get("controlled_loss_macro_regime") or "neutral"
            )
            sell_context["controlled_loss_macro_reason"] = str(
                plan_context.get("controlled_loss_macro_reason") or sell_context.get("controlled_loss_macro_reason") or "neutral"
            )
            sell_context["controlled_loss_volatility_reason"] = str(
                plan_context.get("controlled_loss_volatility_reason")
                or sell_context.get("controlled_loss_volatility_reason")
                or "neutral"
            )
            sell_context["controlled_loss_ramp_profile"] = str(
                plan_context.get("controlled_loss_ramp_profile")
                or sell_context.get("controlled_loss_ramp_profile")
                or "linear"
            )
        for index, step, contracts in allocations:
            markup = self._safe_float(step.get("markup"), 0.0)

            adaptive_markup = markup * self._safe_float(sell_context.get("markup_multiplier"), 1.0)
            if mode == "controlled_loss_exit":
                price = self._controlled_loss_exit_price(symbol, avg_entry_price, markup, context=sell_context)
            else:
                price = self._sell_price_floor(symbol, avg_entry_price, adaptive_markup, context=sell_context)
            try:
                order = self._create_one_way_order(
                    symbol=symbol,
                    order_type="limit",
                    side=exit_side,
                    amount=contracts,
                    price=price,
                    reduce_only=True,
                )
                order_id = str(order.get("id"))
            except Exception as exc:
                band_limit = self._price_band_limit_from_error(exc, side=exit_side)
                if band_limit > 0:
                    adjusted_price = self._price_inside_htx_band(symbol, price, side=exit_side, limit=band_limit)
                    try:
                        order = self._create_one_way_order(
                            symbol=symbol,
                            order_type="limit",
                            side=exit_side,
                            amount=contracts,
                            price=adjusted_price,
                            reduce_only=True,
                        )
                        price = adjusted_price
                        order_id = str(order.get("id"))
                        self._log_event(
                            "WARNING",
                            f"{exit_label} reduce-only price adjusted for HTX band {symbol}: {price}",
                            event="exit_ladder_rebuilt" if rebuild else "exit_ladder_placed",
                            symbol=symbol,
                            side=exit_side,
                            price=price,
                            amount=contracts,
                            reason=f"htx_price_band_adjusted;limit={band_limit:.12f};mode={mode}",
                        )
                    except Exception as retry_exc:
                        if self._is_reduce_only_amount_exceeds_closeable_error(retry_exc):
                            if allocated <= 0:
                                self._mark_exit_ladder_waiting_for_closeable(
                                    symbol,
                                    mode,
                                    "closeable_amount_reserved_by_existing_exit_orders",
                                    amount=contracts,
                                    exception=retry_exc,
                                )
                                return
                            self._log_event(
                                "WARNING",
                                f"{exit_label} reduce-only ladder stopped for {symbol}: remaining closeable amount is unavailable",
                                event="reduce_only_violation_prevented",
                                symbol=symbol,
                                side=exit_side,
                                price=adjusted_price,
                                amount=contracts,
                                reason="partial_exit_ladder_closeable_unavailable",
                                exception=retry_exc,
                            )
                            break
                        self._log_event(
                            "ERROR",
                            f"{exit_label} reduce-only order failed for {symbol} after HTX band adjustment: {retry_exc}",
                            event="reduce_only_violation_prevented",
                            symbol=symbol,
                            side=exit_side,
                            price=adjusted_price,
                            amount=contracts,
                            reason="price_band_retry_rejected",
                            exception=retry_exc,
                        )
                        continue
                else:
                    if self._is_reduce_only_amount_exceeds_closeable_error(exc):
                        if allocated <= 0:
                            self._mark_exit_ladder_waiting_for_closeable(
                                symbol,
                                mode,
                                "closeable_amount_reserved_by_existing_exit_orders",
                                amount=contracts,
                                exception=exc,
                            )
                            return
                        self._log_event(
                            "WARNING",
                            f"{exit_label} reduce-only ladder stopped for {symbol}: remaining closeable amount is unavailable",
                            event="reduce_only_violation_prevented",
                            symbol=symbol,
                            side=exit_side,
                            price=price,
                            amount=contracts,
                            reason="partial_exit_ladder_closeable_unavailable",
                            exception=exc,
                        )
                        break
                    self._log_event(
                        "ERROR",
                        f"{exit_label} reduce-only order failed for {symbol}: {exc}",
                        event="reduce_only_violation_prevented",
                        symbol=symbol,
                        side=exit_side,
                        price=price,
                        amount=contracts,
                        reason="exit_order_rejected",
                        exception=exc,
                    )
                    continue

            allocated += contracts
            ref = {
                "id": order_id,
                "side": exit_side,
                "price": price,
                "amount": contracts,
                "created_at": created_at,
                "stage": index,
                "mode": mode,
                "markup": markup,
                "ladder_name": plan_context.get("ladder_name", mode),
                "exit_scope": ref_exit_scope,
                "external_spread_bps": self._safe_float(plan_context.get("external_spread_bps"), 0.0),
                "operation_id": operation_id,
                "cycle_id": state.cycle_id,
            }
            if mode == "controlled_loss_exit":
                ref["loss_move_fraction"] = markup
                ref["loss_budget_at_placement"] = self._safe_float(sell_context.get("controlled_loss_budget"), 0.0)
                ref["reference_price_at_placement"] = self._safe_float(sell_context.get("controlled_reference_price"), 0.0)
                ref["loss_macro_intensity"] = self._safe_float(sell_context.get("controlled_loss_macro_intensity"), 0.0)
                ref["loss_speed_multiplier"] = self._safe_float(sell_context.get("controlled_loss_speed_multiplier"), 1.0)
                ref["loss_macro_speed_multiplier"] = self._safe_float(sell_context.get("controlled_loss_macro_speed_multiplier"), 1.0)
                ref["loss_volatility_intensity"] = self._safe_float(sell_context.get("controlled_loss_volatility_intensity"), 0.0)
                ref["loss_volatility_speed_multiplier"] = self._safe_float(sell_context.get("controlled_loss_volatility_speed_multiplier"), 1.0)
                ref["loss_volatility_ratio"] = self._safe_float(sell_context.get("controlled_loss_volatility_ratio"), 0.0)
                ref["loss_atr_rate"] = self._safe_float(sell_context.get("controlled_loss_atr_rate"), 0.0)
                ref["loss_ramp_profile"] = str(sell_context.get("controlled_loss_ramp_profile") or "linear")
                ref["loss_directional_macro_gap"] = self._safe_float(sell_context.get("controlled_loss_directional_gap"), 0.0)
                ref["loss_macro_regime"] = str(sell_context.get("controlled_loss_macro_regime") or "neutral")
            state.sell_ladder_orders.append(ref)
            event = "exit_ladder_rebuilt" if rebuild else "exit_ladder_placed"
            action = "rebuilt" if rebuild else "placed"
            stage_notional = self._contracts_to_notional(symbol, contracts, price)
            exit_planned_orders += 1
            exit_planned_notional += stage_notional
            self._record_signal_analytics(
                event,
                symbol=symbol,
                signal=signal or {},
                planned_orders=exit_planned_orders,
                planned_notional=exit_planned_notional,
                placed_orders=exit_planned_orders,
                operation_id=operation_id,
                order_id=order_id,
                cycle_id=state.cycle_id,
                context={
                    "stage": index,
                    "mode": mode,
                    "price": price,
                    "contracts": contracts,
                    "stage_notional": stage_notional,
                    "markup": markup,
                    "adaptive_markup": adaptive_markup,
                    "exit_scope": ref_exit_scope,
                    "plan_context": plan_context,
                    "sell_context": sell_context,
                },
            )
            self._log_event(
                "INFO",
                f"{exit_label} exit ladder {action} for {symbol}: stage={index} contracts={contracts} price={price}",
                event=event,
                symbol=symbol,
                side=exit_side,
                order_id=order_id,
                price=price,
                amount=contracts,
                reason=(
                    f"reduce_only_close;markup_multiplier={sell_context.get('markup_multiplier', 1.0):.3f};"
                    f"base_profit_floor={sell_context.get('base_profit_floor', 0.0):.6f};"
                    f"profit_floor={sell_context.get('profit_floor', 0.0):.6f};"
                    f"profit_floor_mult={sell_context.get('profit_floor_multiplier', 1.0):.3f};"
                    f"profit_floor_reason={sell_context.get('profit_floor_reason', 'neutral')};"
                    f"fee_floor={sell_context.get('fee_floor', 0.0):.6f};"
                    f"spread={sell_context.get('spread_rate', 0.0):.6f};"
                    f"spread_floor={sell_context.get('spread_floor', 0.0):.6f};"
                    f"vol_floor={sell_context.get('volatility_floor', 0.0):.6f};"
                    f"funding={sell_context.get('funding_rate', 0.0):.6f};"
                    f"mode={mode};"
                    f"exit_scope={ref_exit_scope};"
                    f"ladder={plan_context.get('ladder_name', mode)};"
                    f"position_ratio={plan_context.get('position_ratio', 1.0):.4f};"
                    f"position_age_hours={plan_context.get('position_age_hours', 0.0):.2f};"
                    f"runner_contracts={runner_contracts:.12f};"
                    f"controlled_loss_move={plan_context.get('controlled_loss_move_fraction', 0.0):.4f};"
                    f"controlled_loss_macro_intensity={sell_context.get('controlled_loss_macro_intensity', 0.0):.3f};"
                    f"controlled_loss_speed={sell_context.get('controlled_loss_speed_multiplier', 1.0):.3f};"
                    f"controlled_loss_vol_intensity={sell_context.get('controlled_loss_volatility_intensity', 0.0):.3f};"
                    f"controlled_loss_vol_ratio={sell_context.get('controlled_loss_volatility_ratio', 0.0):.3f};"
                    f"controlled_loss_atr_rate={sell_context.get('controlled_loss_atr_rate', 0.0):.6f};"
                    f"controlled_loss_ramp_profile={sell_context.get('controlled_loss_ramp_profile', 'linear')};"
                    f"controlled_loss_gap={sell_context.get('controlled_loss_directional_gap', 0.0):.6f};"
                    f"controlled_loss_macro_regime={sell_context.get('controlled_loss_macro_regime', 'neutral')};"
                    f"external_spread_bps={sell_context.get('external_spread_bps', 0.0):.4f};"
                    f"external_reason={sell_context.get('external_reason', 'unavailable')};"
                    f"{sell_context.get('funding_reason', 'neutral')}"
                ),
            )

        planned_fixed_contracts = sum(self._safe_float(contracts, 0.0) for _, _, contracts in allocations)
        sell_total = sum(self._safe_float(ref.get("amount"), 0.0) for ref in state.sell_ladder_orders)
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)
        if sell_total > ladder_contracts + eps:
            self._log_event(
                "ERROR",
                f"{exit_label} exit ladder total exceeds {config.POSITION_SIDE} position for {symbol}; canceling",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=exit_side,
                amount=sell_total,
                position_size=ladder_contracts,
                reason="exit_amount_exceeds_position",
            )
            self._cancel_sell_orders(symbol, reason="exit_amount_exceeds_position")
            return
        if allocated + eps < planned_fixed_contracts:
            state.sell_ladder_signature = ""
            if allocated <= eps:
                self._reset_exit_runner_state(state)
            self._refresh_active_side(state)
            self._log_event(
                "WARNING",
                f"{exit_label} exit ladder only partially placed for {symbol}; will retry",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=exit_side,
                amount=sell_total,
                position_size=ladder_contracts,
                reason=(
                    "exit_ladder_partially_placed;"
                    f"planned_fixed_contracts={planned_fixed_contracts:.12f};"
                    f"placed_fixed_contracts={allocated:.12f};"
                    f"runner_contracts={runner_contracts:.12f}"
                ),
            )
            self._save_state()
            return

        self._refresh_active_side(state)
        self._save_state()

    def _place_average_recovery_exit_order(
        self,
        symbol: str,
        contracts: float,
        price: float,
        rebuild: bool,
        operation_id: str,
        signature: str,
    ) -> bool:
        state = self._get_state(symbol)
        existing_exit_contracts = sum(self._safe_float(ref.get("amount"), 0.0) for ref in state.sell_ladder_orders)
        if state.position_size > 0:
            contracts = min(contracts, max(0.0, state.position_size - existing_exit_contracts))
        contracts = self._amount_to_precision(symbol, contracts)
        if contracts <= 0 or price <= 0:
            return False

        exit_side = config.EXIT_SIDE
        exit_label = "Buy" if exit_side == "buy" else "Sell"
        created_at = time.time()
        try:
            order = self._create_one_way_order(
                symbol=symbol,
                order_type="limit",
                side=exit_side,
                amount=contracts,
                price=price,
                reduce_only=True,
            )
            order_id = str(order.get("id"))
        except Exception as exc:
            band_limit = self._price_band_limit_from_error(exc, side=exit_side)
            if band_limit > 0:
                adjusted_price = self._price_inside_htx_band(symbol, price, side=exit_side, limit=band_limit)
                try:
                    order = self._create_one_way_order(
                        symbol=symbol,
                        order_type="limit",
                        side=exit_side,
                        amount=contracts,
                        price=adjusted_price,
                        reduce_only=True,
                    )
                    price = adjusted_price
                    order_id = str(order.get("id"))
                except Exception as retry_exc:
                    reason = "average_recovery_price_band_retry_rejected"
                    if self._is_reduce_only_amount_exceeds_closeable_error(retry_exc):
                        reason = "average_recovery_closeable_unavailable"
                    self._log_event(
                        "WARNING",
                        f"{exit_label} average recovery order failed for {symbol}: {retry_exc}",
                        event="reduce_only_violation_prevented",
                        symbol=symbol,
                        side=exit_side,
                        price=adjusted_price,
                        amount=contracts,
                        reason=reason,
                        exception=retry_exc,
                    )
                    return False
            else:
                reason = "average_recovery_order_rejected"
                if self._is_reduce_only_amount_exceeds_closeable_error(exc):
                    reason = "average_recovery_closeable_unavailable"
                self._log_event(
                    "WARNING",
                    f"{exit_label} average recovery order failed for {symbol}: {exc}",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    price=price,
                    amount=contracts,
                    reason=reason,
                    exception=exc,
                )
                return False

        ref = {
            "id": order_id,
            "side": exit_side,
            "price": price,
            "amount": contracts,
            "created_at": created_at,
            "stage": len(state.sell_ladder_orders) + 1,
            "mode": "normal",
            "markup": 0.0,
            "ladder_name": "average_recovery",
            "exit_scope": "average_recovery",
            "operation_id": operation_id,
            "cycle_id": state.cycle_id,
        }
        state.sell_ladder_orders.append(ref)
        state.sell_ladder_signature = signature
        self._refresh_active_side(state)
        self._save_state()

        event = "exit_ladder_rebuilt" if rebuild else "exit_ladder_placed"
        stage_notional = self._contracts_to_notional(symbol, contracts, price)
        self._record_signal_analytics(
            event,
            symbol=symbol,
            signal={},
            planned_orders=len(state.sell_ladder_orders),
            planned_notional=stage_notional,
            placed_orders=len(state.sell_ladder_orders),
            operation_id=operation_id,
            order_id=order_id,
            cycle_id=state.cycle_id,
            context={
                "stage": ref["stage"],
                "mode": "normal",
                "price": price,
                "contracts": contracts,
                "stage_notional": stage_notional,
                "markup": 0.0,
                "exit_scope": "average_recovery",
            },
        )
        action = "rebuilt" if rebuild else "placed"
        self._log_event(
            "INFO",
            f"{exit_label} average recovery exit {action} for {symbol}: contracts={contracts} price={price}",
            event=event,
            symbol=symbol,
            side=exit_side,
            order_id=order_id,
            price=price,
            amount=contracts,
            reason="reduce_only_close;mode=normal;exit_scope=average_recovery;fee_floor_protected=1",
        )
        return True

    def _place_split_exit_ladder(
        self,
        symbol: str,
        total_contracts: float,
        avg_entry_price: float,
        rebuild: bool,
        closeable_contracts: Optional[float] = None,
        mode: str = "normal",
    ):
        state = self._get_state(symbol)
        if not self._should_use_split_exit_ladder(symbol, state, mode):
            self._place_sell_ladder(ExitLadderConfig(
                symbol,
                total_contracts,
                avg_entry_price,
                rebuild,
                closeable_contracts=closeable_contracts,
                mode=mode,
            ))
            return

        total_contracts = max(0.0, min(total_contracts, state.position_size))
        preflight = self._exit_ladder_preflight(
            symbol,
            total_contracts,
            closeable_contracts,
            rebuild=rebuild,
        )
        if not preflight.ok:
            if preflight.reason == "existing_exit_ladder_not_canceled":
                self._log_event(
                    "WARNING",
                    f"{config.EXIT_SIDE.title()} split exit ladder blocked for {symbol}: existing tracked exits must be canceled first",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=config.EXIT_SIDE,
                    amount=preflight.existing_tracked_contracts,
                    position_size=state.position_size,
                    reason=preflight.reason,
                )
                return
            self._mark_exit_ladder_waiting_for_closeable(symbol, mode, preflight.reason)
            return
        closeable_total = preflight.planned_contracts

        base_contracts = max(0.0, min(state.base_entry_amount, state.position_size))
        recovery_contracts_raw = max(0.0, min(state.averaging_entry_amount, max(0.0, state.position_size - base_contracts)))
        recovery_contracts = self._amount_to_precision(symbol, min(recovery_contracts_raw, closeable_total))
        base_closeable = self._amount_to_precision(symbol, min(base_contracts, max(0.0, closeable_total - recovery_contracts)))
        signature = self._split_sell_ladder_signature(symbol, state)
        base_price = state.base_entry_price or state.entry_price

        if base_closeable <= 0 and recovery_contracts <= 0:
            self._mark_exit_ladder_waiting_for_closeable(symbol, mode, "no_closeable_position_available")
            return

        if closeable_total + max(self._get_min_contracts(symbol) * 1e-9, 1e-12) < total_contracts:
            self._log_event(
                "INFO",
                f"{config.EXIT_SIDE.title()} split exit ladder capped for {symbol}: closeable={closeable_total} position={total_contracts}",
                event="exit_ladder_rebuilt" if rebuild else "exit_ladder_placed",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=closeable_total,
                position_size=total_contracts,
                reason="split_closeable_amount_cap",
            )

        state.sell_ladder_orders = []
        state.sell_ladder_mode = mode
        state.sell_ladder_signature = signature
        self._clear_pending_exit_ladder(state)
        if base_closeable <= 0:
            self._reset_exit_runner_state(state)
        self._refresh_active_side(state)
        self._save_state()

        if base_closeable > 0:
            self._place_sell_ladder(ExitLadderConfig(
                symbol,
                base_contracts,
                base_price,
                rebuild,
                closeable_contracts=base_closeable,
                mode=mode,
                exit_scope="base",
                signature_override=signature,
                use_trailing_exit=False,
            ))

        state = self._get_state(symbol)
        if recovery_contracts > 0:
            recovery_price = self._sell_price_floor(
                symbol,
                base_price,
                0.0,
                context=self._sell_ladder_context(symbol, mode=mode),
            )
            self._place_average_recovery_exit_order(
                symbol,
                recovery_contracts,
                recovery_price,
                rebuild,
                operation_id=self._operation_id("average_recovery_exit", symbol=symbol),
                signature=signature,
            )

        state = self._get_state(symbol)
        sell_total = sum(self._safe_float(ref.get("amount"), 0.0) for ref in state.sell_ladder_orders)
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)
        if sell_total > closeable_total + eps:
            self._log_event(
                "ERROR",
                f"{config.EXIT_SIDE.title()} split exit ladder total exceeds closeable position for {symbol}; canceling",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=sell_total,
                position_size=closeable_total,
                reason="split_exit_amount_exceeds_position",
            )
            self._cancel_sell_orders(symbol, reason="split_exit_amount_exceeds_position")
            return
        expected_total = self._amount_to_precision(symbol, base_closeable + recovery_contracts)
        if sell_total + eps < expected_total:
            state.sell_ladder_signature = ""
            self._reset_exit_runner_state(state)
            self._refresh_active_side(state)
            self._log_event(
                "WARNING",
                f"{config.EXIT_SIDE.title()} split exit ladder only partially placed for {symbol}; will retry",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=sell_total,
                position_size=expected_total,
                reason=(
                    "split_exit_ladder_partially_placed;"
                    f"expected_contracts={expected_total:.12f};"
                    f"placed_contracts={sell_total:.12f};"
                    f"base_contracts={base_closeable:.12f};"
                    f"recovery_contracts={recovery_contracts:.12f}"
                ),
            )
            self._save_state()
            return
        state.sell_ladder_signature = signature
        self._refresh_active_side(state)
        self._save_state()

    def _place_position_exit_ladder(
        self,
        symbol: str,
        total_contracts: float,
        avg_entry_price: float,
        rebuild: bool,
        closeable_contracts: Optional[float] = None,
        mode: str = "normal",
    ):
        state = self._get_state(symbol)
        if self._should_use_split_exit_ladder(symbol, state, mode):
            self._place_split_exit_ladder(
                symbol,
                total_contracts,
                avg_entry_price,
                rebuild,
                closeable_contracts=closeable_contracts,
                mode=mode,
            )
            return
        self._place_sell_ladder(ExitLadderConfig(
            symbol,
            total_contracts,
            avg_entry_price,
            rebuild,
            closeable_contracts=closeable_contracts,
            mode=mode,
        ))

    def _mark_zombie_position(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        if state.zombie_position:
            return
        state.zombie_position = True
        state.zombie_marked_at = time.time()
        state.frozen_no_more_buys = True
        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason=reason)
        self._log_event(
            "WARNING",
            f"Position marked as zombie for {symbol}: {reason}",
            event="position_frozen",
            symbol=symbol,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"zombie_position;{reason}",
        )
        self._save_state()

    def _order_effective_exit_price(self, order: dict) -> float:
        price = self._safe_float(order.get("price"), 0.0)
        if price > 0:
            return price

        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        for source in (order, info):
            for key in ("triggerPrice", "trigger_price", "order_price", "active_price"):
                price = self._safe_float(source.get(key), 0.0)
                if price > 0:
                    return price
        return 0.0

    def _hidden_order_type(self, order: dict) -> str:
        return str(order.get("bot_hidden_order_type") or "").lower()

    def _hidden_close_trade_type(self) -> str:
        return "3" if config.POSITION_SIDE == "short" else "4"

    def _is_hidden_close_order(self, order: dict) -> bool:
        hidden_type = self._hidden_order_type(order)
        if not hidden_type:
            return False
        if (order.get("side") or "").lower() != config.EXIT_SIDE:
            return False

        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        offset = ""
        trade_type = ""
        for source in (order, info):
            offset = offset or str(source.get("offset") or "").lower()
            trade_type = trade_type or str(source.get("trade_type") or source.get("tradeType") or "")

        if offset == "open":
            return False
        if offset == "close":
            return True
        if trade_type == self._hidden_close_trade_type():
            return True
        if hidden_type == "tpsl":
            return True
        return self._order_reduce_only_flag(order) is True

    def _exit_orders_priced_safely(self, symbol: str, orders: List[dict]) -> Tuple[bool, str]:
        state = self._get_state(symbol)
        if state.entry_price <= 0:
            return False, "unknown_exit_order_entry_price_missing"

        floor_price = self._sell_price_floor(
            symbol,
            state.entry_price,
            0.0,
            context=self._static_exit_profit_context(),
        )
        eps = max(abs(floor_price) * 1e-9, 1e-12)
        for order in orders:
            price = self._order_effective_exit_price(order)
            if price <= 0:
                return False, "unknown_exit_order_price_missing"
            if config.POSITION_SIDE == "short":
                if price > floor_price + eps:
                    return False, f"unknown_exit_order_above_profit_floor;floor={floor_price:.12f}"
            elif price < floor_price - eps:
                return False, f"unknown_exit_order_below_profit_floor;floor={floor_price:.12f}"
        return True, "exit_orders_price_safe"

    def _unknown_exit_adoption_reason(self, symbol: str, orders: List[dict], remaining: float) -> Tuple[bool, str]:
        if remaining <= 0:
            return False, "unknown_exit_order_empty"

        prices_ok, price_reason = self._exit_orders_priced_safely(symbol, orders)
        if not prices_ok:
            return False, price_reason

        reduce_only_flags = [self._order_reduce_only_flag(order) for order in orders]
        state = self._get_state(symbol)
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)
        if (
            all(self._is_hidden_close_order(order) for order in orders)
            and state.position_frozen >= remaining - eps
        ):
            return True, "hidden_close_exit_orders_adopted"

        if any(flag is False for flag in reduce_only_flags):
            return False, "unknown_exit_order_not_reduce_only"
        if reduce_only_flags and all(flag is True for flag in reduce_only_flags):
            return True, "unknown_reduce_only_exit_orders_adopted"

        if state.position_frozen >= remaining - eps:
            return True, "unknown_frozen_exit_orders_adopted"

        return False, "unknown_exit_orders_without_reduce_only_proof"

    def _should_cancel_hidden_exit_orders(self, orders: List[dict], reason: str) -> bool:
        if not config.STRATEGY.cancel_unsafe_hidden_close_orders:
            return False
        if not orders or not all(self._is_hidden_close_order(order) for order in orders):
            return False
        return reason.startswith(
            (
                "unknown_exit_order_price_missing",
                "unknown_exit_order_above_profit_floor",
                "unknown_exit_order_below_profit_floor",
            )
        )

    def _unknown_exit_order_age_sec(self, orders: List[dict], now: float) -> float:
        ages = []
        for order in orders or []:
            timestamp = self._safe_float(order.get("timestamp"), 0.0)
            if timestamp <= 0 and isinstance(order.get("info"), dict):
                timestamp = self._safe_float(
                    order["info"].get("created_at", order["info"].get("createdAt", order["info"].get("ctime"))),
                    0.0,
                )
            if timestamp <= 0:
                continue
            if timestamp > 1_000_000_000_000:
                timestamp = timestamp / 1000.0
            ages.append(max(0.0, now - timestamp))
        return max(ages) if ages else 0.0

    def _unknown_exit_wait_timeout_sec(self) -> float:
        return max(
            60.0,
            self._safe_float(getattr(config.RUNTIME, "order_timeout_sec", 0.0), 0.0) * 3.0,
            self._safe_float(getattr(config.RUNTIME, "poll_interval_sec", 0.0), 0.0) * 3.0,
        )

    def _wait_or_cancel_stale_unknown_exit_orders(
        self,
        symbol: str,
        orders: List[dict],
        reason: str,
    ) -> bool:
        state = self._get_state(symbol)
        now = time.time()
        wait_reason = reason or "unknown_exit_orders_unadoptable"
        if not state.pending_exit_ladder_since or state.pending_exit_ladder_reason != wait_reason:
            state.pending_exit_ladder_since = now
            state.pending_exit_ladder_reason = wait_reason
            state.frozen_no_more_buys = True
            self._refresh_active_side(state)
            self._save_state()
            return False

        elapsed = max(
            now - self._safe_float(state.pending_exit_ladder_since, now),
            self._unknown_exit_order_age_sec(orders, now),
        )
        timeout = self._unknown_exit_wait_timeout_sec()
        state.frozen_no_more_buys = True
        if elapsed < timeout:
            self._refresh_active_side(state)
            self._save_state()
            return False

        self._log_event(
            "WARNING",
            f"Stale untracked {config.EXIT_SIDE} exit orders for {symbol} remained unsafe for {elapsed:.1f}s; canceling",
            event="reduce_only_violation_prevented",
            symbol=symbol,
            side=config.EXIT_SIDE,
            amount=sum(self._order_remaining_amount(order) for order in orders or []),
            position_size=state.position_size,
            reason=f"{wait_reason};stale_unknown_exit_orders_canceled;elapsed={elapsed:.1f};timeout={timeout:.1f}",
        )
        if self._cancel_exchange_orders(symbol, orders, side=config.EXIT_SIDE, reason="stale_unknown_exit_orders_canceled"):
            self._clear_pending_exit_ladder(state)
        self._refresh_active_side(state)
        self._save_state()
        return False

    def _adopt_sell_orders(self, symbol: str, open_sell_orders: List[dict], reason: str) -> bool:
        state = self._get_state(symbol)
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)
        remaining = sum(self._order_remaining_amount(order) for order in open_sell_orders)
        if remaining <= 0:
            return False
        if remaining > state.position_size + eps:
            return False

        created_at = time.time()
        adopted = []
        for index, order in enumerate(open_sell_orders, start=1):
            amount = self._order_remaining_amount(order)
            if amount <= 0:
                continue
            raw_created_at = self._safe_float(order.get("timestamp"), 0.0)
            if raw_created_at > 1_000_000_000_000:
                created_ref_at = raw_created_at / 1000.0
            elif raw_created_at > 0:
                created_ref_at = raw_created_at
            else:
                created_ref_at = created_at
            ref = {
                "id": str(order.get("id")),
                "side": config.EXIT_SIDE,
                "price": self._order_effective_exit_price(order),
                "amount": amount,
                "created_at": created_ref_at,
                "stage": index,
                "mode": state.sell_ladder_mode or "normal",
                "adopted": True,
            }
            hidden_type = self._hidden_order_type(order)
            if hidden_type:
                ref["hidden_order_type"] = hidden_type
                trigger_price = self._safe_float(order.get("triggerPrice"), 0.0)
                if trigger_price <= 0 and isinstance(order.get("info"), dict):
                    trigger_price = self._safe_float(order["info"].get("trigger_price"), 0.0)
                if trigger_price > 0:
                    ref["trigger_price"] = trigger_price
            cancel_params = order.get("bot_cancel_params") or order.get("cancel_params")
            if isinstance(cancel_params, dict):
                ref["cancel_params"] = dict(cancel_params)
            adopted.append(ref)

        if not adopted:
            return False

        state.sell_ladder_orders = adopted
        state.sell_ladder_mode = state.sell_ladder_mode or "normal"
        full_coverage = remaining + eps >= state.position_size
        state.sell_ladder_signature = (
            self._exit_ladder_signature(state.sell_ladder_mode, symbol, state)
            if full_coverage
            else ""
        )
        self._clear_pending_exit_ladder(state)
        self._reset_exit_runner_state(state)
        self._refresh_active_side(state)
        log_reason = reason
        if not full_coverage:
            log_reason = (
                f"{reason};partial_external_exit_coverage;"
                f"covered={remaining:.12f};position={state.position_size:.12f}"
            )
        self._log_event(
            "INFO",
            f"Adopted existing {config.EXIT_SIDE} exit ladder for {symbol}: orders={len(adopted)} amount={remaining}",
            event="state_exchange_mismatch",
            symbol=symbol,
            side=config.EXIT_SIDE,
            amount=remaining,
            position_size=state.position_size,
            reason=log_reason,
        )
        self._save_state()
        return True

    def _exit_order_exposure(self, symbol: str, open_orders: List[dict]) -> dict:
        state = self._get_state(symbol)
        exit_side = config.EXIT_SIDE
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)
        open_exit_orders = [
            order for order in open_orders
            if (order.get("side") or "").lower() == exit_side
            and self._order_remaining_amount(order) > eps
        ]
        known_exit_ids = self._order_ids(state.sell_ladder_orders)
        hard_stop_ids = self._order_ids([state.hard_stop_order] if state.hard_stop_order else [])
        tracked_exit_orders = [order for order in open_exit_orders if str(order.get("id")) in known_exit_ids]
        tracked_hard_stop_orders = [order for order in open_exit_orders if str(order.get("id")) in hard_stop_ids]
        unknown_exit_orders = [
            order for order in open_exit_orders
            if str(order.get("id")) not in known_exit_ids and str(order.get("id")) not in hard_stop_ids
        ]
        tracked_remaining = sum(self._order_remaining_amount(order) for order in tracked_exit_orders)
        unknown_remaining = sum(self._order_remaining_amount(order) for order in unknown_exit_orders)
        return {
            "exit_side": exit_side,
            "open_exit_orders": open_exit_orders,
            "known_exit_ids": known_exit_ids,
            "hard_stop_ids": hard_stop_ids,
            "tracked_exit_orders": tracked_exit_orders,
            "tracked_hard_stop_orders": tracked_hard_stop_orders,
            "unknown_exit_orders": unknown_exit_orders,
            "tracked_remaining": tracked_remaining,
            "unknown_remaining": unknown_remaining,
        }

    def _validate_sell_orders(self, symbol: str, open_orders: List[dict]) -> bool:
        state = self._get_state(symbol)
        exposure = self._exit_order_exposure(symbol, open_orders)
        exit_side = exposure["exit_side"]
        open_sell_orders = exposure["open_exit_orders"]
        tracked_sell_orders = exposure["tracked_exit_orders"]
        tracked_hard_stop_orders = exposure["tracked_hard_stop_orders"]
        unknown_sells = exposure["unknown_exit_orders"]
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)

        if state.position_size <= 0:
            if state.hard_stop_order:
                self._log_event(
                    "WARNING",
                    f"Tracked {exit_side} hard stop without {config.POSITION_SIDE} position for {symbol}; canceling tracked bot order",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    reason=f"hard_stop_without_{config.POSITION_SIDE}_position",
                )
                self._cancel_hard_stop_order(symbol, reason=f"hard_stop_without_{config.POSITION_SIDE}_position")
                return False
            if state.sell_ladder_orders:
                self._log_event(
                    "WARNING",
                    f"Tracked {exit_side} exit orders without {config.POSITION_SIDE} position for {symbol}; canceling tracked bot orders",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    reason=f"{exit_side}_without_{config.POSITION_SIDE}_position",
                )
                self._cancel_sell_orders(symbol, reason=f"{exit_side}_without_{config.POSITION_SIDE}_position")
                return False
            if unknown_sells:
                close_like = [
                    order for order in unknown_sells
                    if self._order_reduce_only_flag(order) is True or self._is_hidden_close_order(order)
                ]
                unsafe = [order for order in unknown_sells if order not in close_like]
                if close_like:
                    self._log_event(
                        "WARNING",
                        f"Untracked {exit_side} close orders found for flat {symbol}; canceling before any new entry",
                        event="reduce_only_violation_prevented",
                        symbol=symbol,
                        side=exit_side,
                        amount=sum(self._order_remaining_amount(order) for order in close_like),
                        reason="flat_unknown_close_orders_canceled",
                    )
                    self._cancel_exchange_orders(symbol, close_like, side=exit_side, reason="flat_unknown_close_orders_canceled")
                    return False
                if unsafe:
                    self._log_event(
                        "WARNING",
                        f"Untracked {exit_side} orders found for flat {symbol}; blocking new entry until they clear",
                        event="reduce_only_violation_prevented",
                        symbol=symbol,
                        side=exit_side,
                        amount=sum(self._order_remaining_amount(order) for order in unsafe),
                        reason="flat_unknown_exit_side_orders_block_entry",
                    )
                    return False
                self._log_event(
                    "DEBUG",
                    f"Untracked {exit_side} orders found for flat {symbol}; leaving them untouched",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    side=exit_side,
                    amount=sum(self._order_remaining_amount(order) for order in unknown_sells),
                    reason="untracked_exit_side_orders_preserved",
                )
            return True

        if tracked_hard_stop_orders:
            unsafe_stop_orders = [
                order for order in tracked_hard_stop_orders
                if self._order_reduce_only_flag(order) is False
            ]
            if unsafe_stop_orders:
                self._log_event(
                    "ERROR",
                    f"Tracked {exit_side} hard stop is not reduce-only for {symbol}; canceling",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=sum(self._order_remaining_amount(order) for order in unsafe_stop_orders),
                    position_size=state.position_size,
                    reason="hard_stop_not_reduce_only",
                )
                self._cancel_hard_stop_order(symbol, reason="hard_stop_not_reduce_only")
                return False
            stop_remaining = sum(self._order_remaining_amount(order) for order in tracked_hard_stop_orders)
            if stop_remaining > state.position_size + eps:
                self._log_event(
                    "ERROR",
                    f"Tracked {exit_side} hard stop exceeds {config.POSITION_SIDE} position for {symbol}; rebuilding",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=stop_remaining,
                    position_size=state.position_size,
                    reason="hard_stop_amount_exceeds_position",
                )
                self._cancel_hard_stop_order(symbol, reason="hard_stop_amount_exceeds_position")
                return False

        non_reduce_tracked = [
            order for order in tracked_sell_orders
            if self._order_reduce_only_flag(order) is False
        ]
        if non_reduce_tracked:
            bad_ids = {str(order.get("id")) for order in non_reduce_tracked}
            self._log_event(
                "ERROR",
                f"Tracked {exit_side} exit orders are not reduce-only for {symbol}; canceling unsafe orders",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=exit_side,
                amount=sum(self._order_remaining_amount(order) for order in non_reduce_tracked),
                position_size=state.position_size,
                reason="tracked_exit_order_not_reduce_only",
            )
            if self._cancel_exchange_orders(symbol, non_reduce_tracked, side=exit_side, reason="tracked_exit_order_not_reduce_only"):
                state.sell_ladder_orders = [
                    ref for ref in state.sell_ladder_orders
                    if str(ref.get("id")) not in bad_ids
                ]
                state.sell_ladder_signature = ""
                self._refresh_active_side(state)
                self._save_state()
            return False

        if unknown_sells:
            unknown_remaining = sum(self._order_remaining_amount(order) for order in unknown_sells)
            if unknown_remaining > state.position_size + eps:
                self._log_event(
                    "ERROR",
                    f"Unknown {exit_side} exit orders exceed {config.POSITION_SIDE} position for {symbol}; canceling",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=unknown_remaining,
                    position_size=state.position_size,
                    reason="unknown_exit_amount_exceeds_position",
                )
                self._cancel_exchange_orders(symbol, unknown_sells, side=exit_side, reason="unknown_exit_amount_exceeds_position")
                return False

            tracked_remaining = sum(self._order_remaining_amount(order) for order in tracked_sell_orders)
            if tracked_remaining > 0 and tracked_remaining + unknown_remaining > state.position_size + eps:
                self._log_event(
                    "ERROR",
                    f"Combined tracked and unknown {exit_side} orders exceed {config.POSITION_SIDE} position for {symbol}; canceling tracked bot orders",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=tracked_remaining + unknown_remaining,
                    position_size=state.position_size,
                    reason="combined_exit_amount_exceeds_position",
                )
                self._cancel_sell_orders(symbol, reason="combined_exit_amount_exceeds_position")
                return False

            if tracked_remaining > 0 and tracked_remaining + unknown_remaining <= state.position_size + eps:
                can_adopt, adopt_reason = self._unknown_exit_adoption_reason(symbol, unknown_sells, unknown_remaining)
                if can_adopt:
                    return self._adopt_sell_orders(
                        symbol,
                        tracked_sell_orders + unknown_sells,
                        reason=f"{adopt_reason};tracked_unknown_exit_orders_merged",
                    )
                if self._should_cancel_hidden_exit_orders(unknown_sells, adopt_reason):
                    self._log_event(
                        "WARNING",
                        f"Unsafe hidden {exit_side} close orders found next to tracked exits for {symbol}; canceling before continuing",
                        event="reduce_only_violation_prevented",
                        symbol=symbol,
                        side=exit_side,
                        amount=unknown_remaining,
                        position_size=state.position_size,
                        reason=f"{adopt_reason};tracked_hidden_close_order_cancel",
                    )
                    state.frozen_no_more_buys = True
                    self._cancel_exchange_orders(
                        symbol,
                        unknown_sells,
                        side=exit_side,
                        reason=f"{adopt_reason};tracked_hidden_close_order_cancel",
                    )
                    self._save_state()
                    return False
                self._log_event(
                    "WARNING",
                    f"Untracked {exit_side} orders next to tracked exits for {symbol} cannot be proven safe; waiting",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=unknown_remaining,
                    position_size=state.position_size,
                    reason=f"tracked_unknown_exit_orders_unadoptable;{adopt_reason}",
                )
                self._freeze_no_more_buys(
                    symbol,
                    reason=f"tracked_unknown_exit_orders_unadoptable;{adopt_reason}",
                )
                return self._wait_or_cancel_stale_unknown_exit_orders(
                    symbol,
                    unknown_sells,
                    reason=f"tracked_unknown_exit_orders_unadoptable;{adopt_reason}",
                )

            if not state.sell_ladder_orders and not tracked_sell_orders:
                can_adopt, adopt_reason = self._unknown_exit_adoption_reason(symbol, unknown_sells, unknown_remaining)
                if can_adopt:
                    return self._adopt_sell_orders(symbol, unknown_sells, reason=adopt_reason)

                if self._should_cancel_hidden_exit_orders(unknown_sells, adopt_reason):
                    self._log_event(
                        "WARNING",
                        f"Unsafe hidden {exit_side} close orders found for {symbol}; canceling before rebuilding exits",
                        event="reduce_only_violation_prevented",
                        symbol=symbol,
                        side=exit_side,
                        amount=unknown_remaining,
                        position_size=state.position_size,
                        reason=f"{adopt_reason};hidden_close_order_cancel",
                    )
                    state.frozen_no_more_buys = True
                    self._cancel_exchange_orders(
                        symbol,
                        unknown_sells,
                        side=exit_side,
                        reason=f"{adopt_reason};hidden_close_order_cancel",
                    )
                    self._save_state()
                    return False

                self._log_event(
                    "WARNING",
                    f"Untracked {exit_side} orders found for {symbol}; waiting instead of placing another exit ladder",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=unknown_remaining,
                    position_size=state.position_size,
                    reason=adopt_reason,
                )
                return self._wait_or_cancel_stale_unknown_exit_orders(
                    symbol,
                    unknown_sells,
                    reason=adopt_reason,
                )

            self._log_event(
                "DEBUG",
                f"Untracked {exit_side} orders found for {symbol}; leaving them untouched",
                event="state_exchange_mismatch",
                symbol=symbol,
                side=exit_side,
                amount=unknown_remaining,
                reason="untracked_exit_side_orders_preserved",
            )
        elif "unknown_exit" in str(getattr(state, "pending_exit_ladder_reason", "") or ""):
            self._clear_pending_exit_ladder(state)
            self._refresh_active_side(state)
            self._save_state()

        open_tracked_sell_ids = {str(order.get("id")) for order in tracked_sell_orders}
        active_refs = [ref for ref in state.sell_ladder_orders if str(ref.get("id")) in open_tracked_sell_ids]
        if state.sell_ladder_orders and len(active_refs) != len(state.sell_ladder_orders):
            unknown_remaining = sum(self._order_remaining_amount(order) for order in unknown_sells)
            if unknown_sells and unknown_remaining <= state.position_size + eps:
                can_adopt, adopt_reason = self._unknown_exit_adoption_reason(symbol, unknown_sells, unknown_remaining)
                if can_adopt:
                    return self._adopt_sell_orders(symbol, unknown_sells, reason=f"{adopt_reason};tracked_exit_id_rotated")

                self._log_event(
                    "WARNING",
                    f"Tracked {exit_side} exit ladder differs from exchange for {symbol}; waiting on unknown exit orders",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    amount=unknown_remaining,
                    position_size=state.position_size,
                    reason=f"tracked_exit_order_set_mismatch_with_unadoptable_unknowns;{adopt_reason}",
                )
                return self._wait_or_cancel_stale_unknown_exit_orders(
                    symbol,
                    unknown_sells,
                    reason=f"tracked_exit_order_set_mismatch_with_unadoptable_unknowns;{adopt_reason}",
                )

            missing_refs = [
                ref for ref in state.sell_ladder_orders
                if str(ref.get("id")) not in open_tracked_sell_ids
            ]
            missing_amount = sum(self._safe_float(ref.get("amount"), 0.0) for ref in missing_refs)
            if missing_refs and not open_sell_orders:
                first_preserve = not all(ref.get("invisible_preserved_at") for ref in missing_refs)
                now = time.time()
                if first_preserve:
                    for ref in missing_refs:
                        ref["invisible_preserved_at"] = now
                    self._log_event(
                        "WARNING",
                        f"Tracked {exit_side} exit ladder for {symbol} is temporarily absent from open-orders response; preserving refs",
                        event="state_exchange_mismatch",
                        symbol=symbol,
                        side=exit_side,
                        amount=missing_amount,
                        position_size=state.position_size,
                        reason="tracked_exit_orders_temporarily_invisible_preserved",
                    )
                    self._refresh_active_side(state)
                    self._save_state()
                    return True
                oldest_preserved_at = min(
                    self._safe_float(ref.get("invisible_preserved_at"), now)
                    for ref in missing_refs
                )
                invisible_elapsed = max(0.0, now - oldest_preserved_at)
                invisible_timeout = self._unknown_exit_wait_timeout_sec()
                if invisible_elapsed < invisible_timeout:
                    self._refresh_active_side(state)
                    self._save_state()
                    return True

                self._log_event(
                    "WARNING",
                    f"Tracked {exit_side} exit ladder for {symbol} stayed invisible for {invisible_elapsed:.1f}s; clearing refs so the ladder can be rebuilt",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    side=exit_side,
                    amount=missing_amount,
                    position_size=state.position_size,
                    reason=(
                        "tracked_exit_orders_invisible_timeout;"
                        f"elapsed={invisible_elapsed:.1f};timeout={invisible_timeout:.1f}"
                    ),
                )
                state.sell_ladder_orders = []
                state.sell_ladder_signature = ""
                self._clear_pending_exit_ladder(state)
                self._reset_exit_runner_state(state)
                self._refresh_active_side(state)
                self._save_state()
                return False

            self._log_event(
                "WARNING",
                f"Tracked {exit_side} exit ladder differs from exchange for {symbol}; rebuilding",
                event="state_exchange_mismatch",
                symbol=symbol,
                side=exit_side,
                reason="exit_ladder_order_set_mismatch",
            )
            if not self._cancel_exchange_orders(symbol, tracked_sell_orders, side=exit_side, reason="exit_ladder_order_set_mismatch"):
                return False
            state.sell_ladder_orders = []
            state.sell_ladder_signature = ""
            self._refresh_active_side(state)
            self._save_state()
            return False

        remaining = 0.0
        for order in tracked_sell_orders:
            remaining += self._order_remaining_amount(order)

        if remaining > state.position_size + eps:
            self._log_event(
                "ERROR",
                f"Open {exit_side} exit orders exceed {config.POSITION_SIDE} position for {symbol}; rebuilding",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=exit_side,
                amount=remaining,
                position_size=state.position_size,
                reason="open_exit_amount_exceeds_position",
            )
            self._cancel_exchange_orders(symbol, tracked_sell_orders, side=exit_side, reason="open_exit_amount_exceeds_position")
            self._cancel_sell_orders(symbol, reason="open_exit_amount_exceeds_position")
            return False

        return True

    def _closeable_contracts_for_exit_ladder(self, symbol: str, had_sell_ladder: bool) -> float:
        state = self._get_state(symbol)
        closeable = state.position_available
        if had_sell_ladder and closeable <= 0:
            closeable = state.position_size
        if closeable <= 0:
            closeable = state.position_size
            reason = (
                "stale_frozen_position_fallback"
                if state.position_frozen > 0
                else "closeable_missing_no_frozen_fallback"
            )
            self._log_event(
                "WARNING",
                f"Using full {config.POSITION_SIDE} position for {config.EXIT_SIDE} exit ladder on {symbol}: no closeable/frozen amount reported",
                event="state_exchange_mismatch",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=reason,
            )
        return closeable

    def _controlled_loss_current_move_fraction(self, state: TradeState) -> float:
        values = [
            self._safe_float(ref.get("loss_move_fraction", ref.get("markup")), 0.0)
            for ref in state.sell_ladder_orders
            if isinstance(ref, dict)
        ]
        return max(values) if values else 0.0

    def _maybe_reprice_controlled_loss_for_volatility(self, symbol: str, signal: Optional[dict] = None) -> bool:
        state = self._get_state(symbol)
        if state.sell_ladder_mode != "controlled_loss_exit" or not state.sell_ladder_orders:
            return False

        ramp_context = self._controlled_loss_ramp_context(state, symbol=symbol, signal=signal)
        volatility_intensity = self._safe_float(ramp_context.get("volatility_intensity"), 0.0)
        if volatility_intensity <= 0.0:
            return False

        desired_move = self._safe_float(ramp_context.get("move_fraction"), 0.0)
        current_move = self._controlled_loss_current_move_fraction(state)
        min_delta = max(
            0.0,
            self._safe_float(getattr(config.STRATEGY, "controlled_loss_volatility_reprice_min_move_delta", 0.05), 0.05),
        )
        if desired_move <= current_move + min_delta:
            return False

        self._log_event(
            "WARNING",
            f"Repricing controlled loss ladder for {symbol}: adverse volatility accelerated exit",
            event="exit_ladder_rebuilt",
            symbol=symbol,
            side=config.EXIT_SIDE,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=(
                "controlled_loss_volatility_acceleration;"
                f"current_move={current_move:.4f};desired_move={desired_move:.4f};"
                f"vol_intensity={volatility_intensity:.3f};"
                f"vol_ratio={self._safe_float(ramp_context.get('volatility_ratio'), 0.0):.3f};"
                f"atr_rate={self._safe_float(ramp_context.get('atr_rate'), 0.0):.6f};"
                f"speed={self._safe_float(ramp_context.get('speed_multiplier'), 1.0):.3f};"
                f"profile={ramp_context.get('ramp_profile', 'linear')}"
            ),
        )
        self._cancel_sell_orders(symbol, reason="controlled_loss_volatility_acceleration")
        state = self._get_state(symbol)
        if state.sell_ladder_orders:
            return True

        self._place_sell_ladder(ExitLadderConfig(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=True),
            mode="controlled_loss_exit",
            signal=signal,
        ))
        return True

    def _controlled_loss_block_reason(self, symbol: str, state: TradeState, reference_price: float) -> str:
        strategy = config.STRATEGY
        hard_time_exit = self._hard_time_exit_elapsed(state)
        if not strategy.enable_controlled_loss_exit and not hard_time_exit:
            return "controlled_loss_disabled"
        if state.position_size <= 0 or state.entry_price <= 0:
            return "no_position"

        if not state.zombie_position and not hard_time_exit:
            return "not_zombie"
        if state.entry_orders:
            return "entry_orders_active"
        if not hard_time_exit:
            if state.zombie_marked_at:
                zombie_age = (time.time() - state.zombie_marked_at) / 60.0
                if zombie_age < max(0.0, strategy.controlled_loss_after_zombie_minutes):
                    return f"controlled_loss_wait_zombie_age;age={zombie_age:.1f}"
            elif strategy.controlled_loss_after_zombie_minutes > 0:
                return "controlled_loss_missing_zombie_age"

        drawdown = self._position_drawdown(state, reference_price)
        if not hard_time_exit and drawdown < max(0.0, strategy.controlled_loss_min_drawdown):
            return f"controlled_loss_drawdown_too_small;drawdown={drawdown:.5f}"

        budget = self._controlled_loss_available_budget()
        if budget <= 0 and not self._hard_time_exit_bypasses_profit_bank(state):
            return "controlled_loss_no_profit_bank"
        return ""

    def _rebuild_controlled_loss_exit_ladder(self, symbol: str, reason: str, signal: Optional[dict] = None) -> bool:
        state = self._get_state(symbol)
        reference_price, _ = self._fetch_reference_price(symbol)
        if reference_price <= 0:
            return False

        block_reason = self._controlled_loss_block_reason(symbol, state, reference_price)
        if block_reason:
            return False

        had_sell_ladder = bool(state.sell_ladder_orders)
        close_contracts = self._controlled_loss_contracts(symbol, state, reference_price, had_sell_ladder=had_sell_ladder)
        if close_contracts <= 0:
            return False

        if state.sell_ladder_orders:
            self._cancel_sell_orders(symbol, reason=reason)
            state = self._get_state(symbol)
            if state.sell_ladder_orders:
                return True

        state.sell_ladder_mode = "controlled_loss_exit"
        state.sell_ladder_signature = ""
        if not state.time_exit_activated_at:
            state.time_exit_activated_at = time.time()
        self._refresh_active_side(state)
        self._save_state()

        self._place_sell_ladder(ExitLadderConfig(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=close_contracts,
            mode="controlled_loss_exit",
            signal=signal,
        ))
        state = self._get_state(symbol)
        if not state.sell_ladder_orders:
            return True
        ramp_context = self._controlled_loss_ramp_context(state, symbol=symbol, signal=signal)
        self._log_event(
            "WARNING",
            f"Controlled loss exit ladder activated for {symbol}: contracts={close_contracts}",
            event="exit_ladder_rebuilt",
            symbol=symbol,
            side=config.EXIT_SIDE,
            amount=close_contracts,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=(
                f"{reason};drawdown={self._position_drawdown(state, reference_price):.5f};"
                f"hard_close_fraction={self._hard_time_exit_close_fraction(state):.3f};"
                f"loss_budget={self._controlled_loss_available_budget():.8f};"
                f"max_loss={self._controlled_loss_max_loss_on_notional(state):.5f};"
                f"loss_move={self._safe_float(ramp_context.get('move_fraction'), 0.0):.4f};"
                f"macro_intensity={self._safe_float(ramp_context.get('macro_intensity'), 0.0):.3f};"
                f"speed={self._safe_float(ramp_context.get('speed_multiplier'), 1.0):.3f};"
                f"macro_gap={self._safe_float(ramp_context.get('directional_gap'), 0.0):.6f};"
                f"vol_intensity={self._safe_float(ramp_context.get('volatility_intensity'), 0.0):.3f};"
                f"vol_ratio={self._safe_float(ramp_context.get('volatility_ratio'), 0.0):.3f};"
                f"ramp_profile={ramp_context.get('ramp_profile', 'linear')}"
            ),
        )
        return True

    def _maybe_apply_controlled_loss_exit(self, symbol: str, signal: Optional[dict] = None) -> bool:
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            return False
        if state.sell_ladder_mode == "controlled_loss_exit":
            reference_price, _ = self._fetch_reference_price(symbol)
            block_reason = self._controlled_loss_block_reason(symbol, state, reference_price) if reference_price > 0 else "reference_price_unavailable"
            if block_reason:
                if state.sell_ladder_orders:
                    self._cancel_sell_orders(symbol, reason=block_reason)
                state = self._get_state(symbol)
                state.sell_ladder_mode = "urgent_time_exit"
                state.sell_ladder_signature = ""
                self._refresh_active_side(state)
                self._save_state()
                if not state.sell_ladder_orders:
                    self._place_sell_ladder(ExitLadderConfig(
                        symbol,
                        state.position_size,
                        state.entry_price,
                        rebuild=True,
                        closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=False),
                        mode="urgent_time_exit",
                    ))
                return True
            if not state.sell_ladder_orders:
                if self._is_exit_ladder_waiting_for_closeable(symbol, "controlled_loss_exit", state):
                    return True
                return self._rebuild_controlled_loss_exit_ladder(symbol, reason="controlled_loss_missing_ladder", signal=signal)
            if self._maybe_reprice_controlled_loss_for_volatility(symbol, signal):
                return True
            return self._maybe_reprice_time_exit_ladder(symbol, signal=signal) or True

        return self._rebuild_controlled_loss_exit_ladder(symbol, reason="controlled_loss_activation", signal=signal)

    def _maybe_apply_urgent_time_exit(self, symbol: str, signal: Optional[dict] = None) -> bool:
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            return False

        after_minutes = self._effective_urgent_time_exit_after_minutes()
        if after_minutes <= 0:
            return False
        held_minutes = self._position_held_minutes(state)
        if state.sell_ladder_mode != "urgent_time_exit" and held_minutes < after_minutes:
            return False

        if state.sell_ladder_mode == "urgent_time_exit":
            if not state.frozen_no_more_buys:
                state.frozen_no_more_buys = True
                self._refresh_active_side(state)
                self._save_state()
            if state.sell_ladder_orders:
                return self._maybe_reprice_time_exit_ladder(symbol) or True
            if self._is_exit_ladder_waiting_for_closeable(symbol, "urgent_time_exit", state):
                return True
            self._place_sell_ladder(ExitLadderConfig(
                symbol,
                state.position_size,
                state.entry_price,
                rebuild=True,
                closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=False),
                mode="urgent_time_exit",
            ))
            return True

        had_sell_ladder = bool(state.sell_ladder_orders)
        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason="urgent_time_exit_activated")
            state = self._get_state(symbol)
        state.frozen_no_more_buys = True
        state.sell_ladder_mode = "urgent_time_exit"
        state.sell_ladder_signature = ""
        state.time_exit_activated_at = state.time_exit_activated_at or time.time()
        self._refresh_active_side(state)
        self._save_state()

        if had_sell_ladder:
            self._cancel_sell_orders(symbol, reason="urgent_time_exit_activated")
        state = self._get_state(symbol)
        if state.sell_ladder_orders:
            return True
        self._place_sell_ladder(ExitLadderConfig(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=had_sell_ladder),
            mode="urgent_time_exit",
        ))
        self._log_event(
            "WARNING",
            f"Urgent time exit activated for {symbol}: held={held_minutes:.1f}m",
            event="exit_ladder_rebuilt",
            symbol=symbol,
            side=config.EXIT_SIDE,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"urgent_time_exit_activated;holding_minutes={held_minutes:.1f};threshold_minutes={after_minutes:.1f}",
        )
        return True

    def _time_exit_reprice_after_minutes(self, mode: str) -> float:
        if mode in {"breakeven", "urgent_time_exit"}:
            return max(0.0, config.STRATEGY.ema_breakeven_reprice_minutes)
        if mode == "controlled_loss_exit":
            return max(0.0, config.STRATEGY.controlled_loss_reprice_minutes)
        return 0.0

    def _maybe_reprice_time_exit_ladder(self, symbol: str, signal: Optional[dict] = None) -> bool:
        state = self._get_state(symbol)
        mode = state.sell_ladder_mode if self._is_managed_exit_mode(state.sell_ladder_mode) else "time_exit"
        reprice_after = self._time_exit_reprice_after_minutes(mode)
        if reprice_after <= 0:
            return False

        if not self._is_managed_exit_mode(state.sell_ladder_mode) or not state.sell_ladder_orders:
            return False

        now = time.time()
        created_values = [
            self._safe_float(ref.get("created_at"), now)
            for ref in state.sell_ladder_orders
        ]
        oldest_created_at = min(created_values) if created_values else now
        if now - oldest_created_at < reprice_after * 60.0:
            return False

        self._log_event(
            "INFO",
            f"Repricing stale {mode} ladder for {symbol}",
            event="ema_breakeven_repriced" if mode == "breakeven" else "exit_ladder_rebuilt",
            symbol=symbol,
            side=config.EXIT_SIDE,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"{mode}_reprice;age_minutes={(now - oldest_created_at) / 60.0:.1f}",
        )
        self._cancel_sell_orders(symbol, reason=f"{mode}_reprice")
        state = self._get_state(symbol)
        if state.sell_ladder_orders:
            return True

        self._place_sell_ladder(ExitLadderConfig(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=True),
            mode=mode,
            signal=signal if mode == "controlled_loss_exit" else None,
        ))
        return True

    def _trigger_ema_broken_against_position(self, signal: Optional[dict]) -> bool:
        if not signal:
            return False
        if "trigger_valid" in signal:
            return not bool(signal.get("trigger_valid"))

        fast = self._safe_float(signal.get("ema_trigger_fast", signal.get("ema50")), 0.0)
        slow = self._safe_float(signal.get("ema_trigger_slow", signal.get("ema100")), 0.0)
        if fast <= 0 or slow <= 0:
            return False
        if config.POSITION_SIDE == "short":
            return fast >= slow
        return fast <= slow

    def _exit_runner_close_price(self, symbol: str, avg_entry_price: float, current_price: float) -> float:
        breakeven = self._breakeven_exit_price(avg_entry_price)
        if current_price <= 0 or breakeven <= 0:
            return 0.0
        if config.POSITION_SIDE == "short":
            if current_price > breakeven:
                return 0.0
            return self._price_at_or_below(symbol, min(current_price, breakeven))
        if current_price < breakeven:
            return 0.0
        return self._price_at_or_above(symbol, max(current_price, breakeven))

    def _exit_runner_close_contracts(self, symbol: str, state: TradeState) -> float:
        runner_contracts = self._safe_float(state.exit_runner_contracts, 0.0)
        non_runner_reserved = sum(
            self._safe_float(ref.get("amount"), 0.0)
            for ref in state.sell_ladder_orders
            if not ref.get("runner")
        )
        if runner_contracts <= 0:
            runner_contracts = max(0.0, state.position_size - non_runner_reserved)

        closeable = min(runner_contracts, max(0.0, state.position_size - non_runner_reserved))
        if state.position_available > 0:
            closeable = min(closeable, state.position_available)
        return self._amount_to_precision(symbol, closeable)

    def _exit_runner_reference_entry_price(self, symbol: str, state: TradeState) -> float:
        if self._should_use_split_exit_ladder(symbol, state, "normal") and state.base_entry_price > 0:
            return state.base_entry_price
        return state.entry_price

    def _exit_runner_base_contracts(self, symbol: str, state: TradeState) -> float:
        if self._should_use_split_exit_ladder(symbol, state, "normal"):
            return max(0.0, min(state.base_entry_amount, state.position_size))
        return state.position_size

    def _place_exit_runner_close_order(self, symbol: str, current_price: float, reason: str) -> bool:
        state = self._get_state(symbol)
        if any(ref.get("runner") for ref in state.sell_ladder_orders):
            return False
        if not config.RUNTIME.reduce_only_enabled:
            self._log_event(
                "ERROR",
                f"Runner close blocked for {symbol}: reduce-only disabled",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason="runner_reduce_only_disabled",
            )
            return True

        contracts = self._exit_runner_close_contracts(symbol, state)
        runner_entry_price = self._exit_runner_reference_entry_price(symbol, state)
        price = self._exit_runner_close_price(symbol, runner_entry_price, current_price)
        if contracts <= 0 or price <= 0:
            self._log_event(
                "WARNING",
                f"Runner close delayed for {symbol}: no safe closeable amount or price",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=price,
                amount=contracts,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{reason};runner_close_unavailable",
            )
            return True

        created_at = time.time()
        try:
            order = self._create_one_way_order(
                symbol=symbol,
                order_type="limit",
                side=config.EXIT_SIDE,
                amount=contracts,
                price=price,
                reduce_only=True,
            )
            order_id = str(order.get("id"))
        except Exception as exc:
            self._log_event(
                "ERROR",
                f"Runner reduce-only close order failed for {symbol}: {exc}",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=price,
                amount=contracts,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{reason};runner_order_rejected",
                exception=exc,
            )
            return True

        ref = {
            "id": order_id,
            "side": config.EXIT_SIDE,
            "price": price,
            "amount": contracts,
            "created_at": created_at,
            "stage": len(state.sell_ladder_orders) + 1,
            "mode": "normal",
            "runner": True,
            "exit_scope": "base",
            "reason": reason,
        }
        state.sell_ladder_orders.append(ref)
        state.exit_runner_contracts = contracts
        state.sell_ladder_signature = self._exit_ladder_signature("normal", symbol, state)
        self._refresh_active_side(state)
        self._save_state()
        self._log_event(
            "INFO",
            f"Runner close order placed for {symbol}: contracts={contracts} price={price}",
            event="exit_ladder_placed",
            symbol=symbol,
            side=config.EXIT_SIDE,
            order_id=order_id,
            price=price,
            amount=contracts,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=reason,
        )
        return True

    def _maybe_manage_exit_runner(self, symbol: str, signal: Optional[dict] = None) -> bool:
        strategy = config.STRATEGY
        if not strategy.ema_exit_runner_enabled:
            return False
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0 or state.sell_ladder_mode != "normal":
            return False
        if any(ref.get("runner") for ref in state.sell_ladder_orders):
            return False

        runner_base_contracts = self._exit_runner_base_contracts(symbol, state)
        runner_entry_price = self._exit_runner_reference_entry_price(symbol, state)
        steps, plan_context = self._sell_ladder_plan(symbol, runner_base_contracts, runner_entry_price, mode="normal", state=state)
        if not plan_context.get("runner_enabled"):
            if state.exit_runner_contracts > 0 or state.exit_runner_active:
                self._reset_exit_runner_state(state)
                self._save_state()
            return False

        if state.exit_runner_contracts <= 0:
            _, runner_contracts = self._exit_ladder_contract_allocations(symbol, runner_base_contracts, steps, state)
            state.exit_runner_contracts = runner_contracts
        if state.exit_runner_contracts <= 0:
            return False

        reference_price, last_price = self._fetch_reference_price(symbol)
        current_price = last_price or reference_price
        if current_price <= 0:
            return False

        activation = max(
            0.0,
            strategy.ema_exit_trailing_activation_markup
            if strategy.ema_exit_trailing_enabled
            else strategy.ema_exit_runner_activation_markup,
        )
        if config.POSITION_SIDE == "short":
            activation_reached = current_price <= state.entry_price * (1 - activation)
        else:
            activation_reached = current_price >= state.entry_price * (1 + activation)

        changed = False
        if not state.exit_runner_active:
            if not activation_reached:
                return False
            state.exit_runner_active = True
            state.exit_runner_activated_at = time.time()
            state.exit_runner_peak_price = current_price
            state.exit_runner_bottom_price = current_price
            changed = True
            self._log_event(
                "INFO",
                f"Runner activated for {symbol}: price={current_price}",
                event="exit_runner_activated",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=current_price,
                amount=state.exit_runner_contracts,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"runner_activation;ladder={plan_context.get('ladder_name', 'normal')}",
            )

        pullback = max(
            0.0,
            strategy.ema_exit_trailing_pullback
            if strategy.ema_exit_trailing_enabled
            else strategy.ema_exit_runner_trailing_pullback,
        )
        take_profit = max(
            0.0,
            strategy.ema_exit_trailing_take_profit_markup
            if strategy.ema_exit_trailing_enabled
            else strategy.ema_exit_runner_take_profit_markup,
        )
        close_reason = ""
        if config.POSITION_SIDE == "short":
            previous_bottom = state.exit_runner_bottom_price or current_price
            state.exit_runner_bottom_price = min(previous_bottom, current_price)
            changed = changed or state.exit_runner_bottom_price != previous_bottom
            if pullback > 0 and current_price >= state.exit_runner_bottom_price * (1 + pullback):
                close_reason = f"runner_trailing_pullback;bottom={state.exit_runner_bottom_price:.12f};pullback={pullback:.5f}"
            elif take_profit > 0 and current_price <= runner_entry_price * (1 - take_profit):
                close_reason = f"runner_take_profit;target_markup={take_profit:.5f}"
        else:
            previous_peak = state.exit_runner_peak_price or current_price
            state.exit_runner_peak_price = max(previous_peak, current_price)
            changed = changed or state.exit_runner_peak_price != previous_peak
            if pullback > 0 and current_price <= state.exit_runner_peak_price * (1 - pullback):
                close_reason = f"runner_trailing_pullback;peak={state.exit_runner_peak_price:.12f};pullback={pullback:.5f}"
            elif take_profit > 0 and current_price >= runner_entry_price * (1 + take_profit):
                close_reason = f"runner_take_profit;target_markup={take_profit:.5f}"

        if not close_reason and self._trigger_ema_broken_against_position(signal):
            close_reason = "runner_trigger_ema_broken"

        if close_reason:
            self._save_state()
            return self._place_exit_runner_close_order(symbol, current_price, close_reason)

        if changed:
            self._save_state()
        return False

    def _maybe_apply_absolute_force_exit(self, symbol: str, reason: str) -> bool:
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            return False
        if not self._absolute_force_exit_elapsed(state):
            return False

        held_minutes = self._position_held_minutes(state)
        had_sell_ladder = bool(state.sell_ladder_orders)
        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason=reason)
        if state.sell_ladder_orders:
            self._cancel_sell_orders(symbol, reason=reason)
            state = self._get_state(symbol)
            if state.sell_ladder_orders:
                return True
        if state.hard_stop_order:
            self._cancel_hard_stop_order(symbol, reason=reason)
            state = self._get_state(symbol)
            if state.hard_stop_order:
                return True

        state.frozen_no_more_buys = True
        state.zombie_position = True
        state.sell_ladder_mode = "absolute_force_exit"
        state.sell_ladder_signature = ""
        if not state.zombie_marked_at:
            state.zombie_marked_at = time.time()
        if not state.time_exit_activated_at:
            state.time_exit_activated_at = time.time()
        self._refresh_active_side(state)
        self._save_state()

        if had_sell_ladder:
            self._log_event(
                "INFO",
                f"Absolute force exit waiting for {symbol}: exit order cancellation must settle before market close",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"absolute_force_exit_wait_after_cancel;held_minutes={held_minutes:.1f}",
            )
            return True

        if not config.RUNTIME.reduce_only_enabled:
            self._log_event(
                "ERROR",
                f"Absolute force exit blocked for {symbol}: reduce-only disabled",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"absolute_force_exit_reduce_only_disabled;held_minutes={held_minutes:.1f}",
            )
            return True

        closeable = self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=False)
        closeable = min(max(0.0, closeable), max(0.0, state.position_size))
        close_amount = self._amount_to_precision(symbol, closeable)
        if close_amount <= 0:
            self._log_event(
                "WARNING",
                f"Absolute force exit delayed for {symbol}: no closeable amount",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"absolute_force_exit_no_closeable;held_minutes={held_minutes:.1f};available={state.position_available:.12f};frozen={state.position_frozen:.12f}",
            )
            return True

        try:
            order = self._create_one_way_order(
                symbol=symbol,
                order_type="market",
                side=config.EXIT_SIDE,
                amount=close_amount,
                price=None,
                reduce_only=True,
            )
            order_id = str(order.get("id", ""))
        except Exception as exc:
            self._log_event(
                "ERROR",
                f"Absolute force exit market order failed for {symbol}: {exc}",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=close_amount,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"absolute_force_exit_order_failed;held_minutes={held_minutes:.1f}",
                exception=exc,
            )
            return True

        self._log_event(
            "WARNING",
            f"Absolute force exit market order placed for {symbol}: contracts={close_amount}",
            event="absolute_force_exit_order_placed",
            symbol=symbol,
            side=config.EXIT_SIDE,
            order_id=order_id,
            amount=close_amount,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"{reason};held_minutes={held_minutes:.1f}",
        )
        return True

    def _maybe_apply_time_based_exit(self, symbol: str, signal: Optional[dict]) -> bool:
        if not config.STRATEGY.ema_breakeven_enabled:
            return False
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            return False

        held_minutes = self._position_held_minutes(state)
        breakeven_after_minutes = self._effective_time_exit_after_minutes()

        if state.sell_ladder_mode == "breakeven":
            if not state.frozen_no_more_buys:
                state.frozen_no_more_buys = True
                self._refresh_active_side(state)
                self._save_state()
            if state.sell_ladder_orders:
                return self._maybe_reprice_time_exit_ladder(symbol) or True
            if self._is_exit_ladder_waiting_for_closeable(symbol, "breakeven", state):
                return True
            self._place_sell_ladder(ExitLadderConfig(
                symbol,
                state.position_size,
                state.entry_price,
                rebuild=True,
                closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=False),
                mode="breakeven",
            ))
            return True

        if not state.cycle_opened_at or held_minutes < breakeven_after_minutes:
            return False

        had_sell_ladder = bool(state.sell_ladder_orders)
        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason="ema_breakeven_activated")
            state = self._get_state(symbol)
        state.frozen_no_more_buys = True
        state.sell_ladder_mode = "breakeven"
        state.sell_ladder_signature = ""
        now = time.time()
        state.time_exit_activated_at = state.time_exit_activated_at or now
        state.breakeven_activated_at = state.breakeven_activated_at or now
        self._refresh_active_side(state)
        self._save_state()

        if had_sell_ladder:
            self._cancel_sell_orders(symbol, reason="ema_breakeven_activated")
        state = self._get_state(symbol)
        if state.sell_ladder_orders:
            return True
        self._place_sell_ladder(ExitLadderConfig(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=had_sell_ladder),
            mode="breakeven",
        ))
        self._log_event(
            "INFO",
            f"EMA breakeven activated for {symbol}: held={held_minutes:.1f}m",
            event="ema_breakeven_activated",
            symbol=symbol,
            side=config.EXIT_SIDE,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"ema_breakeven_activated;holding_minutes={held_minutes:.1f}",
        )
        return True

    def _ensure_sell_ladder(self, symbol: str):
        state = self._get_state(symbol)
        if state.position_size <= 0:
            return
        mode = state.sell_ladder_mode if self._is_managed_exit_mode(state.sell_ladder_mode) else "normal"
        desired_signature = self._exit_ladder_signature(mode, symbol, state)
        if state.sell_ladder_orders:
            if state.sell_ladder_signature == desired_signature:
                return
            had_sell_ladder = True
            self._log_event(
                "INFO",
                f"{config.EXIT_SIDE} exit ladder config changed for {symbol}; rebuilding",
                event="exit_ladder_rebuilt",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=(
                    f"exit_ladder_config_changed;mode={mode};"
                    f"desired_external={ 'external_tightened' in desired_signature }"
                ),
            )
            self._cancel_sell_orders(symbol, reason="exit_ladder_config_changed")
            state = self._get_state(symbol)
            if state.sell_ladder_orders:
                return
        else:
            if state.sell_ladder_signature == desired_signature and state.exit_runner_contracts > 0:
                return
            if self._is_exit_ladder_waiting_for_closeable(symbol, mode, state):
                return
            had_sell_ladder = False
        self._place_position_exit_ladder(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=False,
            closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=had_sell_ladder),
            mode=mode,
        )


__all__ = ["ExitStrategy"]
