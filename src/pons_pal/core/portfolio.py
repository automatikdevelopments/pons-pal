# SPDX-License-Identifier: MIT
# Pons Family - portfolio view and target-book builder for pons.family
"""Turn many strategy signals into a few orders, and keep strategies isolated.

Strategies never see the live book. They receive a deep copy of
``PonsPortfolioView`` and return signals; only the builder turns those into
orders, and only after the risk gate has judged them do they change the book.
A strategy that could reach the book directly could also size around the gate.

Signals are blended per pair by strategy weight and confidence, then damped by
how correlated the pair's returns are with the rest of the universe: five
signals on five tokens that all track the same stock are one bet, and are
sized like one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

import numpy as np

from pons_pal.config import CapitalSection, PortfolioSection, RiskLimits
from pons_pal.models import PonsBar, PonsModel, PonsOrder, PonsPosition, PonsSignal, Side, utcnow


class PonsPortfolioView(PonsModel):
    """A frozen snapshot of the book handed to strategies."""

    equity_usd: float
    cash_usd: float
    positions: dict[str, PonsPosition]

    def position_usd(self, pair_id: str) -> float:
        """Marked value of the position in ``pair_id``, or zero."""
        position = self.positions.get(pair_id)
        return position.mark_usd if position else 0.0

    @property
    def gross_exposure_usd(self) -> float:
        """Sum of every position's mark."""
        return float(sum(p.mark_usd for p in self.positions.values()))


class PortfolioBuilder:
    """Blends signals into target weights and emits the orders that reach them."""

    def __init__(
        self,
        config: PortfolioSection,
        capital: CapitalSection,
        strategy_weights: Mapping[str, float],
        max_slippage_bps: int,
    ) -> None:
        self._config = config
        self._capital = capital
        self._weights = dict(strategy_weights)
        self._max_slippage_bps = max_slippage_bps

    def aggregate(self, signals: Sequence[PonsSignal]) -> dict[str, tuple[float, tuple[str, ...]]]:
        """Weighted-average score per pair, with the contributing strategy names."""
        totals: dict[str, float] = {}
        weights: dict[str, float] = {}
        contributors: dict[str, list[str]] = {}
        for signal in signals:
            weight = self._weights.get(signal.strategy, 1.0) * signal.confidence
            if weight <= 0.0:
                continue
            totals[signal.pair_id] = totals.get(signal.pair_id, 0.0) + weight * signal.score
            weights[signal.pair_id] = weights.get(signal.pair_id, 0.0) + weight
            contributors.setdefault(signal.pair_id, []).append(signal.strategy)
        return {
            pair_id: (
                float(np.clip(totals[pair_id] / weights[pair_id], -1.0, 1.0)),
                tuple(sorted(set(contributors[pair_id]))),
            )
            for pair_id in totals
        }

    def correlation_penalty(
        self, history: Mapping[str, Sequence[PonsBar]], pair_ids: Sequence[str]
    ) -> dict[str, float]:
        """Per-pair divisor in [1, n]: 1 when uncorrelated, larger when it moves with others."""
        lookback = self._config.correlation_lookback_bars
        series: dict[str, np.ndarray] = {}
        for pair_id in pair_ids:
            bars = history.get(pair_id, ())
            closes = np.asarray([bar.close for bar in bars[-lookback:]], dtype=float)
            if closes.size >= 10:
                series[pair_id] = np.diff(np.log(closes))
        penalties = dict.fromkeys(pair_ids, 1.0)
        if len(series) < 2:
            return penalties
        length = min(len(values) for values in series.values())
        keys = list(series)
        matrix = np.vstack([series[key][-length:] for key in keys])
        if length < 5 or np.any(np.std(matrix, axis=1) == 0.0):
            return penalties
        corr = np.corrcoef(matrix)
        if not np.all(np.isfinite(corr)):
            return penalties
        for index, key in enumerate(keys):
            penalties[key] = float(max(1.0, np.sum(np.abs(corr[index]))))
        return penalties

    def build(
        self,
        signals: Sequence[PonsSignal],
        view: PonsPortfolioView,
        history: Mapping[str, Sequence[PonsBar]],
        limits: RiskLimits,
    ) -> list[PonsOrder]:
        """Emit the orders that move the book toward its blended targets.

        Long-only: a negative aggregate on a held pair is an exit, and on an
        unheld pair it is nothing.
        """
        aggregated = self.aggregate(signals)
        penalties = self.correlation_penalty(history, list(aggregated))
        max_position_usd = view.equity_usd * limits.max_position_pct / 100.0
        orders: list[PonsOrder] = []
        for pair_id, (score, strategies) in aggregated.items():
            held = view.position_usd(pair_id)
            damped = score / penalties.get(pair_id, 1.0)
            if score < 0.0 and held > 0.0:
                target = 0.0 if abs(score) >= self._config.signal_floor else held
            elif damped >= self._config.signal_floor:
                target = damped * max_position_usd
            else:
                target = held
            delta = target - held
            if abs(delta) < self._capital.min_order_usd:
                continue
            notional = min(abs(delta), self._capital.per_order_max_usd)
            if delta < 0.0:
                notional = min(abs(delta), held)
            orders.append(
                PonsOrder(
                    order_id=uuid.uuid4().hex,
                    pair_id=pair_id,
                    side=Side.BUY if delta > 0.0 else Side.SELL,
                    notional_usd=notional,
                    max_slippage_bps=self._max_slippage_bps,
                    strategies=strategies,
                    created_at=utcnow(),
                )
            )
        return orders
