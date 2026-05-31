with open('htxbot/shared_exchange.py', 'r') as f:
    content = f.read()

diff1 = """        value = self._exchange.fetch_ticker(symbol, params=params or {})
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

    def fetch_funding_rate(self, symbol: str, params=None):"""

old1 = """        value = self._exchange.fetch_ticker(symbol, params=params or {})
        self._ticker_cache[key] = (now, value)
        return value

    def fetch_funding_rate(self, symbol: str, params=None):"""

content = content.replace(old1, diff1)

with open('htxbot/shared_exchange.py', 'w') as f:
    f.write(content)
