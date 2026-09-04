# SPDX-License-Identifier: MIT
# Pons Family - event-drift strategy for pons.family
"""Lean into fresh retail sentiment on the paired stock, and let it decay.

Robinhood retail attention is the demand side of a token-stock pair: the
stock-back flywheel needs traders, and traders follow the names they are
talking about. The score is the blended sentiment on the paired stock decayed
by its age with the configured half-life, so a burst of attention an hour old
counts for half of a burst right now and a day-old one counts for nothing.
"""

from __future__ import annotations

import math

from pons_pal.models import PonsSignal
from pons_pal.strategies.base import PonsStrategyContext, Strategy


class EventDriftStrategy(Strategy):
    """Time-decayed sentiment drift on the paired stock."""

    name = "event"

    def generate(self, ctx: PonsStrategyContext) -> list[PonsSignal]:
        """One signal per pair whose stock has a recent sentiment reading."""
        signals: list[PonsSignal] = []
        half_life = float(self.config.half_life_s)
        for pair_id, pair in ctx.pairs.items():
            reading = ctx.sentiment.get(pair.stock.symbol)
            if reading is None:
                continue
            score, ts = reading
            age = max(0.0, (ctx.now - ts).total_seconds())
            decay = math.pow(0.5, age / half_life)
            if decay < 0.05:
                continue
            signals.append(
                PonsSignal(
                    strategy=self.name,
                    pair_id=pair_id,
                    score=self.clip_score(score * decay),
                    confidence=float(decay),
                    horizon_s=int(half_life * 2),
                    rationale=f"sentiment {score:+.2f} on {pair.stock.symbol}, {age:.0f}s old",
                    ts=ctx.now,
                )
            )
        return signals
