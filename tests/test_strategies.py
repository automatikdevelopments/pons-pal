# SPDX-License-Identifier: MIT
# Pons Family - strategy and portfolio builder tests for pons.family
"""Strategies emit well-formed signals from synthetic history; the builder sizes long-only."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from pons_pal.adapters.stockback import stockback_for
from pons_pal.config import CapitalSection, PortfolioSection, RiskLimits, StrategyConfig
from pons_pal.core.portfolio import PonsPortfolioView, PortfolioBuilder
from pons_pal.models import PonsPosition, PonsSignal, Side
from pons_pal.strategies import (
    EventDriftStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    PonsStrategyContext,
    StatisticalPairsStrategy,
    StockBackStrategy,
)
from tests.conftest import NOW, make_bars, make_pair, make_reading


def rising(n: int = 120, start: float = 1.0, step: float = 0.002) -> list[float]:
    return [start * (1.0 + step) ** i for i in range(n)]


def context(
    closes: list[float],
    *,
    stock_history: list[float] | None = None,
    sentiment: dict[str, tuple[float, object]] | None = None,
    held_usd: float = 0.0,
    fee_flow: float = 6_000.0,
) -> PonsStrategyContext:
    pair = make_pair(fee_flow=fee_flow)
    positions = (
        {
            pair.pair_id: PonsPosition(
                pair_id=pair.pair_id,
                stock_symbol="AAPL",
                token_amount=held_usd,
                cost_basis_usd=held_usd,
                mark_usd=held_usd,
            )
        }
        if held_usd
        else {}
    )
    view = PonsPortfolioView(equity_usd=10_000.0, cash_usd=10_000.0 - held_usd, positions=positions)
    return PonsStrategyContext(
        now=NOW,
        view=view,
        pairs={pair.pair_id: pair},
        history={pair.pair_id: make_bars(pair.pair_id, closes)},
        stock_readings={"AAPL": make_reading()},
        stock_history={"AAPL": stock_history or rising(60, 200.0, 0.001)},
        sentiment=sentiment or {},  # type: ignore[arg-type]
        stockback={pair.pair_id: stockback_for(pair, held_usd, 0.0005)},
    )


def assert_well_formed(signals: list[PonsSignal], name: str) -> None:
    for signal in signals:
        assert signal.strategy == name
        assert -1.0 <= signal.score <= 1.0
        assert 0.0 <= signal.confidence <= 1.0
        assert signal.horizon_s > 0
        assert np.isfinite(signal.score)


def test_momentum_is_positive_on_an_uptrend() -> None:
    signals = MomentumStrategy(StrategyConfig(lookback_bars=60)).generate(context(rising()))
    assert_well_formed(signals, "momentum")
    assert signals and signals[0].score > 0.0


def test_momentum_is_silent_without_history() -> None:
    assert MomentumStrategy(StrategyConfig(lookback_bars=60)).generate(context(rising(5))) == []


def test_mean_reversion_fades_a_spike() -> None:
    closes = [1.0] * 100 + [1.0 + 0.01 * i for i in range(1, 21)]
    signals = MeanReversionStrategy(StrategyConfig(lookback_bars=120, z_entry=2.0)).generate(
        context(closes)
    )
    assert_well_formed(signals, "mean_reversion")
    assert signals and signals[0].score < 0.0


def test_mean_reversion_is_quiet_inside_the_band() -> None:
    closes = [1.0 + 0.001 * ((-1) ** i) for i in range(120)]
    assert MeanReversionStrategy(StrategyConfig(lookback_bars=120)).generate(context(closes)) == []


def test_pairs_leans_against_a_stretched_spread() -> None:
    stock = rising(120, 200.0, 0.001)
    token = [s / 200.0 for s in stock]
    token[-1] *= 1.05
    signals = StatisticalPairsStrategy(StrategyConfig(lookback_bars=120, z_entry=2.0)).generate(
        context(token, stock_history=stock)
    )
    assert_well_formed(signals, "pairs")
    assert signals and signals[0].score < 0.0


def test_event_decays_sentiment() -> None:
    fresh = context(rising(), sentiment={"AAPL": (0.8, NOW)})
    stale = context(rising(), sentiment={"AAPL": (0.8, NOW - timedelta(days=2))})
    strategy = EventDriftStrategy(StrategyConfig(half_life_s=3600))
    fresh_signals = strategy.generate(fresh)
    assert_well_formed(fresh_signals, "event")
    assert fresh_signals and fresh_signals[0].score == pytest.approx(0.8)
    assert strategy.generate(stale) == []


def test_stockback_buys_live_pairs_and_sells_dead_ones() -> None:
    strategy = StockBackStrategy(StrategyConfig(top_k=3))
    live = strategy.generate(context(rising()))
    assert_well_formed(live, "stockback")
    assert live and live[0].score == 1.0
    dead = strategy.generate(context(rising(), held_usd=300.0, fee_flow=1.0))
    assert dead and dead[0].score == -1.0
    assert strategy.generate(context(rising(), fee_flow=1.0)) == []


def test_stock_quality_is_bounded() -> None:
    assert 0.0 <= StockBackStrategy.stock_quality(np.asarray(rising(30, 100.0, -0.01))) <= 1.0
    assert StockBackStrategy.stock_quality(np.asarray([1.0, 1.0])) == 0.5


def builder() -> PortfolioBuilder:
    return PortfolioBuilder(
        PortfolioSection(), CapitalSection(), {"momentum": 1.0, "stockback": 1.5}, 50
    )


def test_builder_sizes_long_only_and_respects_caps() -> None:
    view = PonsPortfolioView(equity_usd=10_000.0, cash_usd=10_000.0, positions={})
    signals = [
        PonsSignal(
            strategy="momentum", pair_id="PONS-AAPL", score=1.0, confidence=1.0, horizon_s=60
        ),
        PonsSignal(
            strategy="stockback", pair_id="PONS-AAPL", score=1.0, confidence=1.0, horizon_s=60
        ),
    ]
    orders = builder().build(signals, view, {}, RiskLimits())
    assert len(orders) == 1
    assert orders[0].side is Side.BUY
    assert orders[0].notional_usd == 500.0  # 5% of equity, also the per-order cap
    assert orders[0].strategies == ("momentum", "stockback")


def test_builder_ignores_negative_signals_on_unheld_pairs() -> None:
    view = PonsPortfolioView(equity_usd=10_000.0, cash_usd=10_000.0, positions={})
    signals = [
        PonsSignal(
            strategy="momentum", pair_id="PONS-AAPL", score=-1.0, confidence=1.0, horizon_s=60
        )
    ]
    assert builder().build(signals, view, {}, RiskLimits()) == []


def test_builder_exits_held_pair_on_negative_signal() -> None:
    position = PonsPosition(
        pair_id="PONS-AAPL",
        stock_symbol="AAPL",
        token_amount=100.0,
        cost_basis_usd=300.0,
        mark_usd=300.0,
    )
    view = PonsPortfolioView(
        equity_usd=10_000.0, cash_usd=9_700.0, positions={"PONS-AAPL": position}
    )
    signals = [
        PonsSignal(
            strategy="momentum", pair_id="PONS-AAPL", score=-0.5, confidence=1.0, horizon_s=60
        )
    ]
    orders = builder().build(signals, view, {}, RiskLimits())
    assert len(orders) == 1 and orders[0].side is Side.SELL and orders[0].notional_usd == 300.0


def test_correlation_penalty_damps_correlated_pairs() -> None:
    closes = rising()
    history = {"A": make_bars("A", closes), "B": make_bars("B", [c * 2.0 for c in closes])}
    penalties = builder().correlation_penalty(history, ["A", "B"])
    assert penalties["A"] > 1.5 and penalties["B"] > 1.5
    assert builder().correlation_penalty({"A": make_bars("A", closes)}, ["A"]) == {"A": 1.0}
