# SPDX-License-Identifier: MIT
# Pons Family - mean-reversion strategy for pons.family
"""Fade the z-score of price against its rolling mean.

Emits only beyond ``z_entry`` so it stays quiet in normal ranges; inside the
band the momentum strategy has the better claim. The score is the negative
z-score normalized by the entry threshold, so a two-sigma stretch is a full
signal and a four-sigma one is still capped at full: extreme readings on a
thin token are more often a repricing than an overshoot, and the cap keeps the
strategy from doubling down on them.
"""

from __future__ import annotations

import numpy as np

from pons_pal.models import PonsSignal
from pons_pal.strategies.base import PonsStrategyContext, Strategy


class MeanReversionStrategy(Strategy):
    """Rolling z-score reversion."""

    name = "mean_reversion"

    def generate(self, ctx: PonsStrategyContext) -> list[PonsSignal]:
        """One signal per pair whose price is beyond the entry band."""
        signals: list[PonsSignal] = []
        lookback = self.config.lookback_bars
        for pair_id in ctx.pairs:
            closes = self.closes(ctx.history.get(pair_id, ()), lookback)
            if closes.size < max(20, lookback // 2):
                continue
            mean = float(np.mean(closes))
            std = float(np.std(closes))
            if std <= 0.0:
                continue
            z = (float(closes[-1]) - mean) / std
            if abs(z) < self.config.z_entry:
                continue
            score = self.clip_score(-z / self.config.z_entry)
            signals.append(
                PonsSignal(
                    strategy=self.name,
                    pair_id=pair_id,
                    score=score,
                    confidence=float(min(1.0, abs(z) / (2.0 * self.config.z_entry))),
                    horizon_s=lookback * 60,
                    rationale=f"z-score {z:+.2f} against a {closes.size}-bar mean",
                    ts=ctx.now,
                )
            )
        return signals
