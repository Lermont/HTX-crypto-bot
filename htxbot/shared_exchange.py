# -*- coding: utf-8 -*-

import time
import threading
from typing import Any, Dict, Tuple


def _timeframe_seconds(timeframe: str) -> int:
    unit = timeframe[-1:] or "m"
    try:
        value = int(timeframe[:-1] or "1")
    except ValueError:
        return 60
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 60 * 60
    if unit == "d":
        return value * 24 * 60 * 60
    return 60


def _cache_key(value: Any):
    if value in (None, {}, [], ()):
        return ()
    if isinstance(value, dict):
        return tuple(sorted((str(key), _cache_key(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_cache_key(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_cache_key(item) for item in value))
    if isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


class CachedMarketDataExchange:
    """Small shared cache for immutable-ish market reads inside one bot process."""

    def __init__(self, exchange, ticker_ttl_sec: float = 1.0, funding_ttl_sec: float = 300.0):
        object.__setattr__(self, "_exchange", exchange)
        object.__setattr__(self, "_ticker_ttl_sec", max(0.0, ticker_ttl_sec))
        object.__setattr__(self, "_funding_ttl_sec", max(0.0, funding_ttl_sec))
        object.__setattr__(self, "_ohlcv_cache", {})
        object.__setattr__(self, "_ohlcv_bucket_by_timeframe", {})
        object.__setattr__(self, "_ohlcv_inflight", {})
        object.__setattr__(self, "_ticker_cache", {})
        object.__setattr__(self, "_funding_cache", {})
        object.__setattr__(self, "_cache_lock", threading.RLock())

    def __getattr__(self, name: str):
        return getattr(self._exchange, name)

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._exchange, name, value)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", since=None, limit=None, params=None):
        timeframe_sec = max(1, _timeframe_seconds(timeframe))
        bucket = int(time.time() // timeframe_sec)
        key = (symbol, timeframe, since, limit, _cache_key(params), bucket)
        cache: Dict[Tuple[Any, ...], Any] = self._ohlcv_cache
        bucket_by_timeframe = self._ohlcv_bucket_by_timeframe
        inflight: Dict[Tuple[Any, ...], dict] = self._ohlcv_inflight
        entry = None
        owner = False
        with self._cache_lock:
            previous_bucket = bucket_by_timeframe.get(timeframe)
            if previous_bucket is not None and previous_bucket != bucket:
                stale_keys = [item for item in cache if item[1] == timeframe and item[-1] != bucket]
                for item in stale_keys:
                    cache.pop(item, None)
                    inflight.pop(item, None)
            bucket_by_timeframe[timeframe] = bucket
            cached = cache.get(key)
            if cached is not None:
                return cached

            entry = inflight.get(key)
            if entry is None:
                entry = {"event": threading.Event(), "value": None, "exception": None}
                inflight[key] = entry
                owner = True

        if not owner:
            entry["event"].wait()
            if entry.get("exception") is not None:
                raise entry["exception"]
            return entry.get("value")

        try:
            value = self._exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=since,
                limit=limit,
                params=params or {},
            )
            if isinstance(value, list):
                with self._cache_lock:
                    cache[key] = value
            return value
        except Exception as exc:
            entry["exception"] = exc
            raise
        finally:
            if entry.get("exception") is None:
                entry["value"] = value
            with self._cache_lock:
                inflight.pop(key, None)
                entry["event"].set()

    def fetch_ticker(self, symbol: str, params=None):
        ttl = self._ticker_ttl_sec
        if ttl <= 0:
            return self._exchange.fetch_ticker(symbol, params=params or {})
        now = time.time()
        key = (symbol, _cache_key(params))
        with self._cache_lock:
            cached = self._ticker_cache.get(key)
            if cached and now - cached[0] <= ttl:
                return cached[1]
            value = self._exchange.fetch_ticker(symbol, params=params or {})
            self._ticker_cache[key] = (now, value)
            return value

    def fetch_tickers(self, symbols=None, params=None):
        ttl = self._ticker_ttl_sec
        now = time.time()
        key_params = _cache_key(params)

        if ttl <= 0:
            return self._exchange.fetch_tickers(symbols, params=params or {})

        if symbols is None:
            return self._exchange.fetch_tickers(symbols, params=params or {})

        with self._cache_lock:
            missing_symbols = []
            result = {}
            for symbol in symbols:
                key = (symbol, key_params)
                cached = self._ticker_cache.get(key)
                if cached and now - cached[0] <= ttl:
                    result[symbol] = cached[1]
                else:
                    missing_symbols.append(symbol)

            if missing_symbols:
                fetched = self._exchange.fetch_tickers(missing_symbols, params=params or {})
                for symbol, value in fetched.items():
                    self._ticker_cache[(symbol, key_params)] = (now, value)
                    result[symbol] = value

            return result

    def _funding_rate_value(self, payload: Any):
        if not isinstance(payload, dict):
            return None
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        for source in (payload, info):
            if not isinstance(source, dict):
                continue
            for key in ("fundingRate", "funding_rate", "rate"):
                if key not in source or source.get(key) in (None, ""):
                    continue
                try:
                    return float(source.get(key))
                except (TypeError, ValueError):
                    continue
        return None

    def fetch_funding_rate(self, symbol: str, params=None):
        ttl = self._funding_ttl_sec
        if ttl <= 0:
            return self._exchange.fetch_funding_rate(symbol, params=params or {})
        now = time.time()
        key = (symbol, _cache_key(params))
        with self._cache_lock:
            cached = self._funding_cache.get(key)
            if cached and now - cached[0] <= ttl:
                return cached[1]
            value = self._exchange.fetch_funding_rate(symbol, params=params or {})
            if self._funding_rate_value(value) is not None:
                self._funding_cache[key] = (now, value)
            return value


__all__ = ["CachedMarketDataExchange"]
