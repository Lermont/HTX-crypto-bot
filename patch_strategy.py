with open('htxbot/strategy.py', 'r') as f:
    content = f.read()

diff1 = """            tickers = self._bulk_tickers_by_symbol()
            ticker = tickers.get(symbol) if tickers else None
            if not ticker:
                ticker = self.exchange.fetch_ticker(symbol)
            market = self.market_by_symbol.get(symbol) or self.exchange.market(symbol)
            context = self.external_price_feed.get_context(symbol, ticker, market=market)
        except Exception as exc:
            context = {"valid": False, "stale": True, "reason": f"external_price_error:{exc}", "symbol": symbol}
            return remember(context)"""

old1 = """            ticker = self.exchange.fetch_ticker(symbol)
            market = self.market_by_symbol.get(symbol) or self.exchange.market(symbol)
            context = self.external_price_feed.get_context(symbol, ticker, market=market)
        except Exception as exc:
            context = {"valid": False, "stale": True, "reason": f"external_price_error:{exc}", "symbol": symbol}
            cache[symbol] = dict(context)
            return context
            return remember({"valid": False, "stale": True, "reason": f"external_price_error:{exc}", "symbol": symbol})"""

content = content.replace(old1, diff1)

with open('htxbot/strategy.py', 'w') as f:
    f.write(content)
