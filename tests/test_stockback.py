# SPDX-License-Identifier: MIT
# Pons Family - stock-back accrual tests for pons.family
"""The accrual arithmetic is right, and every degenerate input yields zero, never NaN."""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from pons_pal.adapters.stockback import (
    StockBackLedger,
    accrual_rate_daily,
    accrued_stock_units,
    stockback_for,
)
from tests.conftest import NOW, make_pair


def test_accrual_rate() -> None:
    # 6,000 USD of daily fees, half paid in stock, over 1,000,000 USD of liquidity.
    assert accrual_rate_daily(6_000.0, 0.5, 1_000_000.0) == pytest.approx(0.003)


@pytest.mark.parametrize(
    ("fees", "share", "liquidity"),
    [
        (6_000.0, 0.5, 0.0),
        (6_000.0, 0.5, -1.0),
        (-1.0, 0.5, 1_000_000.0),
        (6_000.0, 1.5, 1_000_000.0),
        (math.nan, 0.5, 1_000_000.0),
        (6_000.0, 0.5, math.inf),
    ],
)
def test_degenerate_inputs_yield_zero(fees: float, share: float, liquidity: float) -> None:
    assert accrual_rate_daily(fees, share, liquidity) == 0.0


def test_accrued_units() -> None:
    # 1,000 USD position at 0.3%/day for one day at a 200 USD stock price = 3 USD = 0.015 units.
    assert accrued_stock_units(1_000.0, 0.003, 200.0, 86_400.0) == pytest.approx(0.015)
    assert accrued_stock_units(1_000.0, 0.003, 200.0, 43_200.0) == pytest.approx(0.0075)


@pytest.mark.parametrize(
    ("position", "rate", "price", "elapsed"),
    [
        (1_000.0, 0.003, 0.0, 86_400.0),
        (1_000.0, 0.003, 200.0, -5.0),
        (0.0, 0.003, 200.0, 86_400.0),
        (1_000.0, math.nan, 200.0, 86_400.0),
    ],
)
def test_accrued_units_degenerate(
    position: float, rate: float, price: float, elapsed: float
) -> None:
    assert accrued_stock_units(position, rate, price, elapsed) == 0.0


def test_stockback_record_and_liveness() -> None:
    live = stockback_for(make_pair(), 500.0, 0.0005)
    assert live.live and live.accrual_rate_daily == pytest.approx(0.003)
    dead = stockback_for(make_pair(fee_flow=10.0), 500.0, 0.0005)
    assert not dead.live
    empty = stockback_for(make_pair(liquidity=0.0), 500.0, 0.0005)
    assert not empty.live and empty.accrual_rate_daily == 0.0


def test_ledger_accrues_between_observations() -> None:
    ledger = StockBackLedger()
    record = stockback_for(make_pair(), 1_000.0, 0.0005)
    assert ledger.observe(record, 200.0, NOW) == (0.0, 0.0)
    units, usd = ledger.observe(record, 200.0, NOW + timedelta(days=1))
    assert units == pytest.approx(0.015)
    assert usd == pytest.approx(3.0)
    assert ledger.units("PONS-AAPL") == pytest.approx(0.015)
    # Clock going backwards accrues nothing.
    assert ledger.observe(record, 200.0, NOW) == (0.0, 0.0)
