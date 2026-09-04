# SPDX-License-Identifier: MIT
# Pons Family - shared test fixtures for pons.family
"""Fixtures for pairs, bars, books, and a temporary config directory.

The private key used here is the first well-known development account
shipped with every EVM toolchain. It controls nothing and is public by
design; it exists so the key-to-address assertion can be tested at all.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pons_pal.config import RiskLimits
from pons_pal.core.risk import PonsBook, PonsPairContext
from pons_pal.models import (
    PairStage,
    PonsBar,
    PonsFeedReading,
    PonsOrder,
    PonsPair,
    PonsStockToken,
    Side,
)

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"

DEV_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEV_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
OTHER_ADDRESS = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

FEED_ADDRESS = "0x1111111111111111111111111111111111111111"
TOKEN_ADDRESS = "0x2222222222222222222222222222222222222222"
POOL_ADDRESS = "0x3333333333333333333333333333333333333333"
STOCK_ADDRESS = "0x4444444444444444444444444444444444444444"

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


def make_pair(
    pair_id: str = "PONS-AAPL",
    symbol: str = "AAPL",
    *,
    stage: PairStage = PairStage.GRADUATED,
    volume: float = 2_000_000.0,
    fee_flow: float = 6_000.0,
    liquidity: float = 1_000_000.0,
    share: float = 0.5,
) -> PonsPair:
    return PonsPair(
        pair_id=pair_id,
        token_symbol=pair_id.split("-")[0],
        token_name=f"{pair_id} token",
        token_address=TOKEN_ADDRESS,
        pool_address=POOL_ADDRESS,
        stock=PonsStockToken(
            symbol=symbol,
            name=f"{symbol} stock token",
            address=STOCK_ADDRESS,
            feed_address=FEED_ADDRESS,
        ),
        stage=stage,
        stockback_share=share,
        volume_24h_usd=volume,
        fee_flow_24h_usd=fee_flow,
        liquidity_usd=liquidity,
        created_at=NOW - timedelta(days=30),
    )


def make_bars(
    pair_id: str, closes: list[float], start: datetime = NOW - timedelta(hours=4)
) -> list[PonsBar]:
    bars = []
    for index, close in enumerate(closes):
        bars.append(
            PonsBar(
                pair_id=pair_id,
                ts=start + timedelta(minutes=index),
                open=close,
                high=close * 1.001,
                low=close * 0.999,
                close=close,
                volume_usd=1_000.0,
            )
        )
    return bars


def make_reading(
    symbol: str = "AAPL", price: float = 200.0, age_s: float = 60.0
) -> PonsFeedReading:
    return PonsFeedReading(
        feed_address=FEED_ADDRESS,
        symbol=symbol,
        price_usd=price,
        updated_at=NOW - timedelta(seconds=age_s),
        round_id=1,
        decimals=8,
    )


def make_order(
    notional: float = 100.0, side: Side = Side.BUY, pair_id: str = "PONS-AAPL"
) -> PonsOrder:
    return PonsOrder(
        order_id="order-1", pair_id=pair_id, side=side, notional_usd=notional, max_slippage_bps=50
    )


def make_book(**overrides: object) -> PonsBook:
    base: dict[str, object] = {
        "equity_usd": 10_000.0,
        "pnl_intraday_usd": 0.0,
        "pnl_weekly_usd": 0.0,
        "pnl_monthly_usd": 0.0,
        "chain_exposure_usd": 0.0,
        "position_usd_by_pair": {},
        "stock_exposure_usd_by_symbol": {},
        "eth_balance": 0.1,
        "daily_notional_used_usd": 0.0,
        "min_order_usd": 25.0,
    }
    base.update(overrides)
    return PonsBook.model_validate(base)


def make_ctx(**overrides: object) -> PonsPairContext:
    base: dict[str, object] = {
        "pair_id": "PONS-AAPL",
        "stock_symbol": "AAPL",
        "volume_24h_usd": 2_000_000.0,
        "feed_age_s": 60.0,
        "stockback_rate_daily": 0.003,
        "underlying_return_5d_pct": 1.5,
        "expected_slippage_bps": 32.0,
        "price_impact_bps": 2.0,
        "gas_estimate_eth": 0.0005,
    }
    base.update(overrides)
    return PonsPairContext.model_validate(base)


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A copy of the repo config with one static pair and a temporary database path."""
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    pons = target / "pons.yaml"
    text = pons.read_text()
    text = text.replace(
        "  static: []                         # optional inline PonsPair records for paper mode",
        "\n".join(
            [
                "  static:",
                "    - pair_id: PONS-AAPL",
                "      token_symbol: PONS",
                "      token_name: Pons AAPL token",
                f"      token_address: '{TOKEN_ADDRESS}'",
                f"      pool_address: '{POOL_ADDRESS}'",
                "      stock:",
                "        symbol: AAPL",
                "        name: Apple stock token",
                f"        address: '{STOCK_ADDRESS}'",
                f"        feed_address: '{FEED_ADDRESS}'",
                "      stage: graduated",
                "      stockback_share: 0.5",
                "      volume_24h_usd: 2000000",
                "      fee_flow_24h_usd: 6000",
                "      liquidity_usd: 1000000",
                "      created_at: '2026-08-01T00:00:00+00:00'",
            ]
        ),
    )
    pons.write_text(text)
    return target
