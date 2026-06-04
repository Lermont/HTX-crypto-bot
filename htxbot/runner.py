# -*- coding: utf-8 -*-

import time

import config


class RunnerMixin:
    def _log_step_exception(self, symbol: str, exc: Exception):
        is_transient = bool(getattr(self, "_is_transient_exchange_error", lambda _exc: False)(exc))
        self._log_event(
            "WARNING" if is_transient else "FAULT",
            f"Step failed for {symbol}: {exc}",
            event="state_exchange_mismatch",
            symbol=symbol,
            reason="step_network_error" if is_transient else "step_error",
            exception=exc,
            retryable=is_transient,
        )

    def _post_sync_order_hygiene(self, symbol: str, reason: str):
        reset_private_caches = getattr(self, "_reset_private_caches", None)
        if reset_private_caches:
            reset_private_caches()
        open_orders = self._fetch_open_orders(symbol)
        if open_orders is None:
            self._log_event(
                "WARNING",
                f"Skipping post-sync order hygiene for {symbol}: open orders are unavailable",
                event="state_exchange_mismatch",
                symbol=symbol,
                reason=f"{reason}_open_orders_unavailable",
            )
            return
        self._validate_sell_orders(symbol, open_orders)
        self._validate_entry_orders(symbol, open_orders)

    def setup(self):
        self._log_event("INFO", "Initializing HTX futures bot", event="futures_setup", reason="startup")
        self._load_markets_with_retry()

        self.benchmark_symbol = self._find_futures_symbol("btc")
        if not self.benchmark_symbol:
            self._log_event(
                "ERROR",
                "BTC USDT-M futures benchmark is missing",
                event="futures_setup",
                reason="benchmark_missing",
            )
            raise RuntimeError("BTC benchmark futures symbol is missing")

        if config.MACRO.enable_gold_btc_rsi_overlay:
            self.macro_gold_symbol = self._find_macro_gold_symbol()
            self._macro_gold_lookup_done = True
            self.macro_direct_gold_btc_symbol = self._find_direct_gold_btc_symbol()
            self._macro_direct_gold_btc_lookup_done = True
            if self.macro_gold_symbol:
                self._log_event(
                    "INFO",
                    f"Macro gold symbol found: {self.macro_gold_symbol}",
                    event="futures_setup",
                    symbol=self.macro_gold_symbol,
                    reason=f"macro_gold_symbol;spot={int(bool(getattr(self, 'macro_gold_is_spot', False)))}",
                )
            else:
                self._log_event(
                    "WARNING",
                    "Macro gold symbol is unavailable; gold/BTC RSI overlay will run in neutral fallback",
                    event="macro_context_unavailable",
                    reason="gold_symbol_not_found",
                )

        seen = set()
        for coin in config.COINS:
            symbol = self._find_futures_symbol(coin)
            if not symbol:
                self._log_event(
                    "WARNING",
                    f"USDT-M futures pair is unavailable for {coin}",
                    event="futures_setup",
                    symbol=coin.upper(),
                    reason="symbol_not_found",
                )
                continue
            if symbol == self.benchmark_symbol:
                self._log_event(
                    "WARNING",
                    f"Skipping benchmark symbol from entry universe: {symbol}",
                    event="futures_setup",
                    symbol=symbol,
                    reason="benchmark_symbol_not_traded",
                )
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            market = self._market(symbol)
            self.symbols.append(symbol)
            self.market_by_symbol[symbol] = market
            state = self._get_state(symbol)
            state.market_symbol = symbol
            self.entry_symbols.add(symbol)

        for symbol, state in list(self.states.items()):
            if symbol in seen or symbol not in self.exchange.markets:
                continue
            has_local_exposure = bool(state.position_size > 0 or state.entry_orders or state.sell_ladder_orders)
            if not has_local_exposure:
                continue
            market = self._market(symbol)
            if not (market.get("linear") and (market.get("swap") or market.get("future"))):
                continue
            seen.add(symbol)
            self.symbols.append(symbol)
            self.market_by_symbol[symbol] = market
            state.market_symbol = symbol
            state.frozen_no_more_buys = True
            self._log_event(
                "WARNING",
                f"Tracking removed symbol in maintenance-only mode: {symbol}",
                event="position_frozen",
                symbol=symbol,
                reason="maintenance_only_removed_from_coins",
            )

        self._log_event(
            "INFO",
            f"Found {len(self.entry_symbols)} entry symbols and {len(self.symbols)} total tracked HTX USDT-M futures symbols",
            event="futures_setup",
            reason=f"benchmark={self.benchmark_symbol}",
        )
        if getattr(self, "skip_futures_account_setup", False):
            self._log_event(
                "INFO",
                "Futures account setup skipped; another profile already performed shared startup setup",
                event="futures_setup",
                reason="shared_account_setup_skipped",
            )
        else:
            self._setup_futures_account()
        if not getattr(self, "skip_live_balance_log", False):
            account = self._account_snapshot()
            self._log_event(
                "INFO",
                f"Futures cross balance free={account['free']:.8f} total={account['total']:.8f} USDT",
                event="futures_setup",
                reason="cross_balance_checked",
            )
        self._save_state()

    def step_symbol(self, symbol: str):
        state = self._get_state(symbol)
        had_tracked_exit_orders = bool(state.sell_ladder_orders or state.hard_stop_order)
        snapshot = self._fetch_position_snapshot(symbol)
        if not snapshot.get("ok", False):
            return

        open_orders = self._fetch_open_orders(symbol)
        if open_orders is None:
            self._log_event(
                "WARNING",
                f"Skipping {symbol}: open orders are unavailable",
                event="state_exchange_mismatch",
                symbol=symbol,
                reason="open_orders_unavailable_skip",
            )
            return
        sync_status = self._sync_state_with_position(symbol, snapshot, open_orders=open_orders)
        if sync_status in {"disabled", "reserved"}:
            return
        if sync_status == "position_changed":
            if not had_tracked_exit_orders:
                self._post_sync_order_hygiene(symbol, reason="post_position_change")
            return
        if sync_status == "closed":
            self._post_sync_order_hygiene(symbol, reason="post_position_closed")
            return
        if self._maybe_close_dust_position(symbol, open_orders):
            return

        state = self._get_state(symbol)
        external_reserved_symbols = getattr(self, "external_reserved_symbols", set())
        if symbol in external_reserved_symbols and state.position_size <= 0:
            if state.entry_orders:
                self._cancel_entry_orders(symbol, reason="reserved_by_other_profile")
            if state.sell_ladder_orders:
                self._cancel_sell_orders(symbol, reason="reserved_by_other_profile")
            self._log_reserved_by_other_profile(symbol)
            return

        signal = self.signal_cache.get("symbols", {}).get(symbol)
        signal_valid = bool(signal and signal.get("valid") and self.signal_cache.get("benchmark_ok"))

        if not self._validate_sell_orders(symbol, open_orders):
            return
        if not self._validate_entry_orders(symbol, open_orders):
            return
        self._manage_entry_orders(symbol, signal, open_orders)

        state = self._get_state(symbol)
        if state.position_size > 0:
            if not signal_valid:
                self._freeze_no_more_buys(symbol, reason="signal_invalid_or_missing")
            if self._maybe_apply_absolute_force_exit(symbol, reason="absolute_force_exit_elapsed"):
                return
            self._ensure_hard_stop_loss(symbol, signal=signal)
            state = self._get_state(symbol)
            if state.sell_ladder_mode == "hard_stop_loss":
                return
            if self._maybe_apply_controlled_loss_exit(symbol, signal):
                return
            if self._maybe_apply_urgent_time_exit(symbol, signal):
                return
            if self._maybe_apply_account_pnl_trailing(symbol, signal):
                return
            if self._maybe_apply_account_profit_unload(symbol, signal):
                return
            time_exit_applied = self._maybe_apply_time_based_exit(symbol, signal)
            if not time_exit_applied:
                self._maybe_manage_exit_runner(symbol, signal)
            self._ensure_sell_ladder(symbol)
            state = self._get_state(symbol)
            self._maybe_place_average_buy(symbol, signal)
            return

        if state.position_size <= 0:
            exit_side = config.EXIT_SIDE
            if state.sell_ladder_orders:
                self._log_event(
                    "WARNING",
                    f"Tracked {exit_side} exit orders remain on flat {symbol}; canceling tracked bot orders",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    reason="flat_symbol_exit_order",
                )
                self._cancel_sell_orders(symbol, reason="flat_symbol_exit_order")
            if state.hard_stop_order:
                self._log_event(
                    "WARNING",
                    f"Tracked {exit_side} hard stop remains on flat {symbol}; canceling tracked bot order",
                    event="reduce_only_violation_prevented",
                    symbol=symbol,
                    side=exit_side,
                    reason="flat_symbol_hard_stop_order",
                )
                self._cancel_hard_stop_order(symbol, reason="flat_symbol_hard_stop_order")
            if not state.entry_orders:
                self._maybe_place_initial_buy(symbol, signal)

    def run(self):
        self._acquire_runtime_lock()
        try:
            self.setup()
            self._log_event("INFO", "HTX futures bot loop started", event="futures_setup", reason="bot_started")

            while True:
                started_at = time.time()
                self._assert_runtime_lock_owned()
                self._reset_private_caches()
                reset_market_data = getattr(self, "_reset_market_data_caches", None)
                if reset_market_data:
                    reset_market_data()
                self._update_signal_cache_if_needed()
                self._prepare_new_entry_gate()
                prefetch_market_data = getattr(self, "_prefetch_market_data_snapshots", None)
                if prefetch_market_data:
                    prefetch_market_data()
                prefetch_private = getattr(self, "_prefetch_private_snapshots", None)
                if prefetch_private:
                    prefetch_private()

                for symbol in self.symbols:
                    self._run_step_symbol_safe(symbol)

                self._save_state()
                self._sleep_after_poll(started_at)
        finally:
            self._release_runtime_lock()

    def _run_step_symbol_safe(self, symbol: str):
        try:
            self.step_symbol(symbol)
        except Exception as exc:
            self._log_step_exception(symbol, exc)

    def _sleep_after_poll(self, started_at: float):
        elapsed = max(0.0, time.time() - started_at)
        interval = max(0.0, float(config.RUNTIME.poll_interval_sec))
        time.sleep(max(0.0, interval - elapsed))
