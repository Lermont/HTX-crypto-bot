with open('htxbot/exchange.py', 'r') as f:
    content = f.read()

diff1 = """
    def _bulk_tickers_by_symbol(self) -> Optional[Dict[str, dict]]:
        if config.RUNTIME.dry_run:
            return {}
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

    def _bulk_positions_by_symbol(self) -> Optional[Dict[str, List[dict]]]:"""

content = content.replace("    def _bulk_positions_by_symbol(self) -> Optional[Dict[str, List[dict]]]:", diff1)

diff2 = """        self._private_open_orders_by_symbol = None
        self._private_tickers_by_symbol = None
        self._private_positions_bulk_failed = False
        self._private_open_orders_bulk_failed = False
        self._private_tickers_bulk_failed = False"""

content = content.replace("        self._private_open_orders_by_symbol = None\n        self._private_positions_bulk_failed = False\n        self._private_open_orders_bulk_failed = False", diff2)

diff3 = """        tickers = self._bulk_tickers_by_symbol()
        ticker = tickers.get(symbol) if tickers else None
        if not ticker:
            ticker = self.exchange.fetch_ticker(symbol)"""

content = content.replace("        ticker = self.exchange.fetch_ticker(symbol)", diff3)

with open('htxbot/exchange.py', 'w') as f:
    f.write(content)
