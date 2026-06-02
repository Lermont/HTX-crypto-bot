# -*- coding: utf-8 -*-

import csv
import threading
import time
from typing import Dict, List, Optional, Tuple

import config

from .models import ExitLadderConfig, ExitLadderPreflight, SellLadderParams, TradeState


class SignalFilters:
    def _profile_health_block_reason(self) -> str:
        threshold = int(config.STRATEGY.max_unhealthy_positions_for_new_entries)
        if threshold < 0:
            return ""
        unhealthy = self._unhealthy_position_count()
        if unhealthy >= threshold:
            return f"profile_health_blocked;unhealthy_positions={unhealthy};threshold={threshold}"
        return ""

    def _entry_orderbook_spread_bps(self, symbol: str) -> Tuple[float, float, float]:
        fetch_cached = getattr(self, "_cached_order_book", None)
        book = fetch_cached(symbol, limit=5) if fetch_cached else self.exchange.fetch_order_book(symbol, limit=5)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid = self._safe_float((bids[0] if bids else [0.0])[0], 0.0)
        ask = self._safe_float((asks[0] if asks else [0.0])[0], 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return 0.0, bid, ask
        midpoint = (bid + ask) / 2.0
        if midpoint <= 0:
            return 0.0, bid, ask
        return ((ask - bid) / midpoint) * 10000.0, bid, ask

    def _entry_orderbook_spread_block_reason(self, symbol: str) -> str:
        strategy = config.STRATEGY
        if not strategy.entry_spread_filter_enabled:
            return ""
        max_spread = max(0.0, self._safe_float(strategy.entry_spread_filter_max_bps, 0.0))
        if max_spread <= 0:
            return ""
        try:
            spread_bps, bid, ask = self._entry_orderbook_spread_bps(symbol)
        except Exception as exc:
            self._log_event(
                "WARNING" if strategy.entry_spread_filter_block_if_unavailable else "DEBUG",
                f"HTX order book spread unavailable for {symbol}: {exc}",
                event="signal_valid",
                symbol=symbol,
                reason="htx_orderbook_spread_unavailable",
                exception=exc,
            )
            return "htx_orderbook_spread_unavailable" if strategy.entry_spread_filter_block_if_unavailable else ""
        if spread_bps <= 0:
            return "htx_orderbook_spread_unavailable" if strategy.entry_spread_filter_block_if_unavailable else ""
        if spread_bps > max_spread:
            return f"htx_orderbook_spread_too_wide;spread_bps={spread_bps:.4f};max_bps={max_spread:.4f};bid={bid:.12f};ask={ask:.12f}"
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
        return "entry_expansion_disabled"

    def _external_price_settings_enabled(self) -> bool:
        return bool(getattr(config.EXTERNAL_PRICE_FEED, "enabled", False) and getattr(self, "external_price_feed", None))

    def _external_price_context(self, symbol: str) -> dict:
        with self._runtime_rlock("_external_price_context_lock"):
            cache = getattr(self, "_external_price_context_cache", None)
            if cache is None:
                cache = {}
                self._external_price_context_cache = cache
            if symbol in cache:
                return dict(cache[symbol])

            def remember(context: dict) -> dict:
                cache[symbol] = dict(context)
                return dict(context)

            if not self._external_price_settings_enabled():
                return remember({"valid": False, "stale": True, "reason": "disabled", "symbol": symbol})
            try:
                tickers = self._bulk_tickers_by_symbol()
                ticker = tickers.get(symbol) if tickers else None
                if not ticker:
                    cache_lookup = getattr(self, "_ticker_from_market_data_cache", None)
                    ticker = cache_lookup(symbol) if cache_lookup else None
                if not ticker:
                    fetch_uncached = getattr(self, "_fetch_ticker_uncached", None)
                    ticker = fetch_uncached(symbol) if fetch_uncached else self.exchange.fetch_ticker(symbol)
                market = self.market_by_symbol.get(symbol) or self.exchange.market(symbol)
                context = self.external_price_feed.get_context(symbol, ticker, market=market)
            except Exception as exc:
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


__all__ = ["SignalFilters"]
