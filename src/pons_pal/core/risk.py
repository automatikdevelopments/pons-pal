# SPDX-License-Identifier: MIT
# Pons Family - pre-trade risk gate and circuit breakers for pons.family
"""Seven ordered checks plus circuit breakers, evaluated before every order.

Limits are code, not policy: they run at the execution boundary so a mis-sized
signal or a bad model read cannot spend more than the book allows. Breakers halt
all trading on breach and require a manual resume, because an agent that can
re-arm itself after a loss streak is not risk-managed.

The order of the checks matters. The loss breakers come first so a losing
book blocks before any sizing logic runs; the Pons-native checks (feed
freshness, stock-back liveness, underlying health, gas reserve) come after the
book-level checks because they are per-pair; the execution floors (per-order
and daily ceilings, slippage, impact) come last because they act on the size
the earlier checks may already have reduced.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import structlog

from pons_pal.config import RiskLimits
from pons_pal.core.session import TradingSession
from pons_pal.errors import ConfigError
from pons_pal.models import (
    PonsBreaker,
    PonsModel,
    PonsOrder,
    PonsRiskDecision,
    RiskAction,
    Side,
    utcnow,
)
from pons_pal.store import PonsStore

log = structlog.get_logger(__name__)

BREAKER_INTRADAY = "intraday_loss"
BREAKER_WEEKLY = "weekly_loss"
BREAKER_MONTHLY = "monthly_loss"
BREAKER_NAMES: tuple[str, ...] = (BREAKER_INTRADAY, BREAKER_WEEKLY, BREAKER_MONTHLY)


class PonsBook(PonsModel):
    """The book-level facts the gate needs, computed by the engine before each order."""

    equity_usd: float
    pnl_intraday_usd: float
    pnl_weekly_usd: float
    pnl_monthly_usd: float
    chain_exposure_usd: float
    position_usd_by_pair: dict[str, float]
    stock_exposure_usd_by_symbol: dict[str, float]
    eth_balance: float
    daily_notional_used_usd: float
    min_order_usd: float


class PonsPairContext(PonsModel):
    """The per-pair facts the gate needs: market, feed, stock-back, and quote."""

    pair_id: str
    stock_symbol: str
    volume_24h_usd: float
    feed_age_s: float | None
    stockback_rate_daily: float
    underlying_return_5d_pct: float | None
    expected_slippage_bps: float
    price_impact_bps: float
    gas_estimate_eth: float


@dataclass
class _Eval:
    """Mutable working state for one evaluation; reductions compose in place."""

    order: PonsOrder
    notional: float
    reductions: list[str]


class CircuitBreakers:
    """Three loss breakers whose state survives restarts.

    A trip is persisted before the block is returned, so a crash between the
    two cannot lose it. ``resume`` demands ``confirm=True`` and exists only for
    the CLI and the secret-gated MCP tool: nothing in the trading loop calls it.
    """

    def __init__(self, store: PonsStore | None = None) -> None:
        self._store = store
        self._state: dict[str, PonsBreaker] = {
            name: PonsBreaker(name=name, tripped=False) for name in BREAKER_NAMES
        }
        if store is not None:
            self._state.update(store.load_breakers())

    def snapshot(self) -> tuple[PonsBreaker, ...]:
        """Every breaker, in ladder order."""
        return tuple(self._state[name] for name in BREAKER_NAMES)

    def tripped(self) -> list[PonsBreaker]:
        """Breakers currently tripped."""
        return [b for b in self.snapshot() if b.tripped]

    @property
    def any_tripped(self) -> bool:
        """True when trading is halted by at least one breaker."""
        return any(b.tripped for b in self._state.values())

    def trip(
        self, name: str, value: float, limit: float, now: datetime | None = None
    ) -> PonsBreaker:
        """Trip ``name`` and persist it."""
        if name not in self._state:
            raise ConfigError("breaker", f"unknown breaker {name}")
        breaker = PonsBreaker(
            name=name, tripped=True, value=value, limit=limit, tripped_at=now or utcnow()
        )
        self._state[name] = breaker
        if self._store is not None:
            self._store.save_breaker(breaker)
        log.error("risk.breaker_tripped", breaker=name, value=value, limit=limit)
        return breaker

    def resume(self, name: str, *, confirm: bool) -> PonsBreaker:
        """Clear a tripped breaker. Requires an explicit ``confirm=True`` from a person.

        Raises:
            ConfigError: if ``confirm`` is false or the breaker name is unknown.
        """
        if not confirm:
            raise ConfigError("resume", "manual confirmation required to resume a breaker")
        if name not in self._state:
            raise ConfigError("breaker", f"unknown breaker {name}")
        breaker = PonsBreaker(name=name, tripped=False)
        self._state[name] = breaker
        if self._store is not None:
            self._store.save_breaker(breaker)
        log.warning("risk.breaker_resumed", breaker=name)
        return breaker


class RiskGate:
    """Evaluates every order against the current limits and the breakers.

    Args:
        limits: Callable returning the current ``RiskLimits``; a ``HotReloader``
            in production, a lambda in tests.
        breakers: Shared breaker state.
        session: Decides which feed staleness limit applies.
    """

    def __init__(
        self,
        limits: Callable[[], RiskLimits],
        breakers: CircuitBreakers,
        session: TradingSession | None = None,
    ) -> None:
        self._limits = limits
        self._breakers = breakers
        self._session = session or TradingSession()

    @property
    def breakers(self) -> CircuitBreakers:
        """The breaker state this gate consults."""
        return self._breakers

    def evaluate(
        self,
        order: PonsOrder,
        book: PonsBook,
        ctx: PonsPairContext,
        now: datetime | None = None,
    ) -> PonsRiskDecision:
        """Run every check in order and return the decision.

        The first BLOCK wins. REDUCE outcomes compose: the surviving notional is
        the smallest any check allowed, and it must still clear the minimum order
        size or the order is blocked as uneconomic.
        """
        limits = self._limits()
        current = now or utcnow()
        state = _Eval(order=order, notional=order.notional_usd, reductions=[])

        checks: list[
            Callable[
                [_Eval, PonsBook, PonsPairContext, RiskLimits, datetime], PonsRiskDecision | None
            ]
        ] = [
            self._check_breakers_tripped,
            self._check_intraday,
            self._check_weekly,
            self._check_monthly,
            self._check_chain_exposure,
            self._check_position_notional,
            self._check_pair_concentration,
            self._check_adv,
            self._check_stock_feed_fresh,
            self._check_stockback_live,
            self._check_underlying_ok,
            self._check_eth_reserve,
            self._check_per_order_ceiling,
            self._check_daily_ceiling,
            self._check_slippage,
            self._check_price_impact,
        ]
        for check in checks:
            decision = check(state, book, ctx, limits, current)
            if decision is not None:
                return decision

        if state.notional < book.min_order_usd:
            return self._block(
                order,
                "min_order",
                state.notional,
                book.min_order_usd,
                "reduced below the minimum order size",
            )
        if state.reductions:
            return PonsRiskDecision(
                order_id=order.order_id,
                action=RiskAction.REDUCE,
                check=state.reductions[0],
                value=order.notional_usd,
                limit=state.notional,
                reason="; ".join(state.reductions),
                adjusted_notional_usd=state.notional,
            )
        return PonsRiskDecision(
            order_id=order.order_id,
            action=RiskAction.ALLOW,
            reason="all checks passed",
            adjusted_notional_usd=state.notional,
        )

    # --- helpers -----------------------------------------------------------------

    @staticmethod
    def _block(
        order: PonsOrder, check: str, value: float, limit: float, reason: str
    ) -> PonsRiskDecision:
        return PonsRiskDecision(
            order_id=order.order_id,
            action=RiskAction.BLOCK,
            check=check,
            value=value,
            limit=limit,
            reason=reason,
            adjusted_notional_usd=0.0,
        )

    @staticmethod
    def _loss_pct(pnl_usd: float, equity_usd: float) -> float:
        # Equity at or below zero is a total loss; report it as 100% so every
        # breaker trips rather than dividing by zero.
        if equity_usd <= 0.0:
            return 100.0
        return max(0.0, -pnl_usd) / equity_usd * 100.0

    def _loss_breaker(
        self,
        state: _Eval,
        name: str,
        pnl_usd: float,
        equity_usd: float,
        limit_pct: float,
        now: datetime,
    ) -> PonsRiskDecision | None:
        loss_pct = self._loss_pct(pnl_usd, equity_usd)
        if loss_pct > limit_pct:
            self._breakers.trip(name, loss_pct, limit_pct, now)
            return self._block(
                state.order,
                name,
                loss_pct,
                limit_pct,
                "loss limit breached; breaker tripped, manual resume required",
            )
        return None

    # --- checks, in order ----------------------------------------------------------

    def _check_breakers_tripped(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        tripped = self._breakers.tripped()
        if tripped:
            first = tripped[0]
            return self._block(
                state.order,
                first.name,
                first.value or 0.0,
                first.limit or 0.0,
                "circuit breaker tripped; manual resume required",
            )
        return None

    def _check_intraday(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        return self._loss_breaker(
            state,
            BREAKER_INTRADAY,
            book.pnl_intraday_usd,
            book.equity_usd,
            limits.intraday_loss_pct,
            now,
        )

    def _check_weekly(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        return self._loss_breaker(
            state, BREAKER_WEEKLY, book.pnl_weekly_usd, book.equity_usd, limits.weekly_loss_pct, now
        )

    def _check_monthly(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        return self._loss_breaker(
            state,
            BREAKER_MONTHLY,
            book.pnl_monthly_usd,
            book.equity_usd,
            limits.monthly_loss_pct,
            now,
        )

    def _check_chain_exposure(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        if state.order.side is not Side.BUY or book.equity_usd <= 0.0:
            return None
        after = book.chain_exposure_usd + state.notional
        pct = after / book.equity_usd * 100.0
        if pct > limits.chain_exposure_pct:
            return self._block(
                state.order,
                "chain_exposure",
                pct,
                limits.chain_exposure_pct,
                "on-chain exposure would exceed the limit",
            )
        return None

    def _check_position_notional(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        if state.order.side is not Side.BUY:
            return None
        cap = book.equity_usd * limits.max_position_pct / 100.0
        existing = book.position_usd_by_pair.get(state.order.pair_id, 0.0)
        allowed = cap - existing
        if state.notional > allowed:
            if allowed < book.min_order_usd:
                return self._block(
                    state.order,
                    "position_notional",
                    existing + state.notional,
                    cap,
                    "position is already at its cap",
                )
            state.notional = allowed
            state.reductions.append("position_notional")
        return None

    def _check_pair_concentration(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        # "Sector" on Pons is the paired stock: several tokens can pair with the
        # same stock, and buying all of them is one bet, not five.
        if state.order.side is not Side.BUY or book.equity_usd <= 0.0:
            return None
        existing = book.stock_exposure_usd_by_symbol.get(ctx.stock_symbol, 0.0)
        pct = (existing + state.notional) / book.equity_usd * 100.0
        if pct > limits.max_sector_pct:
            return self._block(
                state.order,
                "pair_concentration",
                pct,
                limits.max_sector_pct,
                "too much exposure to one paired stock",
            )
        return None

    def _check_adv(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        if ctx.volume_24h_usd < limits.min_adv_usd:
            return self._block(
                state.order,
                "adv",
                ctx.volume_24h_usd,
                limits.min_adv_usd,
                "24h on-chain volume below minimum",
            )
        return None

    def _check_stock_feed_fresh(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        max_age = self._session.feed_max_age_s(
            limits.feed_max_age_s, limits.feed_max_age_offhours_s, now
        )
        if ctx.feed_age_s is None:
            return self._block(
                state.order, "stock_feed_fresh", -1.0, max_age, "no stock price feed reading"
            )
        if ctx.feed_age_s > max_age:
            return self._block(
                state.order,
                "stock_feed_fresh",
                ctx.feed_age_s,
                max_age,
                "stock price feed is stale",
            )
        return None

    def _check_stockback_live(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        # Sells are exempt: leaving a pair whose stock-back died is the point.
        if state.order.side is Side.BUY and ctx.stockback_rate_daily < limits.stockback_min_rate:
            return self._block(
                state.order,
                "stockback_live",
                ctx.stockback_rate_daily,
                limits.stockback_min_rate,
                "stock-back accrual is below the live threshold",
            )
        return None

    def _check_underlying_ok(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        if state.order.side is not Side.BUY or ctx.underlying_return_5d_pct is None:
            return None
        if ctx.underlying_return_5d_pct < -limits.underlying_max_drawdown_pct:
            return self._block(
                state.order,
                "underlying_ok",
                ctx.underlying_return_5d_pct,
                -limits.underlying_max_drawdown_pct,
                "paired stock is in freefall",
            )
        return None

    def _check_eth_reserve(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        remaining = book.eth_balance - ctx.gas_estimate_eth
        if remaining < limits.eth_gas_reserve:
            return self._block(
                state.order,
                "eth_reserve",
                remaining,
                limits.eth_gas_reserve,
                "swap would dip into the gas reserve",
            )
        return None

    def _check_per_order_ceiling(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        if state.notional > limits.per_order_max_usd:
            state.notional = limits.per_order_max_usd
            state.reductions.append("per_order_ceiling")
        return None

    def _check_daily_ceiling(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        remaining = limits.daily_notional_max_usd - book.daily_notional_used_usd
        if remaining < book.min_order_usd:
            return self._block(
                state.order,
                "daily_ceiling",
                book.daily_notional_used_usd,
                limits.daily_notional_max_usd,
                "24h notional ceiling reached",
            )
        if state.notional > remaining:
            state.notional = remaining
            state.reductions.append("daily_ceiling")
        return None

    def _check_slippage(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        if state.order.max_slippage_bps > limits.max_slippage_bps:
            return self._block(
                state.order,
                "slippage",
                float(state.order.max_slippage_bps),
                float(limits.max_slippage_bps),
                "order tolerates more slippage than the limit allows",
            )
        if ctx.expected_slippage_bps > limits.max_slippage_bps:
            return self._block(
                state.order,
                "slippage",
                ctx.expected_slippage_bps,
                float(limits.max_slippage_bps),
                "quoted slippage above the cap",
            )
        return None

    def _check_price_impact(
        self, state: _Eval, book: PonsBook, ctx: PonsPairContext, limits: RiskLimits, now: datetime
    ) -> PonsRiskDecision | None:
        if ctx.price_impact_bps > limits.max_price_impact_bps:
            return self._block(
                state.order,
                "price_impact",
                ctx.price_impact_bps,
                float(limits.max_price_impact_bps),
                "modeled price impact above the floor",
            )
        return None
