# SPDX-License-Identifier: MIT
# Pons Family - slippage and price-impact model for pons.family
"""Constant-product impact estimate used to refuse orders before they are quoted.

For an order of size ``q`` against a pool with ``L`` of two-sided liquidity,
the marginal price moves roughly by ``q / (L/2 + q)``. It is an estimate; the
router still checks the real quote. The estimate exists so an order that could
never clear the impact floor is refused in the gate, before any RPC is spent.
"""

from __future__ import annotations

import math


def price_impact_bps(notional_usd: float, liquidity_usd: float) -> float:
    """Approximate price impact in basis points; 10,000 when liquidity is absent."""
    if not (math.isfinite(notional_usd) and math.isfinite(liquidity_usd)):
        return 10_000.0
    if liquidity_usd <= 0.0 or notional_usd <= 0.0:
        return 10_000.0 if notional_usd > 0.0 else 0.0
    side = liquidity_usd / 2.0
    return notional_usd / (side + notional_usd) * 10_000.0


def expected_slippage_bps(
    notional_usd: float, liquidity_usd: float, fee_bps: float = 30.0
) -> float:
    """Impact plus the pool fee: what a taker should expect to give up."""
    return price_impact_bps(notional_usd, liquidity_usd) + max(0.0, fee_bps)


def min_amount_out(quoted_amount: float, max_slippage_bps: int) -> float:
    """The floor passed to the router so a worse fill reverts on-chain."""
    tolerance = max(0, min(10_000, max_slippage_bps)) / 10_000.0
    return max(0.0, quoted_amount * (1.0 - tolerance))
