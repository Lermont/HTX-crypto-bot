# -*- coding: utf-8 -*-

import concurrent.futures
import json
import math
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import ccxt

import config

from .concurrency import instance_rlock
from .shared_exchange import ThreadSafeExchange


class UnexpectedExchangeResponse(RuntimeError):
    """Raised when CCXT returns a payload shape that cannot be trusted."""


class ExchangeMixin:
    def _runtime_rlock(self, name: str):
        lock = getattr(self, name, None)
        if lock is None:
            lock = threading.RLock()
            setattr(self, name, lock)
        return lock

    def _private_cache_runtime_lock(self):
        return self._runtime_rlock("_private_cache_lock")

    def _funding_cache_runtime_lock(self):
        return self._runtime_rlock("_funding_cache_lock")

    def _market_data_cache_runtime_lock(self):
        return self._runtime_rlock("_market_data_cache_lock")

    def _exchange_runtime_lock(self):
        getter = getattr(self.exchange, "thread_safe_lock", None)
        if callable(getter):
            lock = getter()
            if lock is not None:
                return lock
        return instance_rlock(self.exchange, "_thread_safe_exchange_lock")

    def _create_exchange(self):
        if not config.API_CREDENTIALS.api_key or not config.API_CREDENTIALS.api_secret:
            raise ValueError("HTX API credentials are required")

        exchange_config = {
            "enableRateLimit": config.EXCHANGE.enable_rate_limit,
            "timeout": config.EXCHANGE.timeout_ms,
            "options": {
                "defaultType": config.EXCHANGE.default_type,
                "defaultSubType": "linear",
                "fetchMarkets": {
                    "types": {
                        "spot": False,
                        "linear": True,
                        "inverse": False,
                    },
                },
            },
        }
        if config.API_CREDENTIALS.api_key and config.API_CREDENTIALS.api_secret:
            exchange_config["apiKey"] = config.API_CREDENTIALS.api_key
            exchange_config["secret"] = config.API_CREDENTIALS.api_secret

        exchange = ccxt.htx(exchange_config)
        exchange.has["fetchCurrencies"] = False
        self._set_contract_hostname(exchange, self._contract_hostnames()[0])
        return ThreadSafeExchange(exchange)

    def _timeframe_to_seconds(self, timeframe: str) -> int:
        try:
            return int(self.exchange.parse_timeframe(timeframe))
        except Exception:
            return 60

    def _contract_hostnames(self) -> Tuple[str, ...]:
        return tuple(config.EXCHANGE.contract_hostnames or ("api.hbdm.com", "api.hbdm.vn"))

    def _set_contract_hostname(self, exchange, hostname: str):
        if not hostname:
            return
        urls = exchange.urls.setdefault("hostnames", {})
        urls["contract"] = hostname

    def _save_markets_cache(self, markets: dict):
        if not getattr(self, "markets_cache_path", None):
            return
        try:
            payload = {
                "saved_at": time.time(),
                "contract_hostname": (self.exchange.urls.get("hostnames") or {}).get("contract", ""),
                "markets": markets,
            }
            self.markets_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.markets_cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self._log_event(
                "DEBUG",
                f"Could not save futures markets cache: {exc}",
                event="futures_setup",
                reason="markets_cache_save_failed",
            )

    def _load_markets_from_cache(self) -> Optional[dict]:
        markets_cache_path = getattr(self, "markets_cache_path", None)
        if not markets_cache_path or not markets_cache_path.exists():
            return None
        try:
            payload = json.loads(markets_cache_path.read_text(encoding="utf-8"))
            saved_at = self._safe_float(payload.get("saved_at"), 0.0)
            max_age = max(config.EXCHANGE.markets_cache_max_age_sec, 0)
            if max_age and time.time() - saved_at > max_age:
                return None
            markets = payload.get("markets")
            if not isinstance(markets, dict) or not markets:
                return None
            self.exchange.set_markets(markets)
            self._log_event(
                "WARNING",
                f"Using cached HTX futures markets from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(saved_at))}",
                event="futures_setup",
                reason="markets_cache_fallback",
            )
            return self.exchange.markets
        except Exception as exc:
            self._log_event(
                "WARNING",
                f"Could not load futures markets cache: {exc}",
                event="futures_setup",
                reason="markets_cache_load_failed",
            )
            return None

    def _load_markets_with_retry(self, reload: bool = False) -> dict:
        if not reload and self._markets_loaded():
            return getattr(self.exchange, "markets", {}) or {}

        last_exc = None
        retries = max(1, config.EXCHANGE.market_load_retries)
        hostnames = self._contract_hostnames()
        for attempt in range(1, retries + 1):
            for hostname in hostnames:
                try:
                    with self._exchange_runtime_lock():
                        self._set_contract_hostname(self.exchange, hostname)
                        markets = self.exchange.load_markets(reload=reload)
                    self._save_markets_cache(markets)
                    return markets
                except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
                    last_exc = exc
                    self._log_event(
                        "WARNING",
                        f"HTX futures markets load attempt {attempt}/{retries} failed via {hostname}: {exc}",
                        event="futures_setup",
                        reason="load_markets_network_retry",
                    )
            time.sleep(min(2 * attempt, 8))

        cached = self._load_markets_from_cache()
        if cached:
            return cached
        raise last_exc

    def _is_transient_exchange_error(self, exc: Exception) -> bool:
        if not isinstance(exc, (ccxt.RequestTimeout, ccxt.NetworkError)):
            return False
        text = str(exc or "").lower()
        diagnostic_error_code = getattr(self, "_diagnostic_error_code", None)
        error_code = ""
        if diagnostic_error_code:
            try:
                error_code = str(diagnostic_error_code(exc, "") or "").lower()
            except Exception:
                error_code = ""
        non_retryable_codes = {
            "bad-request",
            "bad_request",
            "invalid-parameter",
            "invalid_parameter",
            "invalid-symbol",
            "invalid_symbol",
        }
        if error_code in non_retryable_codes:
            return False
        if any(
            marker in text
            for marker in (
                "invalid-parameter",
                "invalid parameter",
                "invalid-symbol",
                "invalid symbol",
                "bad request",
            )
        ):
            return False
        return True

    def _markets_loaded(self) -> bool:
        markets = getattr(self.exchange, "markets", None)
        return isinstance(markets, dict) and bool(markets)

    def _ensure_markets_loaded(self) -> dict:
        if self._markets_loaded():
            return getattr(self.exchange, "markets", {}) or {}
        return self._load_markets_with_retry()

    def _market(self, symbol: str) -> dict:
        if not symbol:
            raise ValueError("symbol is required")

        market_by_symbol = getattr(self, "market_by_symbol", None)
        if isinstance(market_by_symbol, dict):
            market = market_by_symbol.get(symbol)
            if isinstance(market, dict) and market:
                return market

        self._ensure_markets_loaded()

        market = None
        try:
            market = self.exchange.market(symbol)
        except Exception:
            markets = getattr(self.exchange, "markets", None) or {}
            if isinstance(markets, dict):
                market = markets.get(symbol)
            if not market:
                raise

        if isinstance(market_by_symbol, dict) and isinstance(market, dict) and market:
            market_by_symbol[symbol] = market
        return market

    def _fetch_ohlcv_with_retry(self, symbol: str, timeframe: str = "1m", since=None, limit=None, params=None):
        hostnames = list(self._contract_hostnames())
        current = str((self.exchange.urls.get("hostnames") or {}).get("contract") or "")
        ordered_hostnames = []
        if current:
            ordered_hostnames.append(current)
        ordered_hostnames.extend(hostname for hostname in hostnames if hostname and hostname != current)
        if not ordered_hostnames:
            ordered_hostnames = [""]

        attempts = max(1, config.EXCHANGE.market_load_retries)
        last_exc = None
        for attempt in range(1, attempts + 1):
            hostname = ordered_hostnames[(attempt - 1) % len(ordered_hostnames)]
            try:
                with self._exchange_runtime_lock():
                    if hostname:
                        self._set_contract_hostname(self.exchange, hostname)
                    ohlcv = self.exchange.fetch_ohlcv(
                        symbol,
                        timeframe=timeframe,
                        since=since,
                        limit=limit,
                        params=params or {},
                    )
                    return self._expect_ccxt_list_response(
                        ohlcv,
                        "fetch_ohlcv",
                        symbol=symbol,
                        item_types=(list, tuple),
                    )
            except Exception as exc:
                if not self._is_transient_exchange_error(exc):
                    raise
                last_exc = exc
                if attempt >= attempts:
                    break
                self._log_event(
                    "WARNING",
                    f"Transient HTX public API failure while fetching candles for {symbol}; "
                    f"retry {attempt}/{attempts} via {hostname or 'default'}: {exc}",
                    event="signal_invalid",
                    symbol=symbol,
                    reason="ohlcv_network_retry",
                    exception=exc,
                    retryable=True,
                    attempt=attempt,
                    hostname=hostname or "default",
                )
                time.sleep(min(0.5 * attempt, 2.0))
        raise last_exc

    def _private_fetch_with_retry(self, symbol: str, reason: str, description: str, fetch):
        hostnames = list(self._contract_hostnames())
        current = str((self.exchange.urls.get("hostnames") or {}).get("contract") or "")
        ordered_hostnames = []
        if current:
            ordered_hostnames.append(current)
        ordered_hostnames.extend(hostname for hostname in hostnames if hostname and hostname != current)
        if not ordered_hostnames:
            ordered_hostnames = [""]

        attempts = max(1, config.EXCHANGE.market_load_retries)
        last_exc = None
        for attempt in range(1, attempts + 1):
            hostname = ordered_hostnames[(attempt - 1) % len(ordered_hostnames)]
            try:
                with self._exchange_runtime_lock():
                    if hostname:
                        self._set_contract_hostname(self.exchange, hostname)
                    return fetch()
            except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                self._log_event(
                    "WARNING",
                    f"Transient HTX private API failure while fetching {description}; "
                    f"retry {attempt}/{attempts} via {hostname or 'default'}: {exc}",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    reason=f"{reason}_network_retry",
                    exception=exc,
                    retryable=True,
                    attempt=attempt,
                    hostname=hostname or "default",
                )
                time.sleep(min(0.5 * attempt, 2.0))
        raise last_exc

    def _ccxt_response_preview(self, payload: Any) -> str:
        try:
            sanitizer = getattr(self, "_sanitize_for_log", None)
            clean = sanitizer(payload) if sanitizer else payload
            if isinstance(clean, dict):
                keys = list(clean.keys())[:8]
                clean = {key: clean.get(key) for key in keys}
            elif isinstance(clean, list):
                clean = clean[:3]
            text = json.dumps(clean, ensure_ascii=True, default=str, sort_keys=True)
        except Exception:
            text = repr(payload)
        redactor = getattr(self, "_redact_sensitive_text", None)
        if redactor:
            text = redactor(text)
        if len(text) > 500:
            text = f"{text[:500]}...<truncated>"
        return text

    def _expected_item_type_name(self, item_types: Tuple[type, ...]) -> str:
        return "|".join(item_type.__name__ for item_type in item_types)

    def _is_ccxt_error_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False

        normalized = {str(key).lower().replace("-", "_"): value for key, value in payload.items()}
        status = str(normalized.get("status") or "").strip().lower()
        if status in {"error", "err", "failed", "fail"}:
            return True
        if normalized.get("success") is False:
            return True
        if any(key in normalized for key in ("err_code", "err_msg", "error_code", "error_msg")):
            return True

        error = normalized.get("error")
        if isinstance(error, dict):
            return bool(error)
        if isinstance(error, str):
            return bool(error.strip())
        return bool(error)

    def _expect_ccxt_list_response(
        self,
        payload: Any,
        method: str,
        symbol: str = "",
        item_types: Optional[Tuple[type, ...]] = None,
    ) -> List[Any]:
        location = f" for {symbol}" if symbol else ""
        if not isinstance(payload, list):
            raise UnexpectedExchangeResponse(
                f"{method} returned {type(payload).__name__}; expected list{location}; "
                f"payload={self._ccxt_response_preview(payload)}"
            )
        if item_types:
            for index, item in enumerate(payload):
                if isinstance(item, item_types):
                    if isinstance(item, dict) and self._is_ccxt_error_payload(item):
                        expected = self._expected_item_type_name(item_types)
                        raise UnexpectedExchangeResponse(
                            f"{method} returned list with error dict item at index {index}; "
                            f"expected list[{expected}]{location}; payload={self._ccxt_response_preview(payload)}"
                        )
                    continue
                expected = self._expected_item_type_name(item_types)
                raise UnexpectedExchangeResponse(
                    f"{method} returned list with {type(item).__name__} item at index {index}; "
                    f"expected list[{expected}]{location}; payload={self._ccxt_response_preview(payload)}"
                )
        return payload

    def _price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.exchange.price_to_precision(symbol, price))

    def _price_tick(self, symbol: str) -> float:
        market = self._market(symbol)
        raw_precision = (market.get("precision") or {}).get("price")
        precision = self._safe_float(raw_precision, -1.0)
        precision_mode = getattr(self.exchange, "precisionMode", None)
        fallback_tick = max(abs(self._safe_float(market.get("last"), 1.0)) * 1e-10, 1e-12)
        if precision_mode == ccxt.TICK_SIZE:
            return precision if precision > 0 else fallback_tick
        if precision_mode == ccxt.DECIMAL_PLACES and precision >= 0 and float(precision).is_integer():
            return 10 ** (-int(precision))
        if precision <= 0:
            return fallback_tick
        if precision >= 1 and float(precision).is_integer():
            return 10 ** (-int(precision))
        return precision

    def _price_at_or_above(self, symbol: str, raw_price: float) -> float:
        price = self._price_to_precision(symbol, raw_price)
        if price + 1e-15 >= raw_price:
            return price

        tick = self._price_tick(symbol)
        candidate = price
        for _ in range(20):
            candidate = self._price_to_precision(symbol, candidate + tick)
            if candidate + 1e-15 >= raw_price:
                return candidate
        return self._price_to_precision(symbol, raw_price * (1 + 1e-8))

    def _price_at_or_below(self, symbol: str, raw_price: float) -> float:
        price = self._price_to_precision(symbol, raw_price)
        if price <= raw_price + 1e-15:
            return price

        tick = self._price_tick(symbol)
        candidate = price
        for _ in range(20):
            candidate = self._price_to_precision(symbol, max(tick, candidate - tick))
            if candidate <= raw_price + 1e-15:
                return candidate
        return self._price_to_precision(symbol, max(tick, raw_price * (1 - 1e-8)))

    def _price_band_limit_from_error(self, exc: Exception, side: str) -> float:
        text = str(exc)
        if side == "sell":
            match = re.search(r"Sell price must be higher than\s*([0-9.]+)", text, re.IGNORECASE)
        else:
            match = re.search(r"Buy price must be lower than\s*([0-9.]+)", text, re.IGNORECASE)
        if not match:
            return 0.0
        return self._safe_float(match.group(1), 0.0)

    def _price_inside_htx_band(self, symbol: str, price: float, side: str, limit: float) -> float:
        if limit <= 0:
            return price
        tick = self._price_tick(symbol)
        if side == "sell":
            return self._price_at_or_above(symbol, limit + tick)
        return self._price_at_or_below(symbol, max(tick, limit - tick))

    def _amount_to_precision(self, symbol: str, contracts: float) -> float:
        try:
            amount = float(self.exchange.amount_to_precision(symbol, contracts))
        except ccxt.InvalidOrder:
            return 0.0
        except Exception:
            return 0.0

        min_contracts = self._get_min_contracts(symbol)
        if min_contracts and amount + 1e-12 < min_contracts:
            return 0.0
        return amount

    def _get_min_contracts(self, symbol: str) -> float:
        market = self._market(symbol)
        limits = market.get("limits") or {}
        precision = (market.get("precision") or {}).get("amount")
        candidates = []
        contract_size = self._safe_float(market.get("contractSize"), 1.0) or 1.0
        precision_amount = self._safe_float(precision, 0.0)
        min_amount = self._safe_float((limits.get("amount") or {}).get("min"), 0.0)
        if min_amount > 0:
            min_contracts = min_amount
            if contract_size > 0:
                eps = max(abs(min_amount), abs(contract_size), 1.0) * 1e-12
                if abs(min_amount - contract_size) <= eps or (
                    precision_amount > 0 and min_amount < precision_amount - eps
                ):
                    min_contracts = min_amount / contract_size
            candidates.append(min_contracts)
        if precision_amount > 0 and getattr(self.exchange, "precisionMode", None) == ccxt.TICK_SIZE:
            candidates.append(precision_amount)
        if (
            precision is not None
            and getattr(self.exchange, "precisionMode", None) == ccxt.DECIMAL_PLACES
            and precision_amount >= 0
            and float(precision_amount).is_integer()
        ):
            candidates.append(10 ** (-int(precision_amount)))
        return max(candidates) if candidates else 0.0

    def _contract_size(self, symbol: str) -> float:
        market = self._market(symbol)
        return self._safe_float(market.get("contractSize"), 1.0) or 1.0

    def _contracts_to_notional(self, symbol: str, contracts: float, price: float) -> float:
        return max(0.0, contracts) * self._contract_size(symbol) * max(0.0, price)

    def _contracts_for_notional(self, symbol: str, notional: float, price: float) -> float:
        if price <= 0:
            return 0.0
        raw_contracts = notional / (price * self._contract_size(symbol))
        return self._amount_to_precision(symbol, raw_contracts)

    def _average_price_from_notional(self, symbol: str, contracts: float, notional: float) -> float:
        if contracts <= 0:
            return 0.0
        return notional / (contracts * self._contract_size(symbol))

    def _fetch_reference_price(self, symbol: str) -> Tuple[float, float]:
        tickers = self._bulk_tickers_by_symbol()
        ticker = tickers.get(symbol) if tickers else None
        if not ticker:
            ticker = self._ticker_from_market_data_cache(symbol) or self._fetch_ticker_uncached(symbol)
        bid = self._safe_float(ticker.get("bid"))
        last = self._safe_float(ticker.get("last"))
        ask = self._safe_float(ticker.get("ask"))
        if config.ENTRY_SIDE == "sell":
            reference = ask or last or bid
        else:
            reference = bid or last or ask
        return reference, last or reference

    def _ticker_spread_rate(self, symbol: str) -> float:
        try:
            tickers = self._bulk_tickers_by_symbol()
            ticker = tickers.get(symbol) if tickers else None
            if not ticker:
                ticker = self._ticker_from_market_data_cache(symbol) or self._fetch_ticker_uncached(symbol)
        except Exception as exc:
            self._log_event(
                "DEBUG",
                f"Spread unavailable for {symbol}: {exc}",
                event="exit_ladder_rebuilt",
                symbol=symbol,
                reason="spread_unavailable",
            )
            return 0.0
        bid = self._safe_float(ticker.get("bid"), 0.0)
        ask = self._safe_float(ticker.get("ask"), 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return 0.0
        midpoint = (bid + ask) / 2.0
        if midpoint <= 0:
            return 0.0
        return max(0.0, (ask - bid) / midpoint)

    def _funding_rate_context(self, symbol: str) -> dict:
        strategy = config.STRATEGY
        if not strategy.enable_funding_aware_exit:
            return {"rate": 0.0, "markup_multiplier": 1.0, "reason": "disabled"}

        with self._funding_cache_runtime_lock():
            now = time.time()
            cached = self.funding_cache.get(symbol)
            cached_until = self._safe_float((cached or {}).get("expires_at"), 0.0) if isinstance(cached, dict) else 0.0
            if cached and cached_until > now:
                return dict(cached)
            if (
                cached
                and cached_until <= 0
                and now - self._safe_float(cached.get("ts"), 0.0) < strategy.funding_cache_ttl_sec
            ):
                return dict(cached)

            rate = 0.0
            reason = "unavailable"
            if not self.exchange.has.get("fetchFundingRate"):
                payload = {
                    "rate": 0.0,
                    "markup_multiplier": 1.0,
                    "reason": "funding_rate_unavailable",
                    "valid": False,
                    "ts": now,
                    "expires_at": now + min(max(1.0, self._safe_float(strategy.funding_cache_ttl_sec, 1.0)), 30.0),
                }
                self.funding_cache[symbol] = payload
                return dict(payload)

            try:
                funding = self.exchange.fetch_funding_rate(symbol)
                parsed_rate = self._parse_funding_rate_payload(funding)
                if parsed_rate is None:
                    raise ValueError("funding rate payload is missing a numeric rate")
                rate = parsed_rate
                reason = "neutral"
            except Exception as exc:
                self._log_event(
                    "DEBUG",
                    f"Funding rate unavailable for {symbol}: {exc}",
                    event="signal_invalid",
                    symbol=symbol,
                    reason="funding_rate_unavailable",
                )
                payload = {
                    "rate": 0.0,
                    "markup_multiplier": 1.0,
                    "reason": "funding_rate_unavailable",
                    "valid": False,
                    "ts": now,
                    "expires_at": now + min(max(1.0, self._safe_float(strategy.funding_cache_ttl_sec, 1.0)), 30.0),
                }
                self.funding_cache[symbol] = payload
                return dict(payload)

            markup_multiplier = 1.0
            if config.POSITION_SIDE == "short":
                if rate >= strategy.funding_positive_threshold:
                    markup_multiplier = strategy.funding_negative_markup_multiplier
                    reason = "positive_funding_short_receives"
                elif rate <= strategy.funding_negative_threshold:
                    markup_multiplier = strategy.funding_positive_markup_multiplier
                    reason = "negative_funding_short_pays"
            else:
                if rate >= strategy.funding_positive_threshold:
                    markup_multiplier = strategy.funding_positive_markup_multiplier
                    reason = "positive_funding_long_pays"
                elif rate <= strategy.funding_negative_threshold:
                    markup_multiplier = strategy.funding_negative_markup_multiplier
                    reason = "negative_funding_long_receives"

            payload = {
                "rate": rate,
                "markup_multiplier": markup_multiplier,
                "reason": reason,
                "valid": True,
                "ts": now,
                "expires_at": now + max(0.0, self._safe_float(strategy.funding_cache_ttl_sec, 0.0)),
            }
            self.funding_cache[symbol] = payload
            return dict(payload)

    def _parse_funding_rate_payload(self, funding: dict) -> Optional[float]:
        if not isinstance(funding, dict):
            return None
        info = funding.get("info") if isinstance(funding.get("info"), dict) else {}
        for source in (funding, info):
            if not isinstance(source, dict):
                continue
            for key in ("fundingRate", "funding_rate", "rate"):
                if key not in source or source.get(key) in (None, ""):
                    continue
                try:
                    rate = float(source.get(key))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(rate):
                    return rate
        return None

    def _account_snapshot(self) -> dict:
        entry = None
        owner = False
        with self._private_cache_runtime_lock():
            cached = getattr(self, "_account_snapshot_cache", None)
            if isinstance(cached, dict):
                return dict(cached)
            entry = getattr(self, "_account_snapshot_inflight", None)
            if not isinstance(entry, dict):
                entry = {"event": threading.Event(), "value": None, "exception": None}
                self._account_snapshot_inflight = entry
                owner = True

        if not owner:
            entry["event"].wait()
            if entry.get("exception") is not None:
                raise entry["exception"]
            value = entry.get("value") or {"free": 0.0, "total": 0.0}
            return dict(value) if isinstance(value, dict) else {"free": 0.0, "total": 0.0}

        value = None
        try:
            value = self._fetch_account_snapshot_uncached()
            with self._private_cache_runtime_lock():
                self._account_snapshot_cache = dict(value)
            return dict(value)
        except Exception as exc:
            entry["exception"] = exc
            raise
        finally:
            if entry.get("exception") is None:
                entry["value"] = dict(value) if isinstance(value, dict) else value
            with self._private_cache_runtime_lock():
                if getattr(self, "_account_snapshot_inflight", None) is entry:
                    self._account_snapshot_inflight = None
                entry["event"].set()

    def _fetch_account_snapshot_uncached(self) -> dict:
        try:
            balance = self.exchange.fetch_balance(
                {
                    "type": config.EXCHANGE.default_type,
                    "marginMode": config.RISK.margin_mode,
                }
            )
        except Exception as exc:
            self._log_event(
                "ERROR",
                f"Could not fetch futures balance: {exc}",
                event="margin_error",
                reason="balance_fetch_failed",
                exception=exc,
            )
            return {"free": 0.0, "total": 0.0}

        if not self._ensure_cross_margin_response(balance, context="futures_balance"):
            return {"free": 0.0, "total": 0.0}

        quote = config.EXCHANGE.quote_currency
        free = self._safe_float((balance.get("free") or {}).get(quote), 0.0)
        total = self._safe_float((balance.get("total") or {}).get(quote), 0.0)
        if not free and isinstance(balance.get(quote), dict):
            free = self._safe_float(balance[quote].get("free"), 0.0)
            total = self._safe_float(balance[quote].get("total"), total)

        if not free:
            raw_data = (balance.get("info") or {}).get("data")
            if isinstance(raw_data, list):
                raw_free = 0.0
                raw_total = 0.0
                for item in raw_data:
                    if not isinstance(item, dict):
                        continue
                    item_margin_mode = self._extract_margin_mode(item)
                    if item_margin_mode and item_margin_mode != config.RISK.margin_mode:
                        continue
                    margin_asset = str(item.get("margin_asset") or item.get("symbol") or "").upper()
                    if margin_asset and margin_asset != quote:
                        continue
                    available = self._safe_float(
                        item.get("margin_available", item.get("withdraw_available", item.get("margin_balance", 0.0))),
                        0.0,
                    )
                    equity = self._safe_float(
                        item.get("margin_balance", item.get("margin_static", available)),
                        available,
                    )
                    raw_free += max(0.0, available)
                    raw_total += max(0.0, equity)
                free = raw_free
                total = raw_total or total

        if not free:
            nested_free = 0.0
            nested_total = 0.0
            for value in balance.values():
                if not isinstance(value, dict):
                    continue
                quote_balance = value.get(quote)
                if not isinstance(quote_balance, dict):
                    continue
                item_free = self._safe_float(quote_balance.get("free"), 0.0)
                item_total = self._safe_float(quote_balance.get("total"), item_free)
                nested_free += max(0.0, item_free)
                nested_total += max(0.0, item_total)
            free = nested_free
            total = nested_total or total

        if not total:
            total = free
        return {"free": free, "total": total}

    def _reset_private_caches(self):
        with self._private_cache_runtime_lock():
            self._private_positions_by_symbol = None
            self._private_open_orders_by_symbol = None
            self._private_tickers_by_symbol = None
            self._account_snapshot_cache = None
            self._account_snapshot_inflight = None
            self._private_positions_bulk_failed = False
            self._private_open_orders_bulk_failed = False
            self._private_tickers_bulk_failed = False
            self._external_price_context_cache = {}

    def _reset_market_data_caches(self):
        with self._market_data_cache_runtime_lock():
            self._ticker_cache = {}
            self._ticker_inflight = {}
            self._order_book_cache = {}
            self._order_book_inflight = {}

    def _market_data_cache_ttl_sec(self) -> float:
        try:
            poll_interval = self._safe_float(getattr(config.RUNTIME, "poll_interval_sec", 1.0), 1.0)
        except Exception:
            poll_interval = 1.0
        return max(1.0, min(10.0, poll_interval))

    def _fetch_ticker_uncached(self, symbol: str) -> dict:
        return self.exchange.fetch_ticker(symbol)

    def _ticker_from_market_data_cache(self, symbol: str) -> Optional[dict]:
        now = time.time()
        key = (symbol,)
        ttl = self._market_data_cache_ttl_sec()
        with self._market_data_cache_runtime_lock():
            cache = getattr(self, "_ticker_cache", None)
            if not isinstance(cache, dict):
                return None
            cached = cache.get(key)
            if cached and now - self._safe_float(cached[0], 0.0) <= ttl:
                value = cached[1]
                return dict(value) if isinstance(value, dict) else None
        return None

    def _cached_ticker(self, symbol: str) -> dict:
        now = time.time()
        key = (symbol,)
        ttl = self._market_data_cache_ttl_sec()
        entry = None
        owner = False

        with self._market_data_cache_runtime_lock():
            cache = getattr(self, "_ticker_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._ticker_cache = cache
            inflight = getattr(self, "_ticker_inflight", None)
            if not isinstance(inflight, dict):
                inflight = {}
                self._ticker_inflight = inflight

            cached = cache.get(key)
            if cached and now - self._safe_float(cached[0], 0.0) <= ttl:
                return dict(cached[1])

            entry = inflight.get(key)
            if entry is None:
                entry = {"event": threading.Event(), "value": None, "exception": None}
                inflight[key] = entry
                owner = True

        if not owner:
            entry["event"].wait()
            if entry.get("exception") is not None:
                raise entry["exception"]
            value = entry.get("value") or {}
            return dict(value) if isinstance(value, dict) else {}

        value = None
        try:
            value = self._fetch_ticker_uncached(symbol)
            if isinstance(value, dict) and not self._is_ccxt_error_payload(value):
                with self._market_data_cache_runtime_lock():
                    self._ticker_cache[key] = (time.time(), dict(value))
            return value
        except Exception as exc:
            entry["exception"] = exc
            raise
        finally:
            if entry.get("exception") is None:
                entry["value"] = dict(value) if isinstance(value, dict) else value
            with self._market_data_cache_runtime_lock():
                inflight = getattr(self, "_ticker_inflight", {})
                if isinstance(inflight, dict):
                    inflight.pop(key, None)
                entry["event"].set()

    def _ticker_prefetch_symbols(self) -> List[str]:
        symbols = []
        seen = set()
        for symbol in list(getattr(self, "symbols", []) or []):
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def _prefetch_ticker_snapshots(self, symbols: Optional[List[str]] = None) -> Dict[str, dict]:
        if not hasattr(self.exchange, "fetch_ticker"):
            return {}

        prefetch_symbols = list(symbols) if symbols is not None else self._ticker_prefetch_symbols()
        if not prefetch_symbols:
            return {}

        max_workers_resolver = getattr(self, "_market_data_max_workers", None)
        if max_workers_resolver:
            max_workers = max_workers_resolver()
        else:
            try:
                max_workers = int(getattr(config.RUNTIME, "market_data_max_workers", 1) or 1)
            except (TypeError, ValueError):
                max_workers = 1
            max_workers = max(1, max_workers)
        max_workers = min(max_workers, len(prefetch_symbols))
        profile = getattr(self, "profile", None) or config.current_profile()

        def fetch_ticker_safe(symbol: str):
            try:
                with config.use_profile(profile):
                    return symbol, self._cached_ticker(symbol), None
            except Exception as exc:
                return symbol, None, exc

        if max_workers <= 1 or len(prefetch_symbols) <= 1:
            results = [fetch_ticker_safe(symbol) for symbol in prefetch_symbols]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(fetch_ticker_safe, prefetch_symbols))

        tickers: Dict[str, dict] = {}
        for symbol, ticker, exc in results:
            if exc is not None:
                self._log_event(
                    "DEBUG",
                    f"HTX ticker prefetch failed for {symbol}: {exc}",
                    event="market_data_prefetch",
                    symbol=symbol,
                    reason="ticker_prefetch_failed",
                    exception=exc,
                    retryable=getattr(self, "_is_transient_exchange_error", lambda _exc: False)(exc),
                )
                continue
            if isinstance(ticker, dict) and ticker:
                tickers[symbol] = dict(ticker)

        if tickers:
            with self._private_cache_runtime_lock():
                cached = getattr(self, "_private_tickers_by_symbol", None)
                if not isinstance(cached, dict):
                    cached = {}
                cached.update(tickers)
                self._private_tickers_by_symbol = cached
        return tickers

    def _fetch_order_book_uncached(self, symbol: str, limit: int = 5):
        try:
            return self.exchange.fetch_order_book(symbol, limit=limit)
        except TypeError:
            return self.exchange.fetch_order_book(symbol)

    def _cached_order_book(self, symbol: str, limit: int = 5) -> dict:
        now = time.time()
        key = (symbol, int(limit or 0))
        ttl = self._market_data_cache_ttl_sec()
        entry = None
        owner = False

        with self._market_data_cache_runtime_lock():
            cache = getattr(self, "_order_book_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._order_book_cache = cache
            inflight = getattr(self, "_order_book_inflight", None)
            if not isinstance(inflight, dict):
                inflight = {}
                self._order_book_inflight = inflight

            cached = cache.get(key)
            if cached and now - self._safe_float(cached[0], 0.0) <= ttl:
                return cached[1]

            entry = inflight.get(key)
            if entry is None:
                entry = {"event": threading.Event(), "value": None, "exception": None}
                inflight[key] = entry
                owner = True

        if not owner:
            entry["event"].wait()
            if entry.get("exception") is not None:
                raise entry["exception"]
            return entry.get("value") or {}

        value = None
        try:
            value = self._fetch_order_book_uncached(symbol, limit=limit)
            if isinstance(value, dict) and isinstance(value.get("bids"), list) and isinstance(value.get("asks"), list):
                with self._market_data_cache_runtime_lock():
                    self._order_book_cache[key] = (time.time(), value)
            return value
        except Exception as exc:
            entry["exception"] = exc
            raise
        finally:
            if entry.get("exception") is None:
                entry["value"] = value
            with self._market_data_cache_runtime_lock():
                inflight = getattr(self, "_order_book_inflight", {})
                if isinstance(inflight, dict):
                    inflight.pop(key, None)
                entry["event"].set()

    def _order_book_prefetch_symbols(self) -> List[str]:
        symbols = []
        seen = set()
        for symbol in list(getattr(self, "symbols", []) or []):
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def _prefetch_market_data_snapshots(self):
        symbols = self._order_book_prefetch_symbols()
        if not symbols:
            return

        exchange_has = getattr(self.exchange, "has", {}) or {}
        if not exchange_has.get("fetchTickers"):
            self._prefetch_ticker_snapshots(symbols)

        strategy = config.STRATEGY
        if not getattr(strategy, "entry_spread_filter_enabled", False):
            return
        max_spread = max(0.0, self._safe_float(getattr(strategy, "entry_spread_filter_max_bps", 0.0), 0.0))
        if max_spread <= 0 or not hasattr(self.exchange, "fetch_order_book"):
            return

        max_workers_resolver = getattr(self, "_market_data_max_workers", None)
        if max_workers_resolver:
            max_workers = max_workers_resolver()
        else:
            try:
                max_workers = int(getattr(config.RUNTIME, "market_data_max_workers", 1) or 1)
            except (TypeError, ValueError):
                max_workers = 1
            max_workers = max(1, max_workers)
        max_workers = min(max_workers, len(symbols))
        profile = getattr(self, "profile", None) or config.current_profile()

        def fetch_order_book_safe(symbol: str):
            try:
                with config.use_profile(profile):
                    self._cached_order_book(symbol, limit=5)
                return symbol, None
            except Exception as exc:
                return symbol, exc

        if max_workers <= 1 or len(symbols) <= 1:
            results = [fetch_order_book_safe(symbol) for symbol in symbols]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(fetch_order_book_safe, symbols))

        for symbol, exc in results:
            if exc is None:
                continue
            self._log_event(
                "DEBUG",
                f"HTX order book prefetch failed for {symbol}: {exc}",
                event="market_data_prefetch",
                symbol=symbol,
                reason="order_book_prefetch_failed",
                exception=exc,
                retryable=getattr(self, "_is_transient_exchange_error", lambda _exc: False)(exc),
            )

    def _payload_symbol(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""

        for source in (payload, payload.get("info") if isinstance(payload.get("info"), dict) else {}):
            for key in ("symbol", "market", "contract_code", "contractCode"):
                value = source.get(key)
                if value:
                    raw_symbol = str(value)
                    if raw_symbol in self.market_by_symbol:
                        return raw_symbol
                    for known_symbol, market in self.market_by_symbol.items():
                        market_id = str((market or {}).get("id") or "")
                        if market_id and raw_symbol == market_id:
                            return known_symbol
                    try:
                        return self.exchange.safe_symbol(raw_symbol)
                    except Exception:
                        return raw_symbol
        return ""

    def _group_payloads_by_symbol(self, payloads: List[dict]) -> Tuple[Dict[str, List[dict]], bool]:
        grouped: Dict[str, List[dict]] = {}
        missing_symbol = False
        for payload in payloads or []:
            symbol = self._payload_symbol(payload)
            if not symbol:
                missing_symbol = True
                continue
            grouped.setdefault(symbol, []).append(payload)
        return grouped, missing_symbol


    def _bulk_tickers_by_symbol(self) -> Optional[Dict[str, dict]]:
        with self._private_cache_runtime_lock():
            cached = getattr(self, "_private_tickers_by_symbol", None)
            if cached is not None:
                return cached
            if getattr(self, "_private_tickers_bulk_failed", False):
                return None
            if not self.exchange.has.get("fetchTickers"):
                self._private_tickers_bulk_failed = True
                return None

            try:
                tickers = self._private_fetch_with_retry(
                    "",
                    "bulk_tickers_fetch_failed",
                    "bulk tickers",
                    lambda: self.exchange.fetch_tickers(list(self.symbols)),
                )
            except Exception as exc:
                self._private_tickers_bulk_failed = True
                self._log_event(
                    "DEBUG",
                    f"Bulk tickers fetch unavailable; falling back to per-symbol sync: {exc}",
                    event="state_exchange_mismatch",
                    reason="bulk_tickers_fetch_failed_fallback",
                    exception=exc,
                )
                return None

            if isinstance(tickers, dict):
                self._private_tickers_by_symbol = tickers
                return tickers

            self._private_tickers_bulk_failed = True
            return None

    def _bulk_positions_by_symbol(self) -> Optional[Dict[str, List[dict]]]:
        with self._private_cache_runtime_lock():
            cached = getattr(self, "_private_positions_by_symbol", None)
            if cached is not None:
                return cached
            if getattr(self, "_private_positions_bulk_failed", False):
                return None
            if not self.exchange.has.get("fetchPositions"):
                self._private_positions_bulk_failed = True
                return None

            try:
                positions = self._private_fetch_with_retry(
                    "",
                    "bulk_positions_fetch_failed",
                    "bulk positions",
                    lambda: self.exchange.fetch_positions(list(self.symbols), self._position_params()),
                )
                positions = self._expect_ccxt_list_response(
                    positions,
                    "fetch_positions",
                    item_types=(dict,),
                )
            except Exception as exc:
                self._private_positions_bulk_failed = True
                self._log_event(
                    "DEBUG",
                    f"Bulk positions fetch unavailable; falling back to per-symbol sync: {exc}",
                    event="state_exchange_mismatch",
                    reason="bulk_positions_fetch_failed",
                    exception=exc,
                )
                return None

            grouped, missing_symbol = self._group_payloads_by_symbol(positions)
            if positions and missing_symbol:
                self._private_positions_bulk_failed = True
                self._log_event(
                    "DEBUG",
                    "Bulk positions response contains payloads without symbols; falling back to per-symbol sync",
                    event="state_exchange_mismatch",
                    reason="bulk_positions_missing_symbol_fallback",
                )
                return None
            self._private_positions_by_symbol = grouped
            return grouped

    def _bulk_open_orders_by_symbol(self) -> Optional[Dict[str, List[dict]]]:
        with self._private_cache_runtime_lock():
            cached = getattr(self, "_private_open_orders_by_symbol", None)
            if cached is not None:
                return cached
            if getattr(self, "_private_open_orders_bulk_failed", False):
                return None
            if not self.exchange.has.get("fetchOpenOrders"):
                self._private_open_orders_bulk_failed = True
                return None

            def fetch_bulk_open_orders():
                return self.exchange.fetch_open_orders(None, params=self._position_params())

            try:
                orders = self._private_fetch_with_retry(
                    "",
                    "bulk_open_orders_fetch_failed",
                    "bulk open orders",
                    fetch_bulk_open_orders,
                )
                orders = self._expect_ccxt_list_response(
                    orders,
                    "fetch_open_orders",
                    item_types=(dict,),
                )
            except Exception as exc:
                self._private_open_orders_bulk_failed = True
                self._log_event(
                    "DEBUG",
                    f"Bulk open-orders fetch unavailable; falling back to per-symbol sync: {exc}",
                    event="state_exchange_mismatch",
                    reason="bulk_open_orders_fetch_failed",
                    exception=exc,
                )
                return None

            grouped, missing_symbol = self._group_payloads_by_symbol(orders)
            if orders and missing_symbol:
                self._private_open_orders_bulk_failed = True
                self._log_event(
                    "DEBUG",
                    "Bulk open-orders response contains payloads without symbols; falling back to per-symbol sync",
                    event="state_exchange_mismatch",
                    reason="bulk_open_orders_missing_symbol_fallback",
                )
                return None
            self._private_open_orders_by_symbol = grouped
            return grouped

    def _prefetch_private_snapshots(self):
        self._bulk_positions_by_symbol()
        self._bulk_open_orders_by_symbol()
        if self._bulk_tickers_by_symbol() is None:
            self._prefetch_ticker_snapshots()

    def _position_params(self) -> dict:
        return {"marginMode": config.RISK.margin_mode}

    def _account_leverage_from_payload(self, symbol: str, payload: dict) -> float:
        if not isinstance(payload, dict):
            return 0.0

        market = self._market(symbol)
        market_id = str((market or {}).get("id") or "")

        def item_matches(item: dict) -> bool:
            if not market_id:
                return True
            for key in ("contract_code", "contractCode", "pair", "symbol"):
                if str(item.get(key) or "") == market_id:
                    return True
            return False

        def item_leverage(item: dict) -> float:
            if not isinstance(item, dict):
                return 0.0
            for key in ("lever_rate", "leverRate", "leverage"):
                leverage = self._safe_float(item.get(key), 0.0)
                if leverage > 0:
                    return leverage
            info = item.get("info") if isinstance(item.get("info"), dict) else {}
            for key in ("lever_rate", "leverRate", "leverage"):
                leverage = self._safe_float(info.get(key), 0.0)
                if leverage > 0:
                    return leverage
            return 0.0

        data = payload.get("data", payload)
        containers = [data]
        if isinstance(data, dict):
            for key in ("positions", "contract_detail", "futures_contract_detail"):
                values = data.get(key)
                if isinstance(values, list):
                    containers.extend(values)
        if isinstance(data, list):
            containers.extend(data)

        fallback = 0.0
        for item in containers:
            if not isinstance(item, dict):
                continue
            leverage = item_leverage(item)
            if leverage <= 0:
                continue
            fallback = fallback or leverage
            if item_matches(item):
                return leverage
        return fallback

    def _fetch_account_order_leverage(self, symbol: str) -> float:
        configured = self._safe_float(getattr(config.RISK, "account_leverage", 0.0), 0.0)
        if configured > 0:
            return configured

        if not hasattr(self, "order_leverage_cache"):
            self.order_leverage_cache = {}

        cached = self.order_leverage_cache.get(symbol)
        if cached and cached > 0:
            return cached

        state = self._get_state(symbol)
        state_leverage = self._safe_float(state.leverage, 0.0)
        if state.position_size > 0 and state_leverage > 0:
            self.order_leverage_cache[symbol] = state_leverage
            return state_leverage

        method = getattr(self.exchange, "contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo", None)
        if not method:
            self._log_event(
                "ERROR",
                f"Could not read manual HTX leverage for {symbol}: raw account-position endpoint is unavailable",
                event="entry_order_canceled",
                symbol=symbol,
                reason="manual_account_leverage_unavailable",
            )
            return 0.0

        try:
            market = self._market(symbol)
            payload = {
                "contract_code": market.get("id") or symbol,
                "margin_account": config.EXCHANGE.quote_currency,
            }
            response = method(payload)
        except Exception as exc:
            self._log_event(
                "ERROR",
                f"Could not read manual HTX leverage for {symbol}: {exc}",
                event="entry_order_canceled",
                symbol=symbol,
                reason="manual_account_leverage_unavailable",
                exception=exc,
            )
            return 0.0

        leverage = self._account_leverage_from_payload(symbol, response)
        if leverage <= 0:
            self._log_event(
                "ERROR",
                f"Could not determine manual HTX leverage for {symbol}; live order is blocked",
                event="entry_order_canceled",
                symbol=symbol,
                reason="manual_account_leverage_missing",
            )
            return 0.0

        self.order_leverage_cache[symbol] = leverage
        return leverage

    def _order_params(self, reduce_only: bool = False, post_only: bool = False, leverage: Optional[float] = None) -> dict:
        lever_rate = self._safe_float(leverage, 0.0)
        if lever_rate <= 0:
            raise RuntimeError("manual_account_leverage_unavailable")

        params = {
            "marginMode": config.RISK.margin_mode,
            "hedged": False,
        }
        if lever_rate > 0:
            params["leverRate"] = int(lever_rate) if lever_rate.is_integer() else lever_rate
        if post_only:
            params["postOnly"] = True
        if reduce_only:
            params["reduceOnly"] = True
        return params

    def _extract_margin_mode(self, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        for key in ("marginMode", "margin_mode", "margin"):
            val = item.get(key)
            if val is not None:
                return str(val).lower()
        info = item.get("info")
        if isinstance(info, dict):
            for key in ("margin_mode", "marginMode", "margin"):
                val = info.get(key)
                if val is not None:
                    return str(val).lower()
        return ""

    def _ensure_cross_margin_response(self, payload: dict, context: str, symbol: str = "") -> bool:
        if config.RISK.margin_mode != "cross":
            self._log_event(
                "ERROR",
                "Bot is configured for non-cross margin; trading is blocked",
                event="futures_setup",
                symbol=symbol,
                reason="non_cross_margin_config",
            )
            return False

        raw_data = (payload.get("info") or {}).get("data") if isinstance(payload, dict) else None
        modes = []
        if isinstance(raw_data, list):
            for item in raw_data:
                mode = self._extract_margin_mode(item)
                if mode:
                    modes.append(mode)
        elif isinstance(raw_data, dict):
            mode = self._extract_margin_mode(raw_data)
            if mode:
                modes.append(mode)

        if modes and not any(mode == "cross" for mode in modes):
            self._log_event(
                "ERROR",
                f"Unexpected non-cross margin response while reading {context}",
                event="state_exchange_mismatch",
                symbol=symbol,
                reason=f"{context}_not_cross_margin",
            )
            return False
        return True

    def _is_hedge_mode_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "hedge mode currently" in text or "one-way mode" in text or '"err_code":1499' in text

    def _is_high_leverage_risk_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            '"err_code":1206' in text
            or "high risk exposure" in text
            or "high leverage is not supported" in text
        )

    def _is_reduce_only_amount_exceeds_closeable_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            '"err_code":1492' in text
            or "amount of reduce only order exceeds" in text
            or "amount available to close" in text
        )

    def _is_position_mode_locked_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            '"err_code":1494' in text
            or '"err_code":1493' in text
            or "position mode cannot be adjusted for existing positions" in text
            or "position mode cannot be adjusted for open orders" in text
        )

    def _position_mode_values_from_payload(self, payload) -> List[str]:
        values: List[str] = []
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized_key = str(key).lower()
                if normalized_key in {"position_mode", "positionmode"} and value:
                    values.append(str(value))
                else:
                    values.extend(self._position_mode_values_from_payload(value))
        elif isinstance(payload, list):
            for item in payload:
                values.extend(self._position_mode_values_from_payload(item))
        return values

    def _position_mode_is_one_way_value(self, value: str) -> Optional[bool]:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized in {"single_side", "single", "one_way", "oneway"}:
            return True
        if normalized in {"dual_side", "dual", "hedge", "hedged"}:
            return False
        return None

    def _fetch_current_position_mode_is_one_way(self) -> Tuple[Optional[bool], str]:
        endpoints = [
            (
                "cross_account_position_info",
                getattr(self.exchange, "contractPrivatePostLinearSwapApiV1SwapCrossAccountPositionInfo", None),
            ),
            (
                "cross_account_info",
                getattr(self.exchange, "contractPrivatePostLinearSwapApiV1SwapCrossAccountInfo", None),
            ),
        ]
        last_reason = "position_mode_query_unavailable"
        request = {"margin_account": config.EXCHANGE.quote_currency}
        for endpoint_name, method in endpoints:
            if not method:
                continue
            try:
                response = method(request)
            except Exception as exc:
                last_reason = f"{endpoint_name}_failed:{exc}"
                continue

            values = self._position_mode_values_from_payload(response)
            parsed = [self._position_mode_is_one_way_value(value) for value in values]
            known = [value for value in parsed if value is not None]
            if known:
                if all(known):
                    return True, f"{endpoint_name}:position_mode=single_side"
                return False, f"{endpoint_name}:position_mode={','.join(values)}"
            last_reason = f"{endpoint_name}:position_mode_missing"
        return None, last_reason

    def _ensure_one_way_position_mode(self, force: bool = False) -> bool:
        if self.one_way_mode_checked and not force:
            return True

        try:
            if config.RISK.margin_mode == "cross":
                self.exchange.set_position_mode(False, None, params=self._position_params())
            else:
                def update_mode(symbol):
                    self.exchange.set_position_mode(False, symbol, params=self._position_params())

                max_workers = min(10, max(1, len(self.symbols)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(update_mode, symbol) for symbol in self.symbols]
                    for future in concurrent.futures.as_completed(futures):
                        future.result()
        except Exception as exc:
            if self._is_position_mode_locked_error(exc):
                mode_is_one_way, mode_reason = self._fetch_current_position_mode_is_one_way()
                if mode_is_one_way is not True:
                    self._log_event(
                        "ERROR",
                        "HTX position mode switch is locked by existing positions or open orders, "
                        "and current one-way mode could not be confirmed",
                        event="futures_setup",
                        reason=(
                            "position_mode_locked_unverified"
                            if mode_is_one_way is None
                            else "position_mode_locked_hedge_mode"
                        ),
                        exception=exc,
                    )
                    return False
                self.one_way_mode_checked = True
                self._log_event(
                    "INFO",
                    "HTX position mode switch skipped because existing positions or open orders "
                    f"lock mode changes; confirmed one-way mode; continuing with one-way order parameters: {exc}",
                    event="futures_setup",
                    reason=f"position_mode_existing_positions;{mode_reason}",
                )
                return True

            self._log_event(
                "ERROR",
                f"Could not switch HTX futures account to one-way mode: {exc}",
                event="futures_setup",
                reason="position_mode_setup_failed",
            )
            return False

        self.one_way_mode_checked = True
        self._log_event(
            "INFO",
            "HTX futures account position mode is set to one-way",
            event="futures_setup",
            reason="position_mode_one_way",
        )
        return True

    def _set_leverage_safe(self, symbol: str, leverage: int) -> bool:
        try:
            self.exchange.set_leverage(int(leverage), symbol, params=self._position_params())
            if not hasattr(self, "order_leverage_cache"):
                self.order_leverage_cache = {}
            self.order_leverage_cache[symbol] = float(leverage)
            return True
        except Exception as exc:
            self._log_event(
                "WARNING",
                f"Could not set leverage {leverage} for {symbol}: {exc}",
                event="futures_setup",
                symbol=symbol,
                reason="set_leverage_failed",
                exception=exc,
            )
            return False

    def _create_one_way_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
        leverage: Optional[float] = None,
        extra_params: Optional[dict] = None,
    ) -> dict:
        if leverage is None:
            leverage = self._fetch_account_order_leverage(symbol)
        params = self._order_params(reduce_only=reduce_only, post_only=post_only, leverage=leverage)
        if extra_params:
            params.update(dict(extra_params))
        try:
            return self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params,
            )
        except Exception as exc:
            if not self._is_hedge_mode_error(exc):
                raise

            self._log_event(
                "WARNING",
                f"HTX account is still in hedge mode while placing {side} {symbol}; switching to one-way and retrying",
                event="futures_setup",
                symbol=symbol,
                side=side,
                reason="hedge_mode_retry_one_way",
                exception=exc,
            )
            if not self._ensure_one_way_position_mode(force=True):
                raise

            return self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params,
            )

    def _fetch_position_snapshot(self, symbol: str) -> dict:
        snapshot = {
            "ok": True,
            "long_size": 0.0,
            "long_available": 0.0,
            "long_frozen": 0.0,
            "long_entry_price": 0.0,
            "long_unrealized_pnl": 0.0,
            "short_size": 0.0,
            "short_available": 0.0,
            "short_frozen": 0.0,
            "short_entry_price": 0.0,
            "short_unrealized_pnl": 0.0,
            "entry_price": 0.0,
            "unrealized_pnl": 0.0,
            "margin_mode": "",
            "leverage": 0.0,
        }
        bulk_positions = self._bulk_positions_by_symbol()
        if bulk_positions is not None:
            positions = bulk_positions.get(symbol, [])
        else:
            try:
                positions = self._private_fetch_with_retry(
                    symbol,
                    "position_fetch_failed",
                    f"position for {symbol}",
                    lambda: self.exchange.fetch_positions([symbol], self._position_params()),
                )
                positions = self._expect_ccxt_list_response(
                    positions,
                    "fetch_positions",
                    symbol=symbol,
                    item_types=(dict,),
                )
            except Exception as exc:
                level = "WARNING" if self._is_transient_exchange_error(exc) else "ERROR"
                self._log_event(
                    level,
                    f"Could not fetch position for {symbol}: {exc}",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    reason="position_fetch_failed",
                    exception=exc,
                )
                snapshot["ok"] = False
                return snapshot

        def available_and_frozen(position: dict, contracts: float) -> Tuple[float, float]:
            info = position.get("info") if isinstance(position.get("info"), dict) else {}
            available = None
            for source in (position, info):
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
                        available = self._safe_float(source.get(key), 0.0)
                        break
                if available is not None:
                    break
            if available is None:
                available = contracts

            frozen = None
            for source in (position, info):
                if not isinstance(source, dict):
                    continue
                for key in ("frozen", "frozenPosition", "frozen_position", "frozen_volume"):
                    if key in source and source.get(key) is not None:
                        frozen = self._safe_float(source.get(key), 0.0)
                        break
                if frozen is not None:
                    break
            if frozen is None:
                frozen = max(0.0, contracts - available)

            return max(0.0, min(contracts, available)), max(0.0, frozen)

        for position in positions or []:
            contracts = self._safe_float(position.get("contracts"), 0.0)
            if contracts <= 0:
                continue
            position_margin_mode = self._extract_margin_mode(position)
            if position_margin_mode and position_margin_mode != config.RISK.margin_mode:
                self._log_event(
                    "ERROR",
                    f"Skipping non-cross position returned for {symbol}",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    reason="position_margin_mode_mismatch",
                )
                continue
            side = (position.get("side") or "").lower()
            if side not in {"long", "short"}:
                continue

            old_size = snapshot[f"{side}_size"]
            old_entry_notional = self._contracts_to_notional(symbol, old_size, snapshot[f"{side}_entry_price"])
            add_entry = self._safe_float(position.get("entryPrice"), 0.0)
            add_notional = self._contracts_to_notional(symbol, contracts, add_entry)
            available, frozen = available_and_frozen(position, contracts)
            snapshot[f"{side}_size"] += contracts
            snapshot[f"{side}_available"] += available
            snapshot[f"{side}_frozen"] += frozen
            snapshot[f"{side}_entry_price"] = self._average_price_from_notional(
                symbol,
                snapshot[f"{side}_size"],
                old_entry_notional + add_notional,
            )
            snapshot[f"{side}_unrealized_pnl"] += self._safe_float(position.get("unrealizedPnl"), 0.0)

            if side == config.POSITION_SIDE:
                snapshot["entry_price"] = snapshot[f"{side}_entry_price"]
                snapshot["unrealized_pnl"] = snapshot[f"{side}_unrealized_pnl"]
                snapshot["margin_mode"] = position.get("marginMode") or ""
                snapshot["leverage"] = self._safe_float(position.get("leverage"), 0.0)

        return snapshot

    def _fetch_open_orders(self, symbol: str) -> Optional[List[dict]]:
        bulk_orders = self._bulk_open_orders_by_symbol()
        if bulk_orders is not None:
            orders = bulk_orders.get(symbol, [])
        else:
            def fetch_symbol_open_orders():
                return self.exchange.fetch_open_orders(symbol, params=self._position_params())

            try:
                orders = self._private_fetch_with_retry(
                    symbol,
                    "open_orders_fetch_failed",
                    f"open orders for {symbol}",
                    fetch_symbol_open_orders,
                )
                orders = self._expect_ccxt_list_response(
                    orders,
                    "fetch_open_orders",
                    symbol=symbol,
                    item_types=(dict,),
                )
            except Exception as exc:
                level = "WARNING" if self._is_transient_exchange_error(exc) else "ERROR"
                self._log_event(
                    level,
                    f"Could not fetch open orders for {symbol}: {exc}",
                    event="state_exchange_mismatch",
                    symbol=symbol,
                    reason="open_orders_fetch_failed",
                    exception=exc,
                )
                return None

        wrong_mode_orders = []
        for order in orders or []:
            order_margin_mode = self._extract_margin_mode(order)
            if order_margin_mode and order_margin_mode != config.RISK.margin_mode:
                wrong_mode_orders.append(order)
        if wrong_mode_orders:
            self._log_event(
                "ERROR",
                f"Open orders response included non-cross orders for {symbol}; skipping symbol",
                event="state_exchange_mismatch",
                symbol=symbol,
                reason="open_order_margin_mode_mismatch",
            )
            return None

        return orders

    def _cancel_order_ref(self, symbol: str, ref: dict, event: str, reason: str) -> bool:
        order_id = str(ref.get("id", ""))
        if not order_id:
            return False

        try:
            params = self._position_params()
            cancel_params = ref.get("cancel_params")
            if isinstance(cancel_params, dict):
                params.update(cancel_params)
            self.exchange.cancel_order(order_id, symbol, params=params)
        except ccxt.OrderNotFound:
            pass
        except Exception as exc:
            self._log_event(
                "WARNING",
                f"Cancel failed for {symbol} order {order_id}: {exc}",
                event="state_exchange_mismatch",
                symbol=symbol,
                order_id=order_id,
                reason="cancel_failed",
                exception=exc,
            )
            return False

        self._record_signal_analytics(
            "order_canceled",
            symbol=symbol,
            signal={"ts": ref.get("signal_ts"), "strategy_name": "ema_pullback"},
            block_reason=reason,
            operation_id=str(ref.get("operation_id") or self._operation_id("order_cancel", symbol=symbol, order_id=order_id)),
            order_id=order_id,
            cycle_id=str(ref.get("cycle_id") or getattr(self._get_state(symbol), "cycle_id", "")),
            context={
                "side": ref.get("side", ""),
                "price": self._safe_float(ref.get("price"), 0.0),
                "amount": self._safe_float(ref.get("amount"), 0.0),
            },
        )
        self._log_event(
            "INFO",
            f"Order canceled for {symbol}: {order_id}",
            event=event,
            symbol=symbol,
            side=ref.get("side", ""),
            order_id=order_id,
            price=self._safe_float(ref.get("price"), 0.0),
            amount=self._safe_float(ref.get("amount"), 0.0),
            reason=reason,
        )
        return True

    def _cancel_entry_orders(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        remaining = []
        for ref in list(state.entry_orders):
            side = str(ref.get("side") or config.ENTRY_SIDE).lower()
            if not self._cancel_order_ref(symbol, ref, event=f"{side}_order_canceled", reason=reason):
                remaining.append(ref)
        state.entry_orders = remaining
        self._refresh_active_side(state)
        self._save_state()

    def _cancel_sell_orders(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        remaining = []
        for ref in list(state.sell_ladder_orders):
            side = str(ref.get("side") or config.EXIT_SIDE).lower()
            if not self._cancel_order_ref(symbol, ref, event=f"{side}_order_canceled", reason=reason):
                remaining.append(ref)
        state.sell_ladder_orders = remaining
        state.sell_ladder_signature = ""
        self._clear_pending_exit_ladder(state)
        self._refresh_active_side(state)
        self._save_state()

    def _cancel_hard_stop_order(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        ref = dict(state.hard_stop_order or {})
        if not ref:
            return
        side = str(ref.get("side") or config.EXIT_SIDE).lower()
        if not self._cancel_order_ref(symbol, ref, event=f"{side}_order_canceled", reason=reason):
            return
        state.hard_stop_order = {}
        state.hard_stop_signature = ""
        self._refresh_active_side(state)
        self._save_state()

    def _cancel_exchange_orders(self, symbol: str, orders: List[dict], side: Optional[str], reason: str) -> bool:
        all_canceled = True
        for order in orders:
            order_side = (order.get("side") or "").lower()
            if side and order_side != side:
                continue
            ref = {
                "id": order.get("id"),
                "side": order.get("side"),
                "price": self._safe_float(order.get("price"), 0.0),
                "amount": self._safe_float(order.get("amount"), 0.0),
            }
            cancel_params = order.get("bot_cancel_params") or order.get("cancel_params")
            if isinstance(cancel_params, dict):
                ref["cancel_params"] = dict(cancel_params)
            event = "buy_order_canceled" if order_side == "buy" else "sell_order_canceled"
            if not self._cancel_order_ref(symbol, ref, event=event, reason=reason):
                all_canceled = False
        return all_canceled

    def _cancel_all_orders(self, symbol: str, reason: str):
        state = self._get_state(symbol)
        entry_refs = list(state.entry_orders)
        sell_refs = list(state.sell_ladder_orders)
        hard_stop_ref = dict(state.hard_stop_order or {})
        canceled_all = True
        for ref in entry_refs:
            side = str(ref.get("side") or config.ENTRY_SIDE).lower()
            if not self._cancel_order_ref(symbol, ref, event=f"{side}_order_canceled", reason=reason):
                canceled_all = False
        for ref in sell_refs:
            side = str(ref.get("side") or config.EXIT_SIDE).lower()
            if not self._cancel_order_ref(symbol, ref, event=f"{side}_order_canceled", reason=reason):
                canceled_all = False
        if hard_stop_ref:
            side = str(hard_stop_ref.get("side") or config.EXIT_SIDE).lower()
            if not self._cancel_order_ref(symbol, hard_stop_ref, event=f"{side}_order_canceled", reason=reason):
                canceled_all = False
        if not canceled_all:
            return
        state.entry_orders = []
        state.sell_ladder_orders = []
        state.sell_ladder_signature = ""
        state.hard_stop_order = {}
        state.hard_stop_signature = ""
        self._clear_pending_exit_ladder(state)
        self._refresh_active_side(state)
        self._save_state()

    def _find_futures_symbol(self, coin: str) -> Optional[str]:
        base = coin.upper()
        exact = f"{base}/{config.EXCHANGE.quote_currency}:USDT"
        market = self.exchange.markets.get(exact)
        if market and market.get("linear") and (market.get("swap") or market.get("future")):
            return exact

        candidates = []
        for symbol, market in self.exchange.markets.items():
            if market.get("base") != base:
                continue
            if market.get("quote") != config.EXCHANGE.quote_currency:
                continue
            if market.get("settle") != config.EXCHANGE.quote_currency:
                continue
            if not market.get("linear"):
                continue
            if not (market.get("swap") or market.get("future")):
                continue
            candidates.append((0 if market.get("swap") else 1, symbol))
        candidates.sort()
        return candidates[0][1] if candidates else None

    def _find_futures_base_quote_symbol(self, base: str, quote: str) -> Optional[str]:
        base = str(base or "").upper()
        quote = str(quote or "").upper()
        if not base or not quote:
            return None

        exact_candidates = (
            f"{base}/{quote}:USDT",
            f"{base}/{quote}",
        )
        for symbol in exact_candidates:
            market = self.exchange.markets.get(symbol)
            if market and market.get("base") == base and market.get("quote") == quote:
                if market.get("linear") and (market.get("swap") or market.get("future")):
                    return symbol

        candidates = []
        for symbol, market in self.exchange.markets.items():
            if market.get("base") != base or market.get("quote") != quote:
                continue
            if not (market.get("swap") or market.get("future")):
                continue
            candidates.append((0 if market.get("linear") else 1, 0 if market.get("swap") else 1, symbol))
        candidates.sort()
        return candidates[0][2] if candidates else None

    def _create_public_spot_exchange(self):
        exchange = ccxt.htx(
            {
                "enableRateLimit": config.EXCHANGE.enable_rate_limit,
                "timeout": config.EXCHANGE.timeout_ms,
                "options": {
                    "defaultType": "spot",
                    "fetchMarkets": {
                        "types": {
                            "spot": True,
                            "linear": False,
                            "inverse": False,
                        },
                    },
                },
            }
        )
        exchange.has["fetchCurrencies"] = False
        return ThreadSafeExchange(exchange)

    def _spot_exchange(self):
        if getattr(self, "macro_spot_exchange", None) is None:
            self.macro_spot_exchange = self._create_public_spot_exchange()
        return self.macro_spot_exchange

    def _load_spot_markets_for_macro(self) -> dict:
        spot = self._spot_exchange()
        if getattr(spot, "markets", None):
            return spot.markets
        return spot.load_markets()

    def _find_spot_base_quote_symbol(self, base: str, quote: str) -> Optional[str]:
        base = str(base or "").upper()
        quote = str(quote or "").upper()
        if not base or not quote:
            return None

        try:
            markets = self._load_spot_markets_for_macro()
        except Exception as exc:
            self._log_event(
                "WARNING",
                f"Spot macro markets unavailable: {exc}",
                event="macro_context_unavailable",
                reason="spot_markets_unavailable",
            )
            return None

        exact = f"{base}/{quote}"
        market = markets.get(exact)
        if market and market.get("base") == base and market.get("quote") == quote:
            return exact

        candidates = []
        for symbol, market in markets.items():
            if market.get("base") == base and market.get("quote") == quote:
                candidates.append(symbol)
        candidates.sort()
        return candidates[0] if candidates else None

    def _macro_gold_coin_candidates(self) -> Tuple[str, ...]:
        aliases = {
            "xault": ("xault", "xaut"),
            "xaut": ("xaut", "xault"),
        }
        candidates = []
        seen = set()
        for coin in config.MACRO.gold_coins:
            normalized = str(coin or "").strip().lower()
            for item in aliases.get(normalized, (normalized,)):
                if item and item not in seen:
                    seen.add(item)
                    candidates.append(item)
        return tuple(candidates)

    def _find_macro_gold_symbol(self) -> Optional[str]:
        self.macro_gold_is_spot = False
        for coin in self._macro_gold_coin_candidates():
            futures_symbol = self._find_futures_base_quote_symbol(coin, config.EXCHANGE.quote_currency)
            if futures_symbol:
                return futures_symbol

        for coin in self._macro_gold_coin_candidates():
            spot_symbol = self._find_spot_base_quote_symbol(coin, config.EXCHANGE.quote_currency)
            if spot_symbol:
                self.macro_gold_is_spot = True
                return spot_symbol
        return None

    def _find_direct_gold_btc_symbol(self) -> Optional[str]:
        self.macro_direct_gold_btc_is_spot = False
        if not config.MACRO.use_direct_gold_btc_pair:
            return None

        configured = str(config.MACRO.direct_gold_btc_symbol or "").strip()
        if configured:
            market = self.exchange.markets.get(configured)
            if market and (market.get("swap") or market.get("future")):
                return configured
            try:
                spot_markets = self._load_spot_markets_for_macro()
            except Exception:
                spot_markets = {}
            if configured in spot_markets:
                self.macro_direct_gold_btc_is_spot = True
                return configured

        for coin in self._macro_gold_coin_candidates():
            futures_symbol = self._find_futures_base_quote_symbol(coin, "BTC")
            if futures_symbol:
                return futures_symbol

        for coin in self._macro_gold_coin_candidates():
            spot_symbol = self._find_spot_base_quote_symbol(coin, "BTC")
            if spot_symbol:
                self.macro_direct_gold_btc_is_spot = True
                return spot_symbol
        return None

    def _setup_futures_account(self):
        if config.RISK.position_mode != "one-way":
            self._log_event(
                "ERROR",
                "Only one-way position mode is supported",
                event="futures_setup",
                reason="unsupported_position_mode",
            )
            raise RuntimeError("Only one-way position mode is supported")

        if config.RISK.margin_mode != "cross":
            self._log_event(
                "ERROR",
                "Only futures cross margin mode is supported for this test",
                event="futures_setup",
                reason="non_cross_margin_config",
            )
            raise RuntimeError("Only futures cross margin mode is supported")

        if config.EXCHANGE.set_position_mode_on_start:
            if not self._ensure_one_way_position_mode(force=True):
                raise RuntimeError("Could not enforce HTX one-way position mode")

        if config.EXCHANGE.set_leverage_on_start:
            self._apply_configured_leverage_on_start()

    def _apply_configured_leverage_on_start(self) -> bool:
        leverage = int(config.RISK.leverage)
        failed_symbols = []
        for symbol in self.symbols:
            if self._set_leverage_safe(symbol, leverage):
                continue
            failed_symbols.append(symbol)

        if failed_symbols:
            preview = ", ".join(failed_symbols[:10])
            suffix = "" if len(failed_symbols) <= 10 else f", +{len(failed_symbols) - 10} more"
            self._log_event(
                "WARNING",
                (
                    f"Configured HTX leverage {leverage} could not be applied to "
                    f"{len(failed_symbols)} of {len(self.symbols)} tracked symbols "
                    f"({preview}{suffix}); startup will continue and orders will use "
                    "the readable/manual account leverage"
                ),
                event="futures_setup",
                reason="set_leverage_partial_failure",
                diagnostic_context={
                    "configured_leverage": leverage,
                    "failed_symbols": failed_symbols,
                    "tracked_symbol_count": len(self.symbols),
                },
            )
            return False
        return True
