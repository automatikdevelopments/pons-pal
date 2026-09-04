# SPDX-License-Identifier: MIT
# Pons Family - risk gate and circuit breaker tests for pons.family
"""Every check blocks or reduces at its threshold; breakers trip, persist, and need a person."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pons_pal.config import RiskLimits
from pons_pal.core.risk import (
    BREAKER_INTRADAY,
    BREAKER_MONTHLY,
    BREAKER_WEEKLY,
    CircuitBreakers,
    RiskGate,
)
from pons_pal.core.session import TradingSession
from pons_pal.errors import ConfigError
from pons_pal.models import RiskAction, Side
from pons_pal.store import PonsStore
from tests.conftest import NOW, make_book, make_ctx, make_order


def gate(limits: RiskLimits | None = None, store: PonsStore | None = None) -> RiskGate:
    current = limits or RiskLimits()
    return RiskGate(lambda: current, CircuitBreakers(store), TradingSession())


def test_clean_order_passes() -> None:
    decision = gate().evaluate(make_order(), make_book(), make_ctx(), NOW)
    assert decision.action is RiskAction.ALLOW
    assert decision.adjusted_notional_usd == 100.0
    assert decision.approved


@pytest.mark.parametrize(
    ("field", "name"),
    [
        ("pnl_intraday_usd", BREAKER_INTRADAY),
        ("pnl_weekly_usd", BREAKER_WEEKLY),
        ("pnl_monthly_usd", BREAKER_MONTHLY),
    ],
)
def test_loss_breakers_trip_at_threshold(field: str, name: str) -> None:
    limits = RiskLimits()
    pct = {
        BREAKER_INTRADAY: limits.intraday_loss_pct,
        BREAKER_WEEKLY: limits.weekly_loss_pct,
        BREAKER_MONTHLY: limits.monthly_loss_pct,
    }[name]
    just_inside = -(pct / 100.0) * 10_000.0
    g = gate(limits)
    assert (
        g.evaluate(make_order(), make_book(**{field: just_inside}), make_ctx(), NOW).action
        is RiskAction.ALLOW
    )
    assert not g.breakers.any_tripped
    decision = g.evaluate(make_order(), make_book(**{field: just_inside - 1.0}), make_ctx(), NOW)
    assert decision.action is RiskAction.BLOCK
    assert decision.check == name
    assert g.breakers.any_tripped


def test_tripped_breaker_blocks_everything_until_manual_resume() -> None:
    g = gate()
    g.evaluate(make_order(), make_book(pnl_intraday_usd=-500.0), make_ctx(), NOW)
    assert g.breakers.any_tripped
    decision = g.evaluate(make_order(side=Side.SELL), make_book(), make_ctx(), NOW)
    assert decision.action is RiskAction.BLOCK
    assert "manual resume" in decision.reason
    with pytest.raises(ConfigError):
        g.breakers.resume(BREAKER_INTRADAY, confirm=False)
    g.breakers.resume(BREAKER_INTRADAY, confirm=True)
    assert g.evaluate(make_order(), make_book(), make_ctx(), NOW).action is RiskAction.ALLOW


def test_breaker_state_survives_restart() -> None:
    store = PonsStore(":memory:")
    g = gate(store=store)
    g.evaluate(make_order(), make_book(pnl_weekly_usd=-1_000.0), make_ctx(), NOW)
    again = CircuitBreakers(store)
    assert again.any_tripped
    assert again.tripped()[0].name == BREAKER_WEEKLY


def test_zero_equity_trips_rather_than_dividing() -> None:
    decision = gate().evaluate(
        make_order(), make_book(equity_usd=0.0, pnl_intraday_usd=-1.0), make_ctx(), NOW
    )
    assert decision.action is RiskAction.BLOCK
    assert decision.check == BREAKER_INTRADAY


def test_chain_exposure_blocks() -> None:
    decision = gate().evaluate(
        make_order(notional=200.0), make_book(chain_exposure_usd=900.0), make_ctx(), NOW
    )
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "chain_exposure"


def test_position_notional_reduces() -> None:
    decision = gate().evaluate(
        make_order(notional=400.0),
        make_book(position_usd_by_pair={"PONS-AAPL": 300.0}),
        make_ctx(),
        NOW,
    )
    assert decision.action is RiskAction.REDUCE
    assert decision.adjusted_notional_usd == pytest.approx(200.0)
    assert decision.check == "position_notional"


def test_position_at_cap_blocks() -> None:
    decision = gate().evaluate(
        make_order(notional=50.0),
        make_book(position_usd_by_pair={"PONS-AAPL": 490.0}),
        make_ctx(),
        NOW,
    )
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "position_notional"


def test_pair_concentration_blocks_same_stock() -> None:
    limits = RiskLimits(chain_exposure_pct=50.0)
    book = make_book(stock_exposure_usd_by_symbol={"AAPL": 2_450.0}, chain_exposure_usd=2_450.0)
    decision = gate(limits).evaluate(make_order(notional=100.0), book, make_ctx(), NOW)
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "pair_concentration"


def test_low_volume_blocks() -> None:
    decision = gate().evaluate(make_order(), make_book(), make_ctx(volume_24h_usd=999_999.0), NOW)
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "adv"


def test_stale_feed_blocks_during_market_hours() -> None:
    market_hours = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)  # 11:00 New York, a Tuesday
    decision = gate().evaluate(make_order(), make_book(), make_ctx(feed_age_s=901.0), market_hours)
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "stock_feed_fresh"


def test_missing_feed_blocks() -> None:
    decision = gate().evaluate(make_order(), make_book(), make_ctx(feed_age_s=None), NOW)
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "stock_feed_fresh"


def test_off_hours_feed_tolerance_is_wider_but_bounded() -> None:
    weekend = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert (
        gate().evaluate(make_order(), make_book(), make_ctx(feed_age_s=3_600.0), weekend).action
        is RiskAction.ALLOW
    )
    decision = gate().evaluate(make_order(), make_book(), make_ctx(feed_age_s=90_000.0), weekend)
    assert decision.action is RiskAction.BLOCK


def test_dead_stockback_blocks_buys_not_sells() -> None:
    ctx = make_ctx(stockback_rate_daily=0.0)
    assert gate().evaluate(make_order(), make_book(), ctx, NOW).check == "stockback_live"
    assert (
        gate().evaluate(make_order(side=Side.SELL), make_book(), ctx, NOW).action
        is RiskAction.ALLOW
    )


def test_underlying_freefall_blocks_buys() -> None:
    decision = gate().evaluate(
        make_order(), make_book(), make_ctx(underlying_return_5d_pct=-16.0), NOW
    )
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "underlying_ok"


def test_gas_reserve_is_never_spent() -> None:
    decision = gate().evaluate(
        make_order(), make_book(eth_balance=0.0201), make_ctx(gas_estimate_eth=0.0002), NOW
    )
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "eth_reserve"


def test_per_order_and_daily_ceilings_compose() -> None:
    limits = RiskLimits(chain_exposure_pct=50.0, max_position_pct=50.0)
    decision = gate(limits).evaluate(make_order(notional=900.0), make_book(), make_ctx(), NOW)
    assert decision.action is RiskAction.REDUCE
    assert decision.adjusted_notional_usd == 500.0
    decision = gate(limits).evaluate(
        make_order(notional=900.0), make_book(daily_notional_used_usd=4_900.0), make_ctx(), NOW
    )
    assert decision.action is RiskAction.REDUCE
    assert decision.adjusted_notional_usd == pytest.approx(100.0)
    decision = gate(limits).evaluate(
        make_order(notional=900.0), make_book(daily_notional_used_usd=4_990.0), make_ctx(), NOW
    )
    assert decision.action is RiskAction.BLOCK
    assert decision.check == "daily_ceiling"


def test_slippage_and_impact_floors() -> None:
    assert (
        gate().evaluate(make_order(), make_book(), make_ctx(expected_slippage_bps=101.0), NOW).check
        == "slippage"
    )
    assert (
        gate().evaluate(make_order(), make_book(), make_ctx(price_impact_bps=151.0), NOW).check
        == "price_impact"
    )
    loose = make_order().model_copy(update={"max_slippage_bps": 500})
    assert gate().evaluate(loose, make_book(), make_ctx(), NOW).check == "slippage"


def test_limits_hot_reload_takes_effect() -> None:
    holder = [RiskLimits()]
    g = RiskGate(lambda: holder[0], CircuitBreakers(), TradingSession())
    assert (
        g.evaluate(make_order(), make_book(), make_ctx(volume_24h_usd=1_500_000.0), NOW).action
        is RiskAction.ALLOW
    )
    holder[0] = RiskLimits(min_adv_usd=2_000_000.0)
    assert (
        g.evaluate(make_order(), make_book(), make_ctx(volume_24h_usd=1_500_000.0), NOW).check
        == "adv"
    )
