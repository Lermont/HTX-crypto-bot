with open('htxbot/exchange.py', 'r') as f:
    content = f.read()

diff1 = """        try:
            tickers = self._bulk_tickers_by_symbol()
            ticker = tickers.get(symbol) if tickers else None
            if not ticker:
                ticker = self.exchange.fetch_ticker(symbol)
        except Exception as exc:"""

old1 = """        try:
            tickers = self._bulk_tickers_by_symbol()
        ticker = tickers.get(symbol) if tickers else None
        if not ticker:
            ticker = self.exchange.fetch_ticker(symbol)
        except Exception as exc:"""

content = content.replace(old1, diff1)

with open('htxbot/exchange.py', 'w') as f:
    f.write(content)
