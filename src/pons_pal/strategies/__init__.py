# SPDX-License-Identifier: MIT
# Pons Family - strategies package for pons.family
"""Signal generators. Each sees a copy of the book and returns ``PonsSignal`` records."""

from pons_pal.strategies.base import BarHistory, PonsStrategyContext, Strategy
from pons_pal.strategies.event import EventDriftStrategy
from pons_pal.strategies.mean_reversion import MeanReversionStrategy
from pons_pal.strategies.momentum import MomentumStrategy
from pons_pal.strategies.pairs import StatisticalPairsStrategy
from pons_pal.strategies.stockback import StockBackStrategy

__all__ = [
    "BarHistory",
    "EventDriftStrategy",
    "MeanReversionStrategy",
    "MomentumStrategy",
    "PonsStrategyContext",
    "StatisticalPairsStrategy",
    "StockBackStrategy",
    "Strategy",
]
