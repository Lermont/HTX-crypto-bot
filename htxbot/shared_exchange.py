# -*- coding: utf-8 -*-

import time
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple


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
        return tuple(
            sorted((str(key), _cache_key(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_cache_key(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_cache_key(item) for item in value))
    if isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _thread_safe_lock(exchange) -> threading.RLock:
    getter = getattr(exchange, "thread_safe_lock", None)
    if callable(getter):
        lock = getter()
        if lock is not None:
            return lock
    lock = getattr(exchange, "_thread_safe_exchange_lock", None)
    if lock is None:
        lock = threading.RLock()
        try:
            setattr(exchange, "_thread_safe_exchange_lock", lock)
        except Exception:
            return threading.RLock()
    return lock


class ThreadSafeExchange:
    """Serialize calls into a synchronous ccxt exchange instance."""

    def __init__(self, exchange, lock=None):
        object.__setattr__(self, "_exchange", exchange)
        object.__setattr__(
            self, "_thread_safe_exchange_lock", lock or _thread_safe_lock(exchange)
        )

    def thread_safe_lock(self):
        return self._thread_safe_exchange_lock

    def unsafe_exchange(self):
        return self._exchange

    def __getattr__(self, name: str):
        value = getattr(self._exchange, name)
        if not callable(value):
            return value

        def locked_call(*args, **kwargs):
            with self._thread_safe_exchange_lock:
                return value(*args, **kwargs)

        return locked_call

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        with self._thread_safe_exchange_lock:
            setattr(self._exchange, name, value)


def ensure_thread_safe_exchange(exchange):
    getter = getattr(exchange, "thread_safe_lock", None)
    if callable(getter):
        return exchange
    return ThreadSafeExchange(exchange)


def _symbol_base(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    if "/" in raw:
        return raw.split("/", 1)[0].strip().lower()
    if "-" in raw:
        return raw.split("-", 1)[0].strip().lower()
    if ":" in raw:
        raw = raw.split(":", 1)[0]
    upper = raw.upper()
    for suffix in ("USDT", "USD"):
        if upper.endswith(suffix) and len(upper) > len(suffix):
            return upper[: -len(suffix)].lower()
    return raw.lower()


class MultiAccountExchange:
    """Route private HTX calls to the API account assigned to each symbol."""

    def __init__(
        self,
        accounts: Dict[str, Any],
        coin_accounts: Dict[str, str],
        default_account: str = "primary",
    ):
        if not accounts:
            raise ValueError("at least one exchange account is required")
        safe_accounts = {
            str(name): ensure_thread_safe_exchange(exchange)
            for name, exchange in accounts.items()
        }
        if default_account not in safe_accounts:
            default_account = next(iter(safe_accounts))
        object.__setattr__(self, "_accounts", safe_accounts)
        object.__setattr__(self, "_default_account", str(default_account))
        object.__setattr__(
            self,
            "_coin_accounts",
            {
                str(coin).strip().lower(): str(account)
                for coin, account in (coin_accounts or {}).items()
            },
        )
        object.__setattr__(self, "_thread_safe_exchange_lock", threading.RLock())

    def thread_safe_lock(self):
        return self._thread_safe_exchange_lock

    def unsafe_exchange(self):
        return self._account(self._default_account)

    def account_names(self) -> Tuple[str, ...]:
        return tuple(self._accounts.keys())

    def account_id_for_symbol(self, symbol: str) -> str:
        base = _symbol_base(symbol)
        account = self._coin_accounts.get(base, self._default_account)
        return account if account in self._accounts else self._default_account

    def set_contract_hostname(self, hostname: str):
        if not hostname:
            return
        for exchange in self._accounts.values():
            urls = getattr(exchange, "urls", None)
            if isinstance(urls, dict):
                urls.setdefault("hostnames", {})["contract"] = hostname

    @property
    def urls(self):
        return getattr(self._account(self._default_account), "urls")

    @property
    def has(self):
        return getattr(self._account(self._default_account), "has")

    @property
    def markets(self):
        return getattr(self._account(self._default_account), "markets")

    @property
    def precisionMode(self):
        return getattr(self._account(self._default_account), "precisionMode", None)

    def __getattr__(self, name: str):
        value = getattr(self._account(self._default_account), name)
        if not callable(value):
            return value

        def default_call(*args, **kwargs):
            return value(*args, **kwargs)

        return default_call

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if name == "markets":
            self.set_markets(value)
            return
        for exchange in self._accounts.values():
            setattr(exchange, name, value)

    def _account(self, name: str):
        return self._accounts.get(str(name), self._accounts[self._default_account])

    def _account_for_symbol(self, symbol: str):
        return self._account(self.account_id_for_symbol(symbol))

    def _call_optional_params(
        self, exchange, method_name: str, *args, params=None, **kwargs
    ):
        method = getattr(exchange, method_name)
        if params is not None:
            try:
                return method(*args, params=params, **kwargs)
            except TypeError:
                return method(*args, **kwargs)
        return method(*args, **kwargs)

    def load_markets(self, reload: bool = False):
        primary = self._account(self._default_account)
        markets = primary.load_markets(reload=reload)
        for name, exchange in self._accounts.items():
            if name == self._default_account:
                continue
            setter = getattr(exchange, "set_markets", None)
            if callable(setter):
                setter(markets)
            else:
                setattr(exchange, "markets", markets)
        return markets

    def set_markets(self, markets):
        for exchange in self._accounts.values():
            setter = getattr(exchange, "set_markets", None)
            if callable(setter):
                setter(markets)
            else:
                setattr(exchange, "markets", markets)

    def market(self, symbol: str):
        return self._account(self._default_account).market(symbol)

    def safe_symbol(self, symbol: str):
        return self._account(self._default_account).safe_symbol(symbol)

    def parse_timeframe(self, timeframe: str):
        return self._account(self._default_account).parse_timeframe(timeframe)

    def price_to_precision(self, symbol: str, price):
        return self._account(self._default_account).price_to_precision(symbol, price)

    def amount_to_precision(self, symbol: str, amount):
        return self._account(self._default_account).amount_to_precision(symbol, amount)

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", since=None, limit=None, params=None
    ):
        return self._account(self._default_account).fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit,
            params=params or {},
        )

    def fetch_ticker(self, symbol: str, params=None):
        return self._call_optional_params(
            self._account(self._default_account), "fetch_ticker", symbol, params=params
        )

    def fetch_tickers(self, symbols=None, params=None):
        return self._call_optional_params(
            self._account(self._default_account),
            "fetch_tickers",
            symbols,
            params=params,
        )

    def fetch_order_book(self, symbol: str, limit=None, params=None):
        return self._call_optional_params(
            self._account(self._default_account),
            "fetch_order_book",
            symbol,
            limit=limit,
            params=params,
        )

    def fetch_funding_rate(self, symbol: str, params=None):
        return self._call_optional_params(
            self._account(self._default_account),
            "fetch_funding_rate",
            symbol,
            params=params,
        )

    def _group_symbols_by_account(self, symbols: Iterable[str]) -> Dict[str, List[str]]:
        grouped: Dict[str, List[str]] = {}
        for symbol in symbols or ():
            account = self.account_id_for_symbol(symbol)
            grouped.setdefault(account, []).append(symbol)
        return grouped

    @staticmethod
    def _payload_identity(payload: Any) -> Tuple[Any, ...]:
        if not isinstance(payload, dict):
            return ("raw", repr(payload))
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        order_id = (
            payload.get("id")
            or info.get("id")
            or info.get("order_id")
            or info.get("orderId")
        )
        symbol = (
            payload.get("symbol")
            or info.get("symbol")
            or info.get("contract_code")
            or info.get("contractCode")
        )
        side = payload.get("side") or info.get("side") or info.get("direction")
        if order_id:
            return ("id", str(order_id), str(symbol or ""), str(side or ""))
        return (
            "shape",
            str(symbol or ""),
            str(side or ""),
            str(payload.get("type") or info.get("order_price_type") or ""),
            str(payload.get("price") or info.get("price") or ""),
            str(
                payload.get("amount")
                or payload.get("contracts")
                or info.get("volume")
                or ""
            ),
        )

    @classmethod
    def _dedupe_payloads(cls, payloads: List[Any]) -> List[Any]:
        seen = set()
        deduped = []
        for payload in payloads:
            key = cls._payload_identity(payload)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(payload)
        return deduped

    def fetch_positions(self, symbols=None, params=None):
        if symbols:
            positions = []
            for account, account_symbols in self._group_symbols_by_account(
                symbols
            ).items():
                positions.extend(
                    self._account(account).fetch_positions(
                        account_symbols, params or {}
                    )
                )
            return positions
        positions = []
        for exchange in self._accounts.values():
            positions.extend(exchange.fetch_positions(symbols, params or {}))
        return self._dedupe_payloads(positions)

    def fetch_open_orders(self, symbol=None, params=None):
        if symbol:
            return self._call_optional_params(
                self._account_for_symbol(symbol),
                "fetch_open_orders",
                symbol,
                params=params or {},
            )
        orders = []
        for exchange in self._accounts.values():
            orders.extend(
                self._call_optional_params(
                    exchange, "fetch_open_orders", None, params=params or {}
                )
            )
        return self._dedupe_payloads(orders)

    def fetch_order(self, order_id, symbol=None, params=None):
        exchange = (
            self._account_for_symbol(symbol)
            if symbol
            else self._account(self._default_account)
        )
        return self._call_optional_params(
            exchange, "fetch_order", order_id, symbol, params=params or {}
        )

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        exchange = (
            self._account_for_symbol(symbol)
            if symbol
            else self._account(self._default_account)
        )
        return self._call_optional_params(
            exchange,
            "fetch_my_trades",
            symbol,
            since=since,
            limit=limit,
            params=params or {},
        )

    def create_order(self, symbol, type, side, amount, price, params=None):
        return self._account_for_symbol(symbol).create_order(
            symbol, type, side, amount, price, params=params or {}
        )

    def cancel_order(self, order_id, symbol, params=None):
        return self._account_for_symbol(symbol).cancel_order(
            order_id, symbol, params=params or {}
        )

    def set_leverage(self, leverage, symbol=None, params=None):
        if symbol:
            return self._account_for_symbol(symbol).set_leverage(
                leverage, symbol, params=params or {}
            )
        result = None
        for exchange in self._accounts.values():
            result = exchange.set_leverage(leverage, symbol, params=params or {})
        return result

    def set_position_mode(self, hedged, symbol=None, params=None):
        if symbol:
            return self._account_for_symbol(symbol).set_position_mode(
                hedged, symbol, params=params or {}
            )
        result = None
        for exchange in self._accounts.values():
            result = exchange.set_position_mode(hedged, symbol, params=params or {})
        return result

    def fetch_balance_for_symbol(self, symbol: str, params=None):
        return self._account(self._default_account).fetch_balance(params or {})

    def fetch_balance(self, params=None):
        return self._account(self._default_account).fetch_balance(params or {})

    def _symbol_from_private_request(self, request: Optional[dict]) -> str:
        if not isinstance(request, dict):
            return ""
        return str(
            request.get("symbol")
            or request.get("contract_code")
            or request.get("contractCode")
            or request.get("market")
            or ""
        )

    def _merge_private_responses(self, responses: List[Any]) -> dict:
        if len(responses) == 1 and isinstance(responses[0], dict):
            return responses[0]
        return {
            "status": "ok",
            "data": [
                response.get("data", response)
                if isinstance(response, dict)
                else response
                for response in responses
            ],
        }

    def contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo(self, request):
        symbol = self._symbol_from_private_request(request)
        if symbol:
            return self._account_for_symbol(
                symbol
            ).contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo(request)
        responses = [
            exchange.contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo(
                request
            )
            for exchange in self._accounts.values()
        ]
        return self._merge_private_responses(responses)

    def contractPrivatePostLinearSwapApiV1SwapCrossAccountInfo(self, request):
        responses = [
            exchange.contractPrivatePostLinearSwapApiV1SwapCrossAccountInfo(request)
            for exchange in self._accounts.values()
            if hasattr(
                exchange, "contractPrivatePostLinearSwapApiV1SwapCrossAccountInfo"
            )
        ]
        return self._merge_private_responses(responses) if responses else None


class CachedMarketDataExchange:
    """Small shared cache for immutable-ish market reads inside one bot process."""

    def __init__(
        self,
        exchange,
        ticker_ttl_sec: float = 1.0,
        funding_ttl_sec: float = 300.0,
        order_book_ttl_sec: float = 1.0,
    ):
        object.__setattr__(self, "_exchange", exchange)
        object.__setattr__(self, "_ticker_ttl_sec", max(0.0, ticker_ttl_sec))
        object.__setattr__(self, "_funding_ttl_sec", max(0.0, funding_ttl_sec))
        object.__setattr__(self, "_order_book_ttl_sec", max(0.0, order_book_ttl_sec))
        object.__setattr__(self, "_exchange_lock", _thread_safe_lock(exchange))
        object.__setattr__(self, "_ohlcv_cache", {})
        object.__setattr__(self, "_ohlcv_bucket_by_timeframe", {})
        object.__setattr__(self, "_ohlcv_inflight", {})
        object.__setattr__(self, "_ticker_cache", {})
        object.__setattr__(self, "_ticker_inflight", {})
        object.__setattr__(self, "_order_book_cache", {})
        object.__setattr__(self, "_order_book_inflight", {})
        object.__setattr__(self, "_funding_cache", {})
        object.__setattr__(self, "_cache_lock", threading.RLock())

    def thread_safe_lock(self):
        return self._exchange_lock

    def __getattr__(self, name: str):
        value = getattr(self._exchange, name)
        if not callable(value):
            return value

        def locked_call(*args, **kwargs):
            with self._exchange_lock:
                return value(*args, **kwargs)

        return locked_call

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            with self._exchange_lock:
                setattr(self._exchange, name, value)

    def _call_exchange(self, method_name: str, *args, **kwargs):
        method = getattr(self._exchange, method_name)
        with self._exchange_lock:
            return method(*args, **kwargs)

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", since=None, limit=None, params=None
    ):
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
                stale_keys = [
                    item
                    for item in cache
                    if item[1] == timeframe and item[-1] != bucket
                ]
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

        value = None
        try:
            value = self._call_exchange(
                "fetch_ohlcv",
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
            return self._call_exchange("fetch_ticker", symbol, params=params or {})
        now = time.time()
        key = (symbol, _cache_key(params))
        entry = None
        owner = False
        with self._cache_lock:
            cached = self._ticker_cache.get(key)
            if cached and now - cached[0] <= ttl:
                return cached[1]
            entry = self._ticker_inflight.get(key)
            if entry is None:
                entry = {"event": threading.Event(), "value": None, "exception": None}
                self._ticker_inflight[key] = entry
                owner = True

        if not owner:
            entry["event"].wait()
            if entry.get("exception") is not None:
                raise entry["exception"]
            return entry.get("value")

        value = None
        try:
            value = self._call_exchange("fetch_ticker", symbol, params=params or {})
            with self._cache_lock:
                self._ticker_cache[key] = (time.time(), value)
            return value
        except Exception as exc:
            entry["exception"] = exc
            raise
        finally:
            if entry.get("exception") is None:
                entry["value"] = value
            with self._cache_lock:
                self._ticker_inflight.pop(key, None)
                entry["event"].set()

    def fetch_tickers(self, symbols=None, params=None):
        ttl = self._ticker_ttl_sec
        now = time.time()
        key_params = _cache_key(params)

        if ttl <= 0:
            return self._call_exchange("fetch_tickers", symbols, params=params or {})

        if symbols is None:
            return self._call_exchange("fetch_tickers", symbols, params=params or {})

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
                fetched = self._call_exchange(
                    "fetch_tickers", missing_symbols, params=params or {}
                )
                for symbol, value in fetched.items():
                    self._ticker_cache[(symbol, key_params)] = (now, value)
                    result[symbol] = value

            return result

    def _fetch_order_book_uncached(self, symbol: str, limit=None, params=None):
        try:
            return self._call_exchange(
                "fetch_order_book", symbol, limit=limit, params=params or {}
            )
        except TypeError:
            return self._call_exchange("fetch_order_book", symbol, limit=limit)

    @staticmethod
    def _valid_order_book_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        return isinstance(payload.get("bids"), list) and isinstance(
            payload.get("asks"), list
        )

    def fetch_order_book(self, symbol: str, limit=None, params=None):
        ttl = self._order_book_ttl_sec
        if ttl <= 0:
            return self._fetch_order_book_uncached(symbol, limit=limit, params=params)

        now = time.time()
        key = (symbol, limit, _cache_key(params))
        entry = None
        owner = False
        with self._cache_lock:
            cached = self._order_book_cache.get(key)
            if cached and now - cached[0] <= ttl:
                return cached[1]
            entry = self._order_book_inflight.get(key)
            if entry is None:
                entry = {"event": threading.Event(), "value": None, "exception": None}
                self._order_book_inflight[key] = entry
                owner = True

        if not owner:
            entry["event"].wait()
            if entry.get("exception") is not None:
                raise entry["exception"]
            return entry.get("value")

        value = None
        try:
            value = self._fetch_order_book_uncached(symbol, limit=limit, params=params)
            if self._valid_order_book_payload(value):
                with self._cache_lock:
                    self._order_book_cache[key] = (time.time(), value)
            return value
        except Exception as exc:
            entry["exception"] = exc
            raise
        finally:
            if entry.get("exception") is None:
                entry["value"] = value
            with self._cache_lock:
                self._order_book_inflight.pop(key, None)
                entry["event"].set()

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
            return self._call_exchange(
                "fetch_funding_rate", symbol, params=params or {}
            )
        now = time.time()
        key = (symbol, _cache_key(params))
        with self._cache_lock:
            cached = self._funding_cache.get(key)
            if cached and now - cached[0] <= ttl:
                return cached[1]
            value = self._call_exchange(
                "fetch_funding_rate", symbol, params=params or {}
            )
            if self._funding_rate_value(value) is not None:
                self._funding_cache[key] = (now, value)
            return value


__all__ = [
    "CachedMarketDataExchange",
    "MultiAccountExchange",
    "ThreadSafeExchange",
    "ensure_thread_safe_exchange",
]
