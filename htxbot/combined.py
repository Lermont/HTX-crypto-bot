# -*- coding: utf-8 -*-

import time
from typing import Dict, Iterable, List, Optional, Tuple

import config

from .app import HtxFuturesBot
from .shared_exchange import CachedMarketDataExchange


class CombinedHtxFuturesBot:
    def __init__(self, profiles: Iterable[str] = ()):
        profile_names = tuple(profiles) or config.enabled_profile_names()
        self.profiles = [config.resolve_profile(name) for name in profile_names]
        if not self.profiles:
            raise RuntimeError("No bot profiles are enabled")
        self._validate_shared_exchange_profiles()

        self.bots: List[HtxFuturesBot] = []
        shared_exchange = None
        shared_external_price_feeds: Dict[config.ExternalPriceFeedSettings, object] = {}
        shared_account_pnl_runtime = {"history": [], "last_sample_at": 0.0}
        for profile in self.profiles:
            feed_settings = profile.external_price_feed
            shared_external_price_feed = shared_external_price_feeds.get(feed_settings)
            bot = HtxFuturesBot(profile=profile, exchange=shared_exchange, external_price_feed=shared_external_price_feed)
            if shared_exchange is None:
                shared_exchange = CachedMarketDataExchange(bot.exchange)
                bot.exchange = shared_exchange
            shared_external_price_feeds.setdefault(feed_settings, bot.external_price_feed)
            bot.skip_futures_account_setup = bool(self.bots)
            bot.skip_live_balance_log = bool(self.bots)
            bot.account_pnl_runtime = shared_account_pnl_runtime
            self.bots.append(bot)
        for bot in self.bots:
            bot.account_pnl_bots = list(self.bots)

    def setup(self):
        for bot in self.bots:
            with config.use_profile(bot.profile):
                bot._acquire_runtime_lock()

        for bot in self.bots:
            with config.use_profile(bot.profile):
                bot.setup()

    def run_once(self):
        had_step_error = False
        for bot in self.bots:
            with config.use_profile(bot.profile):
                reset_private_caches = getattr(bot, "_reset_private_caches", None)
                if reset_private_caches:
                    reset_private_caches()

        for bot in self.bots:
            with config.use_profile(bot.profile):
                bot._update_signal_cache_if_needed()

        for bot in self.bots:
            bot.external_reserved_symbols = self._reserved_symbols(exclude=bot)
            with config.use_profile(bot.profile):
                prepare_entry_gate = getattr(bot, "_prepare_new_entry_gate", None)
                if prepare_entry_gate:
                    prepare_entry_gate()
                for symbol in bot.symbols:
                    try:
                        bot.step_symbol(symbol)
                    except Exception as exc:
                        had_step_error = True
                        log_step_exception = getattr(bot, "_log_step_exception", None)
                        if log_step_exception:
                            log_step_exception(symbol, exc)
                        else:
                            bot._log_event(
                                "FAULT",
                                f"Step failed for {symbol}: {exc}",
                                event="state_exchange_mismatch",
                                symbol=symbol,
                                reason="step_error",
                                exception=exc,
                            )
                bot._save_state()
        try:
            self._rebalance_btc_hedge(skip_reason="step_error" if had_step_error else "")
        except Exception as exc:
            self._log_btc_hedge(
                "ERROR",
                f"BTC hedge skipped after unexpected error: {exc}",
                reason="btc_hedge_unhandled_error",
                event="btc_hedge_order_failed",
                exception=exc,
                throttle_sec=60.0,
            )

    def _validate_shared_exchange_profiles(self):
        first = self.profiles[0].api_credentials
        for profile in self.profiles[1:]:
            if profile.api_credentials != first:
                raise RuntimeError("Combined live profiles must use the same HTX API credentials")

    def _reserved_symbols(self, exclude: HtxFuturesBot) -> set:
        reserved = set()
        for bot in self.bots:
            if bot is exclude:
                continue
            with config.use_profile(bot.profile):
                for symbol, state in bot.states.items():
                    if state.position_size > 0 or state.entry_orders or state.sell_ladder_orders:
                        reserved.add(symbol)
                reserved.update(self._exchange_reserved_symbols(bot))
        return reserved

    @staticmethod
    def _safe_float(bot: HtxFuturesBot, value, default: float = 0.0) -> float:
        safe_float = getattr(bot, "_safe_float", None)
        if safe_float:
            return safe_float(value, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _order_remaining_amount(bot: HtxFuturesBot, order: dict) -> float:
        remaining_amount = getattr(bot, "_order_remaining_amount", None)
        if remaining_amount:
            return remaining_amount(order)
        if isinstance(order, dict) and "remaining" in order and order.get("remaining") is not None:
            return max(0.0, CombinedHtxFuturesBot._safe_float(bot, order.get("remaining"), 0.0))
        return max(0.0, CombinedHtxFuturesBot._safe_float(bot, order.get("amount"), 0.0))

    def _exchange_reserved_symbols(self, bot: HtxFuturesBot) -> set:
        reserved = set()
        symbols = set(getattr(bot, "symbols", []) or [])
        min_contracts = getattr(bot, "_get_min_contracts", None)

        positions_by_symbol = None
        bulk_positions = getattr(bot, "_bulk_positions_by_symbol", None)
        if bulk_positions:
            try:
                positions_by_symbol = bulk_positions()
            except Exception as exc:
                log_event = getattr(bot, "_log_event", None)
                if log_event:
                    log_event(
                        "WARNING",
                        f"Combined reservation could not inspect exchange positions: {exc}",
                        event="state_exchange_mismatch",
                        reason="combined_reserved_positions_fetch_failed",
                        exception=exc,
                    )
                positions_by_symbol = None

        for symbol, positions in (positions_by_symbol or {}).items():
            if symbols and symbol not in symbols:
                continue
            epsilon = 1e-12
            if min_contracts:
                try:
                    epsilon = max(min_contracts(symbol) * 1e-9, epsilon)
                except Exception:
                    pass
            for position in positions or []:
                side = str((position or {}).get("side") or "").lower()
                contracts = self._safe_float(bot, (position or {}).get("contracts"), 0.0)
                if side == bot.profile.position_side and contracts > epsilon:
                    reserved.add(symbol)
                    break

        orders_by_symbol = None
        bulk_orders = getattr(bot, "_bulk_open_orders_by_symbol", None)
        if bulk_orders:
            try:
                orders_by_symbol = bulk_orders()
            except Exception as exc:
                log_event = getattr(bot, "_log_event", None)
                if log_event:
                    log_event(
                        "WARNING",
                        f"Combined reservation could not inspect exchange open orders: {exc}",
                        event="state_exchange_mismatch",
                        reason="combined_reserved_orders_fetch_failed",
                        exception=exc,
                    )
                orders_by_symbol = None

        reserved_order_sides = {bot.profile.entry_side, bot.profile.exit_side}
        for symbol, orders in (orders_by_symbol or {}).items():
            if symbols and symbol not in symbols:
                continue
            epsilon = 1e-12
            if min_contracts:
                try:
                    epsilon = max(min_contracts(symbol) * 1e-9, epsilon)
                except Exception:
                    pass
            for order in orders or []:
                side = str((order or {}).get("side") or "").lower()
                if side not in reserved_order_sides:
                    continue
                if self._order_remaining_amount(bot, order) > epsilon:
                    reserved.add(symbol)
                    break
        return reserved

    def _hedge_control_bot(self) -> Optional[HtxFuturesBot]:
        return self.bots[0] if getattr(self, "bots", None) else None

    def _log_btc_hedge(
        self,
        level: str,
        message: str,
        reason: str,
        event: str = "btc_hedge",
        throttle_sec: float = 0.0,
        **kwargs,
    ):
        bot = self._hedge_control_bot()
        if not bot:
            return
        log_event = getattr(bot, "_log_event", None)
        if not log_event:
            return
        if throttle_sec > 0:
            now = time.time()
            logged = getattr(self, "_btc_hedge_log_at", {})
            if not isinstance(logged, dict):
                logged = {}
            key = (event, reason, str(kwargs.get("symbol") or ""))
            last = self._safe_float(bot, logged.get(key), 0.0)
            if now - last < throttle_sec:
                return
            logged[key] = now
            self._btc_hedge_log_at = logged
        with config.use_profile(bot.profile):
            log_event(level, message, event=event, reason=reason, **kwargs)

    def _btc_hedge_settings(self):
        return getattr(config, "HEDGE", None)

    def _btc_hedge_enabled(self) -> bool:
        settings = self._btc_hedge_settings()
        return bool(settings and getattr(settings, "btc_hedge_enabled", False))

    def _btc_hedge_profiles_ready(self) -> bool:
        sides = set()
        for bot in getattr(self, "bots", []) or []:
            profile = getattr(bot, "profile", None)
            side = str(getattr(profile, "position_side", "") or "").lower()
            if side in {"long", "short"}:
                sides.add(side)
        return {"long", "short"}.issubset(sides)

    def _btc_hedge_symbol(self, bot: HtxFuturesBot) -> str:
        settings = self._btc_hedge_settings()
        coin = str(getattr(settings, "btc_hedge_coin", "btc") or "btc").strip().lower()
        if coin == "btc" and getattr(bot, "benchmark_symbol", None):
            return str(bot.benchmark_symbol)
        find_symbol = getattr(bot, "_find_futures_symbol", None)
        if not find_symbol:
            return ""
        with config.use_profile(bot.profile):
            return find_symbol(coin) or ""

    def _btc_hedge_managed_symbols(self, hedge_symbol: str) -> set:
        symbols = set()
        for bot in self.bots:
            symbols.update(getattr(bot, "symbols", []) or [])
            states = getattr(bot, "states", {}) or {}
            for symbol, state in states.items():
                if state.position_size > 0 or state.entry_orders or state.sell_ladder_orders:
                    symbols.add(symbol)
        symbols.discard(hedge_symbol)
        return symbols

    def _position_payload_symbol(self, bot: HtxFuturesBot, payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""
        symbol = str(payload.get("symbol") or "")
        if symbol:
            return symbol
        payload_symbol = getattr(bot, "_payload_symbol", None)
        if payload_symbol:
            try:
                return str(payload_symbol(payload) or "")
            except Exception:
                return ""
        return ""

    def _position_payload_price(self, bot: HtxFuturesBot, symbol: str, payload: dict) -> float:
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        for source in (payload, info):
            if not isinstance(source, dict):
                continue
            for key in (
                "markPrice",
                "mark_price",
                "lastPrice",
                "last_price",
                "price",
                "entryPrice",
                "entry_price",
            ):
                price = self._safe_float(bot, source.get(key), 0.0)
                if price > 0:
                    return price
        try:
            ticker = bot.exchange.fetch_ticker(symbol)
            for key in ("last", "mark", "bid", "ask"):
                price = self._safe_float(bot, ticker.get(key), 0.0)
                if price > 0:
                    return price
        except Exception:
            pass
        return 0.0

    def _position_payload_available(self, bot: HtxFuturesBot, payload: dict, contracts: float) -> float:
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        for source in (payload, info):
            if not isinstance(source, dict):
                continue
            for key in (
                "available",
                "availablePosition",
                "available_position",
                "avail_position",
                "canCloseVolume",
                "close_available",
                "volume_available",
                "available_volume",
            ):
                if key in source and source.get(key) is not None:
                    return max(0.0, min(contracts, self._safe_float(bot, source.get(key), 0.0)))
        return max(0.0, contracts)

    def _fetch_btc_hedge_positions(self, bot: HtxFuturesBot, symbols: set) -> Optional[List[dict]]:
        ordered_symbols = sorted(symbols)
        with config.use_profile(bot.profile):
            try:
                return bot._private_fetch_with_retry(
                    "",
                    "btc_hedge_positions_fetch_failed",
                    "BTC hedge positions",
                    lambda: bot.exchange.fetch_positions(ordered_symbols, bot._position_params()),
                )
            except Exception as exc:
                level = "WARNING" if bot._is_transient_exchange_error(exc) else "ERROR"
                self._log_btc_hedge(
                    level,
                    f"BTC hedge skipped: could not fetch fresh positions: {exc}",
                    reason="positions_fetch_failed",
                    event="btc_hedge",
                    exception=exc,
                    throttle_sec=60.0,
                )
                return None

    def _fetch_btc_hedge_open_orders(self, bot: HtxFuturesBot, symbol: str) -> Optional[List[dict]]:
        with config.use_profile(bot.profile):
            try:
                return bot._private_fetch_with_retry(
                    symbol,
                    "btc_hedge_open_orders_fetch_failed",
                    f"BTC hedge open orders for {symbol}",
                    lambda: bot.exchange.fetch_open_orders(symbol, params=bot._position_params()),
                )
            except TypeError:
                try:
                    return bot._private_fetch_with_retry(
                        symbol,
                        "btc_hedge_open_orders_fetch_failed",
                        f"BTC hedge open orders for {symbol}",
                        lambda: bot.exchange.fetch_open_orders(symbol),
                    )
                except Exception as exc:
                    level = "WARNING" if bot._is_transient_exchange_error(exc) else "ERROR"
                    self._log_btc_hedge(
                        level,
                        f"BTC hedge skipped: could not fetch open orders for {symbol}: {exc}",
                        reason="open_orders_fetch_failed",
                        symbol=symbol,
                        exception=exc,
                        throttle_sec=60.0,
                    )
                    return None
            except Exception as exc:
                level = "WARNING" if bot._is_transient_exchange_error(exc) else "ERROR"
                self._log_btc_hedge(
                    level,
                    f"BTC hedge skipped: could not fetch open orders for {symbol}: {exc}",
                    reason="open_orders_fetch_failed",
                    symbol=symbol,
                    exception=exc,
                    throttle_sec=60.0,
                )
                return None

    def _btc_hedge_exposure(self, bot: HtxFuturesBot, positions: List[dict], hedge_symbol: str, managed_symbols: set) -> dict:
        exposure = {
            "long_notional": 0.0,
            "short_notional": 0.0,
            "hedge_long_contracts": 0.0,
            "hedge_long_available": 0.0,
            "hedge_short_contracts": 0.0,
            "hedge_short_available": 0.0,
            "hedge_price": 0.0,
        }
        for position in positions or []:
            if not isinstance(position, dict):
                continue
            symbol = self._position_payload_symbol(bot, position)
            if not symbol or (symbol != hedge_symbol and symbol not in managed_symbols):
                continue
            side = str(position.get("side") or "").lower()
            if side not in {"long", "short"}:
                continue
            contracts = self._safe_float(bot, position.get("contracts"), 0.0)
            if contracts <= 0:
                continue
            price = self._position_payload_price(bot, symbol, position)
            if price <= 0:
                continue
            if symbol == hedge_symbol:
                exposure[f"hedge_{side}_contracts"] += contracts
                exposure[f"hedge_{side}_available"] += self._position_payload_available(bot, position, contracts)
                exposure["hedge_price"] = price
                continue
            notional = bot._contracts_to_notional(symbol, contracts, price)
            exposure[f"{side}_notional"] += notional
        return exposure

    def _btc_hedge_reference_price(self, bot: HtxFuturesBot, symbol: str) -> float:
        try:
            ticker = bot.exchange.fetch_ticker(symbol)
        except Exception as exc:
            self._log_btc_hedge(
                "WARNING",
                f"BTC hedge skipped: ticker unavailable for {symbol}: {exc}",
                reason="ticker_unavailable",
                symbol=symbol,
                exception=exc,
                throttle_sec=60.0,
            )
            return 0.0
        for key in ("last", "mark", "bid", "ask"):
            price = self._safe_float(bot, ticker.get(key), 0.0)
            if price > 0:
                return price
        return 0.0

    def _btc_hedge_target(self, bot: HtxFuturesBot, symbol: str, net_notional: float) -> Tuple[str, float, float, float]:
        settings = self._btc_hedge_settings()
        ratio = max(0.0, self._safe_float(bot, getattr(settings, "btc_hedge_ratio", 1.0), 1.0))
        target_notional = abs(net_notional) * ratio
        max_notional = max(0.0, self._safe_float(bot, getattr(settings, "btc_hedge_max_notional", 0.0), 0.0))
        if max_notional > 0:
            target_notional = min(target_notional, max_notional)
        if target_notional <= 0:
            return "", 0.0, 0.0, 0.0
        target_side = "short" if net_notional > 0 else "long"
        price = self._btc_hedge_reference_price(bot, symbol)
        if price <= 0:
            return "", 0.0, 0.0, 0.0
        contracts = bot._contracts_for_notional(symbol, target_notional, price)
        if contracts <= 0:
            return "", 0.0, target_notional, price
        return target_side, contracts, bot._contracts_to_notional(symbol, contracts, price), price

    def _btc_hedge_open_market_safe(self, bot: HtxFuturesBot, symbol: str, amount: float, reference_price: float) -> bool:
        settings = self._btc_hedge_settings()
        max_spread_bps = self._safe_float(bot, getattr(settings, "btc_hedge_max_spread_bps", 0.0), 0.0)
        if max_spread_bps <= 0:
            return True
        try:
            ticker = bot.exchange.fetch_ticker(symbol)
        except Exception as exc:
            self._log_btc_hedge(
                "WARNING",
                f"BTC hedge open skipped: ticker unavailable for {symbol}: {exc}",
                reason="market_spread_unavailable",
                symbol=symbol,
                amount=amount,
                price=reference_price,
                exception=exc,
                throttle_sec=60.0,
            )
            return False

        bid = self._safe_float(bot, (ticker or {}).get("bid"), 0.0)
        ask = self._safe_float(bot, (ticker or {}).get("ask"), 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            self._log_btc_hedge(
                "WARNING",
                f"BTC hedge open skipped: bid/ask spread is unavailable for {symbol}",
                reason="market_spread_unavailable",
                symbol=symbol,
                amount=amount,
                price=reference_price,
                throttle_sec=60.0,
            )
            return False

        midpoint = (bid + ask) / 2.0
        spread_bps = ((ask - bid) / midpoint) * 10000.0 if midpoint > 0 else 0.0
        if spread_bps > max_spread_bps:
            self._log_btc_hedge(
                "WARNING",
                f"BTC hedge open skipped: BTC spread {spread_bps:.4f} bps exceeds limit {max_spread_bps:.4f}",
                reason="market_spread_too_wide",
                symbol=symbol,
                amount=amount,
                price=reference_price,
                diagnostic_context={
                    "bid": bid,
                    "ask": ask,
                    "spread_bps": spread_bps,
                    "max_spread_bps": max_spread_bps,
                },
                throttle_sec=60.0,
            )
            return False
        return True

    def _btc_hedge_order_leverage(self, bot: HtxFuturesBot, symbol: str) -> float:
        with config.use_profile(bot.profile):
            configured = self._safe_float(bot, getattr(config.RISK, "account_leverage", 0.0), 0.0)
            if configured > 0:
                return configured

            if not hasattr(bot, "order_leverage_cache"):
                bot.order_leverage_cache = {}
            cached = bot.order_leverage_cache.get(symbol)
            if cached and cached > 0:
                return cached

            method = getattr(bot.exchange, "contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo", None)
            if not method:
                self._log_btc_hedge(
                    "ERROR",
                    f"BTC hedge cannot read manual HTX leverage for {symbol}: raw account-position endpoint is unavailable",
                    reason="manual_account_leverage_unavailable",
                    event="btc_hedge_order_failed",
                    symbol=symbol,
                    throttle_sec=60.0,
                )
                return 0.0

            try:
                market = bot._market(symbol)
                response = method(
                    {
                        "contract_code": market.get("id") or symbol,
                        "margin_account": config.EXCHANGE.quote_currency,
                    }
                )
            except Exception as exc:
                self._log_btc_hedge(
                    "ERROR",
                    f"BTC hedge cannot read manual HTX leverage for {symbol}: {exc}",
                    reason="manual_account_leverage_unavailable",
                    event="btc_hedge_order_failed",
                    symbol=symbol,
                    exception=exc,
                    throttle_sec=60.0,
                )
                return 0.0

            leverage = bot._account_leverage_from_payload(symbol, response)
            if leverage <= 0:
                self._log_btc_hedge(
                    "ERROR",
                    f"BTC hedge cannot determine manual HTX leverage for {symbol}; hedge order is blocked",
                    reason="manual_account_leverage_missing",
                    event="btc_hedge_order_failed",
                    symbol=symbol,
                    throttle_sec=60.0,
                )
                return 0.0
            bot.order_leverage_cache[symbol] = leverage
            return leverage

    def _place_btc_hedge_order(
        self,
        bot: HtxFuturesBot,
        symbol: str,
        side: str,
        amount: float,
        reference_price: float,
        reduce_only: bool,
        reason: str,
        net_notional: float,
        target_side: str,
        target_contracts: float,
    ) -> bool:
        with config.use_profile(bot.profile):
            amount = bot._amount_to_precision(symbol, amount)
            if amount <= 0:
                self._log_btc_hedge(
                    "WARNING",
                    f"BTC hedge skipped for {symbol}: rebalance amount is below precision/minimum",
                    reason="amount_below_min_contracts",
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    throttle_sec=60.0,
                )
                return False
            if not reduce_only and not self._btc_hedge_open_market_safe(bot, symbol, amount, reference_price):
                return False
            leverage = self._btc_hedge_order_leverage(bot, symbol)
            if leverage <= 0:
                return False
            try:
                order = bot._create_one_way_order(
                    symbol=symbol,
                    order_type="market",
                    side=side,
                    amount=amount,
                    price=None,
                    reduce_only=reduce_only,
                    post_only=False,
                    leverage=leverage,
                )
            except Exception as exc:
                self._log_btc_hedge(
                    "ERROR",
                    f"BTC hedge order failed for {symbol}: {exc}",
                    reason=f"{reason}_order_failed",
                    event="btc_hedge_order_failed",
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    price=reference_price,
                    notional=bot._contracts_to_notional(symbol, amount, reference_price),
                    exception=exc,
                    throttle_sec=60.0,
                )
                return False

            order_id = str((order or {}).get("id") or "")
            self._last_btc_hedge_action_at = time.time()
            self._log_btc_hedge(
                "INFO",
                f"BTC hedge rebalanced for {symbol}: side={side} contracts={amount} reduce_only={int(reduce_only)}",
                reason=reason,
                event="btc_hedge_rebalanced",
                symbol=symbol,
                side=side,
                order_id=order_id,
                amount=amount,
                price=reference_price,
                notional=bot._contracts_to_notional(symbol, amount, reference_price),
                diagnostic_context={
                    "net_notional": net_notional,
                    "target_side": target_side,
                    "target_contracts": target_contracts,
                    "reduce_only": reduce_only,
                },
            )
            return True

    def _rebalance_btc_hedge(self, skip_reason: str = ""):
        if not self._btc_hedge_enabled():
            return
        bot = self._hedge_control_bot()
        if not bot:
            return
        if skip_reason:
            self._log_btc_hedge(
                "WARNING",
                f"BTC hedge skipped: profile step failed earlier in cycle ({skip_reason})",
                reason=f"skip_{skip_reason}",
                throttle_sec=60.0,
            )
            return
        settings = self._btc_hedge_settings()
        cooldown = self._safe_float(bot, getattr(settings, "btc_hedge_cooldown_sec", 0.0), 0.0)
        if cooldown > 0 and time.time() - self._safe_float(bot, getattr(self, "_last_btc_hedge_action_at", 0.0), 0.0) < cooldown:
            return

        hedge_symbol = self._btc_hedge_symbol(bot)
        if not hedge_symbol:
            self._log_btc_hedge(
                "ERROR",
                "BTC hedge enabled but BTC futures symbol was not found",
                reason="btc_symbol_not_found",
                throttle_sec=300.0,
            )
            return
        managed_symbols = self._btc_hedge_managed_symbols(hedge_symbol)
        if not managed_symbols:
            managed_symbols = set()
        positions = self._fetch_btc_hedge_positions(bot, managed_symbols | {hedge_symbol})
        if positions is None:
            return

        exposure = self._btc_hedge_exposure(bot, positions, hedge_symbol, managed_symbols)
        net_notional = exposure["long_notional"] - exposure["short_notional"]
        target_side, target_contracts, target_notional, reference_price = self._btc_hedge_target(bot, hedge_symbol, net_notional)
        current_long = exposure["hedge_long_contracts"]
        current_short = exposure["hedge_short_contracts"]
        hedge_price = reference_price or exposure["hedge_price"] or self._btc_hedge_reference_price(bot, hedge_symbol)
        epsilon = 1e-12
        if current_long > epsilon and current_short > epsilon:
            self._log_btc_hedge(
                "ERROR",
                f"BTC hedge found both long and short positions on {hedge_symbol}; manual review required",
                reason="both_hedge_sides_open",
                symbol=hedge_symbol,
                amount=current_long + current_short,
                throttle_sec=60.0,
            )
            return

        current_side = "long" if current_long > epsilon else "short" if current_short > epsilon else ""
        current_contracts = current_long if current_side == "long" else current_short if current_side == "short" else 0.0
        current_available = (
            exposure["hedge_long_available"]
            if current_side == "long"
            else exposure["hedge_short_available"]
            if current_side == "short"
            else 0.0
        )
        current_notional = bot._contracts_to_notional(hedge_symbol, current_contracts, hedge_price) if hedge_price > 0 else 0.0
        min_contract_notional = bot._contracts_to_notional(hedge_symbol, bot._get_min_contracts(hedge_symbol), hedge_price) if hedge_price > 0 else 0.0
        min_rebalance = max(
            self._safe_float(bot, getattr(settings, "btc_hedge_min_rebalance_notional", 0.0), 0.0),
            min_contract_notional,
        )

        if not self._btc_hedge_profiles_ready():
            if current_contracts <= epsilon:
                self._log_btc_hedge(
                    "WARNING",
                    "BTC hedge skipped: both long and short profiles must be active before opening hedge exposure",
                    reason="profiles_not_ready",
                    symbol=hedge_symbol,
                    throttle_sec=300.0,
                )
                return
            self._log_btc_hedge(
                "WARNING",
                f"BTC hedge closing existing {current_side} exposure on {hedge_symbol}: long/short profiles are not both active",
                reason="profiles_not_ready_close_existing",
                symbol=hedge_symbol,
                side=current_side,
                amount=current_contracts,
                throttle_sec=60.0,
            )
            target_side = ""
            target_contracts = 0.0
            target_notional = 0.0
            reference_price = 0.0

        if not target_side and current_contracts <= epsilon:
            return
        if target_side == current_side and abs(target_notional - current_notional) < min_rebalance:
            return
        if current_side == "" and target_notional < min_rebalance:
            return

        open_orders = self._fetch_btc_hedge_open_orders(bot, hedge_symbol)
        if open_orders is None:
            return
        active_orders = [
            order for order in open_orders
            if self._order_remaining_amount(bot, order) > max(bot._get_min_contracts(hedge_symbol) * 1e-9, epsilon)
        ]
        if active_orders:
            self._log_btc_hedge(
                "WARNING",
                f"BTC hedge skipped for {hedge_symbol}: open BTC orders are present",
                reason="open_hedge_orders_present",
                symbol=hedge_symbol,
                amount=sum(self._order_remaining_amount(bot, order) for order in active_orders),
                throttle_sec=60.0,
            )
            return

        if not target_side:
            close_side = "sell" if current_side == "long" else "buy"
            close_amount = min(current_contracts, current_available)
            return self._place_btc_hedge_order(
                bot,
                hedge_symbol,
                close_side,
                close_amount,
                hedge_price,
                True,
                "btc_hedge_close_to_flat",
                net_notional,
                target_side,
                target_contracts,
            )

        if current_side and current_side != target_side:
            close_side = "sell" if current_side == "long" else "buy"
            close_amount = min(current_contracts, current_available)
            return self._place_btc_hedge_order(
                bot,
                hedge_symbol,
                close_side,
                close_amount,
                hedge_price,
                True,
                "btc_hedge_flip_close_first",
                net_notional,
                target_side,
                target_contracts,
            )

        if current_side == target_side:
            delta = target_contracts - current_contracts
            if bot._contracts_to_notional(hedge_symbol, abs(delta), hedge_price) < min_rebalance:
                return
            if delta > 0:
                side = "buy" if target_side == "long" else "sell"
                return self._place_btc_hedge_order(
                    bot,
                    hedge_symbol,
                    side,
                    delta,
                    hedge_price,
                    False,
                    "btc_hedge_increase",
                    net_notional,
                    target_side,
                    target_contracts,
                )
            side = "sell" if target_side == "long" else "buy"
            close_amount = min(abs(delta), current_available)
            return self._place_btc_hedge_order(
                bot,
                hedge_symbol,
                side,
                close_amount,
                hedge_price,
                True,
                "btc_hedge_reduce",
                net_notional,
                target_side,
                target_contracts,
            )

        side = "buy" if target_side == "long" else "sell"
        return self._place_btc_hedge_order(
            bot,
            hedge_symbol,
            side,
            target_contracts,
            reference_price,
            False,
            "btc_hedge_open",
            net_notional,
            target_side,
            target_contracts,
        )

    def poll_interval(self) -> int:
        intervals = [max(1, int(bot.profile.runtime.poll_interval_sec)) for bot in self.bots]
        return min(intervals) if intervals else 3

    def run(self):
        try:
            self.setup()
            names = ", ".join(bot.profile.name for bot in self.bots)
            for bot in self.bots:
                with config.use_profile(bot.profile):
                    bot._log_event(
                        "INFO",
                        f"Combined HTX futures bot loop started for profiles: {names}",
                        event="futures_setup",
                        reason="combined_bot_started",
                    )

            while True:
                started_at = time.time()
                self.run_once()
                elapsed = time.time() - started_at
                time.sleep(max(0.0, self.poll_interval() - elapsed))
        finally:
            for bot in getattr(self, "bots", []):
                with config.use_profile(bot.profile):
                    bot._release_runtime_lock()


__all__ = ["CombinedHtxFuturesBot"]
