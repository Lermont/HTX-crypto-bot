# -*- coding: utf-8 -*-

import atexit
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, fields
from typing import Dict, List, Optional, Tuple

import config

from .models import TradeState


class StateMixin:
    def _load_state(self) -> Dict[str, TradeState]:
        if not self.state_path.exists():
            return {}

        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger = getattr(self, "log", logging.getLogger(__name__))
            logger.warning("Could not read futures state; starting with empty state: %s", exc)
            return {}

        known_fields = {item.name for item in fields(TradeState)}
        result: Dict[str, TradeState] = {}
        if not isinstance(raw, dict):
            return result

        for symbol, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            has_remaining_cost_basis = (
                "remaining_entry_quote" in payload
                and "remaining_buy_fees_quote" in payload
            )
            safe_payload = {key: value for key, value in payload.items() if key in known_fields}
            state = TradeState(**safe_payload)
            state.symbol = state.symbol or symbol
            state.market_symbol = state.market_symbol or state.symbol
            state.entry_orders = self._normalize_order_refs(state.entry_orders)
            state.sell_ladder_orders = self._normalize_order_refs(state.sell_ladder_orders)
            if has_remaining_cost_basis:
                self._refresh_net_open_pnl(state)
            else:
                self._recalculate_proportional_pnl_from_totals(state)
            result[symbol] = state
        return result

    def _save_state(self):
        if config.RUNTIME.dry_run:
            return
        payload = {symbol: asdict(state) for symbol, state in self.states.items()}
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _pid_is_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        if os.name == "nt":
            cmdline = self._pid_command_line(pid)
            if not cmdline:
                return False
            return "bot.py" in cmdline.replace("\\", "/").lower()
        return True

    def _pid_command_line(self, pid: int) -> str:
        if os.name != "nt":
            return ""
        try:
            output = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            return output.strip()
        except Exception:
            return ""

    def _acquire_runtime_lock(self):
        if self.lock_path.exists():
            raw_pid = self.lock_path.read_text(encoding="utf-8").strip()
            existing_pid = int(raw_pid) if raw_pid.isdigit() else 0
            if existing_pid and self._pid_is_running(existing_pid):
                raise RuntimeError(
                    f"Another bot instance is already running with PID {existing_pid}. "
                    f"Remove {self.lock_path} only after that process exits."
                )

        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(self._release_runtime_lock)

    def _release_runtime_lock(self):
        try:
            if not self.lock_path.exists():
                return
            raw_pid = self.lock_path.read_text(encoding="utf-8").strip()
            if raw_pid == str(os.getpid()):
                self.lock_path.unlink()
        except OSError:
            pass

    def _get_state(self, symbol: str) -> TradeState:
        if symbol not in self.states:
            self.states[symbol] = TradeState(symbol=symbol, market_symbol=symbol)
        state = self.states[symbol]
        state.symbol = state.symbol or symbol
        state.market_symbol = state.market_symbol or symbol
        if state.position_size <= 0 or self._safe_float(state.leverage, 0.0) <= 0:
            state.leverage = config.RISK.leverage
        state.margin_mode = config.RISK.margin_mode
        return state

    def _reset_state(self, symbol: str, preserve_cooldown: Optional[float] = None):
        now = time.time()
        cooldown = preserve_cooldown if preserve_cooldown and preserve_cooldown > now else None
        self.states[symbol] = TradeState(
            symbol=symbol,
            market_symbol=symbol,
            cooldown_until=cooldown,
            leverage=config.RISK.leverage,
            margin_mode=config.RISK.margin_mode,
        )
        self._save_state()

    def _normalize_order_refs(self, refs: list) -> list:
        normalized = []
        for item in refs or []:
            if isinstance(item, dict):
                ref = dict(item)
                if ref.get("id") is not None:
                    ref["id"] = str(ref["id"])
                    normalized.append(ref)
            elif item:
                normalized.append({"id": str(item), "created_at": 0.0})
        return normalized

    def _order_ids(self, refs: list) -> set:
        return {str(item.get("id")) for item in refs or [] if item.get("id") is not None}

    def _order_remaining_amount(self, order: dict) -> float:
        amount = self._safe_float(order.get("remaining"), 0.0)
        if amount <= 0:
            amount = self._safe_float(order.get("amount"), 0.0)
        return amount

    def _order_reduce_only_flag(self, order: dict) -> Optional[bool]:
        if not isinstance(order, dict):
            return None

        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        for source in (order, info):
            for key in ("reduceOnly", "reduce_only", "reduce-only"):
                if key not in source:
                    continue
                value = source.get(key)
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return value != 0
                normalized = str(value).strip().lower()
                if normalized in {"1", "true", "yes", "y", "on"}:
                    return True
                if normalized in {"0", "false", "no", "n", "off"}:
                    return False
        return None

    def _safe_float(self, value, default=0.0) -> float:
        try:
            if value is None:
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _refresh_active_side(self, state: TradeState):
        sides = {
            str(ref.get("side") or config.ENTRY_SIDE).lower()
            for ref in state.entry_orders or []
        }
        sides.update(
            str(ref.get("side") or config.EXIT_SIDE).lower()
            for ref in state.sell_ladder_orders or []
        )
        sides.discard("")
        if len(sides) > 1:
            state.active_side = "both"
        elif sides:
            state.active_side = next(iter(sides))
        else:
            state.active_side = None

    def _estimate_sell_quote_from_refs(self, symbol: str, state: TradeState, contracts: float) -> float:
        if contracts <= 0:
            return 0.0

        remaining = contracts
        quote = 0.0
        for ref in state.sell_ladder_orders:
            if remaining <= 0:
                break
            ref_amount = self._safe_float(ref.get("amount"), 0.0)
            ref_price = self._safe_float(ref.get("price"), 0.0)
            used = min(ref_amount, remaining)
            quote += self._contracts_to_notional(symbol, used, ref_price)
            remaining -= used

        if remaining > 1e-12:
            if config.POSITION_SIDE == "short":
                fallback_price = state.entry_price * (1 - config.SELLING.min_gross_profit_floor)
            else:
                fallback_price = state.entry_price * (1 + config.SELLING.min_gross_profit_floor)
            quote += self._contracts_to_notional(symbol, remaining, fallback_price)
        return quote

    def _fee_quote_from_payload(self, payload: dict) -> Tuple[Optional[float], str]:
        if not isinstance(payload, dict):
            return None, ""

        quote = config.EXCHANGE.quote_currency.upper()
        total = 0.0
        found = False
        currency = ""

        def add_fee_value(raw_value, raw_currency=""):
            nonlocal total, found, currency
            if raw_value is None:
                return
            fee_currency = str(raw_currency or "").upper()
            if fee_currency and fee_currency != quote:
                return
            total += self._safe_float(raw_value, 0.0)
            found = True
            currency = fee_currency or currency or quote

        def add_fee_item(item):
            if isinstance(item, dict):
                add_fee_value(
                    item.get("cost", item.get("fee", item.get("amount"))),
                    item.get("currency", item.get("fee_currency", item.get("fee_asset"))),
                )
            elif item is not None:
                add_fee_value(item)

        fees = payload.get("fees")
        if isinstance(fees, list):
            for fee in fees:
                add_fee_item(fee)
        add_fee_item(payload.get("fee"))

        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        for source, keys in (
            (payload, ("trade_fee", "tradeFee", "fee_amount", "feeAmount")),
            (info, ("trade_fee", "tradeFee", "fee_amount", "feeAmount", "fee")),
        ):
            for key in keys:
                value = source.get(key)
                if isinstance(value, dict):
                    continue
                add_fee_value(
                    value,
                    source.get("fee_currency", source.get("feeCurrency", source.get("fee_asset", source.get("feeAsset")))),
                )

        return (total, currency or quote) if found else (None, "")

    def _order_filled_contracts(self, order: dict) -> float:
        filled = self._safe_float(order.get("filled"), 0.0)
        if filled > 0:
            return filled

        amount = self._safe_float(order.get("amount"), 0.0)
        remaining_present = "remaining" in order and order.get("remaining") is not None
        if amount > 0 and remaining_present:
            return max(0.0, amount - self._safe_float(order.get("remaining"), 0.0))

        status = str(order.get("status") or "").lower()
        if amount > 0 and status in {"closed", "filled"}:
            return amount
        return 0.0

    def _fill_snapshot_from_order(self, symbol: str, order: dict, source: str) -> Optional[dict]:
        if not isinstance(order, dict):
            return None
        contracts = self._order_filled_contracts(order)
        if contracts <= 0:
            return None

        price = 0.0
        for payload in (order, order.get("info") if isinstance(order.get("info"), dict) else {}):
            for key in ("average", "avgPrice", "avg_price", "price", "trade_avg_price", "tradeAvgPrice"):
                price = self._safe_float(payload.get(key), 0.0)
                if price > 0:
                    break
            if price > 0:
                break
        quote = self._safe_float(order.get("cost"), 0.0)
        if quote <= 0:
            info = order.get("info") if isinstance(order.get("info"), dict) else {}
            for key in ("trade_turnover", "tradeTurnover", "filled_amount", "filledAmount", "cost"):
                quote = self._safe_float(info.get(key), 0.0)
                if quote > 0:
                    break
        if quote <= 0 and price > 0:
            quote = self._contracts_to_notional(symbol, contracts, price)
        if price <= 0 and quote > 0:
            price = self._average_price_from_notional(symbol, contracts, quote)
        if quote <= 0 or price <= 0:
            return None

        fee_quote, fee_currency = self._fee_quote_from_payload(order)
        return {
            "order_id": str(order.get("id") or order.get("order") or ""),
            "contracts": contracts,
            "quote": quote,
            "price": price,
            "fee_quote": fee_quote,
            "fee_currency": fee_currency,
            "source": source,
        }

    def _trade_order_id(self, trade: dict) -> str:
        if not isinstance(trade, dict):
            return ""
        info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
        for source in (trade, info):
            for key in ("order", "orderId", "order_id", "order-id"):
                value = source.get(key)
                if value is not None:
                    return str(value)
        return ""

    def _fill_snapshot_from_trades(self, symbol: str, order_id: str, trades: list) -> Optional[dict]:
        contracts = 0.0
        quote = 0.0
        fee_quote = 0.0
        fee_found = False
        fee_currency = ""

        for trade in trades or []:
            if order_id and self._trade_order_id(trade) != order_id:
                continue
            amount = self._safe_float(trade.get("amount"), 0.0)
            price = self._safe_float(trade.get("price"), 0.0)
            cost = self._safe_float(trade.get("cost"), 0.0)
            if amount <= 0:
                continue
            contracts += amount
            quote += cost if cost > 0 else self._contracts_to_notional(symbol, amount, price)
            fee, currency = self._fee_quote_from_payload(trade)
            if fee is not None:
                fee_quote += fee
                fee_currency = currency or fee_currency
                fee_found = True

        if contracts <= 0 or quote <= 0:
            return None
        return {
            "order_id": order_id,
            "contracts": contracts,
            "quote": quote,
            "price": self._average_price_from_notional(symbol, contracts, quote),
            "fee_quote": fee_quote if fee_found else None,
            "fee_currency": fee_currency,
            "source": "trades",
        }

    def _fetch_order_fill_snapshot(self, symbol: str, ref: dict, open_order: Optional[dict] = None) -> Optional[dict]:
        order_id = str(ref.get("id") or "")
        snapshot = self._fill_snapshot_from_order(symbol, open_order, "open_order") if open_order else None

        if (
            not snapshot
            and not config.RUNTIME.dry_run
            and config.RUNTIME.fetch_fill_details_on_sync
            and order_id
            and self.exchange.has.get("fetchOrder")
        ):
            try:
                order = self.exchange.fetch_order(order_id, symbol, params=self._position_params())
                snapshot = self._fill_snapshot_from_order(symbol, order, "order")
            except Exception as exc:
                self._log_event(
                    "DEBUG",
                    f"Fill order details unavailable for {symbol} order {order_id}: {exc}",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    order_id=order_id,
                    reason="fill_order_fetch_failed",
                )

        if (
            (not snapshot or snapshot.get("fee_quote") is None)
            and not config.RUNTIME.dry_run
            and config.RUNTIME.fetch_fill_details_on_sync
            and order_id
            and self.exchange.has.get("fetchMyTrades")
        ):
            since = None
            created_at = self._safe_float(ref.get("created_at"), 0.0)
            if created_at > 0:
                since = int(max(0.0, created_at - config.RUNTIME.fill_detail_lookback_sec) * 1000)
            try:
                trades = self.exchange.fetch_my_trades(symbol, since=since, limit=100, params=self._position_params())
                trade_snapshot = self._fill_snapshot_from_trades(symbol, order_id, trades)
                if trade_snapshot:
                    snapshot = trade_snapshot
            except Exception as exc:
                self._log_event(
                    "DEBUG",
                    f"Fill trade details unavailable for {symbol} order {order_id}: {exc}",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    order_id=order_id,
                    reason="fill_trades_fetch_failed",
                )

        return snapshot

    def _fill_delta_from_snapshot(self, symbol: str, ref: dict, snapshot: dict) -> Optional[dict]:
        cumulative_contracts = self._safe_float(snapshot.get("contracts"), 0.0)
        if cumulative_contracts <= 0:
            return None

        previous_contracts = self._safe_float(ref.get("filled"), 0.0)
        delta_contracts = cumulative_contracts - previous_contracts
        if delta_contracts <= max(self._get_min_contracts(symbol) * 1e-9, 1e-12):
            return None

        cumulative_quote = self._safe_float(snapshot.get("quote"), 0.0)
        previous_quote = self._safe_float(ref.get("filled_quote"), 0.0)
        delta_quote = cumulative_quote - previous_quote
        if delta_quote <= 0:
            delta_quote = self._contracts_to_notional(
                symbol,
                delta_contracts,
                self._safe_float(snapshot.get("price"), 0.0),
            )

        cumulative_fee = snapshot.get("fee_quote")
        delta_fee = None
        if cumulative_fee is not None:
            cumulative_fee = self._safe_float(cumulative_fee, 0.0)
            delta_fee = cumulative_fee - self._safe_float(ref.get("filled_fee_quote"), 0.0)
            ref["filled_fee_quote"] = cumulative_fee

        ref["filled"] = cumulative_contracts
        ref["filled_quote"] = max(cumulative_quote, previous_quote + delta_quote)
        return {
            "order_id": str(ref.get("id") or snapshot.get("order_id") or ""),
            "contracts": delta_contracts,
            "quote": delta_quote,
            "price": self._average_price_from_notional(symbol, delta_contracts, delta_quote),
            "fee_quote": delta_fee,
            "fee_currency": snapshot.get("fee_currency") or config.EXCHANGE.quote_currency,
            "source": snapshot.get("source") or "exchange",
        }

    def _scale_fill_details(self, symbol: str, details: List[dict], expected_contracts: float) -> List[dict]:
        total_contracts = sum(self._safe_float(item.get("contracts"), 0.0) for item in details)
        if expected_contracts <= 0 or total_contracts <= expected_contracts + max(self._get_min_contracts(symbol) * 1e-9, 1e-12):
            return details

        ratio = expected_contracts / total_contracts
        scaled = []
        for item in details:
            detail = dict(item)
            detail["contracts"] = self._safe_float(detail.get("contracts"), 0.0) * ratio
            detail["quote"] = self._safe_float(detail.get("quote"), 0.0) * ratio
            if detail.get("fee_quote") is not None:
                detail["fee_quote"] = self._safe_float(detail.get("fee_quote"), 0.0) * ratio
            detail["price"] = self._average_price_from_notional(symbol, detail["contracts"], detail["quote"])
            scaled.append(detail)
        return scaled

    def _collect_order_fill_details(
        self,
        symbol: str,
        state: TradeState,
        side: str,
        expected_contracts: float,
        open_orders: Optional[List[dict]] = None,
    ) -> List[dict]:
        if expected_contracts <= 0 or not config.RUNTIME.fetch_fill_details_on_sync:
            return []

        if side == config.ENTRY_SIDE:
            refs = state.entry_orders
        elif side == config.EXIT_SIDE:
            refs = state.sell_ladder_orders
        else:
            refs = []
        open_lookup = {str(order.get("id")): order for order in (open_orders or []) if order.get("id") is not None}
        details = []
        for ref in refs or []:
            if str(ref.get("side") or side).lower() != side:
                continue
            order_id = str(ref.get("id") or "")
            snapshot = self._fetch_order_fill_snapshot(symbol, ref, open_lookup.get(order_id))
            if not snapshot:
                continue
            detail = self._fill_delta_from_snapshot(symbol, ref, snapshot)
            if detail:
                details.append(detail)

        return self._scale_fill_details(symbol, details, expected_contracts)

    def _fill_details_with_fallback(
        self,
        symbol: str,
        side: str,
        contracts: float,
        fallback_price: float,
        fill_details: Optional[List[dict]],
        fallback_quote: float = 0.0,
    ) -> List[dict]:
        details = [dict(item) for item in (fill_details or []) if self._safe_float(item.get("contracts"), 0.0) > 0]
        details = self._scale_fill_details(symbol, details, contracts)
        detailed_contracts = sum(self._safe_float(item.get("contracts"), 0.0) for item in details)
        remaining = max(0.0, contracts - detailed_contracts)
        if remaining > max(self._get_min_contracts(symbol) * 1e-9, 1e-12):
            quote = (
                fallback_quote * (remaining / contracts)
                if fallback_quote > 0 and contracts > 0
                else self._contracts_to_notional(symbol, remaining, fallback_price)
            )
            rate = config.SELLING.buy_fee_rate if side == "buy" else config.SELLING.sell_fee_rate
            details.append(
                {
                    "order_id": "",
                    "contracts": remaining,
                    "quote": quote,
                    "price": self._average_price_from_notional(symbol, remaining, quote),
                    "fee_quote": quote * rate,
                    "fee_currency": config.EXCHANGE.quote_currency,
                    "source": "fallback_config_fee",
                }
            )
        return details

    def _refresh_net_open_pnl(self, state: TradeState):
        state.remaining_entry_quote = max(0.0, self._safe_float(state.remaining_entry_quote, 0.0))
        state.remaining_buy_fees_quote = max(0.0, self._safe_float(state.remaining_buy_fees_quote, 0.0))
        state.net_open_pnl = self._safe_float(state.realized_pnl, 0.0) + self._safe_float(state.unrealized_pnl, 0.0)

    def _recalculate_proportional_pnl_from_totals(self, state: TradeState):
        bought_amount = max(0.0, self._safe_float(state.total_bought_amount, 0.0))
        sold_amount = max(0.0, self._safe_float(state.total_sold_amount, 0.0))

        if config.POSITION_SIDE == "short":
            entry_amount = sold_amount
            exit_amount = bought_amount
            closed_ratio = min(1.0, exit_amount / entry_amount) if entry_amount > 0 else 0.0
            total_entry_quote = max(0.0, self._safe_float(state.total_sold_quote, 0.0))
            total_entry_fees = max(0.0, self._safe_float(state.paid_sell_fees_quote, 0.0))
            allocated_entry_quote = total_entry_quote * closed_ratio
            allocated_entry_fees = total_entry_fees * closed_ratio
            state.remaining_entry_quote = max(0.0, total_entry_quote - allocated_entry_quote)
            state.remaining_buy_fees_quote = max(0.0, total_entry_fees - allocated_entry_fees)
            state.realized_pnl = (
                allocated_entry_quote
                - self._safe_float(state.total_bought_quote, 0.0)
                - allocated_entry_fees
                - self._safe_float(state.paid_buy_fees_quote, 0.0)
            )
            self._refresh_net_open_pnl(state)
            return

        sold_ratio = min(1.0, sold_amount / bought_amount) if bought_amount > 0 else 0.0
        total_entry_quote = max(0.0, self._safe_float(state.total_bought_quote, 0.0))
        total_buy_fees = max(0.0, self._safe_float(state.paid_buy_fees_quote, 0.0))
        allocated_entry_quote = total_entry_quote * sold_ratio
        allocated_buy_fees = total_buy_fees * sold_ratio

        state.remaining_entry_quote = max(0.0, total_entry_quote - allocated_entry_quote)
        state.remaining_buy_fees_quote = max(0.0, total_buy_fees - allocated_buy_fees)
        state.realized_pnl = (
            self._safe_float(state.total_sold_quote, 0.0)
            - allocated_entry_quote
            - allocated_buy_fees
            - self._safe_float(state.paid_sell_fees_quote, 0.0)
        )
        self._refresh_net_open_pnl(state)

    def _ensure_cost_basis_initialized(self, state: TradeState):
        entry_quote = state.total_sold_quote if config.POSITION_SIDE == "short" else state.total_bought_quote
        entry_fees = state.paid_sell_fees_quote if config.POSITION_SIDE == "short" else state.paid_buy_fees_quote
        exit_amount = state.total_bought_amount if config.POSITION_SIDE == "short" else state.total_sold_amount

        if entry_quote <= 0:
            self._refresh_net_open_pnl(state)
            return
        if state.remaining_entry_quote > 0 or state.remaining_buy_fees_quote > 0:
            self._refresh_net_open_pnl(state)
            return
        if exit_amount <= 0:
            state.remaining_entry_quote = max(0.0, entry_quote)
            state.remaining_buy_fees_quote = max(0.0, entry_fees)
            self._refresh_net_open_pnl(state)
            return
        self._recalculate_proportional_pnl_from_totals(state)

    def _side_fee_rate(self, side: str) -> float:
        return config.SELLING.buy_fee_rate if side == "buy" else config.SELLING.sell_fee_rate

    def _record_entry_fill(
        self,
        symbol: str,
        state: TradeState,
        side: str,
        contracts: float,
        entry_price: float,
        reason: str,
        fill_details: Optional[List[dict]] = None,
    ):
        if contracts <= 0:
            return
        self._ensure_cost_basis_initialized(state)
        details = self._fill_details_with_fallback(symbol, side, contracts, entry_price, fill_details)
        notional = sum(self._safe_float(item.get("quote"), 0.0) for item in details)
        fee = 0.0
        for item in details:
            item_fee = item.get("fee_quote")
            if item_fee is None:
                item_fee = self._safe_float(item.get("quote"), 0.0) * self._side_fee_rate(side)
                item["fee_quote"] = item_fee
                item["source"] = f"{item.get('source', 'exchange')}:config_fee"
            fee += self._safe_float(item_fee, 0.0)

        avg_entry = self._average_price_from_notional(symbol, contracts, notional) or entry_price
        if state.initial_entry_notional <= 0:
            leverage = max(self._safe_float(state.leverage, 0.0), self._safe_float(config.RISK.leverage, 1.0), 1.0)
            planned_notional = self._safe_float(state.planned_quote_budget, 0.0) * leverage
            state.initial_entry_notional = max(planned_notional, notional)
        if side == "buy":
            state.total_bought_amount += contracts
            state.total_bought_quote += notional
            state.paid_buy_fees_quote += fee
        else:
            state.total_sold_amount += contracts
            state.total_sold_quote += notional
            state.paid_sell_fees_quote += fee
        state.remaining_entry_quote += notional
        state.remaining_buy_fees_quote += fee
        state.last_buy_amount = contracts
        state.last_buy_price = avg_entry
        state.buy_stage = min(config.STRATEGY.max_buy_stages, max(state.buy_stage + 1, 1))
        if state.position_size <= 0 or state.cycle_opened_at is None:
            state.cycle_opened_at = time.time()
        self._refresh_net_open_pnl(state)

        for detail in details:
            detail_contracts = self._safe_float(detail.get("contracts"), 0.0)
            detail_quote = self._safe_float(detail.get("quote"), 0.0)
            detail_price = self._safe_float(detail.get("price"), 0.0) or self._average_price_from_notional(symbol, detail_contracts, detail_quote)
            detail_fee = self._safe_float(detail.get("fee_quote"), 0.0)
            source = str(detail.get("source") or "exchange")
            self._log_event(
                "INFO",
                f"{side.title()} entry fill synced for {symbol}: contracts={detail_contracts} avg={detail_price}",
                event=f"{side}_order_filled",
                symbol=symbol,
                side=side,
                order_id=str(detail.get("order_id") or ""),
                amount=detail_contracts,
                filled=detail_contracts,
                price=detail_price,
                notional=detail_quote,
                fee_quote=detail_fee,
                fee_currency=str(detail.get("fee_currency") or config.EXCHANGE.quote_currency),
                fill_source=source,
                position_size=state.position_size + contracts,
                reason=f"{reason};entry_fill=1;aggregate_avg={avg_entry:.12f};fee_source={source}",
            )

    def _record_exit_fill(
        self,
        symbol: str,
        state: TradeState,
        side: str,
        contracts: float,
        reason: str,
        fill_details: Optional[List[dict]] = None,
        fallback_price: float = 0.0,
    ):
        if contracts <= 0:
            return
        self._ensure_cost_basis_initialized(state)
        fallback_quote = self._estimate_sell_quote_from_refs(symbol, state, contracts)
        if fallback_quote <= 0 and fallback_price > 0:
            fallback_quote = self._contracts_to_notional(symbol, contracts, fallback_price)
        details = self._fill_details_with_fallback(
            symbol,
            side,
            contracts,
            fallback_price or self._average_price_from_notional(symbol, contracts, fallback_quote),
            fill_details,
            fallback_quote=fallback_quote,
        )
        exit_quote = sum(self._safe_float(item.get("quote"), 0.0) for item in details)
        avg_exit = self._average_price_from_notional(symbol, contracts, exit_quote)
        fee = 0.0
        for item in details:
            item_fee = item.get("fee_quote")
            if item_fee is None:
                item_fee = self._safe_float(item.get("quote"), 0.0) * self._side_fee_rate(side)
                item["fee_quote"] = item_fee
                item["source"] = f"{item.get('source', 'exchange')}:config_fee"
            fee += self._safe_float(item_fee, 0.0)

        if config.POSITION_SIDE == "short":
            entry_amount = self._safe_float(state.total_sold_amount, 0.0)
            exit_amount = self._safe_float(state.total_bought_amount, 0.0)
        else:
            entry_amount = self._safe_float(state.total_bought_amount, 0.0)
            exit_amount = self._safe_float(state.total_sold_amount, 0.0)
        open_contracts = max(
            self._safe_float(state.position_size, 0.0),
            entry_amount - exit_amount,
            contracts,
        )
        close_ratio = min(1.0, contracts / open_contracts) if open_contracts > 0 else 1.0
        allocated_entry_quote = state.remaining_entry_quote * close_ratio
        allocated_entry_fees = state.remaining_buy_fees_quote * close_ratio

        if side == "buy":
            state.total_bought_amount += contracts
            state.total_bought_quote += exit_quote
            state.paid_buy_fees_quote += fee
        else:
            state.total_sold_amount += contracts
            state.total_sold_quote += exit_quote
            state.paid_sell_fees_quote += fee
        state.remaining_entry_quote = max(0.0, state.remaining_entry_quote - allocated_entry_quote)
        state.remaining_buy_fees_quote = max(0.0, state.remaining_buy_fees_quote - allocated_entry_fees)
        if config.POSITION_SIDE == "short":
            state.realized_pnl += allocated_entry_quote - exit_quote - allocated_entry_fees - fee
        else:
            state.realized_pnl += exit_quote - allocated_entry_quote - allocated_entry_fees - fee
        self._refresh_net_open_pnl(state)

        for detail in details:
            detail_contracts = self._safe_float(detail.get("contracts"), 0.0)
            detail_quote = self._safe_float(detail.get("quote"), 0.0)
            detail_price = self._safe_float(detail.get("price"), 0.0) or self._average_price_from_notional(symbol, detail_contracts, detail_quote)
            detail_fee = self._safe_float(detail.get("fee_quote"), 0.0)
            source = str(detail.get("source") or "exchange")
            self._log_event(
                "INFO",
                f"{side.title()} exit fill synced for {symbol}: contracts={detail_contracts} avg={detail_price}",
                event=f"{side}_order_filled",
                symbol=symbol,
                side=side,
                order_id=str(detail.get("order_id") or ""),
                amount=detail_contracts,
                filled=detail_contracts,
                price=detail_price,
                notional=detail_quote,
                fee_quote=detail_fee,
                fee_currency=str(detail.get("fee_currency") or config.EXCHANGE.quote_currency),
                fill_source=source,
                position_size=max(0.0, state.position_size - contracts),
                reason=f"{reason};exit_fill=1;aggregate_avg={avg_exit:.12f};fee_source={source}",
            )

    def _record_buy_fill(
        self,
        symbol: str,
        state: TradeState,
        contracts: float,
        entry_price: float,
        reason: str,
        fill_details: Optional[List[dict]] = None,
    ):
        if config.ENTRY_SIDE == "buy":
            self._record_entry_fill(symbol, state, "buy", contracts, entry_price, reason, fill_details)
        else:
            self._record_exit_fill(symbol, state, "buy", contracts, reason, fill_details, fallback_price=entry_price)

    def _record_sell_fill(
        self,
        symbol: str,
        state: TradeState,
        contracts: float,
        reason: str,
        fill_details: Optional[List[dict]] = None,
        entry_price: float = 0.0,
    ):
        if config.ENTRY_SIDE == "sell":
            self._record_entry_fill(symbol, state, "sell", contracts, entry_price or state.entry_price, reason, fill_details)
        else:
            self._record_exit_fill(symbol, state, "sell", contracts, reason, fill_details, fallback_price=entry_price)

    def _sync_state_with_position(self, symbol: str, snapshot: dict, open_orders: Optional[List[dict]] = None) -> str:
        state = self._get_state(symbol)
        old_size = state.position_size
        position_side = config.POSITION_SIDE
        opposite_side = config.OPPOSITE_POSITION_SIDE
        entry_side = config.ENTRY_SIDE
        exit_side = config.EXIT_SIDE
        new_size = self._safe_float(snapshot.get(f"{position_side}_size"), 0.0)
        new_available = self._safe_float(snapshot.get(f"{position_side}_available"), new_size)
        new_frozen = self._safe_float(snapshot.get(f"{position_side}_frozen"), max(0.0, new_size - new_available))
        opposite_size = self._safe_float(snapshot.get(f"{opposite_side}_size"), 0.0)
        new_entry = self._safe_float(snapshot.get(f"{position_side}_entry_price"), 0.0)
        if new_entry <= 0:
            new_entry = self._safe_float(snapshot.get("entry_price"), 0.0)
        new_leverage = self._safe_float(snapshot.get("leverage"), 0.0)
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)

        def record_side_fill(side: str, contracts: float, price: float, reason: str, fill_details: Optional[List[dict]]):
            if side == "buy":
                self._record_buy_fill(symbol, state, contracts, price, reason=reason, fill_details=fill_details)
            else:
                self._record_sell_fill(symbol, state, contracts, reason=reason, fill_details=fill_details, entry_price=price)

        if opposite_size > eps:
            external_reserved_symbols = getattr(self, "external_reserved_symbols", set())
            if symbol in external_reserved_symbols and old_size <= eps:
                if state.entry_orders:
                    self._cancel_entry_orders(symbol, reason="reserved_by_other_profile")
                if state.sell_ladder_orders:
                    self._cancel_sell_orders(symbol, reason="reserved_by_other_profile")
                if symbol in self.disabled_symbols:
                    self.disabled_symbols.discard(symbol)
                self._log_event(
                    "DEBUG",
                    f"Skipping {symbol}: {opposite_side} position is managed by another profile",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    side=opposite_side,
                    amount=opposite_size,
                    reason="reserved_by_other_profile",
                )
                return "reserved"

            if symbol not in self.disabled_symbols:
                self._log_event(
                    "ERROR",
                    f"Unexpected {opposite_side} position detected for {symbol}; trading disabled for symbol",
                    event=f"unexpected_{opposite_side}_position",
                    symbol=symbol,
                    side=opposite_side,
                    amount=opposite_size,
                    reason=f"{opposite_side}_position_detected",
                )
            self._cancel_entry_orders(symbol, reason=f"unexpected_{opposite_side}_position")
            self._cancel_sell_orders(symbol, reason=f"unexpected_{opposite_side}_position")
            self.disabled_symbols.add(symbol)
            return "disabled"

        if symbol in self.disabled_symbols:
            self.disabled_symbols.discard(symbol)
            self._log_event(
                "INFO",
                f"{opposite_side.title()} position cleared for {symbol}; trading re-enabled",
                event="position_reenabled",
                symbol=symbol,
                side=opposite_side,
                reason=f"{opposite_side}_position_cleared",
            )

        state.position_available = max(0.0, min(new_size, new_available))
        state.position_frozen = max(0.0, new_frozen)
        if new_leverage > 0:
            state.leverage = int(new_leverage) if new_leverage.is_integer() else new_leverage

        if new_size > eps and old_size <= eps:
            if not state.entry_orders:
                self._log_event(
                    "WARNING",
                    f"Exchange has a {position_side} position not present in state for {symbol}; adopting it",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    amount=new_size,
                    price=new_entry,
                    reason=f"state_empty_exchange_{position_side}",
                )
            fill_details = self._collect_order_fill_details(symbol, state, entry_side, new_size, open_orders)
            record_side_fill(entry_side, new_size, new_entry, reason="position_appeared", fill_details=fill_details)
            state.position_size = new_size
            state.position_side = position_side
            state.entry_price = new_entry
            if state.initial_entry_notional <= 0:
                state.initial_entry_notional = self._contracts_to_notional(symbol, new_size, new_entry)
            state.unrealized_pnl = self._safe_float(snapshot.get("unrealized_pnl"), 0.0)
            self._refresh_net_open_pnl(state)
            self._cancel_sell_orders(symbol, reason="rebuild_after_entry_fill")
            self._save_state()
            return "position_changed"

        if new_size > old_size + eps:
            added = new_size - old_size
            old_notional = self._contracts_to_notional(symbol, old_size, state.entry_price)
            new_notional = self._contracts_to_notional(symbol, new_size, new_entry)
            added_notional = max(0.0, new_notional - old_notional)
            added_entry = self._average_price_from_notional(symbol, added, added_notional)
            if added_entry <= 0:
                added_entry = new_entry or state.entry_price
            fill_details = self._collect_order_fill_details(symbol, state, entry_side, added, open_orders)
            record_side_fill(entry_side, added, added_entry, reason="position_increased", fill_details=fill_details)
            state.position_size = new_size
            state.position_side = position_side
            state.entry_price = new_entry or state.entry_price
            state.exit_runner_active = False
            state.exit_runner_activated_at = None
            state.exit_runner_peak_price = 0.0
            state.exit_runner_bottom_price = 0.0
            state.exit_runner_contracts = 0.0
            state.unrealized_pnl = self._safe_float(snapshot.get("unrealized_pnl"), 0.0)
            self._refresh_net_open_pnl(state)
            self._cancel_sell_orders(symbol, reason="rebuild_after_add")
            self._save_state()
            return "position_changed"

        if old_size > new_size + eps:
            closed = old_size - new_size
            fill_details = self._collect_order_fill_details(symbol, state, exit_side, closed, open_orders)
            record_side_fill(exit_side, closed, 0.0, reason="position_decreased", fill_details=fill_details)
            if new_size <= eps:
                state.position_size = 0.0
                state.position_side = ""
                state.entry_price = 0.0
                self._close_cycle(symbol, reason="exit_ladder_filled")
                return "closed"

            state.position_size = new_size
            state.position_side = position_side
            state.entry_price = new_entry or state.entry_price
            if state.exit_runner_contracts > new_size:
                state.exit_runner_contracts = max(0.0, new_size)
            state.unrealized_pnl = self._safe_float(snapshot.get("unrealized_pnl"), 0.0)
            self._refresh_net_open_pnl(state)
            self._cancel_sell_orders(symbol, reason="rebuild_after_partial_exit")
            self._save_state()
            return "position_changed"

        if new_size > eps:
            state.position_size = new_size
            state.position_side = position_side
            if new_entry:
                state.entry_price = new_entry
            state.unrealized_pnl = self._safe_float(snapshot.get("unrealized_pnl"), 0.0)
            self._refresh_net_open_pnl(state)
            self._log_event(
                "DEBUG",
                f"Position synced for {symbol}",
                event="position_synced",
                symbol=symbol,
                position_size=state.position_size,
                entry_price=state.entry_price,
                reason="no_size_change",
            )
            self._save_state()
            return "position_synced"

        if old_size > eps and new_size <= eps:
            fill_details = self._collect_order_fill_details(symbol, state, exit_side, old_size, open_orders)
            record_side_fill(exit_side, old_size, 0.0, reason="position_gone", fill_details=fill_details)
            state.position_size = 0.0
            state.position_available = 0.0
            state.position_frozen = 0.0
            state.position_side = ""
            state.entry_price = 0.0
            self._close_cycle(symbol, reason="position_closed")
            return "closed"

        state.position_available = 0.0
        state.position_frozen = 0.0
        return "flat"

    def _close_cycle(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        self._cancel_sell_orders(symbol, reason="cycle_closed")
        self._cancel_entry_orders(symbol, reason="cycle_closed")

        if config.POSITION_SIDE == "short":
            entry_notional = state.total_sold_quote
            exit_notional = state.total_bought_quote
            entry_amount = state.total_sold_amount
            exit_amount = state.total_bought_amount
            realized = entry_notional - exit_notional - state.paid_buy_fees_quote - state.paid_sell_fees_quote
        else:
            entry_notional = state.total_bought_quote
            exit_notional = state.total_sold_quote
            entry_amount = state.total_bought_amount
            exit_amount = state.total_sold_amount
            realized = exit_notional - entry_notional - state.paid_buy_fees_quote - state.paid_sell_fees_quote
        state.realized_pnl = realized
        state.remaining_entry_quote = 0.0
        state.remaining_buy_fees_quote = 0.0
        self._refresh_net_open_pnl(state)
        avg_entry = self._average_price_from_notional(symbol, entry_amount, entry_notional)
        avg_exit = self._average_price_from_notional(symbol, exit_amount, exit_notional)
        opened_at = state.cycle_opened_at or time.time()
        closed_at = time.time()

        cycle_leverage = self._safe_float(state.leverage, 0.0) or self._safe_float(config.RISK.leverage, 1.0)
        cycle_leverage = max(cycle_leverage, 1.0)

        if entry_notional > 0:
            self._append_cycle_stats_row(
                {
                    "symbol": symbol,
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "leverage": cycle_leverage,
                    "margin_mode": config.RISK.margin_mode,
                    "planned_budget": state.planned_quote_budget,
                    "total_entry_notional": entry_notional,
                    "total_exit_notional": exit_notional,
                    "average_entry_price": avg_entry,
                    "average_exit_price": avg_exit,
                    "buy_fees": state.paid_buy_fees_quote,
                    "sell_fees": state.paid_sell_fees_quote,
                    "realized_pnl_quote": realized,
                    "realized_pnl_percent_on_notional": (realized / entry_notional) * 100 if entry_notional else 0,
                    "realized_pnl_percent_on_margin": (realized / (entry_notional / cycle_leverage)) * 100 if entry_notional else 0,
                    "holding_minutes": (closed_at - opened_at) / 60.0,
                    "max_buy_stage": state.buy_stage,
                    "frozen_no_more_buys": state.frozen_no_more_buys,
                    "close_reason": reason,
                    "entry_rs30": state.entry_rs30,
                    "entry_rs60": state.entry_rs60,
                    "entry_ema30": state.entry_ema30,
                    "entry_ema60": state.entry_ema60,
                    "strategy_name": state.strategy_name or "ema_pullback",
                    "entry_ema25d": state.entry_ema25d,
                    "entry_ema50d": state.entry_ema50d,
                    "entry_ema1d": state.entry_ema1d,
                    "entry_ema2d": state.entry_ema2d,
                    "entry_ema50": state.entry_ema50,
                    "entry_ema100": state.entry_ema100,
                    "entry_btc_return_30m": state.entry_btc_return_30m,
                    "max_averaging_stage": state.average_stage,
                    "breakeven_activated": bool(state.breakeven_activated_at),
                }
            )

        self._log_event(
            "INFO",
            f"Cycle closed for {symbol}: pnl={realized:.8f}",
            event="cycle_closed",
            symbol=symbol,
            reason=reason,
        )
        cooldown_until = time.time() + config.RISK.cooldown_minutes_after_close * 60
        self._reset_state(symbol, preserve_cooldown=cooldown_until)
