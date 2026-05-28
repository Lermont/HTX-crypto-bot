# -*- coding: utf-8 -*-

import time
from typing import Dict, Iterable, List

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

    def _validate_shared_exchange_profiles(self):
        dry_run_values = {bool(profile.runtime.dry_run) for profile in self.profiles}
        if len(dry_run_values) > 1:
            raise RuntimeError("Combined profiles must all use the same DRY_RUN mode")

        if next(iter(dry_run_values), True):
            return

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
        remaining = CombinedHtxFuturesBot._safe_float(bot, order.get("remaining"), 0.0)
        if remaining <= 0:
            remaining = CombinedHtxFuturesBot._safe_float(bot, order.get("amount"), 0.0)
        return remaining

    def _exchange_reserved_symbols(self, bot: HtxFuturesBot) -> set:
        reserved = set()
        if bool(getattr(bot.profile.runtime, "dry_run", True)):
            return reserved

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
