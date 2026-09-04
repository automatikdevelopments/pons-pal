# SPDX-License-Identifier: MIT
# Pons Family - stock-back accrual accounting for pons.family
"""The arithmetic behind the stock-back edge, with every division guarded.

Accrual rate is the share of the pair's daily fee flow that is paid back in
the tokenized stock, divided by the pool liquidity that earns it. A pool with
no liquidity has no rate rather than an infinite one, and a NaN anywhere in the
inputs is refused before it can become a NaN position size.
"""

from __future__ import annotations

import math
from datetime import datetime

from pons_pal.models import PonsPair, PonsStockBack

SECONDS_PER_DAY = 86_400.0


def accrual_rate_daily(
    fee_flow_24h_usd: float, stockback_share: float, liquidity_usd: float
) -> float:
    """Daily stock-back yield on capital in the pool, as a fraction.

    Returns 0.0 when liquidity is not positive or any input is not finite.
    """
    values = (fee_flow_24h_usd, stockback_share, liquidity_usd)
    if any(not math.isfinite(v) for v in values):
        return 0.0
    if liquidity_usd <= 0.0 or fee_flow_24h_usd < 0.0 or not 0.0 <= stockback_share <= 1.0:
        return 0.0
    return fee_flow_24h_usd * stockback_share / liquidity_usd


def accrued_stock_units(
    position_usd: float, rate_daily: float, stock_price_usd: float, elapsed_s: float
) -> float:
    """Units of the tokenized stock accrued on ``position_usd`` over ``elapsed_s``.

    Returns 0.0 for a non-positive stock price or elapsed time; a negative
    elapsed time (clock skew) accrues nothing rather than un-accruing.
    """
    values = (position_usd, rate_daily, stock_price_usd, elapsed_s)
    if any(not math.isfinite(v) for v in values):
        return 0.0
    if stock_price_usd <= 0.0 or elapsed_s <= 0.0 or position_usd <= 0.0 or rate_daily <= 0.0:
        return 0.0
    accrued_usd = position_usd * rate_daily * (elapsed_s / SECONDS_PER_DAY)
    return accrued_usd / stock_price_usd


def stockback_for(pair: PonsPair, position_usd: float, min_rate: float) -> PonsStockBack:
    """Build the ``PonsStockBack`` record for a pair and the position held in it."""
    rate = accrual_rate_daily(pair.fee_flow_24h_usd, pair.stockback_share, pair.liquidity_usd)
    return PonsStockBack(
        pair_id=pair.pair_id,
        stock_symbol=pair.stock.symbol,
        fee_flow_24h_usd=pair.fee_flow_24h_usd,
        stockback_share=pair.stockback_share,
        liquidity_usd=pair.liquidity_usd,
        position_usd=max(0.0, position_usd),
        accrual_rate_daily=rate,
        live=rate >= min_rate and rate > 0.0,
    )


class StockBackLedger:
    """Tracks accrued stock per pair between observations."""

    def __init__(self) -> None:
        self._last_seen: dict[str, datetime] = {}
        self._units: dict[str, float] = {}

    def observe(
        self, record: PonsStockBack, stock_price_usd: float, now: datetime
    ) -> tuple[float, float]:
        """Accrue since the last observation and return ``(units, usd)`` added."""
        last = self._last_seen.get(record.pair_id)
        self._last_seen[record.pair_id] = now
        if last is None:
            return 0.0, 0.0
        elapsed = (now - last).total_seconds()
        units = accrued_stock_units(
            record.position_usd, record.accrual_rate_daily, stock_price_usd, elapsed
        )
        self._units[record.pair_id] = self._units.get(record.pair_id, 0.0) + units
        return units, units * stock_price_usd

    def units(self, pair_id: str) -> float:
        """Total units accrued for ``pair_id`` in this process."""
        return self._units.get(pair_id, 0.0)
