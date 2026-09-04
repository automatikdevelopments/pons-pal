# SPDX-License-Identifier: MIT
# Pons Family - momentum strategy for pons.family
"""Volatility-scaled trailing return.

The score is the lookback return divided by the lookback volatility, squashed
into [-1, 1]. Scaling by volatility matters more on launchpad tokens than on
equities: a 20% move on a token that moves 20% every hour is noise, and an
unscaled momentum signal would chase exactly the pairs that are most likely to
reverse.
"""

from __future__ import annotations

import numpy as np

from pons_pal.models import PonsSignal
from pons_pal.strategies.base import PonsStrategyContext, Strategy


class MomentumStrategy(Strategy):
    """Trailing-return momentum, volatility scaled."""

    name = "momentum"

    def generate(self, ctx: PonsStrategyContext) -> list[PonsSignal]:
        """One signal per pair with enough history."""
        signals: list[PonsSignal] = []
        lookback = self.config.lookback_bars
        for pair_id in ctx.pairs:
            closes = self.closes(ctx.history.get(pair_id, ()), lookback)
            if closes.size < max(10, lookback // 2):
                continue
            returns = np.diff(np.log(closes))
            vol = float(np.std(returns)) * np.sqrt(returns.size)
            if vol <= 0.0:
                continue
            total = float(np.log(closes[-1] / closes[0]))
            score = self.clip_score(total / (2.0 * vol))
            confidence = float(min(1.0, closes.size / lookback))
            signals.append(
                PonsSignal(
                    strategy=self.name,
                    pair_id=pair_id,
                    score=score,
                    confidence=confidence,
                    horizon_s=lookback * 60,
                    rationale=f"return {total:+.3f} over {closes.size} bars, vol {vol:.3f}",
                    ts=ctx.now,
                )
            )
        return signals
