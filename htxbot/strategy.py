# -*- coding: utf-8 -*-

from .strategy_entry import EntryStrategy
from .strategy_exit import ExitStrategy
from .strategy_filters import SignalFilters
from .strategy_risk import RiskManager


class StrategyMixin(RiskManager, SignalFilters, ExitStrategy, EntryStrategy):
    """Compatibility facade for the decomposed strategy components."""


__all__ = ["StrategyMixin"]
