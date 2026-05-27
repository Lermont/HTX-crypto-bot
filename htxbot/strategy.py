# -*- coding: utf-8 -*-

import csv
import time
from typing import List, Optional, Tuple

import config

from .models import TradeState


class StrategyMixin:
    def _current_total_notional(self) -> float:
        total = 0.0
        for symbol, state in self.states.items():
            if symbol not in self.market_by_symbol:
                continue
            if state.position_size > 0 and state.entry_price > 0:
                total += self._contracts_to_notional(symbol, state.position_size, state.entry_price)
            for ref in state.entry_orders or []:
                total += self._contracts_to_notional(
                    symbol,
                    self._safe_float(ref.get("amount"), 0.0),
                    self._safe_float(ref.get("price"), 0.0),
                )
        return total

    def _symbol_open_notional(self, symbol: str, state: TradeState) -> float:
        total = self._contracts_to_notional(symbol, state.position_size, state.entry_price)
        for ref in state.entry_orders or []:
            total += self._contracts_to_notional(
                symbol,
                self._safe_float(ref.get("amount"), 0.0),
                self._safe_float(ref.get("price"), 0.0),
            )
        return total

    def _active_position_slots(self) -> int:
        slots = 0
        min_notional = max(0.0, config.RISK.active_position_min_notional_for_slot)
        for symbol, state in self.states.items():
            if symbol not in self.market_by_symbol:
                continue
            if not (state.position_size > 0 or state.entry_orders):
                continue
            notional = self._symbol_open_notional(symbol, state)
            if notional >= min_notional:
                slots += 1
        return slots

    def _position_notional(self, symbol: str, state: TradeState) -> float:
        return self._contracts_to_notional(symbol, state.position_size, state.entry_price)

    def _is_dust_position(self, symbol: str, state: TradeState) -> bool:
        threshold = max(0.0, config.RISK.dust_position_notional)
        if threshold <= 0 or state.position_size <= 0:
            return False
        notional = self._position_notional(symbol, state)
        return 0 < notional <= threshold

    def _place_small_position_close_order(
        self,
        symbol: str,
        close_reason: str,
        reason_detail: str,
        event_prefix: str,
        open_orders: Optional[List[dict]] = None,
    ) -> bool:
        state = self._get_state(symbol)
        notional = self._position_notional(symbol, state)
        if not config.RUNTIME.reduce_only_enabled:
            self._log_event(
                "ERROR",
                f"Small position close blocked for {symbol}: reduce-only disabled",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{close_reason}_reduce_only_disabled;notional={notional:.8f};{reason_detail}",
            )
            return True

        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason=close_reason)
        if state.sell_ladder_orders:
            self._cancel_sell_orders(symbol, reason=close_reason)
        if state.entry_orders or state.sell_ladder_orders:
            self._log_event(
                "WARNING",
                f"Small position close delayed for {symbol}: tracked order cancel failed",
                event=f"{event_prefix}_failed",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{close_reason}_cancel_failed;notional={notional:.8f};{reason_detail}",
            )
            return True

        visible_exit_remaining = 0.0
        for order in open_orders or []:
            if (order.get("side") or "").lower() == config.EXIT_SIDE:
                visible_exit_remaining += self._order_remaining_amount(order)

        closeable = state.position_size
        if state.position_available > 0:
            closeable = min(closeable, state.position_available)
        elif state.position_frozen > 0:
            closeable = 0.0

        if visible_exit_remaining > 0:
            closeable = min(closeable, max(0.0, state.position_size - visible_exit_remaining))

        close_amount = self._amount_to_precision(symbol, closeable)
        if close_amount <= 0:
            no_closeable = closeable <= 0
            self._log_event(
                "WARNING",
                (
                    f"Small position close delayed for {symbol}: no closeable amount"
                    if no_closeable
                    else f"Small position close skipped for {symbol}: amount below exchange minimum"
                ),
                event=f"{event_prefix}_failed",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=(
                    f"{close_reason}_{'no_closeable_amount' if no_closeable else 'amount_below_minimum'};"
                    f"notional={notional:.8f};"
                    f"available={state.position_available:.12f};"
                    f"frozen={state.position_frozen:.12f};"
                    f"visible_exit_remaining={visible_exit_remaining:.12f};"
                    f"{reason_detail}"
                ),
            )
            state.frozen_no_more_buys = True
            self._refresh_active_side(state)
            self._save_state()
            return True

        if close_amount + max(self._get_min_contracts(symbol) * 1e-9, 1e-12) < state.position_size:
            self._log_event(
                "INFO",
                f"Small position close capped for {symbol}: closeable={close_amount} position={state.position_size}",
                event=f"{event_prefix}_amount_capped",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=close_amount,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=(
                    f"{close_reason}_closeable_amount_cap;notional={notional:.8f};"
                    f"available={state.position_available:.12f};"
                    f"frozen={state.position_frozen:.12f};"
                    f"visible_exit_remaining={visible_exit_remaining:.12f};"
                    f"{reason_detail}"
                ),
            )

        try:
            order = self._create_one_way_order(
                symbol=symbol,
                order_type="market",
                side=config.EXIT_SIDE,
                amount=close_amount,
                price=None,
                reduce_only=True,
            )
        except Exception as exc:
            closeable_rejected = self._is_reduce_only_amount_exceeds_closeable_error(exc)
            if closeable_rejected:
                state.position_available = 0.0
                state.position_frozen = max(state.position_frozen, state.position_size)
                state.frozen_no_more_buys = True
                self._refresh_active_side(state)
                self._save_state()
            self._log_event(
                "WARNING",
                (
                    f"Small position close delayed for {symbol}: HTX reports no closeable amount"
                    if closeable_rejected
                    else f"Small position close order failed for {symbol}: {exc}"
                ),
                event=f"{event_prefix}_failed",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=close_amount,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=(
                    f"{close_reason}_closeable_amount_rejected;notional={notional:.8f};"
                    f"available={state.position_available:.12f};"
                    f"frozen={state.position_frozen:.12f};"
                    f"visible_exit_remaining={visible_exit_remaining:.12f};"
                    f"{reason_detail}"
                    if closeable_rejected
                    else f"{close_reason}_order_failed;notional={notional:.8f};{reason_detail}"
                ),
            )
            return True

        state.frozen_no_more_buys = True
        state.zombie_position = True
        self._refresh_active_side(state)
        self._save_state()
        self._log_event(
            "INFO",
            f"Small position reduce-only market order placed for {symbol}: contracts={close_amount}",
            event=f"{event_prefix}_order_placed",
            symbol=symbol,
            side=config.EXIT_SIDE,
            order_id=str(order.get("id", "")),
            amount=close_amount,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"{close_reason};notional={notional:.8f};{reason_detail}",
        )
        return True

    def _maybe_close_dust_position(self, symbol: str, open_orders: List[dict]) -> bool:
        state = self._get_state(symbol)
        if not config.RISK.dust_close_enabled or config.RUNTIME.dry_run:
            return False
        if not self._is_dust_position(symbol, state):
            return False

        return self._place_small_position_close_order(
            symbol,
            close_reason="dust_position_close",
            reason_detail=f"dust_threshold={max(0.0, config.RISK.dust_position_notional):.8f}",
            event_prefix="dust_close",
            open_orders=open_orders,
        )

    def _tiny_partial_entry_close_detail(self, symbol: str, state: TradeState) -> str:
        if not config.RISK.tiny_entry_close_enabled or config.RUNTIME.dry_run:
            return ""
        if state.position_size <= 0:
            return ""

        notional = self._position_notional(symbol, state)
        if notional <= 0:
            return ""

        leverage = max(self._safe_float(getattr(state, "leverage", 0.0), 0.0), self._safe_float(config.RISK.leverage, 1.0), 1.0)
        planned_notional = self._safe_float(state.initial_entry_notional, 0.0)
        if planned_notional <= 0:
            planned_notional = self._safe_float(state.planned_quote_budget, 0.0) * leverage

        max_notional = max(0.0, config.RISK.tiny_entry_max_notional)
        max_fraction = max(0.0, config.RISK.tiny_entry_max_planned_fraction)
        by_notional = max_notional > 0 and notional <= max_notional
        by_fraction = planned_notional > 0 and max_fraction > 0 and notional <= planned_notional * max_fraction
        if not by_notional and not by_fraction:
            return ""

        return (
            f"notional={notional:.8f};max_notional={max_notional:.8f};"
            f"planned_notional={planned_notional:.8f};max_planned_fraction={max_fraction:.5f};"
            f"match_notional={int(by_notional)};match_fraction={int(by_fraction)}"
        )

    def _maybe_close_tiny_partial_entry_after_timeout(self, symbol: str, open_orders: Optional[List[dict]] = None) -> bool:
        state = self._get_state(symbol)
        detail = self._tiny_partial_entry_close_detail(symbol, state)
        if not detail:
            return False
        return self._place_small_position_close_order(
            symbol,
            close_reason="tiny_partial_entry_timeout_close",
            reason_detail=detail,
            event_prefix="tiny_entry_close",
            open_orders=open_orders,
        )

    def _risk_budget(
        self,
        symbol: str,
        state: TradeState,
        reference_price: float,
        is_new_position: bool,
        signal: Optional[dict] = None,
        budget_scale: float = 1.0,
    ) -> Tuple[float, str]:
        account = self._account_snapshot()
        free = account["free"]
        equity = account["total"] or free
        if free <= config.RISK.min_quote_reserve:
            return 0.0, "free_margin_below_reserve"

        if is_new_position and self._active_position_slots() >= config.RISK.max_active_positions:
            return 0.0, "max_active_positions_reached"

        base_margin_budget = equity * config.BUYING.position_budget_fraction
        available_after_reserve = max(0.0, free - config.RISK.min_quote_reserve)
        planned_margin = min(base_margin_budget, available_after_reserve)

        leverage = max(float(config.RISK.leverage), 1.0)
        total_cap_notional = equity * leverage * config.RISK.max_total_notional_fraction
        position_cap_notional = equity * leverage * config.RISK.max_position_notional_fraction
        current_total_notional = self._current_total_notional()
        current_symbol_notional = self._symbol_open_notional(symbol, state)
        total_remaining = max(0.0, total_cap_notional - current_total_notional)
        symbol_remaining = max(0.0, position_cap_notional - current_symbol_notional)

        base_notional = planned_margin * leverage
        multiplier = self._safe_float((signal or {}).get("budget_multiplier"), 1.0)
        volatility_budget = max(0.0, self._safe_float((signal or {}).get("volatility_budget_multiplier"), 1.0))
        effective_multiplier = multiplier * volatility_budget
        margin_cap_notional = available_after_reserve * leverage
        scale = max(0.0, budget_scale)
        planned_notional = min(base_notional * effective_multiplier * scale, total_remaining, symbol_remaining, margin_cap_notional)
        min_contracts = self._get_min_contracts(symbol)
        min_notional = self._contracts_to_notional(symbol, min_contracts, reference_price)
        if (
            0 < planned_notional < min_notional <= min(base_notional, total_remaining, symbol_remaining, margin_cap_notional)
            and effective_multiplier < 1.0
        ):
            planned_notional = min_notional
        planned_margin = planned_notional / leverage

        if planned_notional <= 0 or planned_margin <= 0:
            return 0.0, "notional_limit_reached"

        contracts = self._contracts_for_notional(symbol, planned_notional, reference_price)
        if contracts <= 0:
            return 0.0, "order_size_below_exchange_minimum"

        return planned_margin, (
            f"ok:budget_multiplier={multiplier:.3f};"
            f"vol_budget={volatility_budget:.3f};"
            f"effective_budget_multiplier={effective_multiplier:.3f};budget_scale={scale:.3f}"
        )

    def _place_buy_ladder(
        self,
        symbol: str,
        margin_budget: float,
        reference_price: float,
        signal: dict,
        reason: str,
        offset_multiplier: float = 1.0,
    ):
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
        account_leverage = configured_leverage if config.RUNTIME.dry_run else self._fetch_account_order_leverage(symbol)
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
            return

        sizing_leverage = min(configured_leverage, max(account_leverage, 1.0))
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
            order_id = f"dry_{entry_side}_{symbol}_{int(created_at)}_{index}"
            order_leverage = account_leverage
            if not config.RUNTIME.dry_run:
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
            event = "entry_ladder_planned" if config.RUNTIME.dry_run else "entry_ladder_placed"
            action = "planned" if config.RUNTIME.dry_run else "placed"
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
            return

        self._refresh_active_side(state)
        if config.RUNTIME.dry_run:
            self._log_dry_run_sell_preview(symbol, state.entry_orders)
        self._save_state()

    def _log_dry_run_sell_preview(self, symbol: str, buy_refs: list):
        total_contracts = sum(self._safe_float(ref.get("amount"), 0.0) for ref in buy_refs)
        total_notional = sum(
            self._contracts_to_notional(symbol, self._safe_float(ref.get("amount"), 0.0), self._safe_float(ref.get("price"), 0.0))
            for ref in buy_refs
        )
        avg_entry = self._average_price_from_notional(symbol, total_contracts, total_notional)
        if total_contracts <= 0 or avg_entry <= 0:
            return

        sell_context = self._sell_ladder_context(symbol, mode="normal")
        state = self._get_state(symbol)
        steps, plan_context = self._sell_ladder_plan(symbol, total_contracts, avg_entry, mode="normal", state=state)
        allocations, runner_contracts = self._exit_ladder_contract_allocations(symbol, total_contracts, steps, state)
        for index, step, contracts in allocations:
            markup = self._safe_float(step.get("markup"), 0.0)
            adaptive_markup = markup * self._safe_float(sell_context.get("markup_multiplier"), 1.0)
            price = self._sell_price_floor(symbol, avg_entry, adaptive_markup, context=sell_context)
            self._log_event(
                "DEBUG",
                f"Dry-run {config.EXIT_SIDE} exit ladder preview for {symbol}: stage={index} contracts={contracts} price={price}",
                event="exit_ladder_placed",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=price,
                amount=contracts,
                reason=(
                    f"dry_run_preview;markup_multiplier={sell_context.get('markup_multiplier', 1.0):.3f};"
                    f"base_profit_floor={sell_context.get('base_profit_floor', 0.0):.6f};"
                    f"profit_floor={sell_context.get('profit_floor', 0.0):.6f};"
                    f"profit_floor_mult={sell_context.get('profit_floor_multiplier', 1.0):.3f};"
                    f"profit_floor_reason={sell_context.get('profit_floor_reason', 'neutral')};"
                    f"fee_floor={sell_context.get('fee_floor', 0.0):.6f};"
                    f"spread={sell_context.get('spread_rate', 0.0):.6f};"
                    f"spread_floor={sell_context.get('spread_floor', 0.0):.6f};"
                    f"vol_floor={sell_context.get('volatility_floor', 0.0):.6f};"
                    f"funding={sell_context.get('funding_rate', 0.0):.6f};"
                    f"ladder={plan_context.get('ladder_name', 'normal')};"
                    f"runner_contracts={runner_contracts:.12f}"
                ),
            )

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
        return self._is_time_exit_mode(mode)

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

    def _sell_ladder_plan(
        self,
        symbol: str,
        total_contracts: float,
        avg_entry_price: float,
        mode: str = "normal",
        state: Optional[TradeState] = None,
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
            return funding_rate >= strategy.funding_positive_threshold
        return funding_rate <= strategy.funding_negative_threshold

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

        steps, plan_context = self._sell_ladder_plan(symbol, total_contracts, avg_entry_price, mode=mode, state=state)
        plan = ",".join(
            f"{self._safe_float(step.get('fraction'), 0.0):.8f}@runner"
            if step.get("runner")
            else f"{self._safe_float(step.get('fraction'), 0.0):.8f}@{self._safe_float(step.get('markup'), 0.0):.8f}"
            for step in steps
        )
        strategy = config.STRATEGY
        return (
            f"{mode}|strategy=ema_pullback|direction={config.POSITION_SIDE}|exit_side={config.EXIT_SIDE}|"
            f"plan={plan}|ladder={plan_context.get('ladder_name', mode)}|"
            f"adaptive={int(strategy.ema_adaptive_exit_enabled)}|"
            f"ratio={plan_context.get('position_ratio', 1.0):.4f}|"
            f"runner={int(plan_context.get('runner_enabled', False))}|"
            f"external_spread={plan_context.get('external_spread_bps', 0.0):.4f}|"
            f"tp={strategy.ema_take_profit_markup:.8f}|"
            f"breakeven_after={strategy.ema_breakeven_after_hours:.4f}|"
            f"breakeven_reprice={strategy.ema_breakeven_reprice_minutes:.4f}|"
            f"breakeven_buffer={strategy.ema_breakeven_fee_buffer:.8f}|"
            f"decay_first={strategy.ema_exit_decay_first_markup_after_hours:.4f}:"
            f"{strategy.ema_exit_decay_first_markup_cap:.8f}|"
            f"decay_max={strategy.ema_exit_decay_max_markup_after_hours:.4f}:"
            f"{strategy.ema_exit_decay_max_markup:.8f}"
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
        return state.sell_ladder_signature == self._pending_exit_ladder_signature(mode, symbol, state)

    def _mark_exit_ladder_waiting_for_closeable(self, symbol: str, mode: str, reason: str, amount: float = 0.0):
        state = self._get_state(symbol)
        state.sell_ladder_orders = []
        state.sell_ladder_mode = mode
        state.sell_ladder_signature = self._pending_exit_ladder_signature(mode, symbol, state)
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

    def _place_sell_ladder(
        self,
        symbol: str,
        total_contracts: float,
        avg_entry_price: float,
        rebuild: bool,
        closeable_contracts: Optional[float] = None,
        mode: str = "normal",
        exit_scope: Optional[str] = None,
        signature_override: str = "",
    ):
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

        ladder_contracts = total_contracts
        if closeable_contracts is not None:
            ladder_contracts = min(total_contracts, max(0.0, closeable_contracts))
        ladder_contracts = self._amount_to_precision(symbol, ladder_contracts)
        if ladder_contracts <= 0:
            self._mark_exit_ladder_waiting_for_closeable(
                symbol,
                mode,
                "no_closeable_position_available",
            )
            return

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
        steps, plan_context = self._sell_ladder_plan(symbol, total_contracts, avg_entry_price, mode=mode, state=state)
        allocations, runner_contracts = self._exit_ladder_contract_allocations(symbol, ladder_contracts, steps, state)
        state.sell_ladder_signature = signature_override or self._sell_ladder_signature(
            mode,
            symbol,
            state,
            total_contracts=total_contracts,
            avg_entry_price=avg_entry_price,
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
        for index, step, contracts in allocations:
            markup = self._safe_float(step.get("markup"), 0.0)

            adaptive_markup = markup * self._safe_float(sell_context.get("markup_multiplier"), 1.0)
            if mode == "controlled_loss_exit":
                price = self._controlled_loss_exit_price(symbol, avg_entry_price, markup, context=sell_context)
            else:
                price = self._sell_price_floor(symbol, avg_entry_price, adaptive_markup, context=sell_context)
            order_id = f"dry_{exit_side}_{symbol}_{int(created_at)}_{index}"
            if not config.RUNTIME.dry_run:
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
            state.sell_ladder_orders.append(ref)
            event = "exit_ladder_planned" if config.RUNTIME.dry_run else ("exit_ladder_rebuilt" if rebuild else "exit_ladder_placed")
            action = "planned" if config.RUNTIME.dry_run else ("rebuilt" if rebuild else "placed")
            stage_notional = self._contracts_to_notional(symbol, contracts, price)
            exit_planned_orders += 1
            exit_planned_notional += stage_notional
            self._record_signal_analytics(
                event,
                symbol=symbol,
                signal={},
                planned_orders=exit_planned_orders,
                planned_notional=exit_planned_notional,
                placed_orders=0 if config.RUNTIME.dry_run else exit_planned_orders,
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
                    f"external_spread_bps={sell_context.get('external_spread_bps', 0.0):.4f};"
                    f"external_reason={sell_context.get('external_reason', 'unavailable')};"
                    f"{sell_context.get('funding_reason', 'neutral')}"
                ),
            )

        sell_total = sum(self._safe_float(ref.get("amount"), 0.0) for ref in state.sell_ladder_orders)
        if sell_total > ladder_contracts + max(self._get_min_contracts(symbol) * 1e-9, 1e-12):
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
        contracts = self._amount_to_precision(symbol, contracts)
        if contracts <= 0 or price <= 0:
            return False

        exit_side = config.EXIT_SIDE
        exit_label = "Buy" if exit_side == "buy" else "Sell"
        created_at = time.time()
        order_id = f"dry_{exit_side}_{symbol}_{int(created_at)}_average_recovery"
        if not config.RUNTIME.dry_run:
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

        event = "exit_ladder_planned" if config.RUNTIME.dry_run else ("exit_ladder_rebuilt" if rebuild else "exit_ladder_placed")
        stage_notional = self._contracts_to_notional(symbol, contracts, price)
        self._record_signal_analytics(
            event,
            symbol=symbol,
            signal={},
            planned_orders=len(state.sell_ladder_orders),
            planned_notional=stage_notional,
            placed_orders=0 if config.RUNTIME.dry_run else len(state.sell_ladder_orders),
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
        action = "planned" if config.RUNTIME.dry_run else ("rebuilt" if rebuild else "placed")
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
            self._place_sell_ladder(
                symbol,
                total_contracts,
                avg_entry_price,
                rebuild,
                closeable_contracts=closeable_contracts,
                mode=mode,
            )
            return

        total_contracts = max(0.0, min(total_contracts, state.position_size))
        closeable_total = total_contracts
        if closeable_contracts is not None:
            closeable_total = min(total_contracts, max(0.0, closeable_contracts))
        closeable_total = self._amount_to_precision(symbol, closeable_total)
        if closeable_total <= 0:
            self._mark_exit_ladder_waiting_for_closeable(symbol, mode, "no_closeable_position_available")
            return

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
        if base_closeable <= 0:
            self._reset_exit_runner_state(state)
        self._refresh_active_side(state)
        self._save_state()

        if base_closeable > 0:
            self._place_sell_ladder(
                symbol,
                base_contracts,
                base_price,
                rebuild,
                closeable_contracts=base_closeable,
                mode=mode,
                exit_scope="base",
                signature_override=signature,
            )

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
        if sell_total > closeable_total + max(self._get_min_contracts(symbol) * 1e-9, 1e-12):
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
        self._place_sell_ladder(
            symbol,
            total_contracts,
            avg_entry_price,
            rebuild,
            closeable_contracts=closeable_contracts,
            mode=mode,
        )

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
        signal_valid = self._is_entry_signal_valid(signal_for_quality)
        if not signal_valid:
            self._cancel_entry_orders(symbol, reason="ema_average_entry_signal_invalid" if is_average_ladder else "ema_entry_signal_invalid")
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

        if config.RUNTIME.dry_run:
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
        if config.RUNTIME.dry_run:
            return True

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
        self._cancel_exchange_orders(symbol, unknown_entries, side=entry_side, reason="unknown_entry_orders")

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

    def _static_exit_profit_context(self) -> dict:
        roundtrip_fee = config.SELLING.buy_fee_rate + config.SELLING.sell_fee_rate
        fee_floor = roundtrip_fee * config.STRATEGY.min_profit_fee_multiplier
        return {"profit_floor": max(config.SELLING.min_gross_profit_floor, fee_floor)}

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
            ref = {
                "id": str(order.get("id")),
                "side": config.EXIT_SIDE,
                "price": self._order_effective_exit_price(order),
                "amount": amount,
                "created_at": self._safe_float(order.get("timestamp"), created_at * 1000) / 1000.0,
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
                cancel_params = order.get("bot_cancel_params")
                if isinstance(cancel_params, dict):
                    ref["cancel_params"] = dict(cancel_params)
            adopted.append(ref)

        if not adopted:
            return False

        state.sell_ladder_orders = adopted
        state.sell_ladder_mode = state.sell_ladder_mode or "normal"
        state.sell_ladder_signature = self._exit_ladder_signature(state.sell_ladder_mode, symbol, state)
        self._reset_exit_runner_state(state)
        self._refresh_active_side(state)
        self._log_event(
            "INFO",
            f"Adopted existing {config.EXIT_SIDE} exit ladder for {symbol}: orders={len(adopted)} amount={remaining}",
            event="state_exchange_mismatch",
            symbol=symbol,
            side=config.EXIT_SIDE,
            amount=remaining,
            reason=reason,
        )
        self._save_state()
        return True

    def _validate_sell_orders(self, symbol: str, open_orders: List[dict]) -> bool:
        state = self._get_state(symbol)
        exit_side = config.EXIT_SIDE
        open_sell_orders = [order for order in open_orders if (order.get("side") or "").lower() == exit_side]
        known_sell_ids = self._order_ids(state.sell_ladder_orders)
        tracked_sell_orders = [order for order in open_sell_orders if str(order.get("id")) in known_sell_ids]
        unknown_sells = [order for order in open_sell_orders if str(order.get("id")) not in known_sell_ids]
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)

        if state.position_size <= 0:
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

        if config.RUNTIME.dry_run:
            return True

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
                return False

            self._log_event(
                "DEBUG",
                f"Untracked {exit_side} orders found for {symbol}; leaving them untouched",
                event="state_exchange_mismatch",
                symbol=symbol,
                side=exit_side,
                amount=unknown_remaining,
                reason="untracked_exit_side_orders_preserved",
            )

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
                self._refresh_active_side(state)
                self._save_state()
                return False

            missing_refs = [
                ref for ref in state.sell_ladder_orders
                if str(ref.get("id")) not in open_tracked_sell_ids
            ]
            missing_amount = sum(self._safe_float(ref.get("amount"), 0.0) for ref in missing_refs)
            if missing_refs and not open_sell_orders:
                first_preserve = not all(ref.get("invisible_preserved_at") for ref in missing_refs)
                if first_preserve:
                    now = time.time()
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

    def _unhealthy_position_count(self) -> int:
        return self._exit_tracking_health_counts()["unhealthy"]

    def _exit_tracking_health_counts(self) -> dict:
        active = 0
        tracked = 0
        unhealthy = 0
        for state in self.states.values():
            if state.position_size <= 0:
                continue
            active += 1
            if state.zombie_position or (not state.sell_ladder_orders and state.exit_runner_contracts <= 0):
                unhealthy += 1
            else:
                tracked += 1
        ratio = tracked / active if active > 0 else 1.0
        return {
            "active": active,
            "tracked": tracked,
            "unhealthy": unhealthy,
            "tracked_ratio": ratio,
        }

    def _profile_health_block_reason(self) -> str:
        threshold = int(config.STRATEGY.max_unhealthy_positions_for_new_entries)
        if threshold < 0:
            return ""
        unhealthy = self._unhealthy_position_count()
        if unhealthy >= threshold:
            return f"profile_health_blocked;unhealthy_positions={unhealthy};threshold={threshold}"
        return ""

    def _entry_signal_rank_key(self, symbol: str, signal: dict) -> tuple:
        score = self._safe_float(signal.get("score"), 0.0) + self._external_entry_score_bonus(signal, symbol=symbol)
        rs60 = self._directional_entry_value(self._safe_float(signal.get("rs60"), 0.0))
        rs30 = self._directional_entry_value(self._safe_float(signal.get("rs30"), 0.0))
        trend_gap = max(0.0, self._safe_float(signal.get("trend_ema_gap"), 0.0))
        trigger_gap = max(0.0, self._safe_float(signal.get("ema_gap"), 0.0))
        return (-score, -rs60, -rs30, -trend_gap, -trigger_gap, symbol)

    def _entry_gate_signal_ts(self) -> Optional[float]:
        ts = self.signal_cache.get("closed_candle_ts")
        if ts is not None:
            return self._safe_float(ts, 0.0)
        latest = 0.0
        for signal in self.signal_cache.get("symbols", {}).values():
            latest = max(latest, self._safe_float(signal.get("ts"), 0.0))
        return latest or None

    def _entry_state_competes_for_signal(self, symbol: str, signal_ts: Optional[float], now: float) -> bool:
        state = self._get_state(symbol)
        if state.frozen_no_more_buys or state.zombie_position:
            return False
        if state.cooldown_until and now < state.cooldown_until:
            return False
        if symbol in getattr(self, "external_reserved_symbols", set()) and state.position_size <= 0:
            return False
        if state.position_size > 0:
            return bool(
                signal_ts is not None
                and self._safe_float(state.last_entry_ladder_signal_timestamp, -1.0) == self._safe_float(signal_ts, -2.0)
            )
        return True

    def _recent_new_entry_count(self, window_sec: float, now: Optional[float] = None) -> int:
        if window_sec <= 0:
            return 0
        now = time.time() if now is None else now
        cutoff = now - window_sec
        count = 0

        for state in self.states.values():
            opened_at = self._safe_float(state.cycle_opened_at, 0.0)
            if state.position_size > 0 and opened_at >= cutoff:
                count += 1
                continue

            created_at = 0.0
            for ref in state.entry_orders or []:
                created_at = max(created_at, self._safe_float(ref.get("created_at"), 0.0))
            if state.entry_orders and created_at >= cutoff:
                count += 1

        path = getattr(self, "cycle_stats_path", None)
        if path:
            try:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        opened_at = self._safe_float(row.get("opened_at"), 0.0)
                        if opened_at >= cutoff:
                            count += 1
            except FileNotFoundError:
                pass
            except Exception as exc:
                self._log_event(
                    "WARNING",
                    f"Could not read cycle stats for entry rate limit: {exc}",
                    event="state_exchange_mismatch",
                    reason="entry_rate_limit_read_failed",
                )
        return count

    def _entry_crowded_mode(self, raw_count: int, universe_count: int) -> bool:
        strategy = config.STRATEGY
        min_signals = max(0, int(strategy.entry_crowded_min_signals))
        fraction = max(0.0, self._safe_float(strategy.entry_crowded_signal_fraction, 0.0))
        if min_signals > 0 and raw_count >= min_signals:
            return True
        if fraction > 0 and universe_count > 0 and raw_count / universe_count >= fraction:
            return True
        return False

    def _prepare_new_entry_gate(self) -> dict:
        now = time.time()
        signal_ts = self._entry_gate_signal_ts()
        signals = self.signal_cache.get("symbols", {})
        raw_candidates = []
        for symbol in self.entry_symbols:
            signal = signals.get(symbol)
            if not self._is_raw_entry_signal_valid(signal):
                continue
            if not self._entry_state_competes_for_signal(symbol, signal_ts, now):
                continue
            raw_candidates.append(symbol)

        universe_count = max(1, len(self.entry_symbols))
        crowded = self._entry_crowded_mode(len(raw_candidates), universe_count)
        blocked_reasons = {}
        quality_candidates = []
        for symbol in raw_candidates:
            signal = signals.get(symbol)
            signal_for_quality = dict(signal or {})
            signal_for_quality["symbol"] = symbol
            reason = self._entry_signal_quality_block_reason(signal_for_quality, crowded=crowded)
            if reason:
                blocked_reasons[symbol] = f"entry_quality_blocked;crowded={int(crowded)};{reason}"
                continue
            quality_candidates.append(symbol)

        ranked = sorted(
            quality_candidates,
            key=lambda item: self._entry_signal_rank_key(item, signals.get(item, {})),
        )
        strategy = config.STRATEGY
        per_signal_limit = int(
            strategy.entry_crowded_max_new_ladders_per_signal
            if crowded
            else strategy.entry_max_new_ladders_per_signal
        )
        if per_signal_limit <= 0:
            per_signal_limit = len(ranked)

        rate_limit = int(strategy.entry_rate_limit_ladders)
        window_sec = max(0.0, self._safe_float(strategy.entry_rate_limit_window_minutes, 0.0)) * 60.0
        recent_count = self._recent_new_entry_count(window_sec, now=now) if rate_limit > 0 else 0
        rate_remaining = max(0, rate_limit - recent_count) if rate_limit > 0 else len(ranked)
        allowed_count = min(len(ranked), per_signal_limit, rate_remaining)
        allowed = set(ranked[:allowed_count])

        for index, symbol in enumerate(ranked, start=1):
            if symbol in allowed:
                continue
            if index > per_signal_limit:
                blocked_reasons[symbol] = (
                    "entry_top_n_blocked;"
                    f"rank={index};limit={per_signal_limit};crowded={int(crowded)}"
                )
            elif index > rate_remaining:
                blocked_reasons[symbol] = (
                    "entry_rate_limited;"
                    f"recent={recent_count};limit={rate_limit};window_minutes={strategy.entry_rate_limit_window_minutes:.1f}"
                )

        gate = {
            "signal_ts": signal_ts,
            "raw_count": len(raw_candidates),
            "quality_count": len(quality_candidates),
            "allowed_symbols": allowed,
            "ranked_symbols": ranked,
            "blocked_reasons": blocked_reasons,
            "crowded": crowded,
            "per_signal_limit": per_signal_limit,
            "rate_limit": rate_limit,
            "rate_remaining": rate_remaining,
            "recent_count": recent_count,
        }
        self.entry_gate = gate

        last_logged_ts = getattr(self, "_last_entry_gate_logged_ts", None)
        if signal_ts is not None and last_logged_ts != signal_ts and raw_candidates:
            self._last_entry_gate_logged_ts = signal_ts
            self._log_event(
                "INFO",
                (
                    "Entry gate prepared: "
                    f"raw={len(raw_candidates)} quality={len(quality_candidates)} allowed={len(allowed)}"
                ),
                event="entry_gate_updated",
                reason=(
                    f"signal_ts={signal_ts};crowded={int(crowded)};per_signal_limit={per_signal_limit};"
                    f"recent_entries={recent_count};rate_limit={rate_limit};rate_remaining={rate_remaining}"
                ),
            )
        return gate

    def _entry_gate_block_reason(self, symbol: str, signal: Optional[dict]) -> str:
        gate = getattr(self, "entry_gate", None)
        if not gate:
            return ""

        signal_ts = self._safe_float((signal or {}).get("ts"), 0.0)
        gate_ts = self._safe_float(gate.get("signal_ts"), 0.0)
        if not signal_ts or not gate_ts:
            return ""
        if signal_ts != gate_ts:
            return ""
        if symbol in gate.get("allowed_symbols", set()):
            return ""
        blocked = gate.get("blocked_reasons", {}).get(symbol)
        if blocked:
            return blocked
        if symbol in gate.get("ranked_symbols", []):
            return "entry_top_n_blocked"
        return "entry_gate_not_ranked"

    def _entry_expansion_block_reason(self) -> str:
        strategy = config.STRATEGY
        if not strategy.enable_entry_expansion:
            return "entry_expansion_disabled"
        counts = self._exit_tracking_health_counts()
        max_unhealthy = int(strategy.entry_expansion_max_unhealthy_positions)
        if max_unhealthy >= 0 and counts["unhealthy"] > max_unhealthy:
            return (
                "entry_expansion_blocked_exit_tracking;"
                f"unhealthy_positions={counts['unhealthy']};max_unhealthy={max_unhealthy};"
                f"tracked={counts['tracked']};active={counts['active']}"
            )
        min_ratio = self._clamp(strategy.entry_expansion_min_tracked_exit_ratio, 0.0, 1.0)
        if counts["tracked_ratio"] + 1e-12 < min_ratio:
            return (
                "entry_expansion_blocked_exit_tracking_ratio;"
                f"tracked_ratio={counts['tracked_ratio']:.3f};min_ratio={min_ratio:.3f};"
                f"tracked={counts['tracked']};active={counts['active']}"
            )
        return ""

    def _closeable_contracts_for_exit_ladder(self, symbol: str, had_sell_ladder: bool) -> float:
        state = self._get_state(symbol)
        closeable = state.position_available
        if had_sell_ladder and closeable <= 0:
            closeable = state.position_size
        if config.RUNTIME.dry_run and closeable <= 0:
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

    def _realized_pnl_since(self, since_ts: float) -> float:
        path = config.MONITORING.cycle_stats_csv_file
        total = 0.0
        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    closed_at = self._safe_float(row.get("closed_at"), 0.0)
                    if closed_at < since_ts:
                        continue
                    total += self._safe_float(row.get("realized_pnl_quote"), 0.0)
        except FileNotFoundError:
            return 0.0
        except Exception as exc:
            self._log_event(
                "WARNING",
                f"Could not read cycle stats for controlled loss budget: {exc}",
                event="state_exchange_mismatch",
                reason="controlled_loss_budget_read_failed",
            )
            return 0.0
        return total

    def _position_held_minutes(self, state: TradeState) -> float:
        if not state.cycle_opened_at:
            return 0.0
        return max(0.0, (time.time() - state.cycle_opened_at) / 60.0)


    def _external_price_settings_enabled(self) -> bool:
        return bool(getattr(config.EXTERNAL_PRICE_FEED, "enabled", False) and getattr(self, "external_price_feed", None))

    def _external_price_context(self, symbol: str) -> dict:
        cache = getattr(self, "_external_price_context_cache", None)
        if cache is None:
            cache = {}
            self._external_price_context_cache = cache
        if symbol in cache:
            return dict(cache[symbol])

        if not self._external_price_settings_enabled():
            context = {"valid": False, "stale": True, "reason": "disabled", "symbol": symbol}
            cache[symbol] = dict(context)
            return context
        def remember(context: dict) -> dict:
            cache[symbol] = dict(context)
            return dict(context)

        if not self._external_price_settings_enabled():
            return remember({"valid": False, "stale": True, "reason": "disabled", "symbol": symbol})
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            market = self.market_by_symbol.get(symbol) or self.exchange.market(symbol)
            context = self.external_price_feed.get_context(symbol, ticker, market=market)
        except Exception as exc:
            context = {"valid": False, "stale": True, "reason": f"external_price_error:{exc}", "symbol": symbol}
            cache[symbol] = dict(context)
            return context
            return remember({"valid": False, "stale": True, "reason": f"external_price_error:{exc}", "symbol": symbol})
        if not isinstance(context, dict):
            return remember({"valid": False, "stale": True, "reason": "external_price_context_invalid", "symbol": symbol})
        try:
            self._append_external_price_csv(context)
        except Exception as exc:
            self._log_event(
                "WARNING",
                f"Could not append external price context for {symbol}: {exc}",
                event="state_exchange_mismatch",
                symbol=symbol,
                reason="external_price_csv_failed",
            )
        cache[symbol] = dict(context)
        return context
        return remember(context)

    def _external_context_tradable(self, context: dict) -> bool:
        if not context:
            return False
        if context.get("valid"):
            return True
        settings = config.EXTERNAL_PRICE_FEED
        if not context.get("stale"):
            return False
        return bool(getattr(settings, "ignore_reference_if_stale", True) and not getattr(settings, "disable_trading_if_reference_stale", False))

    def _external_price_reason(self, context: dict) -> str:
        return (
            f"spread_bps={self._safe_float(context.get('spread_bps'), 0.0):.4f};"
            f"age_ms={int(self._safe_float(context.get('age_ms'), 0.0))};"
            f"htx_mid={self._safe_float(context.get('htx_mid'), 0.0):.12f};"
            f"mexc_mid={self._safe_float(context.get('mexc_mid'), 0.0):.12f};"
            f"reason={context.get('reason', '')}"
        )


    def _external_entry_score_bonus(self, signal: Optional[dict], symbol: str = "") -> float:
        settings = config.EXTERNAL_PRICE_FEED
        if not getattr(settings, "impulse_confirmation_enabled", True):
            return 0.0
        if not self._external_price_settings_enabled():
            return 0.0
        symbol = symbol or str((signal or {}).get("symbol") or "")
        if not symbol:
            return 0.0
        context = self._external_price_context(symbol)
        if not context.get("valid"):
            return 0.0
        threshold = max(0.0, self._safe_float(settings.mexc_lead_threshold_bps_30s, 0.0))
        if threshold <= 0:
            return 0.0
        htx_change = self._safe_float(context.get("htx_change_30s_bps"), 0.0)
        mexc_change = self._safe_float(context.get("mexc_change_30s_bps"), 0.0)
        spread_bps = self._safe_float(context.get("spread_bps"), 0.0)
        same_direction_required = bool(getattr(settings, "require_same_direction", True))
        if config.POSITION_SIDE == "short":
            max_discount = max(0.0, self._safe_float(settings.max_htx_discount_for_short_bps, 0.0))
            if spread_bps < -max_discount:
                return 0.0
            if mexc_change < htx_change - threshold and mexc_change < 0:
                if same_direction_required and htx_change > 0:
                    return 0.0
                return max(0.0, self._safe_float(settings.impulse_score_bonus, 0.0))
            return 0.0
        max_premium = max(0.0, self._safe_float(settings.max_htx_premium_for_long_bps, 0.0))
        if spread_bps > max_premium:
            return 0.0
        if mexc_change > htx_change + threshold and mexc_change > 0:
            if same_direction_required and htx_change < 0:
                return 0.0
            return max(0.0, self._safe_float(settings.impulse_score_bonus, 0.0))
        return 0.0

    def _external_directional_1m_block_reason(
        self,
        symbol: str,
        context: Optional[dict] = None,
        *,
        scope: str = "entry",
    ) -> str:
        settings = config.EXTERNAL_PRICE_FEED
        if not getattr(settings, "directional_1m_gate_enabled", True):
            return ""
        if not self._external_price_settings_enabled():
            return ""
        if context is None:
            context = self._external_price_context(symbol)
        if not context.get("valid"):
            return ""

        threshold_attr = (
            "directional_averaging_1m_block_bps"
            if scope == "averaging"
            else "directional_entry_1m_block_bps"
        )
        threshold = max(0.0, self._safe_float(getattr(settings, threshold_attr, 0.0), 0.0))
        if threshold <= 0:
            return ""

        htx_change = self._safe_float(context.get("htx_change_1m_bps"), 0.0)
        mexc_change = self._safe_float(context.get("mexc_change_1m_bps"), 0.0)
        direction = -1.0 if config.POSITION_SIDE == "short" else 1.0
        directional_htx_change = htx_change * direction
        directional_mexc_change = mexc_change * direction

        adverse_sources = []
        if directional_htx_change < -threshold:
            adverse_sources.append("htx")
        if directional_mexc_change < -threshold:
            adverse_sources.append("mexc")
        if not adverse_sources:
            return ""

        return (
            "external_directional_1m_blocked;"
            f"scope={scope};limit_bps={threshold:.4f};side={config.POSITION_SIDE};"
            f"adverse_sources={','.join(adverse_sources)};"
            f"directional_htx_change_1m_bps={directional_htx_change:.4f};"
            f"directional_mexc_change_1m_bps={directional_mexc_change:.4f};"
            f"htx_change_1m_bps={htx_change:.4f};mexc_change_1m_bps={mexc_change:.4f};"
            f"{self._external_price_reason(context)}"
        )

    def _external_entry_block_reason(self, symbol: str) -> str:
        settings = config.EXTERNAL_PRICE_FEED
        if not self._external_price_settings_enabled() or not getattr(settings, "entry_filter_enabled", True):
            return ""
        context = self._external_price_context(symbol)
        if not context.get("valid"):
            if self._external_context_tradable(context):
                return ""
            reason = "external_reference_stale" if context.get("stale") else "external_reference_invalid"
            return f"{reason};{self._external_price_reason(context)}"

        htx_change = self._safe_float(context.get("htx_change_1m_bps"), 0.0)
        mexc_change = self._safe_float(context.get("mexc_change_1m_bps"), 0.0)
        divergence = abs(htx_change - mexc_change)
        threshold = max(0.0, self._safe_float(settings.block_if_exchange_divergence_1m_bps, 0.0))
        if threshold > 0 and divergence > threshold:
            state = self._get_state(symbol)
            state.cooldown_until = time.time() + max(0, int(settings.block_duration_sec))
            self._save_state()
            return (
                "external_divergence_blocked;"
                f"divergence_1m_bps={divergence:.4f};htx_change_1m_bps={htx_change:.4f};"
                f"mexc_change_1m_bps={mexc_change:.4f};cooldown_sec={int(settings.block_duration_sec)};"
                f"{self._external_price_reason(context)}"
            )

        directional_reason = self._external_directional_1m_block_reason(symbol, context=context, scope="entry")
        if directional_reason:
            return directional_reason

        spread_bps = self._safe_float(context.get("spread_bps"), 0.0)
        if config.POSITION_SIDE == "short":
            limit = max(0.0, self._safe_float(settings.max_htx_discount_for_short_bps, 0.0))
            if limit > 0 and spread_bps < -limit:
                return f"external_discount_blocked;limit_bps={limit:.4f};{self._external_price_reason(context)}"
        else:
            limit = max(0.0, self._safe_float(settings.max_htx_premium_for_long_bps, 0.0))
            if limit > 0 and spread_bps > limit:
                return f"external_premium_blocked;limit_bps={limit:.4f};{self._external_price_reason(context)}"
        return ""

    def _external_exit_tighten_context(self, symbol: str) -> dict:
        settings = config.EXTERNAL_PRICE_FEED
        if not self._external_price_settings_enabled() or not getattr(settings, "exit_adjustment_enabled", True):
            return {"tighten": False, "reason": "disabled", "spread_bps": 0.0}
        context = self._external_price_context(symbol)
        if not context.get("valid"):
            return {"tighten": False, "reason": "external_reference_stale", "spread_bps": self._safe_float(context.get("spread_bps"), 0.0)}
        spread_bps = self._safe_float(context.get("spread_bps"), 0.0)
        if config.POSITION_SIDE == "short":
            threshold = max(0.0, self._safe_float(settings.short_take_profit_tighten_if_htx_discount_bps, 0.0))
            tighten = threshold > 0 and spread_bps <= -threshold
        else:
            threshold = max(0.0, self._safe_float(settings.long_take_profit_tighten_if_htx_premium_bps, 0.0))
            tighten = threshold > 0 and spread_bps >= threshold
        return {
            "tighten": tighten,
            "reason": "external_exit_tightened" if tighten else "external_exit_neutral",
            "spread_bps": spread_bps,
        }

    def _macro_guard_context(self) -> dict:
        context_getter = getattr(self, "_macro_context_for_trading", None)
        if context_getter:
            return context_getter()
        return {
            "regime": "neutral",
            "gold_rsi": 0.0,
            "btc_rsi": 0.0,
            "rsi_spread": 0.0,
            "reason": "macro_context_unavailable",
        }

    def _macro_block_reason(self, context: dict) -> str:
        return (
            f"macro_regime={context.get('regime', '')};"
            f"gold_rsi={self._safe_float(context.get('gold_rsi'), 0.0):.4f};"
            f"btc_rsi={self._safe_float(context.get('btc_rsi'), 0.0):.4f};"
            f"rsi_spread={self._safe_float(context.get('rsi_spread'), 0.0):.4f};"
            f"reason={context.get('reason', '')}"
        )

    def _log_macro_action_blocked(self, event: str, symbol: str, signal: Optional[dict], context: dict):
        key = (
            event,
            symbol,
            self._safe_float((signal or {}).get("ts"), 0.0),
            context.get("regime", ""),
            context.get("reason", ""),
        )
        logged = getattr(self, "_macro_action_block_logged", set())
        if key in logged:
            return
        logged.add(key)
        self._macro_action_block_logged = logged
        self._log_event(
            "INFO",
            f"Macro overlay blocked action for {symbol}: {context.get('regime', '')}",
            event=event,
            symbol=symbol,
            reason=self._macro_block_reason(context),
        )

    def _effective_time_exit_after_minutes(self) -> float:
        context = self._macro_guard_context()
        multiplier = self._safe_float(context.get("time_exit_multiplier"), 1.0)
        return max(0.0, config.STRATEGY.time_exit_after_minutes) * max(0.0, multiplier)

    def _effective_urgent_time_exit_after_minutes(self) -> float:
        base = max(0.0, config.STRATEGY.urgent_time_exit_after_minutes)
        if base <= 0:
            return 0.0
        context = self._macro_guard_context()
        multiplier = self._safe_float(context.get("time_exit_multiplier"), 1.0)
        return max(15.0, base * max(0.0, multiplier))

    def _hard_time_exit_elapsed(self, state: TradeState) -> bool:
        after_minutes = max(0.0, config.STRATEGY.hard_time_exit_after_minutes)
        return bool(after_minutes > 0 and self._position_held_minutes(state) >= after_minutes)

    def _hard_time_exit_close_fraction(self, state: TradeState) -> float:
        strategy = config.STRATEGY
        base_fraction = self._clamp(strategy.hard_time_exit_close_fraction, 0.0, 1.0)
        if not self._hard_time_exit_elapsed(state):
            return base_fraction

        step_minutes = max(0.0, strategy.hard_time_exit_step_minutes)
        step_fraction = max(0.0, strategy.hard_time_exit_fraction_step)
        if step_minutes > 0 and step_fraction > 0:
            overdue_minutes = max(
                0.0,
                self._position_held_minutes(state) - max(0.0, strategy.hard_time_exit_after_minutes),
            )
            base_fraction += int(overdue_minutes // step_minutes) * step_fraction
        return self._clamp(base_fraction, 0.0, 1.0)

    def _controlled_loss_max_loss_on_notional(self, state: TradeState) -> float:
        strategy = config.STRATEGY
        max_loss = max(0.0, strategy.controlled_loss_max_loss_on_notional)
        if self._hard_time_exit_elapsed(state):
            max_loss = max(max_loss, max(0.0, strategy.hard_time_exit_max_loss_on_notional))
        return max_loss

    def _absolute_force_exit_elapsed(self, state: TradeState) -> bool:
        if not config.STRATEGY.enable_absolute_force_exit:
            return False
        after_minutes = max(0.0, config.STRATEGY.absolute_force_exit_after_minutes)
        return bool(after_minutes > 0 and self._position_held_minutes(state) >= after_minutes)

    def _hard_time_exit_bypasses_profit_bank(self, state: TradeState) -> bool:
        return bool(
            config.STRATEGY.hard_time_exit_bypass_profit_bank
            and self._hard_time_exit_elapsed(state)
        )

    def _position_too_old_for_averaging(self, state: TradeState) -> bool:
        after_minutes = max(0.0, config.STRATEGY.no_more_averaging_after_minutes)
        return bool(after_minutes > 0 and self._position_held_minutes(state) >= after_minutes)

    def _controlled_loss_available_budget(self) -> float:
        strategy = config.STRATEGY
        if not strategy.enable_controlled_loss_exit:
            return 0.0

        now = time.time()
        local_now = time.localtime(now)
        today_start = time.mktime((
            local_now.tm_year,
            local_now.tm_mon,
            local_now.tm_mday,
            0,
            0,
            0,
            local_now.tm_wday,
            local_now.tm_yday,
            local_now.tm_isdst,
        ))
        week_start = now - 7 * 24 * 60 * 60
        today_profit = max(0.0, self._realized_pnl_since(today_start))
        week_profit = max(0.0, self._realized_pnl_since(week_start))
        budget = (
            today_profit * max(0.0, strategy.controlled_loss_profit_bank_today_fraction)
            + week_profit * max(0.0, strategy.controlled_loss_profit_bank_7d_fraction)
        )
        if budget < max(0.0, strategy.controlled_loss_min_bank_usdt):
            return 0.0
        return budget

    def _position_drawdown(self, state: TradeState, reference_price: float) -> float:
        if state.entry_price <= 0 or reference_price <= 0:
            return 0.0
        if config.POSITION_SIDE == "short":
            return max(0.0, (reference_price - state.entry_price) / state.entry_price)
        return max(0.0, (state.entry_price - reference_price) / state.entry_price)

    def _controlled_loss_contracts(
        self,
        symbol: str,
        state: TradeState,
        reference_price: float,
        had_sell_ladder: bool,
    ) -> float:
        closeable = self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=had_sell_ladder)
        closeable = min(max(0.0, closeable), max(0.0, state.position_size))
        if closeable <= 0 or state.entry_price <= 0:
            return 0.0

        strategy = config.STRATEGY
        if self._hard_time_exit_elapsed(state):
            close_fraction = self._hard_time_exit_close_fraction(state)
        else:
            close_fraction = self._clamp(strategy.controlled_loss_max_position_fraction, 0.0, 1.0)
        max_fraction_contracts = state.position_size * close_fraction
        if max_fraction_contracts <= 0:
            return 0.0

        if self._hard_time_exit_bypasses_profit_bank(state):
            contracts = min(closeable, max_fraction_contracts)
            return self._amount_to_precision(symbol, contracts)

        budget = self._controlled_loss_available_budget()
        if budget <= 0:
            return 0.0

        conservative_loss_rate = (
            self._controlled_loss_max_loss_on_notional(state)
            + max(0.0, config.SELLING.buy_fee_rate)
            + max(0.0, config.SELLING.sell_fee_rate)
        )
        if conservative_loss_rate <= 0:
            return 0.0

        max_entry_notional_by_budget = budget / conservative_loss_rate
        budget_contracts = self._contracts_for_notional(symbol, max_entry_notional_by_budget, state.entry_price)
        contracts = min(closeable, max_fraction_contracts, budget_contracts)
        return self._amount_to_precision(symbol, contracts)

    def _controlled_loss_block_reason(self, symbol: str, state: TradeState, reference_price: float) -> str:
        strategy = config.STRATEGY
        if not strategy.enable_controlled_loss_exit:
            return "controlled_loss_disabled"
        if state.position_size <= 0 or state.entry_price <= 0:
            return "no_position"

        hard_time_exit = self._hard_time_exit_elapsed(state)
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

    def _rebuild_controlled_loss_exit_ladder(self, symbol: str, reason: str) -> bool:
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

        self._place_sell_ladder(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=close_contracts,
            mode="controlled_loss_exit",
        )
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
                f"max_loss={self._controlled_loss_max_loss_on_notional(state):.5f}"
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
                    self._place_sell_ladder(
                        symbol,
                        state.position_size,
                        state.entry_price,
                        rebuild=True,
                        closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=False),
                        mode="urgent_time_exit",
                    )
                return True
            if not state.sell_ladder_orders:
                return self._rebuild_controlled_loss_exit_ladder(symbol, reason="controlled_loss_missing_ladder")
            return self._maybe_reprice_time_exit_ladder(symbol) or True

        return self._rebuild_controlled_loss_exit_ladder(symbol, reason="controlled_loss_activation")

    def _time_exit_reprice_after_minutes(self, mode: str) -> float:
        if mode == "breakeven":
            return max(0.0, config.STRATEGY.ema_breakeven_reprice_minutes)
        return 0.0

    def _maybe_reprice_time_exit_ladder(self, symbol: str) -> bool:
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

        self._place_sell_ladder(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=True),
            mode=mode,
        )
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
        order_id = f"dry_{config.EXIT_SIDE}_{symbol}_{int(created_at)}_runner"
        if not config.RUNTIME.dry_run:
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
            event="exit_ladder_planned" if config.RUNTIME.dry_run else "exit_ladder_placed",
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

        activation = max(0.0, strategy.ema_exit_runner_activation_markup)
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

        pullback = max(0.0, strategy.ema_exit_runner_trailing_pullback)
        take_profit = max(0.0, strategy.ema_exit_runner_take_profit_markup)
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

    def _ema_averaging_budget(self, symbol: str, state: TradeState, reference_price: float) -> Tuple[float, str]:
        account = self._account_snapshot()
        free = account["free"]
        equity = account["total"] or free
        if free <= config.RISK.min_quote_reserve:
            return 0.0, "free_margin_below_reserve"

        available_after_reserve = max(0.0, free - config.RISK.min_quote_reserve)
        leverage = max(float(config.RISK.leverage), 1.0)
        current_position_notional = self._position_notional(symbol, state)
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
        desired_notional = base_fraction * base_notional * (ratio ** power)
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
            f"base_notional={base_notional:.8f};"
            f"current_notional={current_position_notional:.8f};"
            f"ratio={ratio:.6f};"
            f"desired_margin={desired_margin:.8f};planned_margin={planned_margin:.8f}"
        )

    def _ema_averaging_drawdown_threshold(self, stage_index: int) -> float:
        steps = tuple(config.STRATEGY.averaging_drawdown_steps or ())
        if steps:
            index = min(max(0, stage_index), len(steps) - 1)
            return max(0.0, self._safe_float(steps[index], 0.0))
        return max(0.0, self._safe_float(config.STRATEGY.ema_averaging_drawdown_step, 0.0))

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
        if state.zombie_position or state.frozen_no_more_buys or state.entry_orders:
            return
        if state.sell_ladder_orders == []:
            return
        signal_for_quality = dict(signal or {})
        signal_for_quality["symbol"] = symbol
        quality_reason = self._entry_signal_quality_block_reason(signal_for_quality)
        if quality_reason:
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal_for_quality,
                block_reason=quality_reason,
                context={"position_size": state.position_size, "entry_price": state.entry_price},
            )
            if signal and not signal.get("macro_valid", False):
                self._log_event(
                    "DEBUG",
                    f"EMA averaging skipped for {symbol}: macro signal broken",
                    event="ema_average_skipped",
                    symbol=symbol,
                    position_size=state.position_size,
                    entry_price=state.entry_price,
                    reason="ema_macro_broken_no_average",
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
        if state.last_average_signal_timestamp == signal.get("ts"):
            return

        reference_price, _ = self._fetch_reference_price(symbol)
        if reference_price <= 0:
            return

        drawdown = self._position_drawdown(state, reference_price)
        drawdown_threshold = self._ema_averaging_drawdown_threshold(state.average_stage)
        if drawdown < drawdown_threshold:
            self._record_signal_analytics(
                "averaging_checked",
                symbol=symbol,
                signal=signal,
                block_reason="drawdown_below_threshold",
                context={"drawdown": drawdown, "threshold": drawdown_threshold, "stage": state.average_stage + 1},
            )
            return

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

        budget, budget_reason = self._ema_averaging_budget(symbol, state, reference_price)
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
                "stage": state.average_stage + 1,
                "budget_reason": budget_reason,
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
        state.last_average_signal_timestamp = signal.get("ts")
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
            reason=f"ema_averaging_stage_{next_stage};drawdown={drawdown:.5f};threshold={drawdown_threshold:.5f};{budget_reason}",
        )
        self._place_buy_ladder(
            symbol,
            budget,
            reference_price,
            signal,
            reason=f"ema_averaging_stage_{next_stage};{budget_reason}",
            offset_multiplier=1.0,
        )
        state = self._get_state(symbol)
        if state.entry_orders:
            state.average_stage = next_stage
        else:
            state.average_stage = previous_average_stage
        self._save_state()

    def _maybe_place_frozen_recovery_buy(self, symbol: str, signal: Optional[dict]):
        macro_context = self._macro_guard_context()
        if macro_context.get("disable_recovery"):
            self._log_macro_action_blocked("macro_recovery_blocked", symbol, signal, macro_context)
            return
        return

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

        order_id = ""
        if not config.RUNTIME.dry_run:
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
                )
                return True

        self._log_event(
            "WARNING",
            f"Absolute force exit market order placed for {symbol}: contracts={close_amount}",
            event="exit_ladder_rebuilt" if config.RUNTIME.dry_run else "absolute_force_exit_order_placed",
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
            self._place_sell_ladder(
                symbol,
                state.position_size,
                state.entry_price,
                rebuild=True,
                closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=False),
                mode="breakeven",
            )
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
        self._place_sell_ladder(
            symbol,
            state.position_size,
            state.entry_price,
            rebuild=True,
            closeable_contracts=self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=had_sell_ladder),
            mode="breakeven",
        )
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
        if state.last_entry_ladder_signal_timestamp == signal.get("ts"):
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
            key = (symbol, signal.get("ts"), gate_reason)
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
