# SPDX-License-Identifier: MIT
# Pons Family - the Pons-native stock-back strategy for pons.family
"""Favor pairs that pay you back in a stock worth holding.

On a token-stock pair the trading-fee cashback is paid in the tokenized stock,
so a position accrues the underlying at a rate set by the pair's fee flow. This
strategy ranks pairs by that accrual rate multiplied by a quality score for the
paired stock (its recent trend and whether its feed is fresh), buys the top
``top_k``, and sells a held pair whose accrual has stopped. It is a heuristic
edge, not a promise: fee flow is reflexive, and the gate still has the last
word on every order it proposes.
"""

from __future__ import annotations

import numpy as np

from pons_pal.models import PonsSignal
from pons_pal.strategies.base import PonsStrategyContext, Strategy


class StockBackStrategy(Strategy):
    """Rank by accrual rate times paired-stock quality."""

    name = "stockback"

    @staticmethod
    def stock_quality(prices: np.ndarray) -> float:
        """Trend quality of the paired stock in [0, 1]: rising and orderly scores high."""
        if prices.size < 5:
            return 0.5
        returns = np.diff(np.log(prices))
        trend = float(np.log(prices[-1] / prices[0]))
        vol = float(np.std(returns)) * np.sqrt(returns.size)
        if vol <= 0.0:
            return 0.5
        sharpe_like = trend / vol
        return float(np.clip(0.5 + sharpe_like / 4.0, 0.0, 1.0))

    def generate(self, ctx: PonsStrategyContext) -> list[PonsSignal]:
        """Buy signals for the best-accruing pairs; sell signals for held pairs gone dead."""
        ranked: list[tuple[float, str, float, float]] = []
        signals: list[PonsSignal] = []
        for pair_id, pair in ctx.pairs.items():
            accrual = ctx.stockback.get(pair_id)
            held = ctx.view.position_usd(pair_id) > 0.0
            if accrual is None:
                continue
            if not accrual.live:
                if held:
                    signals.append(
                        PonsSignal(
                            strategy=self.name,
                            pair_id=pair_id,
                            score=-1.0,
                            confidence=1.0,
                            horizon_s=3600,
                            rationale="stock-back accrual is no longer live",
                            ts=ctx.now,
                        )
                    )
                continue
            prices = np.asarray(ctx.stock_history.get(pair.stock.symbol, ()), dtype=float)
            quality = self.stock_quality(prices)
            feed = ctx.stock_readings.get(pair.stock.symbol)
            if feed is None:
                continue
            ranked.append(
                (accrual.accrual_rate_daily * quality, pair_id, accrual.accrual_rate_daily, quality)
            )
        ranked.sort(reverse=True)
        top = ranked[: self.config.top_k]
        if not top:
            return signals
        best = top[0][0]
        for composite, pair_id, rate, quality in top:
            if best <= 0.0:
                break
            signals.append(
                PonsSignal(
                    strategy=self.name,
                    pair_id=pair_id,
                    score=self.clip_score(composite / best),
                    confidence=float(quality),
                    horizon_s=86_400,
                    rationale=f"accrual {rate:.4%}/day x stock quality {quality:.2f}",
                    ts=ctx.now,
                )
            )
        return signals
