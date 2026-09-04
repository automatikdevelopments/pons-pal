# SPDX-License-Identifier: MIT
# Pons Family - statistical pairs strategy for pons.family
"""Trade the spread between a Pons token and the stock it is paired with.

A token paired with a stock is, by construction, a claim on that stock's fee
flow, so its price should carry some relationship to the stock's. The spread
is the residual of a rolling regression of token log price on stock log price.
When the residual stretches beyond ``z_entry`` the strategy leans against it.
A hedge ratio that is not positive means the relationship has broken and the
strategy stays silent; a spread trade on an uncointegrated pair is a coin flip
with a fee attached.
"""

from __future__ import annotations

import numpy as np
from scipy import stats

from pons_pal.models import PonsSignal
from pons_pal.strategies.base import PonsStrategyContext, Strategy


class StatisticalPairsStrategy(Strategy):
    """Regression-residual spread reversion between token and paired stock."""

    name = "pairs"

    def generate(self, ctx: PonsStrategyContext) -> list[PonsSignal]:
        """One signal per pair whose spread residual is beyond the entry band."""
        signals: list[PonsSignal] = []
        lookback = self.config.lookback_bars
        for pair_id, pair in ctx.pairs.items():
            token = self.closes(ctx.history.get(pair_id, ()), lookback)
            stock_series = ctx.stock_history.get(pair.stock.symbol, ())
            stock = np.asarray(stock_series[-lookback:], dtype=float)
            length = min(token.size, stock.size)
            if length < max(20, lookback // 2):
                continue
            token_log = np.log(token[-length:])
            stock_log = np.log(stock[-length:])
            # ptp rather than std: the std of identical values is a rounding residue, not zero.
            if np.ptp(stock_log) == 0.0 or np.ptp(token_log) == 0.0:
                continue
            fit = stats.linregress(stock_log, token_log)
            slope = float(fit.slope)
            if slope <= 0.0:
                continue
            residual = token_log - (float(fit.intercept) + slope * stock_log)
            std = float(np.std(residual))
            if std <= 0.0:
                continue
            z = float(residual[-1]) / std
            if abs(z) < self.config.z_entry:
                continue
            signals.append(
                PonsSignal(
                    strategy=self.name,
                    pair_id=pair_id,
                    score=self.clip_score(-z / self.config.z_entry),
                    confidence=float(min(1.0, abs(float(fit.rvalue)))),
                    horizon_s=lookback * 60,
                    rationale=f"spread z {z:+.2f}, hedge {slope:.2f}, r {float(fit.rvalue):.2f}",
                    ts=ctx.now,
                )
            )
        return signals
