# SPDX-License-Identifier: MIT
# Pons Family - the disclosure surface for pons.family
"""What the agent says about itself, in one structure, wherever it is asked.

The disclosure is the machine self-report: mode, arm state, limits, breaker
status, feed freshness, and the disclaimer. It is built from the same state
object the engine uses, so it cannot describe an agent that differs from the
one running. The disclaimer text is a constant so it reads the same in the
README, the runtime banner, and here.
"""

from __future__ import annotations

from pons_pal.config import RiskLimits
from pons_pal.models import PonsBreaker, PonsFeedStatus, PonsModel, PonsPalState

DISCLAIMER = (
    "Agentic trading carries significant risk, including total loss. Pons RWA and "
    "launchpad tokens are extremely high-risk. An AI agent can misread data or act on "
    "stale data. This is not financial advice. You are accountable for the agent you run. "
    "No warranty."
)


class PonsLimitsSummary(PonsModel):
    """The limits that matter to a person deciding whether to arm the agent."""

    intraday_loss_pct: float
    weekly_loss_pct: float
    monthly_loss_pct: float
    chain_exposure_pct: float
    max_position_pct: float
    max_sector_pct: float
    min_adv_usd: float
    per_order_max_usd: float
    daily_notional_max_usd: float
    eth_gas_reserve: float
    max_slippage_bps: int
    max_price_impact_bps: int
    stockback_min_rate: float
    feed_max_age_s: float


class PonsDisclosure(PonsModel):
    """The full self-report."""

    state: PonsPalState
    limits: PonsLimitsSummary
    custodial: bool = False
    executes_trades: bool = True
    paper_by_default: bool = True
    disclaimer: str = DISCLAIMER


def summarize_limits(limits: RiskLimits) -> PonsLimitsSummary:
    """Project ``RiskLimits`` onto the disclosure summary."""
    return PonsLimitsSummary(
        intraday_loss_pct=limits.intraday_loss_pct,
        weekly_loss_pct=limits.weekly_loss_pct,
        monthly_loss_pct=limits.monthly_loss_pct,
        chain_exposure_pct=limits.chain_exposure_pct,
        max_position_pct=limits.max_position_pct,
        max_sector_pct=limits.max_sector_pct,
        min_adv_usd=limits.min_adv_usd,
        per_order_max_usd=limits.per_order_max_usd,
        daily_notional_max_usd=limits.daily_notional_max_usd,
        eth_gas_reserve=limits.eth_gas_reserve,
        max_slippage_bps=limits.max_slippage_bps,
        max_price_impact_bps=limits.max_price_impact_bps,
        stockback_min_rate=limits.stockback_min_rate,
        feed_max_age_s=limits.feed_max_age_s,
    )


def build_disclosure(state: PonsPalState, limits: RiskLimits) -> PonsDisclosure:
    """Assemble the disclosure from live state and current limits."""
    return PonsDisclosure(state=state, limits=summarize_limits(limits))


def _breaker_line(breaker: PonsBreaker) -> str:
    if not breaker.tripped:
        return f"  {breaker.name}: clear"
    return (
        f"  {breaker.name}: TRIPPED (value {breaker.value:g}, limit {breaker.limit:g}); "
        "manual resume required"
    )


def _feed_line(feed: PonsFeedStatus) -> str:
    age = "no reading" if feed.age_s is None else f"{feed.age_s:.0f}s"
    return f"  {feed.name}: {age} ({'fresh' if feed.fresh else 'stale'})"


def render_text(disclosure: PonsDisclosure) -> str:
    """Plain-text disclosure for the CLI. No color, no emoji."""
    state = disclosure.state
    limits = disclosure.limits
    lines = [
        "Pons Pal disclosure",
        f"mode: {state.mode.value}    arm state: {state.arm_state.value}",
        f"budget wallet: {state.budget_address or 'none'}",
        f"equity: {state.equity_usd:,.2f} USD    P&L today: {state.pnl_today_usd:+,.2f} USD    "
        f"drawdown: {state.drawdown_pct:.2f}%",
        f"positions: {state.positions_count}    "
        f"stock-back accrued: {state.stockback_accrued_usd:,.2f} USD",
        f"24h notional used: {state.daily_notional_used_usd:,.2f} of "
        f"{limits.daily_notional_max_usd:,.2f} USD",
        "breakers:",
        *(_breaker_line(b) for b in state.breakers),
        "feeds:",
        *(_feed_line(f) for f in state.feeds),
        "limits:",
        f"  loss: intraday {limits.intraday_loss_pct}%, weekly {limits.weekly_loss_pct}%, "
        f"monthly {limits.monthly_loss_pct}%",
        f"  exposure: chain {limits.chain_exposure_pct}%, position {limits.max_position_pct}%, "
        f"paired stock {limits.max_sector_pct}%",
        f"  order: max {limits.per_order_max_usd:,.0f} USD, "
        f"slippage {limits.max_slippage_bps} bps, impact {limits.max_price_impact_bps} bps, "
        f"gas reserve {limits.eth_gas_reserve} ETH",
        f"custodial: {disclosure.custodial}    paper by default: {disclosure.paper_by_default}",
        "",
        disclosure.disclaimer,
    ]
    return "\n".join(lines)
