# -*- coding: utf-8 -*-

import csv
import threading
import time
from typing import Dict, List, Optional, Tuple

import config

from .models import TradeState


class RiskManager:
    def _runtime_rlock(self, name: str):
        lock = getattr(self, name, None)
        if lock is None:
            lock = threading.RLock()
            setattr(self, name, lock)
        return lock

    def _current_total_notional(self) -> float:
        total = 0.0
        for bot in self._account_pnl_bots():
            states = getattr(bot, "states", {}) or {}
            for symbol, state in list(states.items()):
                if symbol not in getattr(bot, "market_by_symbol", {}):
                    continue
                if state.position_size > 0 and state.entry_price > 0:
                    total += bot._contracts_to_notional(symbol, state.position_size, state.entry_price)
                for ref in state.entry_orders or []:
                    total += bot._contracts_to_notional(
                        symbol,
                        bot._safe_float(ref.get("amount"), 0.0),
                        bot._safe_float(ref.get("price"), 0.0),
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
        for symbol, state in list(self.states.items()):
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

    def _quantile(self, values: List[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        q = self._clamp(percentile, 0.0, 1.0)
        raw_index = (len(ordered) - 1) * q
        lower = int(raw_index)
        upper = min(len(ordered) - 1, lower + 1)
        if lower == upper:
            return ordered[lower]
        weight = raw_index - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    def _account_pnl_runtime(self) -> dict:
        runtime = getattr(self, "account_pnl_runtime", None)
        if not isinstance(runtime, dict):
            runtime = {"history": [], "last_sample_at": 0.0}
            self.account_pnl_runtime = runtime
        runtime.setdefault("history", [])
        runtime.setdefault("last_sample_at", 0.0)
        return runtime

    def _account_pnl_runtime_lock(self):
        return self._runtime_rlock("_account_pnl_lock")

    def _account_pnl_bots(self) -> List[object]:
        bots = getattr(self, "account_pnl_bots", None)
        if not bots:
            return [self]
        return list(bots)

    def _account_position_rows(self) -> List[dict]:
        rows: List[dict] = []
        for bot in self._account_pnl_bots():
            states = getattr(bot, "states", {}) or {}
            for symbol, state in list(states.items()):
                if state.position_size <= 0 or state.entry_price <= 0:
                    continue
                try:
                    notional = bot._position_notional(symbol, state)
                except Exception:
                    notional = 0.0
                realized = self._safe_float(getattr(state, "realized_pnl", 0.0), 0.0)
                unrealized = self._safe_float(getattr(state, "unrealized_pnl", 0.0), 0.0)
                net = self._safe_float(getattr(state, "net_open_pnl", realized + unrealized), realized + unrealized)
                rows.append(
                    {
                        "bot": bot,
                        "profile": getattr(bot, "profile_name", ""),
                        "symbol": symbol,
                        "state": state,
                        "notional": notional,
                        "realized_pnl": realized,
                        "unrealized_pnl": unrealized,
                        "net_open_pnl": net,
                    }
                )
        return rows

    def _account_pnl_context(self, reason: str = "sample", force_sample: bool = False) -> Dict[str, object]:
        with self._account_pnl_runtime_lock():
            strategy = config.STRATEGY
            now = time.time()
            rows = self._account_position_rows()
            open_pnl = sum(self._safe_float(row.get("net_open_pnl"), 0.0) for row in rows)
            unrealized = sum(self._safe_float(row.get("unrealized_pnl"), 0.0) for row in rows)
            realized_open = sum(self._safe_float(row.get("realized_pnl"), 0.0) for row in rows)
            open_notional = sum(max(0.0, self._safe_float(row.get("notional"), 0.0)) for row in rows)
            runtime = self._account_pnl_runtime()
            history = runtime.get("history")
            if not isinstance(history, list):
                history = []
                runtime["history"] = history

            window_sec = max(0.0, self._safe_float(strategy.account_pnl_window_minutes, 0.0)) * 60.0
            if window_sec > 0:
                cutoff = now - window_sec
                history[:] = [item for item in history if self._safe_float(item.get("ts"), 0.0) >= cutoff]

            sample_interval = max(0.0, self._safe_float(strategy.account_pnl_sample_interval_sec, 0.0))
            last_sample_at = self._safe_float(runtime.get("last_sample_at"), 0.0)
            should_sample = bool(strategy.account_pnl_enabled) and (
                force_sample or not history or sample_interval <= 0 or now - last_sample_at >= sample_interval
            )
            if should_sample:
                history.append(
                    {
                        "ts": now,
                        "open_pnl": open_pnl,
                        "unrealized_pnl": unrealized,
                        "realized_open_pnl": realized_open,
                        "open_notional": open_notional,
                        "open_pnl_rate": open_pnl / open_notional if open_notional > 0 else 0.0,
                        "position_count": len(rows),
                    }
                )
                runtime["last_sample_at"] = now

            values = [self._safe_float(item.get("open_pnl"), 0.0) for item in history]
            rate_values = [
                self._safe_float(item.get("open_pnl_rate"), 0.0)
                if "open_pnl_rate" in item
                else (
                    self._safe_float(item.get("open_pnl"), 0.0)
                    / self._safe_float(item.get("open_notional"), 0.0)
                    if self._safe_float(item.get("open_notional"), 0.0) > 0
                    else 0.0
                )
                for item in history
            ]
            current_rate = open_pnl / open_notional if open_notional > 0 else 0.0
            if current_rate or not rate_values:
                rate_values = list(rate_values) + [current_rate]
            previous = values[-2] if len(values) >= 2 else 0.0
            delta = open_pnl - previous if len(values) >= 2 else 0.0
            context: Dict[str, object] = {
                "ts": now,
                "positions": rows,
                "open_pnl": open_pnl,
                "unrealized_pnl": unrealized,
                "realized_open_pnl": realized_open,
                "open_notional": open_notional,
                "open_pnl_rate": open_pnl / open_notional if open_notional > 0 else 0.0,
                "position_count": len(rows),
                "history_samples": len(values),
                "history_values": values,
                "min_open_pnl": min(values) if values else open_pnl,
                "p25_open_pnl": self._quantile(values, 0.25) if values else open_pnl,
                "median_open_pnl": self._quantile(values, 0.50) if values else open_pnl,
                "p75_open_pnl": self._quantile(values, 0.75) if values else open_pnl,
                "max_open_pnl": max(values) if values else open_pnl,
                "max_open_pnl_rate": max(rate_values) if rate_values else current_rate,
                "min_open_pnl_rate": min(rate_values) if rate_values else current_rate,
                "previous_open_pnl": previous,
                "delta_open_pnl": delta,
                "reason": reason,
            }
            if should_sample:
                context["history_samples"] = len(history)
                append_account = getattr(self, "_append_account_pnl_csv", None)
                if append_account:
                    append_account(context)
            return context

    def _position_pnl_rate(self, symbol: str, state: TradeState) -> float:
        notional = self._position_notional(symbol, state)
        if notional <= 0:
            return 0.0
        return self._safe_float(state.net_open_pnl, 0.0) / notional

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
        if state.hard_stop_order:
            self._cancel_hard_stop_order(symbol, reason=close_reason)
        if state.entry_orders or state.sell_ladder_orders or state.hard_stop_order:
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
                exception=exc,
            )
            return True

        state.frozen_no_more_buys = True
        state.zombie_position = True
        state.pending_close_order = {
            "id": str(order.get("id", "")),
            "side": config.EXIT_SIDE,
            "price": 0.0,
            "amount": close_amount,
            "created_at": time.time(),
            "market_close": True,
            "reduce_only": True,
            "reason": close_reason,
        }
        state.pending_close_reason = close_reason
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
        if not config.RISK.dust_close_enabled:
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
        if not config.RISK.tiny_entry_close_enabled:
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

    def _place_account_reduce_only_close(
        self,
        symbol: str,
        fraction: float,
        reason: str,
        order_type: str = "limit",
    ) -> bool:
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            return False
        if not config.RUNTIME.reduce_only_enabled:
            self._log_event(
                "ERROR",
                f"Account unload blocked for {symbol}: reduce-only disabled",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{reason};account_unload_reduce_only_disabled",
            )
            return True

        had_sell_ladder = bool(state.sell_ladder_orders)
        if state.entry_orders:
            self._cancel_entry_orders(symbol, reason="account_profit_unload")
        if state.sell_ladder_orders:
            self._cancel_sell_orders(symbol, reason="account_profit_unload")
        if state.hard_stop_order:
            self._cancel_hard_stop_order(symbol, reason="account_profit_unload")
        state = self._get_state(symbol)
        if state.entry_orders or state.sell_ladder_orders or state.hard_stop_order:
            self._log_event(
                "WARNING",
                f"Account unload delayed for {symbol}: tracked order cancel failed",
                event="account_profit_unload_skipped",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{reason};account_unload_cancel_failed",
            )
            return True

        closeable = self._closeable_contracts_for_exit_ladder(symbol, had_sell_ladder=had_sell_ladder)
        requested = state.position_size * self._clamp(fraction, 0.0, 1.0)
        contracts = self._amount_to_precision(symbol, min(max(0.0, requested), max(0.0, closeable), state.position_size))
        if contracts <= 0:
            self._log_event(
                "WARNING",
                f"Account unload skipped for {symbol}: close amount below exchange minimum",
                event="account_profit_unload_skipped",
                symbol=symbol,
                side=config.EXIT_SIDE,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{reason};account_unload_amount_below_minimum;fraction={fraction:.5f}",
            )
            return True

        normalized_order_type = str(order_type or "limit").lower()
        price = None
        if normalized_order_type != "market":
            price = self._aggressive_exit_limit_price(symbol)
        if normalized_order_type != "market" and (price is None or price <= 0):
            self._log_event(
                "WARNING",
                f"Account unload skipped for {symbol}: reference price unavailable",
                event="account_profit_unload_skipped",
                symbol=symbol,
                side=config.EXIT_SIDE,
                amount=contracts,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{reason};account_unload_price_unavailable",
            )
            return True

        created_at = time.time()
        try:
            order = self._create_one_way_order(
                symbol=symbol,
                order_type=normalized_order_type,
                side=config.EXIT_SIDE,
                amount=contracts,
                price=price,
                reduce_only=True,
            )
            order_id = str(order.get("id"))
        except Exception as exc:
            self._log_event(
                "ERROR",
                f"Account unload reduce-only order failed for {symbol}: {exc}",
                event="reduce_only_violation_prevented",
                symbol=symbol,
                side=config.EXIT_SIDE,
                price=price,
                amount=contracts,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason=f"{reason};account_unload_order_rejected",
                exception=exc,
            )
            return True

        ref = {
            "id": order_id,
            "side": config.EXIT_SIDE,
            "price": price or 0.0,
            "amount": contracts,
            "created_at": created_at,
            "stage": 1,
            "mode": "account_unload",
            "account_unload": True,
            "market_close": normalized_order_type == "market",
            "reason": reason,
        }
        state.sell_ladder_orders = [ref]
        state.sell_ladder_mode = "account_unload"
        state.sell_ladder_signature = self._sell_ladder_signature("account_unload", symbol, state)
        state.last_account_unload_at = created_at
        state.account_unload_count = int(self._safe_float(state.account_unload_count, 0.0)) + 1
        state.frozen_no_more_buys = True
        self._refresh_active_side(state)
        self._save_state()
        self._log_event(
            "INFO",
            f"Account profit unload placed for {symbol}: contracts={contracts} type={normalized_order_type} price={price or 0.0}",
            event="account_profit_unload_placed",
            symbol=symbol,
            side=config.EXIT_SIDE,
            order_id=order_id,
            price=price or 0.0,
            amount=contracts,
            position_size=state.position_size,
            entry_price=state.entry_price,
            reason=f"{reason};fraction={fraction:.5f};position_pnl={state.net_open_pnl:.8f};position_pnl_rate={self._position_pnl_rate(symbol, state):.6f}",
        )
        return True

    def _maybe_apply_account_profit_unload(self, symbol: str, signal: Optional[dict] = None) -> bool:
        strategy = config.STRATEGY
        if not (strategy.account_pnl_enabled and strategy.account_profit_unload_enabled):
            return False
        state = self._get_state(symbol)
        if state.position_size <= 0 or state.entry_price <= 0:
            return False
        if state.sell_ladder_mode == "account_unload" and not state.sell_ladder_orders:
            state.sell_ladder_mode = "normal"
            state.sell_ladder_signature = ""
            self._save_state()
        if state.sell_ladder_mode not in {"normal", "account_unload"}:
            return False

        cooldown = max(0.0, strategy.account_profit_unload_cooldown_sec)
        last_unload_at = self._safe_float(state.last_account_unload_at, 0.0)
        if last_unload_at > 0 and cooldown > 0 and time.time() - last_unload_at < cooldown:
            return False

        context = self._account_pnl_context(reason="account_profit_unload")
        account_pnl = self._safe_float(context.get("open_pnl"), 0.0)
        account_notional = self._safe_float(context.get("open_notional"), 0.0)
        if account_pnl <= 0 or account_notional <= 0:
            return False

        history_values = list(context.get("history_values") or [])
        percentile_threshold = self._quantile(
            history_values,
            self._clamp(strategy.account_profit_unload_percentile, 0.0, 1.0),
        ) if history_values else account_pnl
        quote_threshold = max(0.0, strategy.account_profit_unload_min_pnl_quote)
        rate_threshold = account_notional * max(0.0, strategy.account_profit_unload_min_pnl_rate)
        trigger_threshold = max(quote_threshold, rate_threshold, percentile_threshold)
        if account_pnl + 1e-12 < trigger_threshold:
            return False

        position_pnl = self._safe_float(state.net_open_pnl, 0.0)
        position_rate = self._position_pnl_rate(symbol, state)
        if position_pnl < max(0.0, strategy.account_profit_unload_min_position_pnl_quote):
            return False
        if position_rate < max(0.0, strategy.account_profit_unload_min_position_pnl_rate):
            return False

        fraction = self._clamp(strategy.account_profit_unload_fraction, 0.0, 1.0)
        peak = self._safe_float(context.get("max_open_pnl"), account_pnl)
        drawdown = max(0.0, peak - account_pnl)
        peak_drawdown_fraction = max(0.0, strategy.account_profit_unload_peak_drawdown_fraction)
        if peak > 0 and peak_drawdown_fraction > 0 and drawdown >= peak * peak_drawdown_fraction:
            fraction = max(fraction, self._clamp(strategy.account_profit_unload_drawdown_fraction, 0.0, 1.0))
        full_threshold = max(0.0, strategy.account_profit_unload_full_pnl_quote)
        if full_threshold > 0 and account_pnl >= full_threshold:
            fraction = 1.0
        if fraction <= 0:
            return False

        return self._place_account_reduce_only_close(
            symbol,
            fraction=fraction,
            reason=(
                f"account_profit_unload;account_pnl={account_pnl:.8f};"
                f"threshold={trigger_threshold:.8f};pctl={percentile_threshold:.8f};"
                f"peak={peak:.8f};drawdown={drawdown:.8f}"
            ),
        )

    def _maybe_apply_account_pnl_trailing(self, symbol: str, signal: Optional[dict] = None) -> bool:
        strategy = config.STRATEGY
        if not (strategy.account_pnl_enabled and strategy.account_pnl_trailing_enabled):
            return False

        context = self._account_pnl_context(reason="account_pnl_trailing")
        account_pnl = self._safe_float(context.get("open_pnl"), 0.0)
        account_rate = self._safe_float(context.get("open_pnl_rate"), 0.0)
        peak_rate = self._safe_float(context.get("max_open_pnl_rate"), account_rate)
        activation_rate = max(0.0, self._safe_float(strategy.account_pnl_trailing_activation_rate, 0.0))
        stop_rate = max(0.0, self._safe_float(strategy.account_pnl_trailing_stop_rate, 0.0))
        min_pnl_quote = max(0.0, self._safe_float(strategy.account_pnl_trailing_min_pnl_quote, 0.0))
        if activation_rate <= 0 or stop_rate <= 0:
            return False
        if peak_rate + 1e-12 < activation_rate:
            return False
        if account_pnl + 1e-12 < min_pnl_quote:
            return False
        if account_rate > stop_rate + 1e-12:
            return False

        reason = (
            f"account_pnl_trailing;account_pnl={account_pnl:.8f};"
            f"account_rate={account_rate:.6f};peak_rate={peak_rate:.6f};"
            f"activation_rate={activation_rate:.6f};stop_rate={stop_rate:.6f}"
        )
        placed = False
        for row in list(context.get("positions") or []):
            row_bot = row.get("bot") or self
            row_symbol = str(row.get("symbol") or "")
            if not row_symbol:
                continue
            profile = getattr(row_bot, "profile", None) or config.current_profile()
            with config.use_profile(profile):
                row_state = row_bot._get_state(row_symbol)
                ref_reason = str((row_state.sell_ladder_orders[0] if row_state.sell_ladder_orders else {}).get("reason") or "")
                if row_state.sell_ladder_mode == "account_unload" and "account_pnl_trailing" in ref_reason:
                    placed = True
                    continue
                placed = row_bot._place_account_reduce_only_close(
                    row_symbol,
                    fraction=1.0,
                    reason=reason,
                    order_type="market",
                ) or placed

        if placed:
            self._log_event(
                "WARNING",
                "Account PnL trailing triggered; reduce-only market closes requested for all open positions",
                event="account_pnl_trailing_triggered",
                symbol=symbol,
                reason=reason,
            )
        return placed

    def _risk_budget(
        self,
        symbol: str,
        state: TradeState,
        reference_price: float,
        is_new_position: bool,
        signal: Optional[dict] = None,
        budget_scale: float = 1.0,
    ) -> Tuple[float, str]:
        account = self._account_snapshot(symbol)
        free = account["free"]
        equity = account["total"] or free
        context = {
            "free": free,
            "equity": equity,
            "reserve": config.RISK.min_quote_reserve,
            "is_new_position": bool(is_new_position),
            "budget_scale": max(0.0, self._safe_float(budget_scale, 0.0)),
        }

        def finish(budget: float, reason: str, **extra) -> Tuple[float, str]:
            context.update(extra)
            self._last_risk_budget_context = dict(context)
            return budget, reason

        if free <= config.RISK.min_quote_reserve:
            return finish(0.0, "free_margin_below_reserve")

        if is_new_position and self._active_position_slots() >= config.RISK.max_active_positions:
            return finish(0.0, "max_active_positions_reached")

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
        context.update(
            {
                "base_margin_budget": base_margin_budget,
                "available_after_reserve": available_after_reserve,
                "planned_margin": planned_margin,
                "configured_leverage": leverage,
                "total_cap_notional": total_cap_notional,
                "position_cap_notional": position_cap_notional,
                "current_total_notional": current_total_notional,
                "current_symbol_notional": current_symbol_notional,
                "total_remaining": total_remaining,
                "symbol_remaining": symbol_remaining,
                "base_notional": base_notional,
                "budget_multiplier": multiplier,
                "volatility_budget_multiplier": volatility_budget,
                "effective_budget_multiplier": effective_multiplier,
                "margin_cap_notional": margin_cap_notional,
                "planned_notional": planned_notional,
                "min_contracts": min_contracts,
                "min_notional": min_notional,
            }
        )

        if planned_notional <= 0 or planned_margin <= 0:
            return finish(0.0, "notional_limit_reached")

        contracts = self._contracts_for_notional(symbol, planned_notional, reference_price)
        if contracts <= 0:
            return finish(0.0, "order_size_below_exchange_minimum", contracts=contracts)
        context["contracts"] = contracts

        return finish(planned_margin, (
            f"ok:budget_multiplier={multiplier:.3f};"
            f"vol_budget={volatility_budget:.3f};"
            f"effective_budget_multiplier={effective_multiplier:.3f};budget_scale={scale:.3f}"
        ))

    def _static_exit_profit_context(self) -> dict:
        roundtrip_fee = config.SELLING.buy_fee_rate + config.SELLING.sell_fee_rate
        fee_floor = roundtrip_fee * config.STRATEGY.min_profit_fee_multiplier
        return {"profit_floor": max(config.SELLING.min_gross_profit_floor, fee_floor)}

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


__all__ = ["RiskManager"]
