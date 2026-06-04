# -*- coding: utf-8 -*-

import csv
import threading
import time
from typing import Dict, List, Optional, Tuple

import config

from .models import ExitLadderConfig, ExitLadderPreflight, SellLadderParams, TradeState


class EntryStrategy:
    def _place_buy_ladder(
        self,
        symbol: str,
        margin_budget: float,
        reference_price: float,
        signal: dict,
        reason: str,
        offset_multiplier: float = 1.0,
    ) -> int:
        state = self._get_state(symbol)
        was_frozen_no_more_buys = bool(state.frozen_no_more_buys)
        state.entry_orders = []
        state.planned_quote_budget = max(state.planned_quote_budget, margin_budget)
        state.frozen_no_more_buys = False
        state.last_entry_ladder_signal_timestamp = signal.get("ts")
        state.last_signal_timestamp = signal.get("ts")
        state.last_rs30 = signal.get("rs30", state.last_rs30)
        state.last_rs60 = signal.get("rs60", state.last_rs60)
        state.last_ema30 = signal.get("ema30", state.last_ema30)
        state.last_ema60 = signal.get("ema60", state.last_ema60)
        state.strategy_name = "ema_pullback"
        state.last_ema25d = self._safe_float(signal.get("ema_macro_fast"), state.last_ema25d)
        state.last_ema50d = self._safe_float(signal.get("ema_macro_slow"), state.last_ema50d)
        state.last_ema1d = self._safe_float(signal.get("ema_pullback_fast"), state.last_ema1d)
        state.last_ema2d = self._safe_float(signal.get("ema_pullback_slow"), state.last_ema2d)
        state.last_ema50 = self._safe_float(signal.get("ema_trigger_fast"), state.last_ema50)
        state.last_ema100 = self._safe_float(signal.get("ema_trigger_slow"), state.last_ema100)
        state.last_btc_return_30m = self._safe_float(signal.get("btc_return_30m"), state.last_btc_return_30m)
        if state.cycle_opened_at is None:
            state.cycle_opened_at = time.time()
            state.entry_rs30 = state.last_rs30
            state.entry_rs60 = state.last_rs60
            state.entry_ema30 = state.last_ema30
            state.entry_ema60 = state.last_ema60
            state.entry_ema25d = state.last_ema25d
            state.entry_ema50d = state.last_ema50d
            state.entry_ema1d = state.last_ema1d
            state.entry_ema2d = state.last_ema2d
            state.entry_ema50 = state.last_ema50
            state.entry_ema100 = state.last_ema100
            state.entry_btc_return_30m = state.last_btc_return_30m
        if not state.cycle_id:
            state.cycle_id = self._new_cycle_id(symbol, signal)
        cycle_id = state.cycle_id

        ladder_multiplier = self._safe_float(signal.get("ladder_multiplier"), 1.0) * max(0.0, offset_multiplier)
        created_at = time.time()
        operation_id = self._operation_id("entry_ladder", symbol=symbol, signal=signal, suffix=reason)
        planned_orders = 0
        planned_notional = 0.0
        placed_orders = 0
        configured_leverage = max(self._safe_float(config.RISK.leverage, 0.0), 1.0)
        if config.EXCHANGE.set_leverage_on_start:
            self._set_leverage_safe(symbol, int(configured_leverage))

        account_leverage = self._fetch_account_order_leverage(symbol)
        entry_side = config.ENTRY_SIDE
        entry_label = "Sell" if entry_side == "sell" else "Buy"
        if account_leverage <= 0:
            self._log_event(
                "ERROR",
                f"{entry_label} entry ladder blocked for {symbol}: manual HTX leverage is unknown",
                event="entry_order_canceled",
                symbol=symbol,
                side=entry_side,
                reason="manual_account_leverage_unavailable",
            )
            self._reset_state(symbol)
            return 0

        sizing_leverage = max(account_leverage, 1.0)
        notional_budget = margin_budget * sizing_leverage
        if state.position_size <= 0 and state.initial_entry_notional <= 0:
            state.initial_entry_notional = notional_budget

        for index, (fraction, offset) in enumerate(zip(config.BUYING.ladder_fractions, config.BUYING.ladder_offsets), start=1):
            if entry_side == "sell":
                raw_price = reference_price * (1 + offset * ladder_multiplier)
                price = self._price_at_or_above(symbol, raw_price)
            else:
                raw_price = reference_price * (1 - offset * ladder_multiplier)
                price = self._price_at_or_below(symbol, raw_price)
            notional = notional_budget * fraction
            contracts = self._contracts_for_notional(symbol, notional, price)
            if contracts <= 0:
                self._log_event(
                    "DEBUG",
                    f"{entry_label} entry ladder stage skipped for {symbol}: amount below minimum",
                    event="entry_order_canceled",
                    symbol=symbol,
                    side=entry_side,
                    price=price,
                    reason=f"stage_{index}_below_minimum",
                )
                continue

            planned_orders += 1
            planned_notional += notional
            order_leverage = account_leverage
            try:
                order = self._create_one_way_order(
                    symbol=symbol,
                    order_type="limit",
                    side=entry_side,
                    amount=contracts,
                    price=price,
                    post_only=config.RUNTIME.post_only_enabled,
                    leverage=order_leverage,
                )
                order_id = str(order.get("id"))
            except Exception as exc:
                band_limit = self._price_band_limit_from_error(exc, side=entry_side)
                if band_limit > 0:
                    adjusted_price = self._price_inside_htx_band(symbol, price, side=entry_side, limit=band_limit)
                    try:
                        order = self._create_one_way_order(
                            symbol=symbol,
                            order_type="limit",
                            side=entry_side,
                            amount=contracts,
                            price=adjusted_price,
                            post_only=config.RUNTIME.post_only_enabled,
                            leverage=order_leverage,
                        )
                        price = adjusted_price
                        order_id = str(order.get("id"))
                        self._log_event(
                            "WARNING",
                            f"{entry_label} entry order price adjusted for HTX band {symbol}: {price}",
                            event="entry_ladder_placed",
                            symbol=symbol,
                            side=entry_side,
                            price=price,
                            amount=contracts,
                            reason=f"htx_price_band_adjusted;limit={band_limit:.12f}",
                        )
                    except Exception as retry_exc:
                        self._log_event(
                            "WARNING",
                            f"{entry_label} entry order rejected for {symbol} after HTX band adjustment: {retry_exc}",
                            event="entry_order_canceled",
                            symbol=symbol,
                            side=entry_side,
                            price=adjusted_price,
                            amount=contracts,
                            reason="price_band_retry_rejected",
                            exception=retry_exc,
                        )
                        continue
                else:
                    if self._is_high_leverage_risk_error(exc):
                        reject_reason = "manual_account_leverage_rejected"
                    elif self._is_hedge_mode_error(exc):
                        reject_reason = "hedge_mode_error"
                    else:
                        reject_reason = "entry_order_rejected"
                    self._log_event(
                        "WARNING",
                        f"{entry_label} entry order rejected for {symbol}: {exc}",
                        event="entry_order_canceled",
                        symbol=symbol,
                        side=entry_side,
                        price=price,
                        amount=contracts,
                        reason=reject_reason,
                        exception=exc,
                    )
                    continue

            ref = {
                "id": order_id,
                "side": entry_side,
                "price": price,
                "amount": contracts,
                "leverage": order_leverage,
                "sizing_leverage": sizing_leverage,
                "created_at": created_at,
                "stage": index,
                "signal_ts": signal.get("ts"),
                "reason": reason,
                "operation_id": operation_id,
                "cycle_id": cycle_id,
            }
            state.entry_orders.append(ref)
            placed_orders += 1
            event = "entry_ladder_placed"
            action = "placed"
            self._record_signal_analytics(
                event,
                symbol=symbol,
                signal=signal,
                planned_budget=margin_budget,
                planned_orders=planned_orders,
                planned_notional=planned_notional,
                placed_orders=placed_orders,
                operation_id=operation_id,
                order_id=order_id,
                cycle_id=cycle_id,
                context={
                    "stage": index,
                    "price": price,
                    "contracts": contracts,
                    "stage_notional": notional,
                    "ladder_multiplier": ladder_multiplier,
                    "sizing_leverage": sizing_leverage,
                    "account_leverage": order_leverage,
                    "reason": reason,
                },
            )
            self._log_event(
                "INFO",
                f"{entry_label} entry ladder {action} for {symbol}: stage={index} contracts={contracts} price={price}",
                event=event,
                symbol=symbol,
                side=entry_side,
                order_id=order_id,
                price=price,
                amount=contracts,
                reason=(
                    f"{reason};ladder_multiplier={ladder_multiplier:.3f};"
                    f"sizing_leverage={sizing_leverage:g};account_leverage={order_leverage:g}"
                ),
            )

        if not state.entry_orders:
            last_ladder_ts = state.last_entry_ladder_signal_timestamp
            last_signal_ts = state.last_signal_timestamp
            last_rs30 = state.last_rs30
            last_rs60 = state.last_rs60
            last_ema30 = state.last_ema30
            last_ema60 = state.last_ema60
            state.active_side = None
            self._record_signal_analytics(
                "entry_ladder_rejected",
                symbol=symbol,
                signal=signal,
                block_reason="no_valid_ladder_orders",
                planned_budget=margin_budget,
                planned_orders=planned_orders,
                planned_notional=planned_notional,
                placed_orders=placed_orders,
                operation_id=operation_id,
                cycle_id=cycle_id,
            )
            self._log_event(
                "INFO",
                f"No {entry_side} entry ladder orders placed for {symbol}",
                event="entry_order_canceled",
                symbol=symbol,
                side=entry_side,
                reason="no_valid_ladder_orders",
            )
            if state.position_size > 0:
                state.frozen_no_more_buys = was_frozen_no_more_buys or state.zombie_position
                state.last_entry_ladder_signal_timestamp = last_ladder_ts
                state.last_signal_timestamp = last_signal_ts
                state.last_rs30 = last_rs30
                state.last_rs60 = last_rs60
                state.last_ema30 = last_ema30
                state.last_ema60 = last_ema60
                self._refresh_active_side(state)
            else:
                self._reset_state(symbol)
                reset_state = self._get_state(symbol)
                reset_state.last_entry_ladder_signal_timestamp = last_ladder_ts
                reset_state.last_signal_timestamp = last_signal_ts
                reset_state.last_rs30 = last_rs30
                reset_state.last_rs60 = last_rs60
                reset_state.last_ema30 = last_ema30
                reset_state.last_ema60 = last_ema60
            self._save_state()
            return placed_orders

        self._refresh_active_side(state)
        self._save_state()
        return placed_orders

    def _manage_entry_orders(self, symbol: str, signal: Optional[dict], open_orders: List[dict]):
        state = self._get_state(symbol)
        if not state.entry_orders:
            return

        if symbol not in self.entry_symbols:
            self._cancel_entry_orders(symbol, reason="symbol_removed_from_entry_universe")
            if state.position_size <= 0:
                self._reset_state(symbol)
            else:
                state.frozen_no_more_buys = True
                self._save_state()
            return

        is_average_ladder = state.position_size > 0 or any(
            "ema_averaging" in str(ref.get("reason") or "")
            for ref in state.entry_orders
        )
        signal_for_quality = dict(signal or {})
        signal_for_quality["symbol"] = symbol
        signal_block_reason = (
            self._averaging_signal_block_reason(signal_for_quality)
            if is_average_ladder
            else self._entry_signal_quality_block_reason(signal_for_quality)
        )
        if signal_block_reason:
            self._cancel_entry_orders(
                symbol,
                reason=signal_block_reason if is_average_ladder else "ema_entry_signal_invalid",
            )
            if state.position_size <= 0:
                last_ladder_ts = state.last_entry_ladder_signal_timestamp
                self._reset_state(symbol)
                reset_state = self._get_state(symbol)
                reset_state.last_entry_ladder_signal_timestamp = last_ladder_ts
                self._save_state()
            return

        external_directional_reason = self._external_directional_1m_block_reason(
            symbol,
            scope="averaging" if is_average_ladder else "entry",
        )
        if external_directional_reason:
            self._cancel_entry_orders(symbol, reason=external_directional_reason)
            if state.position_size <= 0:
                last_ladder_ts = state.last_entry_ladder_signal_timestamp
                self._reset_state(symbol)
                reset_state = self._get_state(symbol)
                reset_state.last_entry_ladder_signal_timestamp = last_ladder_ts
                self._save_state()
            return

        oldest = min(self._safe_float(ref.get("created_at"), time.time()) for ref in state.entry_orders)
        if time.time() - oldest > config.RUNTIME.order_timeout_sec:
            self._cancel_entry_orders(symbol, reason="order_timeout")
            self._maybe_close_tiny_partial_entry_after_timeout(symbol, open_orders=open_orders)
            return

        open_ids = {str(order.get("id")) for order in open_orders}
        active_refs = [ref for ref in state.entry_orders if str(ref.get("id")) in open_ids]
        if len(active_refs) != len(state.entry_orders):
            missing = len(state.entry_orders) - len(active_refs)
            state.entry_orders = active_refs
            self._refresh_active_side(state)
            self._log_event(
                "INFO",
                f"Entry orders changed on exchange for {symbol}: missing={missing}",
                event="state_exchange_mismatch",
                symbol=symbol,
                reason="entry_order_missing_from_open_orders",
            )
            self._save_state()

    def _validate_entry_orders(self, symbol: str, open_orders: List[dict]) -> bool:
        state = self._get_state(symbol)
        entry_side = config.ENTRY_SIDE
        open_entry_orders = [order for order in open_orders if (order.get("side") or "").lower() == entry_side]
        known_entry_ids = self._order_ids(state.entry_orders)
        unknown_entries = [order for order in open_entry_orders if str(order.get("id")) not in known_entry_ids]

        if not unknown_entries:
            return True

        self._log_event(
            "WARNING",
            f"Unknown {entry_side} entry orders found for {symbol}; canceling before any new entry",
            event="state_exchange_mismatch",
            symbol=symbol,
            side=entry_side,
            reason="unknown_entry_orders",
        )
        self._cancel_exchange_orders(
            symbol,
            unknown_entries,
            side=entry_side,
            reason="unknown_entry_orders",
            event="entry_order_canceled",
        )

        if state.position_size > 0:
            self._freeze_no_more_buys(symbol, reason="unknown_entry_orders")

        return False

    def _freeze_no_more_buys(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        if not state.frozen_no_more_buys:
            state.frozen_no_more_buys = True
            self._log_event(
                "INFO",
                f"Position frozen for {symbol}: {reason}",
                event="position_frozen",
                symbol=symbol,
                reason=reason,
            )
        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason=reason)
        self._save_state()

    def _averaging_stage_plan(
        self,
        state: TradeState,
        signal: dict,
        daily_volatility_fraction: Optional[float] = None,
    ) -> Tuple[int, float, float]:
        strategy = config.STRATEGY
        steps = tuple(strategy.averaging_drawdown_steps)
        fractions = tuple(strategy.averaging_budget_fractions)
        if not steps or not fractions:
            return -1, 0.0, 0.0

        stage = max(1, int(state.buy_stage or 1))
        if stage >= strategy.max_buy_stages:
            return -1, 0.0, 0.0

        index = min(stage - 1, len(steps) - 1, len(fractions) - 1)
        volatility_multiplier = max(1.0, self._safe_float(signal.get("volatility_multiplier"), 1.0))
        drawdown_threshold = steps[index] * volatility_multiplier
        if strategy.enable_volatility_recovery_stages:
            daily_volatility = max(0.0, self._safe_float(signal.get("daily_volatility"), 0.0))
            fraction = (
                strategy.averaging_drawdown_daily_volatility_fraction
                if daily_volatility_fraction is None
                else daily_volatility_fraction
            )
            daily_threshold = daily_volatility * max(0.0, fraction) * (index + 1)
            drawdown_threshold = max(drawdown_threshold, daily_threshold)
        budget_fraction = fractions[index]
        return index + 2, drawdown_threshold, budget_fraction

    def _ema_averaging_budget(
        self,
        symbol: str,
        state: TradeState,
        reference_price: float,
        budget_scale: float = 1.0,
    ) -> Tuple[float, str]:
        account = self._account_snapshot()
        free = account["free"]
        equity = account["total"] or free
        if free <= config.RISK.min_quote_reserve:
            return 0.0, "free_margin_below_reserve"

        if config.EXCHANGE.set_leverage_on_start:
            self._set_leverage_safe(symbol, int(config.RISK.leverage))

        available_after_reserve = max(0.0, free - config.RISK.min_quote_reserve)
        leverage = max(float(config.RISK.leverage), 1.0)
        current_position_notional = self._position_notional(symbol, state)
        effective_scale = max(0.0, budget_scale)
        base_notional = self._safe_float(state.initial_entry_notional, 0.0)
        if base_notional <= 0:
            base_notional = self._contracts_to_notional(symbol, state.base_entry_amount, state.base_entry_price)
        if base_notional <= 0:
            base_notional = self._contracts_to_notional(symbol, state.position_size, state.entry_price)
        if current_position_notional <= 0 or base_notional <= 0:
            return 0.0, "position_notional_unavailable"

        base_fraction = max(0.0, self._safe_float(config.STRATEGY.ema_averaging_base_fraction, 0.0))
        power = max(0.0, self._safe_float(config.STRATEGY.ema_averaging_power, 0.0))
        ratio = max(current_position_notional / base_notional, 1.0)
        desired_notional = base_fraction * base_notional * (ratio ** power) * effective_scale
        desired_margin = desired_notional / leverage

        total_cap_notional = equity * leverage * config.RISK.max_total_notional_fraction
        position_cap_notional = equity * leverage * config.RISK.max_position_notional_fraction
        current_total_notional = self._current_total_notional()
        current_symbol_notional = self._symbol_open_notional(symbol, state)
        total_remaining = max(0.0, total_cap_notional - current_total_notional)
        symbol_remaining = max(0.0, position_cap_notional - current_symbol_notional)
        margin_cap_notional = available_after_reserve * leverage
        planned_notional = min(desired_notional, total_remaining, symbol_remaining, margin_cap_notional)

        min_contracts = self._get_min_contracts(symbol)
        min_notional = self._contracts_to_notional(symbol, min_contracts, reference_price)
        if 0 < planned_notional < min_notional <= min(desired_notional, total_remaining, symbol_remaining, margin_cap_notional):
            planned_notional = min_notional

        planned_margin = planned_notional / leverage
        if planned_notional <= 0 or planned_margin <= 0:
            return 0.0, "notional_limit_reached"
        contracts = self._contracts_for_notional(symbol, planned_notional, reference_price)
        if contracts <= 0:
            return 0.0, "order_size_below_exchange_minimum"
        return planned_margin, (
            f"ok:ema_average_base_fraction={base_fraction:.3f};"
            f"ema_average_power={power:.3f};"
            f"account_budget_scale={effective_scale:.3f};"
            f"base_notional={base_notional:.8f};"
            f"current_notional={current_position_notional:.8f};"
            f"ratio={ratio:.6f};"
            f"desired_margin={desired_margin:.8f};planned_margin={planned_margin:.8f}"
        )

    def _account_averaging_block_reason(
        self,
        symbol: str,
        state: TradeState,
        signal: Optional[dict],
    ) -> str:
        strategy = config.STRATEGY
        if not (strategy.account_pnl_enabled and strategy.account_averaging_enabled):
            return ""
        if signal and self._trigger_ema_broken_against_position(signal):
            return "account_averaging_falling_trend_trigger_broken"
        if signal and signal.get("btc_entry_valid") is False:
            return "account_averaging_btc_guard"

        context = self._account_pnl_context(reason="account_averaging")
        samples = int(context.get("history_samples") or 0)
        min_samples = max(1, int(strategy.account_averaging_min_samples))
        if samples < min_samples:
            return ""

        current = self._safe_float(context.get("open_pnl"), 0.0)
        trough = self._safe_float(context.get("min_open_pnl"), current)
        percentile = self._quantile(
            list(context.get("history_values") or []),
            self._clamp(strategy.account_averaging_percentile, 0.0, 1.0),
        )
        trough_band = max(
            0.0,
            strategy.account_averaging_near_trough_quote,
            abs(trough) * max(0.0, strategy.account_averaging_near_trough_fraction),
        )
        allowed_ceiling = max(trough + trough_band, percentile)
        if current > allowed_ceiling + 1e-12:
            return (
                "account_averaging_not_near_trough;"
                f"account_pnl={current:.8f};allowed={allowed_ceiling:.8f};trough={trough:.8f};pctl={percentile:.8f}"
            )

        previous = self._safe_float(context.get("previous_open_pnl"), current)
        delta = self._safe_float(context.get("delta_open_pnl"), 0.0)
        falling_guard = max(
            0.0,
            strategy.account_averaging_falling_guard_quote,
            abs(current) * max(0.0, strategy.account_averaging_falling_guard_fraction),
        )
        if previous and delta < -falling_guard:
            return (
                "account_averaging_falling_account_pnl;"
                f"delta={delta:.8f};guard={falling_guard:.8f};account_pnl={current:.8f}"
            )

        bounce_quote = max(0.0, strategy.account_averaging_bounce_quote)
        if bounce_quote > 0 and current < trough + bounce_quote and delta < 0:
            return (
                "account_averaging_waiting_for_bounce;"
                f"account_pnl={current:.8f};trough={trough:.8f};bounce={bounce_quote:.8f};delta={delta:.8f}"
            )
        return ""

    def _averaging_signal_block_reason(self, signal: Optional[dict]) -> str:
        if not signal:
            return "signal_missing"
        if not self.signal_cache.get("benchmark_ok"):
            return "benchmark_unavailable"
        if not self._signal_direction_valid(signal):
            return "signal_invalid"
        if not signal.get("macro_valid", False):
            return "ema_macro_broken_no_average"
        if not bool(signal.get("market_structure_valid", True)):
            reason = getattr(self, "_signal_market_structure_block_reason", None)
            if reason:
                return reason(signal, prefix="ema_market_structure_invalid")
            return "ema_market_structure_invalid"
        if not signal.get("add_valid", False):
            return "ema_add_signal_invalid"
        if config.STRATEGY.ema_averaging_require_pullback_recovery and not signal.get("pullback_valid", False):
            return (
                "ema_averaging_pullback_recovery_required;"
                f"pullback_gap={self._safe_float(signal.get('pullback_recovery_gap'), 0.0):.6f};"
                f"min_gap={self._safe_float(signal.get('pullback_recovery_min_gap'), 0.0):.6f};"
                f"trigger_valid={int(bool(signal.get('trigger_valid', False)))}"
            )
        return ""

    def _ema_averaging_drawdown_threshold_context(self, stage_index: int, signal: Optional[dict] = None) -> dict:
        strategy = config.STRATEGY
        stage_number = max(1, int(stage_index) + 1)
        steps = tuple(config.STRATEGY.averaging_drawdown_steps or ())
        if steps:
            index = min(max(0, stage_index), len(steps) - 1)
            configured_threshold = max(0.0, self._safe_float(steps[index], 0.0))
        else:
            configured_threshold = max(0.0, self._safe_float(config.STRATEGY.ema_averaging_drawdown_step, 0.0)) * stage_number

        floor_threshold = max(0.0, self._safe_float(strategy.ema_averaging_min_drawdown_step, 0.0)) * stage_number
        atr_rate = max(0.0, self._safe_float((signal or {}).get("atr_rate"), 0.0))
        hard_atr_threshold = (
            atr_rate
            * max(0.0, self._safe_float(strategy.ema_averaging_min_atr_multiplier, 0.0))
            * stage_number
        )
        daily_volatility = max(0.0, self._safe_float((signal or {}).get("daily_volatility"), 0.0))
        daily_volatility_threshold = (
            daily_volatility
            * max(0.0, self._safe_float(strategy.ema_averaging_min_daily_volatility_fraction, 0.0))
            * stage_number
        )

        configured_atr_threshold = 0.0
        if strategy.ema_averaging_atr_enabled and atr_rate > 0:
            configured_atr_threshold = (
                atr_rate
                * max(0.0, self._safe_float(strategy.ema_averaging_atr_multiplier, 0.0))
                * stage_number
            )

        threshold = max(
            configured_threshold,
            floor_threshold,
            hard_atr_threshold,
            daily_volatility_threshold,
            configured_atr_threshold,
        )
        return {
            "threshold": threshold,
            "configured": configured_threshold,
            "floor": floor_threshold,
            "atr_floor": hard_atr_threshold,
            "daily_volatility_floor": daily_volatility_threshold,
            "configured_atr": configured_atr_threshold,
            "atr_rate": atr_rate,
            "daily_volatility": daily_volatility,
            "stage": stage_number,
        }

    def _ema_averaging_drawdown_threshold(self, stage_index: int, signal: Optional[dict] = None) -> float:
        return self._ema_averaging_drawdown_threshold_context(stage_index, signal).get("threshold", 0.0)

    def _maybe_place_average_buy(self, symbol: str, signal: Optional[dict]):
        if symbol not in self.entry_symbols:
            return
        strategy = config.STRATEGY
        if not strategy.ema_averaging_enabled:
            return
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            return
        if state.breakeven_activated_at or state.sell_ladder_mode == "breakeven":
            return
        if self._position_too_old_for_averaging(state):
            held_minutes = self._position_held_minutes(state)
            block_reason = f"ema_position_too_old_for_averaging;holding_minutes={held_minutes:.1f}"
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal,
                block_reason=block_reason,
                context={"position_size": state.position_size, "entry_price": state.entry_price},
            )
            self._log_event(
                "DEBUG",
                f"EMA averaging skipped for {symbol}: position is too old",
                event="ema_average_skipped",
                symbol=symbol,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=block_reason,
            )
            return
        if state.zombie_position or state.frozen_no_more_buys or state.entry_orders:
            return
        if state.sell_ladder_orders == []:
            return
        signal_for_quality = dict(signal or {})
        signal_for_quality["symbol"] = symbol
        signal_block_reason = self._averaging_signal_block_reason(signal_for_quality)
        if signal_block_reason:
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal_for_quality,
                block_reason=signal_block_reason,
                context={"position_size": state.position_size, "entry_price": state.entry_price},
            )
            if signal_block_reason == "ema_macro_broken_no_average":
                self._log_event(
                    "DEBUG",
                    f"EMA averaging skipped for {symbol}: macro signal broken",
                    event="ema_average_skipped",
                    symbol=symbol,
                    position_size=state.position_size,
                    entry_price=state.entry_price,
                    reason=signal_block_reason,
                )
            else:
                self._log_event(
                    "DEBUG",
                    f"EMA averaging skipped for {symbol}: add signal invalid",
                    event="ema_average_skipped",
                    symbol=symbol,
                    position_size=state.position_size,
                    entry_price=state.entry_price,
                    reason=signal_block_reason,
                )
            return

        macro_context = self._macro_guard_context()
        if macro_context.get("disable_averaging"):
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal,
                block_reason="macro_disable_averaging",
                context={"macro_context": macro_context},
            )
            self._log_macro_action_blocked("macro_averaging_blocked", symbol, signal, macro_context)
            return

        account_block_reason = self._account_averaging_block_reason(symbol, state, signal)
        if account_block_reason:
            self._log_event(
                "DEBUG",
                f"EMA averaging skipped for {symbol}: {account_block_reason}",
                event="ema_average_skipped",
                symbol=symbol,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"account_averaging_blocked:{account_block_reason}",
            )
            return

        if state.average_stage >= max(0, int(strategy.ema_max_averaging_stages)):
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal,
                block_reason="ema_max_averaging_stages_reached",
                context={"average_stage": state.average_stage},
            )
            self._log_event(
                "DEBUG",
                f"EMA averaging skipped for {symbol}: max stages reached",
                event="ema_average_skipped",
                symbol=symbol,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"ema_max_averaging_stages_reached;average_stage={state.average_stage}",
            )
            return

        interval_sec = max(0.0, strategy.ema_averaging_interval_hours) * 60.0 * 60.0
        if state.last_average_at and interval_sec > 0 and time.time() - state.last_average_at < interval_sec:
            return
        if state.last_average_signal_timestamp == (signal or {}).get("ts"):
            return

        reference_price, _ = self._fetch_reference_price(symbol)
        if reference_price <= 0:
            return

        drawdown = self._position_drawdown(state, reference_price)
        threshold_context = self._ema_averaging_drawdown_threshold_context(state.average_stage, signal)
        drawdown_threshold = self._safe_float(threshold_context.get("threshold"), 0.0)
        atr_rate = self._safe_float(threshold_context.get("atr_rate"), 0.0)
        daily_volatility = self._safe_float(threshold_context.get("daily_volatility"), 0.0)
        threshold_reason = (
            f"configured={self._safe_float(threshold_context.get('configured'), 0.0):.5f};"
            f"floor={self._safe_float(threshold_context.get('floor'), 0.0):.5f};"
            f"atr_floor={self._safe_float(threshold_context.get('atr_floor'), 0.0):.5f};"
            f"daily_volatility_floor={self._safe_float(threshold_context.get('daily_volatility_floor'), 0.0):.5f};"
            f"configured_atr={self._safe_float(threshold_context.get('configured_atr'), 0.0):.5f}"
        )

        external_directional_reason = self._external_directional_1m_block_reason(symbol, scope="averaging")
        if external_directional_reason:
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal,
                block_reason=external_directional_reason,
                external_context=self._external_context_from_cache(symbol),
                context={
                    "drawdown": drawdown,
                    "threshold": drawdown_threshold,
                    "atr_rate": atr_rate,
                    "daily_volatility": daily_volatility,
                    "stage": state.average_stage + 1,
                },
            )
            self._log_event(
                "INFO",
                f"EMA averaging skipped for {symbol}: external 1m gate",
                event="ema_average_skipped",
                symbol=symbol,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=external_directional_reason,
            )
            return

        account_budget_scale = (
            self._clamp(strategy.account_averaging_budget_scale, 0.0, 1.0)
            if strategy.account_pnl_enabled and strategy.account_averaging_enabled
            else 1.0
        )
        budget, budget_reason = self._ema_averaging_budget(
            symbol,
            state,
            reference_price,
            budget_scale=account_budget_scale,
        )
        if drawdown + 1e-12 < drawdown_threshold:
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal,
                block_reason="ema_averaging_drawdown_below_threshold",
                external_context=self._external_context_from_cache(symbol),
                context={
                    "drawdown": drawdown,
                    "threshold": drawdown_threshold,
                    "atr_rate": atr_rate,
                    "daily_volatility": daily_volatility,
                    "stage": state.average_stage + 1,
                },
            )
            self._log_event(
                "DEBUG",
                f"EMA averaging skipped for {symbol}: drawdown below threshold",
                event="ema_average_skipped",
                symbol=symbol,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=(
                    f"ema_averaging_drawdown_below_threshold;"
                    f"drawdown={drawdown:.5f};threshold={drawdown_threshold:.5f};"
                    f"atr_rate={atr_rate:.6f};daily_volatility={daily_volatility:.6f};"
                    f"{threshold_reason}"
                ),
            )
            return
        self._record_signal_analytics(
            "averaging_checked",
            symbol=symbol,
            signal=signal,
            block_reason="" if budget > 0 else budget_reason,
            planned_budget=budget,
            planned_notional=budget * max(float(config.RISK.leverage), 1.0),
            context={
                "drawdown": drawdown,
                "threshold": drawdown_threshold,
                "atr_rate": atr_rate,
                "daily_volatility": daily_volatility,
                "stage": state.average_stage + 1,
                "budget_reason": budget_reason,
                "threshold_reason": threshold_reason,
            },
        )
        if budget <= 0:
            self._log_event(
                "INFO",
                f"EMA averaging skipped for {symbol}: {budget_reason}",
                event="ema_average_skipped",
                symbol=symbol,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"ema_averaging_blocked:{budget_reason}",
            )
            return

        previous_average_stage = state.average_stage
        state.last_average_signal_timestamp = (signal or {}).get("ts")
        state.last_average_at = time.time()
        next_stage = previous_average_stage + 1
        self._log_event(
            "INFO",
            f"EMA averaging {config.ENTRY_SIDE} entry ladder for {symbol}: stage={next_stage} drawdown={drawdown:.5f} threshold={drawdown_threshold:.5f}",
            event="ema_average_placed",
            symbol=symbol,
            side=config.ENTRY_SIDE,
            price=reference_price,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=(
                f"ema_averaging_stage_{next_stage};drawdown={drawdown:.5f};"
                f"threshold={drawdown_threshold:.5f};atr_rate={atr_rate:.6f};"
                f"daily_volatility={daily_volatility:.6f};{threshold_reason};{budget_reason}"
            ),
        )
        orders_placed = self._place_buy_ladder(
            symbol,
            budget,
            reference_price,
            signal,
            reason=f"ema_averaging_stage_{next_stage};{budget_reason}",
            offset_multiplier=1.0,
        )
        state = self._get_state(symbol)
        if orders_placed > 0:
            state.average_stage = next_stage
        else:
            state.average_stage = previous_average_stage
        self._save_state()

    def _maybe_place_initial_buy(self, symbol: str, signal: Optional[dict]):
        if symbol not in self.entry_symbols:
            return
        state = self._get_state(symbol)
        if state.position_size > 0 or state.entry_orders or state.frozen_no_more_buys or state.zombie_position:
            return
        if state.cooldown_until and time.time() < state.cooldown_until:
            return
        signal_for_quality = dict(signal or {})
        signal_for_quality["symbol"] = symbol
        quality_reason = self._entry_signal_quality_block_reason(signal_for_quality)
        if quality_reason:
            self._record_signal_analytics(
                "entry_gate_checked",
                symbol=symbol,
                signal=signal_for_quality,
                block_reason=quality_reason,
            )
            return
        if state.last_entry_ladder_signal_timestamp == (signal or {}).get("ts"):
            return
        macro_context = self._macro_guard_context()
        if macro_context.get("disable_new_entries"):
            self._record_signal_analytics(
                "entry_gate_checked",
                symbol=symbol,
                signal=signal,
                block_reason="macro_disable_new_entries",
                context={"macro_context": macro_context},
            )
            self._log_macro_action_blocked("macro_entry_blocked", symbol, signal, macro_context)
            return
        gate_reason = self._entry_gate_block_reason(symbol, signal)
        if gate_reason:
            self._record_signal_analytics(
                "entry_gate_checked",
                symbol=symbol,
                signal=signal,
                block_reason=gate_reason,
            )
            logged = getattr(self, "_entry_gate_skip_logged", set())
            key = (symbol, (signal or {}).get("ts"), gate_reason)
            if key not in logged:
                logged.add(key)
                self._entry_gate_skip_logged = logged
                self._log_event(
                    "DEBUG",
                    f"Signal skipped for {symbol}: entry gate",
                    event="signal_valid",
                    symbol=symbol,
                    reason=gate_reason,
                )
            return
        health_reason = self._profile_health_block_reason()
        if health_reason:
            self._record_signal_analytics(
                "entry_gate_checked",
                symbol=symbol,
                signal=signal,
                block_reason=health_reason,
            )
            self._log_event(
                "INFO",
                f"Signal skipped for {symbol}: profile health gate",
                event="signal_valid",
                symbol=symbol,
                reason=health_reason,
            )
            return

        reference_price, _ = self._fetch_reference_price(symbol)
        if reference_price <= 0:
            self._record_signal_analytics(
                "entry_gate_checked",
                symbol=symbol,
                signal=signal,
                block_reason="reference_price_unavailable",
            )
            self._log_event(
                "WARNING",
                f"No reference bid/last price for {symbol}",
                event="signal_invalid",
                symbol=symbol,
                reason="reference_price_unavailable",
            )
            return

        external_block_reason = self._external_entry_block_reason(symbol)
        if external_block_reason:
            self._record_signal_analytics(
                "entry_gate_checked",
                symbol=symbol,
                signal=signal,
                block_reason=external_block_reason,
                external_context=self._external_context_from_cache(symbol),
            )
            self._log_event(
                "INFO",
                f"Signal skipped for {symbol}: external price filter",
                event="signal_valid",
                symbol=symbol,
                reason=external_block_reason,
            )
            return

        spread_block_reason = self._entry_orderbook_spread_block_reason(symbol)
        if spread_block_reason:
            self._record_signal_analytics(
                "entry_gate_checked",
                symbol=symbol,
                signal=signal,
                block_reason=spread_block_reason,
                external_context=self._external_context_from_cache(symbol),
            )
            self._log_event(
                "INFO",
                f"Signal skipped for {symbol}: HTX order book spread filter",
                event="signal_valid",
                symbol=symbol,
                reason=spread_block_reason,
            )
            return

        self._record_signal_analytics(
            "entry_gate_checked",
            symbol=symbol,
            signal=signal,
            external_context=self._external_context_from_cache(symbol),
        )
        budget, budget_reason = self._risk_budget(
            symbol,
            state,
            reference_price,
            is_new_position=True,
            signal=signal,
            budget_scale=1.0,
        )
        planned_notional = budget * max(float(config.RISK.leverage), 1.0)
        self._record_signal_analytics(
            "entry_budget_calculated" if budget > 0 else "entry_budget_blocked",
            symbol=symbol,
            signal=signal,
            block_reason="" if budget > 0 else budget_reason,
            external_context=self._external_context_from_cache(symbol),
            planned_budget=budget,
            planned_notional=planned_notional,
            context={"reference_price": reference_price, "budget_reason": budget_reason},
        )
        if budget <= 0:
            self._log_event(
                "INFO",
                f"Signal skipped for {symbol}: {budget_reason}",
                event="margin_error" if "margin" in budget_reason else "signal_valid",
                symbol=symbol,
                reason=budget_reason,
            )
            return

        self._place_buy_ladder(symbol, budget, reference_price, signal, reason=f"ema_initial_signal;{budget_reason}")


__all__ = ["EntryStrategy"]
