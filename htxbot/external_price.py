# -*- coding: utf-8 -*-

import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Tuple


@dataclass(frozen=True)
class BookTicker:
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    ts: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return 0.0
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0:
            return 0.0
        return ((self.ask - self.bid) / mid) * 10000.0


class MexcBookTickerClient:
    def __init__(self, timeout_sec: float = 3.0, opener: Optional[Callable] = None):
        self.timeout_sec = max(0.1, float(timeout_sec or 3.0))
        self.opener = opener or urllib.request.urlopen

    def fetch(self, symbol: str) -> BookTicker:
        query = urllib.parse.urlencode({"symbol": symbol})
        url = f"https://api.mexc.com/api/v3/ticker/bookTicker?{query}"
        with self.opener(url, timeout=self.timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return BookTicker(
            bid=_safe_float(payload.get("bidPrice")),
            ask=_safe_float(payload.get("askPrice")),
            bid_qty=_safe_float(payload.get("bidQty")),
            ask_qty=_safe_float(payload.get("askQty")),
            ts=time.time(),
        )


class ExternalPriceFeed:
    def __init__(self, settings, mexc_client: Optional[MexcBookTickerClient] = None, clock: Optional[Callable[[], float]] = None):
        self.settings = settings
        self.clock = clock or time.time
        self.mexc_client = mexc_client or MexcBookTickerClient(timeout_sec=getattr(settings, "rest_timeout_sec", 3.0))
        self.cache: Dict[str, dict] = {}
        self.history: Dict[str, Deque[dict]] = defaultdict(deque)
        self.errors: Dict[str, str] = {}

    def htx_symbol_to_mexc(self, htx_symbol: str, market: Optional[dict] = None) -> str:
        base = ""
        if isinstance(market, dict):
            base = str(market.get("base") or "")
        if not base and htx_symbol:
            base = str(htx_symbol).split("/", 1)[0]
        base = "".join(ch for ch in base.upper() if ch.isalnum())
        return f"{base}USDT" if base else ""

    def get_context(self, symbol: str, htx_ticker: dict, market: Optional[dict] = None) -> dict:
        now = self.clock()
        if not getattr(self.settings, "enabled", False):
            return self._empty_context(symbol, now, "disabled")

        htx_book = self._book_from_htx(htx_ticker, now)
        if not self._book_valid(htx_book, require_qty=False):
            return self._empty_context(symbol, now, "htx_book_invalid")

        mexc_symbol = self.htx_symbol_to_mexc(symbol, market)
        if not mexc_symbol:
            return self._empty_context(symbol, now, "mexc_symbol_unavailable")

        mexc_book, reason = self._mexc_book(symbol, mexc_symbol, now)
        if not self._book_valid(mexc_book, require_qty=True):
            context = self._context_from_books(symbol, mexc_symbol, htx_book, mexc_book, now, reason or "mexc_book_invalid")
            context["valid"] = False
            context["stale"] = True
            return context

        context = self._context_from_books(symbol, mexc_symbol, htx_book, mexc_book, now, "ok")
        self._record_history(symbol, context, now)
        return context

    def _mexc_book(self, symbol: str, mexc_symbol: str, now: float) -> Tuple[BookTicker, str]:
        cached = self.cache.get(symbol)
        poll_interval = max(0.0, float(getattr(self.settings, "rest_poll_interval_sec", 1.0) or 0.0))
        if cached and now - _safe_float(cached.get("fetched_at"), 0.0) < poll_interval:
            return cached["book"], cached.get("reason", "cached")
        try:
            book = self.mexc_client.fetch(mexc_symbol)
            if book.ts <= 0:
                book = BookTicker(book.bid, book.ask, book.bid_qty, book.ask_qty, now)
            self.cache[symbol] = {"book": book, "fetched_at": now, "reason": "ok"}
            self.errors.pop(symbol, None)
            return book, "ok"
        except Exception as exc:
            self.errors[symbol] = str(exc)
            if cached:
                return cached["book"], "mexc_fetch_failed_cached"
            return BookTicker(ts=0.0), "mexc_fetch_failed"

    def _context_from_books(self, symbol: str, mexc_symbol: str, htx: BookTicker, mexc: BookTicker, now: float, reason: str) -> dict:
        htx_mid = htx.mid
        mexc_mid = mexc.mid
        spread_bps = ((htx_mid / mexc_mid) - 1.0) * 10000.0 if htx_mid > 0 and mexc_mid > 0 else 0.0
        age_ms = int(max(0.0, now - mexc.ts) * 1000) if mexc.ts > 0 else 10**12
        age_limits = [
            int(value)
            for value in (
                getattr(self.settings, "stale_after_ms", 3000) or 0,
                getattr(self.settings, "max_price_age_ms", 3000) or 0,
            )
            if int(value) > 0
        ]
        max_age = min(age_limits) if age_limits else 0
        stale = age_ms > max_age if max_age > 0 else False
        internal_ok = self._internal_spreads_ok(htx, mexc)
        valid = bool(htx_mid > 0 and mexc_mid > 0 and internal_ok and not stale)
        stats = self._rolling_stats(symbol, now, spread_bps, htx_mid, mexc_mid)
        return {
            "valid": valid,
            "stale": stale,
            "reason": reason if internal_ok else "internal_spread_too_wide",
            "symbol": symbol,
            "mexc_symbol": mexc_symbol,
            "ts": now,
            "htx_bid": htx.bid,
            "htx_ask": htx.ask,
            "htx_mid": htx_mid,
            "mexc_bid": mexc.bid,
            "mexc_ask": mexc.ask,
            "mexc_mid": mexc_mid,
            "mexc_bid_qty": mexc.bid_qty,
            "mexc_ask_qty": mexc.ask_qty,
            "spread_bps": spread_bps,
            "age_ms": age_ms,
            **stats,
        }

    def _record_history(self, symbol: str, context: dict, now: float) -> None:
        history = self.history[symbol]
        history.append(
            {
                "ts": now,
                "spread_bps": context.get("spread_bps", 0.0),
                "htx_mid": context.get("htx_mid", 0.0),
                "mexc_mid": context.get("mexc_mid", 0.0),
            }
        )
        cutoff = now - 600.0
        while history and history[0]["ts"] < cutoff:
            history.popleft()

    def _rolling_stats(self, symbol: str, now: float, spread_bps: float, htx_mid: float, mexc_mid: float) -> dict:
        history = list(self.history.get(symbol, ()))
        def avg(window: float) -> float:
            values = [item["spread_bps"] for item in history if item["ts"] >= now - window]
            if not values:
                return spread_bps
            return sum(values) / len(values)
        values_10m = [item["spread_bps"] for item in history if item["ts"] >= now - 600.0]
        if len(values_10m) >= 2:
            mean = sum(values_10m) / len(values_10m)
            variance = sum((item - mean) ** 2 for item in values_10m) / len(values_10m)
            std = math.sqrt(max(0.0, variance))
            zscore = (spread_bps - mean) / std if std > 1e-12 else 0.0
        else:
            zscore = 0.0
        return {
            "spread_bps_now": spread_bps,
            "spread_bps_30s_avg": avg(30.0),
            "spread_bps_2m_avg": avg(120.0),
            "spread_bps_10m_avg": avg(600.0),
            "spread_bps_zscore": zscore,
            "htx_change_30s_bps": self._change_bps(history, now, "htx_mid", htx_mid, 30.0),
            "mexc_change_30s_bps": self._change_bps(history, now, "mexc_mid", mexc_mid, 30.0),
            "htx_change_1m_bps": self._change_bps(history, now, "htx_mid", htx_mid, 60.0),
            "mexc_change_1m_bps": self._change_bps(history, now, "mexc_mid", mexc_mid, 60.0),
        }

    def _change_bps(self, history: list, now: float, key: str, current: float, window: float) -> float:
        if current <= 0:
            return 0.0
        candidates = [item for item in history if item["ts"] <= now - window and item.get(key, 0.0) > 0]
        if candidates:
            previous = candidates[-1][key]
        elif history and history[0].get(key, 0.0) > 0:
            previous = history[0][key]
        else:
            return 0.0
        return ((current / previous) - 1.0) * 10000.0

    def _book_from_htx(self, ticker: dict, now: float) -> BookTicker:
        return BookTicker(
            bid=_safe_float(ticker.get("bid")),
            ask=_safe_float(ticker.get("ask")),
            bid_qty=_safe_float(ticker.get("bidVolume", ticker.get("bidQty"))),
            ask_qty=_safe_float(ticker.get("askVolume", ticker.get("askQty"))),
            ts=now,
        )

    def _book_valid(self, book: BookTicker, require_qty: bool) -> bool:
        if book.mid <= 0:
            return False
        if require_qty:
            bid_notional = book.bid * book.bid_qty
            ask_notional = book.ask * book.ask_qty
            if bid_notional < max(0.0, getattr(self.settings, "min_valid_bid_qty_usdt", 0.0)):
                return False
            if ask_notional < max(0.0, getattr(self.settings, "min_valid_ask_qty_usdt", 0.0)):
                return False
        return True

    def _internal_spreads_ok(self, htx: BookTicker, mexc: BookTicker) -> bool:
        max_spread = max(0.0, float(getattr(self.settings, "max_internal_spread_bps", 0.0) or 0.0))
        if max_spread <= 0:
            return True
        return htx.spread_bps <= max_spread and mexc.spread_bps <= max_spread

    def _empty_context(self, symbol: str, now: float, reason: str) -> dict:
        return {
            "valid": False,
            "stale": True,
            "reason": reason,
            "symbol": symbol,
            "mexc_symbol": "",
            "ts": now,
            "htx_bid": 0.0,
            "htx_ask": 0.0,
            "htx_mid": 0.0,
            "mexc_bid": 0.0,
            "mexc_ask": 0.0,
            "mexc_mid": 0.0,
            "mexc_bid_qty": 0.0,
            "mexc_ask_qty": 0.0,
            "spread_bps": 0.0,
            "age_ms": 10**12,
            "spread_bps_now": 0.0,
            "spread_bps_30s_avg": 0.0,
            "spread_bps_2m_avg": 0.0,
            "spread_bps_10m_avg": 0.0,
            "spread_bps_zscore": 0.0,
            "htx_change_30s_bps": 0.0,
            "mexc_change_30s_bps": 0.0,
            "htx_change_1m_bps": 0.0,
            "mexc_change_1m_bps": 0.0,
        }


def _safe_float(value, default=0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = ["BookTicker", "ExternalPriceFeed", "MexcBookTickerClient"]
