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
            self.bots.append(bot)

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
            for symbol, state in bot.states.items():
                if state.position_size > 0 or state.entry_orders or state.sell_ladder_orders:
                    reserved.add(symbol)
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
