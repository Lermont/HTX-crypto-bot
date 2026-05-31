# -*- coding: utf-8 -*-

import time
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
        object.__setattr__(self, "_ticker_cache", {})
        object.__setattr__(self, "_funding_cache", {})

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
        previous_bucket = bucket_by_timeframe.get(timeframe)
        if previous_bucket is not None and previous_bucket != bucket:
            stale_keys = [item for item in cache if item[1] == timeframe and item[-1] != bucket]
            for item in stale_keys:
                cache.pop(item, None)
        bucket_by_timeframe[timeframe] = bucket
        if key not in cache:
            cache[key] = self._exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=since,
                limit=limit,
                params=params or {},
            )
        return cache[key]

    def fetch_ticker(self, symbol: str, params=None):
        ttl = self._ticker_ttl_sec
        if ttl <= 0:
            return self._exchange.fetch_ticker(symbol, params=params or {})
        now = time.time()
        key = (symbol, _cache_key(params))
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

    def fetch_funding_rate(self, symbol: str, params=None):
        ttl = self._funding_ttl_sec
        if ttl <= 0:
            return self._exchange.fetch_funding_rate(symbol, params=params or {})
        now = time.time()
        key = (symbol, _cache_key(params))
        cached = self._funding_cache.get(key)
        if cached and now - cached[0] <= ttl:
            return cached[1]
        value = self._exchange.fetch_funding_rate(symbol, params=params or {})
        self._funding_cache[key] = (now, value)
        return value


__all__ = ["CachedMarketDataExchange"]
