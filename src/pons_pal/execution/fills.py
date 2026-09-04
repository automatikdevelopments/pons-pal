# SPDX-License-Identifier: MIT
# Pons Family - fill confirmation for pons.family
"""Turn a transaction receipt into a validated ``PonsFill``.

A receipt with ``status != 1`` is a revert, not a fill. A fill amount that is
not finite or not positive is a decode failure, not a zero-size fill. Both are
refused loudly, because a fill record that says "filled 0" would update the
book with a phantom position.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from typing import Any

from pons_pal.errors import DecodeError, ExecutionError
from pons_pal.models import PonsFill, PonsOrder, utcnow

WEI = 10**18


def confirm_fill(
    order: PonsOrder,
    receipt: Mapping[str, Any],
    *,
    amount_token: float,
    amount_usd: float,
    price_usd: float,
) -> PonsFill:
    """Build a live fill from a receipt and the decoded swap amounts.

    Raises:
        ExecutionError: if the transaction reverted.
        DecodeError: if the receipt or amounts are malformed.
    """
    status = receipt.get("status")
    if status != 1:
        raise ExecutionError("confirm", "transaction reverted")
    tx_hash = receipt.get("transactionHash")
    if isinstance(tx_hash, bytes | bytearray):
        tx_hash = "0x" + bytes(tx_hash).hex()
    if not isinstance(tx_hash, str):
        raise DecodeError("receipt", "transactionHash", "missing")
    gas_used = receipt.get("gasUsed")
    gas_price = receipt.get("effectiveGasPrice")
    if (
        not isinstance(gas_used, int)
        or not isinstance(gas_price, int)
        or gas_used < 0
        or gas_price < 0
    ):
        raise DecodeError("receipt", "gas", "missing or negative")
    for name, value in (
        ("amount_token", amount_token),
        ("amount_usd", amount_usd),
        ("price_usd", price_usd),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise DecodeError("swap", name, "not a positive finite number")
    return PonsFill(
        fill_id=uuid.uuid4().hex,
        order_id=order.order_id,
        pair_id=order.pair_id,
        side=order.side,
        amount_token=amount_token,
        amount_usd=amount_usd,
        price_usd=price_usd,
        gas_eth=gas_used * gas_price / WEI,
        tx_hash=tx_hash,
        simulated=False,
        ts=utcnow(),
    )


def simulated_fill(
    order: PonsOrder, notional_usd: float, price_usd: float, impact_bps: float
) -> PonsFill:
    """A paper fill at the quote moved by the modeled impact."""
    if not math.isfinite(price_usd) or price_usd <= 0.0:
        raise DecodeError("quote", "price_usd", "not a positive finite number")
    if not math.isfinite(notional_usd) or notional_usd <= 0.0:
        raise DecodeError("order", "notional_usd", "not a positive finite number")
    move = max(0.0, impact_bps) / 10_000.0
    fill_price = price_usd * (1.0 + move) if order.side.value == "buy" else price_usd * (1.0 - move)
    if fill_price <= 0.0:
        raise DecodeError("quote", "fill_price", "impact drove the price to zero")
    return PonsFill(
        fill_id=uuid.uuid4().hex,
        order_id=order.order_id,
        pair_id=order.pair_id,
        side=order.side,
        amount_token=notional_usd / fill_price,
        amount_usd=notional_usd,
        price_usd=fill_price,
        gas_eth=0.0,
        tx_hash=None,
        simulated=True,
        ts=utcnow(),
    )
