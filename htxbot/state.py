# -*- coding: utf-8 -*-

import atexit
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import asdict, fields
from typing import Dict, List, Optional, Tuple

import config

from .concurrency import instance_rlock
from .fileio import is_transient_file_replace_error, replace_path_with_retry, write_text_path_with_retry
from .models import PositionLifecycle, TradeState


_state_io_lock = threading.RLock()


class StateMixin:
    _STATE_FLOAT_FIELDS = {
        "position_size",
        "position_available",
        "position_frozen",
        "entry_price",
        "last_buy_price",
        "last_buy_amount",
        "planned_quote_budget",
        "initial_entry_notional",
        "paid_buy_fees_quote",
        "paid_sell_fees_quote",
        "total_bought_amount",
        "total_bought_quote",
        "total_sold_amount",
        "total_sold_quote",
        "realized_pnl",
        "unrealized_pnl",
        "remaining_entry_quote",
        "remaining_buy_fees_quote",
        "net_open_pnl",
        "base_entry_amount",
        "base_entry_quote",
        "base_entry_fees_quote",
        "base_entry_price",
        "averaging_entry_amount",
        "averaging_entry_quote",
        "averaging_entry_fees_quote",
        "leverage",
        "last_rs30",
        "last_rs60",
        "last_ema30",
        "last_ema60",
        "last_ema25d",
        "last_ema50d",
        "last_ema1d",
        "last_ema2d",
        "last_ema50",
        "last_ema100",
        "last_btc_return_30m",
        "exit_runner_peak_price",
        "exit_runner_bottom_price",
        "exit_runner_contracts",
        "entry_rs30",
        "entry_rs60",
        "entry_ema30",
        "entry_ema60",
        "entry_ema25d",
        "entry_ema50d",
        "entry_ema1d",
        "entry_ema2d",
        "entry_ema50",
        "entry_ema100",
        "entry_btc_return_30m",
    }
    _STATE_OPTIONAL_FLOAT_FIELDS = {
        "pending_exit_ladder_since",
        "cycle_opened_at",
        "cooldown_until",
        "time_exit_activated_at",
        "zombie_marked_at",
        "last_signal_timestamp",
        "last_entry_ladder_signal_timestamp",
        "last_average_signal_timestamp",
        "last_average_at",
        "last_ema_strategy_signal_timestamp",
        "breakeven_activated_at",
        "exit_runner_activated_at",
        "last_account_unload_at",
    }
    _STATE_INT_FIELDS = {
        "buy_stage",
        "average_stage",
        "account_unload_count",
    }
    _STATE_BOOL_FIELDS = {
        "frozen_no_more_buys",
        "zombie_position",
        "exit_runner_active",
    }

    def _is_transient_replace_error(self, exc: OSError) -> bool:
        return is_transient_file_replace_error(exc)

    def _coerce_optional_state_float(self, value):
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _coerce_state_bool(self, value) -> bool:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off", ""}:
                return False
        return bool(value)

    def _coerce_trade_state(self, state: TradeState) -> TradeState:
        for name in self._STATE_FLOAT_FIELDS:
            setattr(state, name, self._safe_float(getattr(state, name, 0.0), 0.0))
        for name in self._STATE_OPTIONAL_FLOAT_FIELDS:
            setattr(state, name, self._coerce_optional_state_float(getattr(state, name, None)))
        for name in self._STATE_INT_FIELDS:
            setattr(state, name, int(self._safe_float(getattr(state, name, 0), 0.0)))
        for name in self._STATE_BOOL_FIELDS:
            setattr(state, name, self._coerce_state_bool(getattr(state, name, False)))
        return state

    def _replace_state_file_with_retry(self, tmp_path, target_path):
        replace_path_with_retry(
            tmp_path,
            target_path,
            attempts=30,
            initial_delay_sec=0.1,
            max_delay_sec=0.5,
            replace_func=os.replace,
        )

    def _write_state_file_with_retry(self, tmp_path, payload: str):
        write_text_path_with_retry(
            tmp_path,
            payload,
            encoding="utf-8",
            attempts=30,
            initial_delay_sec=0.1,
            max_delay_sec=0.5,
        )

    def _load_state(self) -> Dict[str, TradeState]:
        if not self.state_path.exists():
            return {}

        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger = getattr(self, "log", logging.getLogger(__name__))
            logger.warning("Could not read futures state; starting with empty state: %s", exc)
            record_diagnostic = getattr(self, "_record_diagnostic", None)
            if record_diagnostic:
                record_diagnostic(
                    "fault",
                    "state",
                    "state_load_failed",
                    f"Could not read futures state; starting with empty state: {exc}",
                    reason="state_load_failed",
                    exception=exc,
                )
            return {}

        known_fields = {item.name for item in fields(TradeState)}
        result: Dict[str, TradeState] = {}
        if not isinstance(raw, dict):
            return result

        for symbol, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            payload = dict(payload)
            legacy_aliases = {
                "total_bought_base": "total_bought_amount",
                "total_sold_base": "total_sold_amount",
                "total_buy_fees_quote": "paid_buy_fees_quote",
                "total_sell_fees_quote": "paid_sell_fees_quote",
                "buy_fees_quote": "paid_buy_fees_quote",
                "sell_fees_quote": "paid_sell_fees_quote",
            }
            for old_key, new_key in legacy_aliases.items():
                if new_key not in payload and old_key in payload:
                    payload[new_key] = payload[old_key]
            has_remaining_cost_basis = (
                "remaining_entry_quote" in payload
                and "remaining_buy_fees_quote" in payload
            )
            safe_payload = {key: value for key, value in payload.items() if key in known_fields}
            state = TradeState(**safe_payload)
            self._coerce_trade_state(state)
            state.symbol = state.symbol or symbol
            state.market_symbol = state.market_symbol or state.symbol
            if self._safe_float(getattr(state, "leverage", 0.0), 0.0) <= 0:
                state.leverage = config.RISK.leverage
            if not str(getattr(state, "margin_mode", "") or "").strip():
                state.margin_mode = config.RISK.margin_mode
            state.entry_orders = self._normalize_order_refs(state.entry_orders)
            state.sell_ladder_orders = self._normalize_order_refs(state.sell_ladder_orders)
            state.hard_stop_order = self._normalize_order_ref(state.hard_stop_order)
            if has_remaining_cost_basis:
                self._refresh_net_open_pnl(state)
            else:
                self._recalculate_proportional_pnl_from_totals(state)
            self._ensure_cost_basis_initialized(state)
            self._ensure_entry_buckets_initialized(symbol, state)
            self._refresh_active_side(state)
            result[symbol] = state
        return result

    def _save_state(self):
        with instance_rlock(self, "_state_lock"):
            with _state_io_lock:
                payload = {symbol: asdict(state) for symbol, state in list(self.states.items())}
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
                try:
                    state_json = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                    self._write_state_file_with_retry(tmp_path, state_json)
                    self._replace_state_file_with_retry(tmp_path, self.state_path)
                except Exception:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise

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
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    raw_pid = self.lock_path.read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    continue
                existing_pid = int(raw_pid) if raw_pid.isdigit() else 0
                if existing_pid and self._pid_is_running(existing_pid):
                    raise RuntimeError(
                        f"Another bot instance is already running with PID {existing_pid}. "
                        f"Remove {self.lock_path} only after that process exits."
                    )
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RuntimeError(f"Could not remove stale runtime lock {self.lock_path}: {exc}") from exc
                continue

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(str(os.getpid()))
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    self.lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            atexit.register(self._release_runtime_lock)
            return

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
        with instance_rlock(self, "_state_lock"):
            if symbol not in self.states:
                self.states[symbol] = TradeState(symbol=symbol, market_symbol=symbol)
            state = self.states[symbol]
            state.symbol = state.symbol or symbol
            state.market_symbol = state.market_symbol or symbol
            if state.position_size <= 0 or self._safe_float(state.leverage, 0.0) <= 0:
                state.leverage = config.RISK.leverage
            state.margin_mode = config.RISK.margin_mode
            self._ensure_entry_buckets_initialized(symbol, state)
            self._refresh_active_side(state)
            return state

    def _reset_state(self, symbol: str, preserve_cooldown: Optional[float] = None):
        with instance_rlock(self, "_state_lock"):
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

    def _clear_pending_exit_ladder(self, state: TradeState):
        state.pending_exit_ladder_since = None
        state.pending_exit_ladder_reason = ""

    def _normalize_order_refs(self, refs: list) -> list:
        normalized = []
        if isinstance(refs, dict):
            items = [refs]
        elif isinstance(refs, (list, tuple, set)):
            items = list(refs)
        elif refs:
            items = [refs]
        else:
            items = []
        for item in items:
            if isinstance(item, dict):
                ref = dict(item)
                order_id = str(ref.get("id") or "")
                if not order_id:
                    continue
                ref["id"] = order_id
                for key in ("price", "trigger_price", "amount", "filled", "remaining", "created_at", "signal_ts", "loss_rate"):
                    if key in ref and ref.get(key) is not None:
                        ref[key] = self._safe_float(ref.get(key), 0.0)
                if "stage" in ref and ref.get("stage") is not None:
                    ref["stage"] = int(self._safe_float(ref.get("stage"), 0.0))
                normalized.append(ref)
            elif item:
                normalized.append({"id": str(item), "created_at": 0.0})
        return normalized

    def _normalize_order_ref(self, ref) -> dict:
        if isinstance(ref, dict):
            normalized = dict(ref)
            refs = self._normalize_order_refs([normalized])
            return refs[0] if refs else {}
        if isinstance(ref, list):
            refs = self._normalize_order_refs(ref)
            return refs[0] if refs else {}
        if ref:
            return {"id": str(ref), "created_at": 0.0}
        return {}

    def _order_ids(self, refs: list) -> set:
        return {str(item.get("id")) for item in refs or [] if item.get("id") is not None}

    def _order_remaining_amount(self, order: dict) -> float:
        if not isinstance(order, dict):
            return 0.0
        if "remaining" in order and order.get("remaining") is not None:
            return max(0.0, self._safe_float(order.get("remaining"), 0.0))
        return max(0.0, self._safe_float(order.get("amount"), 0.0))

    def _order_reduce_only_flag(self, order: dict) -> Optional[bool]:
        if not isinstance(order, dict):
            return None

        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        for source in (order, info):
            offset = str(source.get("offset") or "").strip().lower()
            if offset == "close":
                return True
            if offset == "open":
                return False
            trade_type = str(source.get("trade_type") or source.get("tradeType") or "").strip()
            if trade_type == "3" and config.POSITION_SIDE == "short":
                return True
            if trade_type == "4" and config.POSITION_SIDE == "long":
                return True
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

    def _log_reserved_by_other_profile(self, symbol: str, side: str = "", amount: float = 0.0):
        now = time.time()
        key = (symbol, side)
        logged = getattr(self, "_reserved_symbol_logged_at", {})
        last = self._safe_float(logged.get(key), 0.0) if isinstance(logged, dict) else 0.0
        if now - last < 300.0:
            return
        if not isinstance(logged, dict):
            logged = {}
        logged[key] = now
        self._reserved_symbol_logged_at = logged
        self._log_event(
            "DEBUG",
            f"Skipping {symbol}: position or orders are managed by another profile",
            event="profile_reserved",
            symbol=symbol,
            side=side,
            amount=amount,
            reason="reserved_by_other_profile",
        )

    def _refresh_active_side(self, state: TradeState):
        sides = {
            str(ref.get("side") or config.ENTRY_SIDE).lower()
            for ref in state.entry_orders or []
        }
        sides.update(
            str(ref.get("side") or config.EXIT_SIDE).lower()
            for ref in state.sell_ladder_orders or []
        )
        if state.hard_stop_order:
            sides.add(str(state.hard_stop_order.get("side") or config.EXIT_SIDE).lower())
        sides.discard("")
        if len(sides) > 1:
            state.active_side = "both"
        elif sides:
            state.active_side = next(iter(sides))
        else:
            state.active_side = None
        state.lifecycle = self._derive_lifecycle(state)

    def _derive_lifecycle(self, state: TradeState) -> str:
        pending_closeable = bool(
            getattr(state, "pending_exit_ladder_since", None)
            or str(getattr(state, "sell_ladder_signature", "") or "").startswith("pending_closeable:")
        )
        mode = str(getattr(state, "sell_ladder_mode", "") or "normal")

        if pending_closeable:
            return PositionLifecycle.PENDING_CLOSEABLE.value
        if mode == "absolute_force_exit":
            return PositionLifecycle.FORCE_EXIT.value
        if getattr(state, "zombie_position", False):
            return PositionLifecycle.ZOMBIE.value

        if self._safe_float(getattr(state, "position_size", 0.0), 0.0) <= 0:
            if getattr(state, "entry_orders", None):
                return PositionLifecycle.ENTERING.value
            if getattr(state, "sell_ladder_orders", None) or getattr(state, "hard_stop_order", None):
                return PositionLifecycle.EXITING.value
            return PositionLifecycle.FLAT.value

        if mode == "breakeven" or getattr(state, "breakeven_activated_at", None):
            return PositionLifecycle.BREAKEVEN.value
        if mode in {"account_unload", "controlled_loss_exit", "urgent_time_exit"}:
            return PositionLifecycle.EXITING.value
        return PositionLifecycle.OPEN.value

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
                trades = self._expect_ccxt_list_response(
                    trades,
                    "fetch_my_trades",
                    symbol=symbol,
                    item_types=(dict,),
                )
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
            "operation_id": ref.get("operation_id", ""),
            "cycle_id": ref.get("cycle_id", ""),
            "ref_reason": ref.get("reason", ""),
            "exit_scope": ref.get("exit_scope", ""),
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
        state.net_open_pnl = (
            self._safe_float(state.realized_pnl, 0.0)
            + self._safe_float(state.unrealized_pnl, 0.0)
            - state.remaining_buy_fees_quote
        )

    def _entry_bucket_total_amount(self, state: TradeState) -> float:
        return (
            self._safe_float(getattr(state, "base_entry_amount", 0.0), 0.0)
            + self._safe_float(getattr(state, "averaging_entry_amount", 0.0), 0.0)
        )

    def _refresh_entry_bucket_prices(self, symbol: str, state: TradeState):
        if state.base_entry_amount > 0 and state.base_entry_quote > 0:
            state.base_entry_price = self._average_price_from_notional(symbol, state.base_entry_amount, state.base_entry_quote)
        elif state.base_entry_amount <= 0:
            state.base_entry_amount = 0.0
            state.base_entry_quote = 0.0
            state.base_entry_fees_quote = 0.0
            state.base_entry_price = 0.0

        if state.averaging_entry_amount <= 0:
            state.averaging_entry_amount = 0.0
            state.averaging_entry_quote = 0.0
            state.averaging_entry_fees_quote = 0.0

    def _ensure_entry_buckets_initialized(self, symbol: str, state: TradeState):
        if state.position_size <= 0:
            return
        if self._entry_bucket_total_amount(state) > 0:
            self._refresh_entry_bucket_prices(symbol, state)
            return

        entry_quote = self._safe_float(state.remaining_entry_quote, 0.0)
        if entry_quote <= 0 and state.entry_price > 0:
            entry_quote = self._contracts_to_notional(symbol, state.position_size, state.entry_price)
        if entry_quote <= 0:
            return

        state.remaining_entry_quote = max(self._safe_float(state.remaining_entry_quote, 0.0), entry_quote)
        state.remaining_buy_fees_quote = max(0.0, self._safe_float(state.remaining_buy_fees_quote, 0.0))
        state.base_entry_amount = max(0.0, self._safe_float(state.position_size, 0.0))
        state.base_entry_quote = entry_quote
        state.base_entry_fees_quote = max(0.0, self._safe_float(state.remaining_buy_fees_quote, 0.0))
        state.base_entry_price = self._average_price_from_notional(symbol, state.base_entry_amount, entry_quote) or state.entry_price
        state.averaging_entry_amount = 0.0
        state.averaging_entry_quote = 0.0
        state.averaging_entry_fees_quote = 0.0

    def _order_ref_by_id(self, refs: list, order_id: str) -> Optional[dict]:
        if not order_id:
            return None
        for ref in refs or []:
            if str(ref.get("id") or "") == order_id:
                return ref
        return None

    def _entry_fill_is_averaging(self, state: TradeState, detail: dict, reason: str) -> bool:
        text = ";".join(
            str(item or "")
            for item in (
                reason,
                detail.get("ref_reason"),
                detail.get("reason"),
            )
        )
        if "ema_averaging_stage_" in text:
            return True

        order_id = str(detail.get("order_id") or "")
        ref = self._order_ref_by_id(state.entry_orders, order_id)
        if ref and "ema_averaging_stage_" in str(ref.get("reason") or ""):
            return True

        if not order_id and "position_increased" in str(reason or ""):
            return any("ema_averaging_stage_" in str(ref.get("reason") or "") for ref in state.entry_orders or [])
        return False

    def _record_entry_bucket_fills(self, symbol: str, state: TradeState, details: List[dict], reason: str):
        self._ensure_entry_buckets_initialized(symbol, state)
        for detail in details:
            contracts = self._safe_float(detail.get("contracts"), 0.0)
            quote = self._safe_float(detail.get("quote"), 0.0)
            fee = self._safe_float(detail.get("fee_quote"), 0.0)
            if contracts <= 0 or quote <= 0:
                continue
            if self._entry_fill_is_averaging(state, detail, reason):
                state.averaging_entry_amount += contracts
                state.averaging_entry_quote += quote
                state.averaging_entry_fees_quote += fee
            else:
                state.base_entry_amount += contracts
                state.base_entry_quote += quote
                state.base_entry_fees_quote += fee
        self._refresh_entry_bucket_prices(symbol, state)

    def _exit_scope_for_detail(self, state: TradeState, detail: dict) -> str:
        scope = str(detail.get("exit_scope") or "")
        if scope:
            return scope
        ref = self._order_ref_by_id(state.sell_ladder_orders, str(detail.get("order_id") or ""))
        return str((ref or {}).get("exit_scope") or "")

    def _take_entry_bucket(self, state: TradeState, bucket: str, contracts: float) -> Tuple[float, float, float]:
        if contracts <= 0:
            return 0.0, 0.0, 0.0
        if bucket == "average":
            amount_attr = "averaging_entry_amount"
            quote_attr = "averaging_entry_quote"
            fee_attr = "averaging_entry_fees_quote"
        else:
            amount_attr = "base_entry_amount"
            quote_attr = "base_entry_quote"
            fee_attr = "base_entry_fees_quote"

        available = self._safe_float(getattr(state, amount_attr, 0.0), 0.0)
        if available <= 0:
            return 0.0, 0.0, 0.0
        used = min(max(0.0, contracts), available)
        ratio = used / available if available > 0 else 0.0
        quote = self._safe_float(getattr(state, quote_attr, 0.0), 0.0) * ratio
        fees = self._safe_float(getattr(state, fee_attr, 0.0), 0.0) * ratio
        setattr(state, amount_attr, max(0.0, available - used))
        setattr(state, quote_attr, max(0.0, self._safe_float(getattr(state, quote_attr, 0.0), 0.0) - quote))
        setattr(state, fee_attr, max(0.0, self._safe_float(getattr(state, fee_attr, 0.0), 0.0) - fees))
        return used, quote, fees

    def _allocate_exit_entry_buckets(self, symbol: str, state: TradeState, details: List[dict]) -> Tuple[float, float]:
        self._ensure_entry_buckets_initialized(symbol, state)
        if self._entry_bucket_total_amount(state) <= 0:
            return 0.0, 0.0

        allocated_quote = 0.0
        allocated_fees = 0.0
        eps = max(self._get_min_contracts(symbol) * 1e-9, 1e-12)
        for detail in details:
            contracts = self._safe_float(detail.get("contracts"), 0.0)
            if contracts <= 0:
                continue
            scope = self._exit_scope_for_detail(state, detail)
            remaining = contracts
            if scope == "average_recovery":
                used, quote, fees = self._take_entry_bucket(state, "average", remaining)
                remaining -= used
                allocated_quote += quote
                allocated_fees += fees
                detail["exit_scope"] = "average_recovery"
            elif scope == "base":
                used, quote, fees = self._take_entry_bucket(state, "base", remaining)
                remaining -= used
                allocated_quote += quote
                allocated_fees += fees
                detail["exit_scope"] = "base"
            else:
                total_amount = self._entry_bucket_total_amount(state)
                if total_amount > 0:
                    base_target = remaining * self._safe_float(state.base_entry_amount, 0.0) / total_amount
                    avg_target = remaining - base_target
                    used_base, quote, fees = self._take_entry_bucket(state, "base", base_target)
                    allocated_quote += quote
                    allocated_fees += fees
                    used_avg, quote, fees = self._take_entry_bucket(state, "average", avg_target + max(0.0, base_target - used_base))
                    allocated_quote += quote
                    allocated_fees += fees
                    remaining -= used_base + used_avg
                    detail["exit_scope"] = "proportional"

            if remaining > eps:
                used, quote, fees = self._take_entry_bucket(state, "base", remaining)
                remaining -= used
                allocated_quote += quote
                allocated_fees += fees
            if remaining > eps:
                _, quote, fees = self._take_entry_bucket(state, "average", remaining)
                allocated_quote += quote
                allocated_fees += fees

        self._refresh_entry_bucket_prices(symbol, state)
        return allocated_quote, allocated_fees

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
            self._ensure_entry_buckets_initialized(state.symbol or state.market_symbol, state)
            return
        if exit_amount <= 0:
            state.remaining_entry_quote = max(0.0, entry_quote)
            state.remaining_buy_fees_quote = max(0.0, entry_fees)
            self._refresh_net_open_pnl(state)
            self._ensure_entry_buckets_initialized(state.symbol or state.market_symbol, state)
            return
        self._recalculate_proportional_pnl_from_totals(state)
        self._ensure_entry_buckets_initialized(state.symbol or state.market_symbol, state)

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
        if not state.cycle_id:
            state.cycle_id = self._new_cycle_id(symbol, {"ts": state.last_signal_timestamp, "strategy_name": state.strategy_name})
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
        self._record_entry_bucket_fills(symbol, state, details, reason)
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
            fill_signal = {
                "ts": state.last_signal_timestamp or state.last_entry_ladder_signal_timestamp,
                "strategy_name": state.strategy_name or "ema_pullback",
                "rs30": state.last_rs30,
                "rs60": state.last_rs60,
                "ema50": state.last_ema50,
                "ema100": state.last_ema100,
                "ema25d": state.last_ema25d,
                "ema50d": state.last_ema50d,
                "btc_return_30m": state.last_btc_return_30m,
                "valid": True,
            }
            self._record_signal_analytics(
                "fill_synced",
                symbol=symbol,
                signal=fill_signal,
                filled_notional=detail_quote,
                operation_id=str(detail.get("operation_id") or self._operation_id("fill_sync", symbol=symbol, order_id=str(detail.get("order_id") or ""))),
                order_id=str(detail.get("order_id") or ""),
                cycle_id=state.cycle_id,
                context={
                    "fill_kind": "entry",
                    "side": side,
                    "contracts": detail_contracts,
                    "price": detail_price,
                    "fee_quote": detail_fee,
                    "fill_source": source,
                    "aggregate_avg": avg_entry,
                },
            )
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
        allocated_entry_quote, allocated_entry_fees = self._allocate_exit_entry_buckets(symbol, state, details)
        if allocated_entry_quote <= 0 and allocated_entry_fees <= 0:
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
        bucket_entry_quote = state.base_entry_quote + state.averaging_entry_quote
        bucket_entry_fees = state.base_entry_fees_quote + state.averaging_entry_fees_quote
        if bucket_entry_quote > 0 or self._entry_bucket_total_amount(state) > 0:
            state.remaining_entry_quote = max(0.0, bucket_entry_quote)
            state.remaining_buy_fees_quote = max(0.0, bucket_entry_fees)
        else:
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
            fill_signal = {
                "ts": state.last_signal_timestamp,
                "strategy_name": state.strategy_name or "ema_pullback",
                "rs30": state.last_rs30,
                "rs60": state.last_rs60,
                "ema50": state.last_ema50,
                "ema100": state.last_ema100,
                "ema25d": state.last_ema25d,
                "ema50d": state.last_ema50d,
                "btc_return_30m": state.last_btc_return_30m,
                "valid": True,
            }
            self._record_signal_analytics(
                "fill_synced",
                symbol=symbol,
                signal=fill_signal,
                filled_notional=detail_quote,
                realized_pnl_quote=state.realized_pnl,
                operation_id=str(detail.get("operation_id") or self._operation_id("fill_sync", symbol=symbol, order_id=str(detail.get("order_id") or ""))),
                order_id=str(detail.get("order_id") or ""),
                cycle_id=state.cycle_id,
                context={
                    "fill_kind": "exit",
                    "side": side,
                    "contracts": detail_contracts,
                    "price": detail_price,
                    "fee_quote": detail_fee,
                    "fill_source": source,
                    "aggregate_avg": avg_exit,
                    "exit_scope": str(detail.get("exit_scope") or ""),
                },
            )
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
                reason=f"{reason};exit_fill=1;exit_scope={str(detail.get('exit_scope') or '')};aggregate_avg={avg_exit:.12f};fee_source={source}",
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
                if state.hard_stop_order:
                    self._cancel_hard_stop_order(symbol, reason="reserved_by_other_profile")
                if symbol in self.disabled_symbols:
                    self.disabled_symbols.discard(symbol)
                self._log_reserved_by_other_profile(symbol, side=opposite_side, amount=opposite_size)
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
            self._cancel_hard_stop_order(symbol, reason=f"unexpected_{opposite_side}_position")
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
                # Use current equity and config budget as a better guess for initial notional
                # if we have no state, as it helps averaging ratio logic stay conservative.
                leverage = max(self._safe_float(state.leverage, 0.0), self._safe_float(config.RISK.leverage, 1.0), 1.0)
                account = self._account_snapshot()
                equity = account.get("total") or account.get("free", 0.0)
                planned_margin = equity * config.BUYING.position_budget_fraction
                state.initial_entry_notional = max(planned_margin * leverage, self._contracts_to_notional(symbol, new_size, new_entry))
            state.unrealized_pnl = self._safe_float(snapshot.get("unrealized_pnl"), 0.0)
            self._refresh_net_open_pnl(state)
            self._clear_pending_exit_ladder(state)
            self._cancel_sell_orders(symbol, reason="rebuild_after_entry_fill")
            self._cancel_hard_stop_order(symbol, reason="rebuild_after_entry_fill")
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
            self._clear_pending_exit_ladder(state)
            self._cancel_sell_orders(symbol, reason="rebuild_after_add")
            self._cancel_hard_stop_order(symbol, reason="rebuild_after_add")
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
                self._clear_pending_exit_ladder(state)
                self._close_cycle(symbol, reason="exit_ladder_filled")
                return "closed"

            state.position_size = new_size
            state.position_side = position_side
            state.entry_price = new_entry or state.entry_price
            if state.exit_runner_contracts > new_size:
                state.exit_runner_contracts = max(0.0, new_size)
            state.unrealized_pnl = self._safe_float(snapshot.get("unrealized_pnl"), 0.0)
            self._refresh_net_open_pnl(state)
            self._clear_pending_exit_ladder(state)
            self._cancel_sell_orders(symbol, reason="rebuild_after_partial_exit")
            self._cancel_hard_stop_order(symbol, reason="rebuild_after_partial_exit")
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
            self._clear_pending_exit_ladder(state)
            self._close_cycle(symbol, reason="position_closed")
            return "closed"

        state.position_available = 0.0
        state.position_frozen = 0.0
        return "flat"

    def _close_cycle(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        self._cancel_sell_orders(symbol, reason="cycle_closed")
        self._cancel_entry_orders(symbol, reason="cycle_closed")
        self._cancel_hard_stop_order(symbol, reason="cycle_closed")

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
        self._clear_pending_exit_ladder(state)
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

        self._record_signal_analytics(
            "cycle_closed",
            symbol=symbol,
            signal={
                "ts": state.last_signal_timestamp,
                "strategy_name": state.strategy_name or "ema_pullback",
                "rs30": state.last_rs30,
                "rs60": state.last_rs60,
                "ema50": state.last_ema50,
                "ema100": state.last_ema100,
                "ema25d": state.last_ema25d,
                "ema50d": state.last_ema50d,
                "btc_return_30m": state.last_btc_return_30m,
                "valid": True,
            },
            filled_notional=exit_notional,
            realized_pnl_quote=realized,
            operation_id=self._operation_id("cycle_closed", symbol=symbol),
            cycle_id=state.cycle_id,
            context={
                "opened_at": opened_at,
                "closed_at": closed_at,
                "holding_minutes": (closed_at - opened_at) / 60.0,
                "entry_notional": entry_notional,
                "exit_notional": exit_notional,
                "avg_entry": avg_entry,
                "avg_exit": avg_exit,
                "close_reason": reason,
                "max_buy_stage": state.buy_stage,
                "max_averaging_stage": state.average_stage,
            },
        )
        self._log_event(
            "INFO",
            f"Cycle closed for {symbol}: pnl={realized:.8f}",
            event="cycle_closed",
            symbol=symbol,
            reason=reason,
        )
        cooldown_minutes = max(0.0, self._safe_float(config.RISK.cooldown_minutes_after_close, 0.0))
        if realized > 0:
            cooldown_minutes = max(
                cooldown_minutes,
                max(0.0, self._safe_float(getattr(config.RISK, "post_win_cooldown_minutes_after_close", 0.0), 0.0)),
            )
        cooldown_until = time.time() + cooldown_minutes * 60.0 if cooldown_minutes > 0 else None
        self._reset_state(symbol, preserve_cooldown=cooldown_until)
