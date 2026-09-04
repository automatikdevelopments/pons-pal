# SPDX-License-Identifier: MIT
# Pons Family - strategy interface for pons.family
"""What every strategy receives and what it must return.

A strategy is a pure function of a frozen context: bars, a copy of the
portfolio view, the pair records, the latest stock readings, and blended
sentiment. It returns signals and nothing else. It cannot place orders, touch
the book, or see another strategy's output, which is what makes one bad
strategy a bad signal rather than a bad trade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from pons_pal.config import StrategyConfig
from pons_pal.core.portfolio import PonsPortfolioView
from pons_pal.models import PonsBar, PonsFeedReading, PonsPair, PonsSignal, PonsStockBack

BarHistory = Mapping[str, Sequence[PonsBar]]


@dataclass(frozen=True)
class PonsStrategyContext:
    """Everything a strategy may look at for one cycle."""

    now: datetime
    view: PonsPortfolioView
    pairs: Mapping[str, PonsPair]
    history: BarHistory
    stock_readings: Mapping[str, PonsFeedReading]
    stock_history: Mapping[str, Sequence[float]] = field(default_factory=dict)
    sentiment: Mapping[str, tuple[float, datetime]] = field(default_factory=dict)
    stockback: Mapping[str, PonsStockBack] = field(default_factory=dict)


class Strategy(ABC):
    """Base class. Subclasses implement ``generate`` and set ``name``."""

    name: str = "strategy"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    @abstractmethod
    def generate(self, ctx: PonsStrategyContext) -> list[PonsSignal]:
        """Return zero or more signals for this cycle."""

    @staticmethod
    def closes(bars: Sequence[PonsBar], lookback: int) -> np.ndarray:
        """Close prices for the last ``lookback`` bars as a float array."""
        return np.asarray([bar.close for bar in bars[-lookback:]], dtype=float)

    @staticmethod
    def clip_score(value: float) -> float:
        """Clamp to [-1, 1] and refuse NaN, which would otherwise pass through as a score."""
        if not np.isfinite(value):
            return 0.0
        return float(np.clip(value, -1.0, 1.0))
