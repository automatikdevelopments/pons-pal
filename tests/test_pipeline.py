# SPDX-License-Identifier: MIT
# Pons Family - end-to-end pipeline tests for pons.family
"""The whole pipeline runs unarmed in paper mode with no network, and the kill switch works."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from pons_pal.agent.disclosure import DISCLAIMER, build_disclosure, render_text
from pons_pal.agent.mcp import McpServer
from pons_pal.app import assemble_engine
from pons_pal.config import load_settings
from pons_pal.data.feeds import ReplayFeed, StaticStockData
from pons_pal.data.normalizer import RawTick
from pons_pal.errors import NotArmedError
from pons_pal.models import ArmState, Mode, RiskAction
from pons_pal.store import PonsStore
from tests.conftest import NOW, make_reading


def replay(n: int = 130, step: float = 0.003) -> ReplayFeed:
    ticks = [
        RawTick(
            pair_id="PONS-AAPL",
            ts=NOW - timedelta(minutes=n - i),
            price_usd=1.0 * (1.0 + step) ** i,
            volume_usd=500.0,
        )
        for i in range(n)
    ]
    return ReplayFeed(ticks, batch=n)


def stock_data() -> StaticStockData:
    return StaticStockData(
        {"AAPL": make_reading(age_s=30.0)}, {"AAPL": [200.0 * (1.001**i) for i in range(60)]}
    )


def build(config_dir: Path, tmp_path: Path, feed: ReplayFeed | None = None):  # type: ignore[no-untyped-def]
    settings = load_settings({"DATABASE_URL": f"sqlite:///{tmp_path}/pal.db"})
    return assemble_engine(
        settings, config_dir, market_feed=feed or replay(), stock_data=stock_data()
    )


async def test_paper_cycle_runs_unarmed_and_fills_are_simulated(
    config_dir: Path, tmp_path: Path
) -> None:
    engine = build(config_dir, tmp_path)
    assert engine.mode is Mode.PAPER
    assert engine.arm_state() is ArmState.UNARMED
    report = await engine.run_cycle(NOW)
    assert report.pairs == 1
    assert report.bars > 100
    assert report.signals >= 1
    assert report.orders >= 1
    assert report.decisions and all(
        d.action in (RiskAction.ALLOW, RiskAction.REDUCE) for d in report.decisions
    )
    assert report.fills and all(f.simulated and f.tx_hash is None for f in report.fills)
    state = engine.state(NOW)
    assert state.positions_count == 1
    assert state.arm_state is ArmState.UNARMED
    assert 0.0 <= state.equity_usd
    assert state.daily_notional_used_usd == pytest.approx(sum(f.amount_usd for f in report.fills))


async def test_disarm_blocks_execution_and_persists(config_dir: Path, tmp_path: Path) -> None:
    engine = build(config_dir, tmp_path)
    engine.disarm()
    assert engine.arm_state() is ArmState.DISARMED
    report = await engine.run_cycle(NOW)
    assert report.orders >= 1
    assert report.fills == ()
    assert any(d.action is RiskAction.BLOCK and "disarmed" in d.reason for d in report.decisions)
    restarted = build(config_dir, tmp_path, feed=replay())
    assert restarted.arm_state() is ArmState.DISARMED
    with pytest.raises(NotArmedError):
        restarted.clear_disarm(confirm=False)
    restarted.clear_disarm(confirm=True)
    assert restarted.arm_state() is ArmState.UNARMED


async def test_stale_feed_blocks_the_whole_cycle(config_dir: Path, tmp_path: Path) -> None:
    settings = load_settings({"DATABASE_URL": f"sqlite:///{tmp_path}/pal.db"})
    stale = StaticStockData({"AAPL": make_reading(age_s=7_200.0)}, {"AAPL": [200.0] * 60})
    engine = assemble_engine(settings, config_dir, market_feed=replay(), stock_data=stale)
    report = await engine.run_cycle(NOW)
    assert report.orders >= 1
    assert report.fills == ()
    assert all(
        d.action is RiskAction.BLOCK and d.check == "stock_feed_fresh" for d in report.decisions
    )


async def test_live_mode_without_key_is_unarmed_and_refuses(
    config_dir: Path, tmp_path: Path
) -> None:
    settings = load_settings(
        {"DATABASE_URL": f"sqlite:///{tmp_path}/pal.db", "PONS_PAL_MODE": "live"}
    )
    engine = assemble_engine(settings, config_dir, market_feed=replay(), stock_data=stock_data())
    assert engine.mode is Mode.LIVE
    assert engine.arm_state() is ArmState.UNARMED
    report = await engine.run_cycle(NOW)
    assert report.fills == ()
    # Without a key the ETH balance is unknown, which the gate treats as zero, so the
    # gas-reserve check refuses before the router ever gets to say "unarmed".
    assert report.decisions and all(d.action is RiskAction.BLOCK for d in report.decisions)
    assert all(d.check in {"eth_reserve", "execution", "unarmed"} for d in report.decisions)


async def test_disclosure_carries_the_disclaimer(config_dir: Path, tmp_path: Path) -> None:
    engine = build(config_dir, tmp_path)
    disclosure = build_disclosure(engine.state(NOW), engine.current_limits())
    assert disclosure.custodial is False and disclosure.paper_by_default is True
    text = render_text(disclosure)
    assert (
        DISCLAIMER in text and "arm state: unarmed" in text and "not financial advice" in DISCLAIMER
    )


async def test_mcp_run_cycle_and_disarm(config_dir: Path, tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    engine = build(config_dir, tmp_path)
    server = McpServer(engine, SecretStr("cycle-secret"), "127.0.0.1", 0)
    headers = {"Authorization": "Bearer cycle-secret"}
    async with TestClient(TestServer(server.app())) as client:
        response = await client.post("/mcp", json={"tool": "run_cycle"}, headers=headers)
        assert response.status == 200 and (await response.json())["result"]["pairs"] == 1
        response = await client.post("/mcp", json={"tool": "disarm"}, headers=headers)
        assert (await response.json())["result"]["arm_state"] == "disarmed"
        response = await client.post(
            "/mcp",
            json={"tool": "resume_breaker", "arguments": {"breaker": "intraday_loss"}},
            headers=headers,
        )
        assert response.status == 409
        response = await client.post("/mcp", json={"tool": "get_disclosure"}, headers=headers)
        assert (await response.json())["result"]["disclaimer"] == DISCLAIMER


def test_store_is_parameterized_and_idempotent(tmp_path: Path) -> None:
    store = PonsStore(tmp_path / "s.db")
    store.set_control("k", "v'; DROP TABLE controls; --", NOW)
    assert store.get_control("k") == "v'; DROP TABLE controls; --"
    assert store.get_control("missing") is None
    assert store.risk_counts() == {}
