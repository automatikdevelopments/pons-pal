# SPDX-License-Identifier: MIT
# Pons Family - event bus and the trading engine for pons.family
"""One event loop, six event types, five stages, and a book that only fills can change.

The bus dispatches each event to its handlers in priority order and drains
until nothing is queued. A cycle publishes ticks, lets the strategies turn
them into signals, lets the builder turn signals into orders, lets the gate
judge every order, and lets the router fill what the gate approved. Every
stage is a handler on the bus rather than a direct call so the order of the
pipeline is a property of the wiring, not of whichever module imported which.

The engine owns the book. Strategies see a copy. The gate sees a snapshot.
Only a ``FillEvent`` mutates positions, and a ``FillEvent`` exists only
because the router produced one after the gate approved the order.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta

import structlog

from pons_pal.adapters.sentiment import RetailSentimentReader
from pons_pal.adapters.stockback import StockBackLedger, stockback_for
from pons_pal.agent.notify import Notifier
from pons_pal.config import EngineConfig, RiskLimits
from pons_pal.core.events import (
    FillEvent,
    OrderEvent,
    PonsEvent,
    RiskEvent,
    SentimentEvent,
    SignalEvent,
    TickEvent,
)
from pons_pal.core.portfolio import PonsPortfolioView, PortfolioBuilder
from pons_pal.core.risk import PonsBook, PonsPairContext, RiskGate
from pons_pal.core.universe import PonsUniverse
from pons_pal.data.cache import RollingCache
from pons_pal.data.feeds import MarketFeed, PairSource, StockDataSource
from pons_pal.data.normalizer import TickNormalizer
from pons_pal.errors import ChainError, NotArmedError, PonsPalError, RiskBlocked
from pons_pal.execution.router import ExecutionRouter
from pons_pal.models import (
    ArmState,
    Mode,
    PonsBar,
    PonsBreaker,
    PonsFeedReading,
    PonsFeedStatus,
    PonsFill,
    PonsModel,
    PonsOrder,
    PonsPair,
    PonsPalState,
    PonsPosition,
    PonsRiskDecision,
    PonsSignal,
    PonsStockBack,
    RiskAction,
    Side,
    utcnow,
)
from pons_pal.monitoring.metrics import PonsMetrics
from pons_pal.store import PonsStore
from pons_pal.strategies.base import PonsStrategyContext, Strategy

log = structlog.get_logger(__name__)

Handler = Callable[[PonsEvent], Awaitable[None]]

CONTROL_DISARMED = "disarmed"
PAPER_ETH_BALANCE = 0.1
STOCK_HISTORY_BARS = 60


class EventBus:
    """Priority-ordered async dispatch over a single queue."""

    def __init__(self) -> None:
        self._handlers: dict[type[PonsEvent], list[tuple[int, Handler]]] = defaultdict(list)
        self._queue: asyncio.Queue[PonsEvent] = asyncio.Queue()
        self.dispatched = 0

    def subscribe(self, event_type: type[PonsEvent], handler: Handler, priority: int = 0) -> None:
        """Register ``handler`` for ``event_type``; lower ``priority`` runs first."""
        self._handlers[event_type].append((priority, handler))
        self._handlers[event_type].sort(key=lambda item: item[0])

    async def publish(self, event: PonsEvent) -> None:
        """Queue an event for dispatch."""
        await self._queue.put(event)

    async def dispatch(self, event: PonsEvent) -> None:
        """Deliver one event to its handlers in order."""
        for _, handler in self._handlers.get(type(event), ()):
            await handler(event)
        self.dispatched += 1

    async def drain(self) -> int:
        """Dispatch until the queue is empty; returns how many events were handled."""
        handled = 0
        while not self._queue.empty():
            event = self._queue.get_nowait()
            await self.dispatch(event)
            handled += 1
        return handled


class PonsCycleReport(PonsModel):
    """What one cycle did, for the CLI and tests."""

    started_at: datetime
    pairs: int
    bars: int
    signals: int
    orders: int
    decisions: tuple[PonsRiskDecision, ...]
    fills: tuple[PonsFill, ...]
    arm_state: ArmState


class PonsPalEngine:
    """The wired pipeline. Construct through ``pons_pal.app.assemble_engine``."""

    def __init__(
        self,
        *,
        mode: Mode,
        config: EngineConfig,
        limits: Callable[[], RiskLimits],
        universe: PonsUniverse,
        market_feed: MarketFeed,
        stock_data: StockDataSource,
        pair_source: PairSource,
        strategies: Sequence[Strategy],
        builder: PortfolioBuilder,
        risk: RiskGate,
        router: ExecutionRouter,
        store: PonsStore,
        metrics: PonsMetrics,
        notifier: Notifier,
        budget_address: str | None,
        sentiment: RetailSentimentReader | None = None,
        eth_balance: Callable[[], Awaitable[float]] | None = None,
    ) -> None:
        self._mode = mode
        self._config = config
        self._limits = limits
        self._universe = universe
        self._market_feed = market_feed
        self._stock_data = stock_data
        self._pair_source = pair_source
        self._strategies = list(strategies)
        self._builder = builder
        self._risk = risk
        self._router = router
        self._store = store
        self._metrics = metrics
        self._notifier = notifier
        self._budget_address = budget_address
        self._sentiment_reader = sentiment
        self._eth_balance_reader = eth_balance

        self._bus = EventBus()
        self._normalizer = TickNormalizer(config.engine.bar_interval_s)
        self._cache = RollingCache(config.engine.history_bars)
        self._ledger = StockBackLedger()
        self._cash = config.engine.paper_equity_usd
        self._positions: dict[str, PonsPosition] = {}
        self._peak_equity = self._cash
        self._pairs: dict[str, PonsPair] = {}
        self._stock_readings: dict[str, PonsFeedReading] = {}
        self._stock_history: dict[str, list[float]] = {}
        self._sentiment: dict[str, tuple[float, datetime]] = {}
        self._stockback: dict[str, PonsStockBack] = {}
        self._eth_balance = PAPER_ETH_BALANCE if mode is Mode.PAPER else 0.0
        self._disarmed = store.get_control(CONTROL_DISARMED) == "true"
        self._last_bar_ts: datetime | None = None
        self._now: datetime = utcnow()
        self._cycle_lock = asyncio.Lock()

        self._signals: list[PonsSignal] = []
        self._decisions: list[PonsRiskDecision] = []
        self._fills: list[PonsFill] = []
        self._order_started: dict[str, float] = {}

        bus = self._bus
        bus.subscribe(TickEvent, self._on_tick)
        bus.subscribe(SentimentEvent, self._on_sentiment)
        bus.subscribe(SignalEvent, self._on_signal)
        bus.subscribe(OrderEvent, self._on_order)
        bus.subscribe(RiskEvent, self._on_risk)
        bus.subscribe(FillEvent, self._on_fill)

    # --- public surface ---------------------------------------------------------

    @property
    def mode(self) -> Mode:
        """Paper or live."""
        return self._mode

    @property
    def risk(self) -> RiskGate:
        """The gate, for the CLI's resume command."""
        return self._risk

    def current_limits(self) -> RiskLimits:
        """The limits in force right now (hot-reloaded)."""
        return self._limits()

    @property
    def positions(self) -> dict[str, PonsPosition]:
        """A copy of the open positions."""
        return dict(self._positions)

    def arm_state(self) -> ArmState:
        """Kill switch first, breakers second, then whether live execution is even possible."""
        if self._disarmed:
            return ArmState.DISARMED
        if self._risk.breakers.any_tripped:
            return ArmState.HALTED
        if self._router.live_capable:
            return ArmState.ARMED
        return ArmState.UNARMED

    def disarm(self) -> None:
        """Kill switch: stop execution immediately and persist it across restarts."""
        self._disarmed = True
        self._store.set_control(CONTROL_DISARMED, "true", utcnow())
        self._metrics.armed.set(0)
        log.warning("engine.disarmed")

    def clear_disarm(self, *, confirm: bool) -> None:
        """Lift the kill switch. Requires an explicit confirmation from a person."""
        if not confirm:
            raise NotArmedError("disarmed; confirmation required to re-arm")
        self._disarmed = False
        self._store.set_control(CONTROL_DISARMED, "false", utcnow())
        log.warning("engine.disarm_cleared")

    def is_disarmed(self) -> bool:
        """For the router's kill-switch callable."""
        return self._disarmed

    def equity_usd(self) -> float:
        """Cash plus every position's mark."""
        return self._cash + sum(p.mark_usd for p in self._positions.values())

    def view(self) -> PonsPortfolioView:
        """A frozen snapshot for the strategies and builder."""
        return PonsPortfolioView(
            equity_usd=self.equity_usd(), cash_usd=self._cash, positions=dict(self._positions)
        )

    def state(self, now: datetime | None = None) -> PonsPalState:
        """The machine self-report."""
        current = now or utcnow()
        equity = self.equity_usd()
        limits = self._limits()
        feeds = [
            PonsFeedStatus(
                name=f"chainlink:{symbol}",
                age_s=reading.age_s(current),
                fresh=reading.age_s(current) <= limits.feed_max_age_offhours_s,
            )
            for symbol, reading in sorted(self._stock_readings.items())
        ]
        market_age = (
            None
            if self._last_bar_ts is None
            else max(0.0, (current - self._last_bar_ts).total_seconds())
        )
        feeds.append(
            PonsFeedStatus(
                name="market:pons_pairs",
                age_s=market_age,
                fresh=market_age is not None
                and market_age <= self._config.engine.cycle_interval_s * 3,
            )
        )
        return PonsPalState(
            mode=self._mode,
            arm_state=self.arm_state(),
            budget_address=self._budget_address,
            equity_usd=max(0.0, equity),
            pnl_today_usd=equity - self._equity_at(self._day_start(current), equity),
            drawdown_pct=self._drawdown_pct(equity),
            positions_count=len(self._positions),
            breakers=self._risk.breakers.snapshot(),
            feeds=tuple(feeds),
            daily_notional_used_usd=self._store.notional_since(current - timedelta(hours=24)),
            stockback_accrued_usd=self._store.stockback_total_usd(),
            updated_at=current,
        )

    async def run_cycle(self, now: datetime | None = None) -> PonsCycleReport:
        """Run one full pass of the pipeline and return what happened."""
        async with self._cycle_lock:
            return await self._run_cycle(now or utcnow())

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Run cycles on the configured interval until ``stop`` is set."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.run_cycle()
            except PonsPalError as exc:
                log.error("engine.cycle_failed", error=exc.message)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._config.engine.cycle_interval_s)
            except TimeoutError:
                continue

    # --- the cycle ----------------------------------------------------------------

    async def _run_cycle(self, now: datetime) -> PonsCycleReport:
        self._signals.clear()
        self._decisions.clear()
        self._fills.clear()
        limits = self._limits()
        self._now = now

        pairs = self._universe.select(await self._pair_source.pairs())
        self._pairs = {pair.pair_id: pair for pair in pairs}

        ticks = await self._market_feed.poll()
        bars = self._normalizer.ingest(ticks) + self._normalizer.flush()
        for bar in bars:
            await self._bus.publish(TickEvent(bar=bar))
        await self._bus.drain()

        symbols = sorted({pair.stock.symbol for pair in pairs})
        readings = await self._stock_data.readings(symbols)
        self._stock_readings = dict(readings)
        for symbol in symbols:
            history = await self._stock_data.history(symbol, STOCK_HISTORY_BARS)
            if history:
                self._stock_history[symbol] = history
            self._metrics.feed_age_seconds.labels(feed=f"chainlink:{symbol}").set(
                readings[symbol].age_s(now) if symbol in readings else -1.0
            )

        if self._sentiment_reader is not None and symbols:
            blended = self._sentiment_reader.blend(await self._sentiment_reader.poll(symbols))
            for symbol, score in blended.items():
                await self._bus.publish(SentimentEvent(symbol=symbol, score=score, sources=1))
            await self._bus.drain()

        await self._refresh_eth_balance()
        self._mark_positions()
        self._observe_stockback(pairs, limits, now)
        self._record_snapshot(now)

        ctx = PonsStrategyContext(
            now=now,
            view=self.view().model_copy(deep=True),
            pairs=dict(self._pairs),
            history=self._cache.snapshot(),
            stock_readings=dict(self._stock_readings),
            stock_history={k: tuple(v) for k, v in self._stock_history.items()},
            sentiment=dict(self._sentiment),
            stockback=dict(self._stockback),
        )
        for strategy in self._strategies:
            try:
                for signal in strategy.generate(ctx):
                    await self._bus.publish(SignalEvent(signal=signal))
            except (ValueError, ZeroDivisionError, FloatingPointError) as exc:
                # One strategy's arithmetic must not take the cycle down.
                log.error("strategy.failed", strategy=strategy.name, error=type(exc).__name__)
        await self._bus.drain()

        orders = self._builder.build(self._signals, self.view(), self._cache.snapshot(), limits)
        for order in orders:
            await self._bus.publish(OrderEvent(order=order))
        await self._bus.drain()

        self._record_snapshot(now)
        self._publish_metrics(now)
        return PonsCycleReport(
            started_at=now,
            pairs=len(pairs),
            bars=len(bars),
            signals=len(self._signals),
            orders=len(orders),
            decisions=tuple(self._decisions),
            fills=tuple(self._fills),
            arm_state=self.arm_state(),
        )

    # --- handlers, in pipeline order ------------------------------------------------

    async def _on_tick(self, event: PonsEvent) -> None:
        if not isinstance(event, TickEvent):
            return
        self._cache.append(event.bar)
        if self._last_bar_ts is None or event.bar.ts > self._last_bar_ts:
            self._last_bar_ts = event.bar.ts

    async def _on_sentiment(self, event: PonsEvent) -> None:
        if not isinstance(event, SentimentEvent):
            return
        self._sentiment[event.symbol] = (event.score, event.ts)

    async def _on_signal(self, event: PonsEvent) -> None:
        if not isinstance(event, SignalEvent):
            return
        self._signals.append(event.signal)

    async def _on_order(self, event: PonsEvent) -> None:
        if not isinstance(event, OrderEvent):
            return
        order = event.order
        pair = self._pairs.get(order.pair_id)
        if pair is None:
            decision = PonsRiskDecision(
                order_id=order.order_id,
                action=RiskAction.BLOCK,
                check="universe",
                reason="pair left the universe",
                adjusted_notional_usd=0.0,
            )
        else:
            decision = self._risk.evaluate(
                order, self._book(), self._pair_context(order, pair), self._now
            )
        self._order_started[order.order_id] = time.monotonic()
        await self._bus.publish(RiskEvent(order=order, decision=decision))

    async def _on_risk(self, event: PonsEvent) -> None:
        if not isinstance(event, RiskEvent):
            return
        order, decision = event.order, event.decision
        self._decisions.append(decision)
        self._store.record_risk(decision)
        if decision.action is RiskAction.BLOCK:
            self._metrics.risk_blocks_total.labels(check=decision.check or "unknown").inc()
            log.warning(
                "risk.blocked",
                order_id=order.order_id,
                check=decision.check,
                reason=decision.reason,
            )
        await self._notifier.notify_decision(order, decision)
        if not decision.approved:
            return
        pair = self._pairs.get(order.pair_id)
        price = self._cache.last_close(order.pair_id)
        if pair is None or price is None:
            return
        quote = self._router.quote(pair, order.side, decision.adjusted_notional_usd or 0.0, price)
        try:
            fill = await self._router.execute(order, decision, pair, quote)
        except (RiskBlocked, NotArmedError) as exc:
            refusal = PonsRiskDecision(
                order_id=order.order_id,
                action=RiskAction.BLOCK,
                check=getattr(exc, "check", "execution"),
                reason=exc.message,
                adjusted_notional_usd=0.0,
            )
            self._decisions.append(refusal)
            self._store.record_risk(refusal)
            self._metrics.risk_blocks_total.labels(check=refusal.check or "execution").inc()
            log.warning("execution.refused", order_id=order.order_id, reason=exc.message)
            return
        except ChainError as exc:
            self._metrics.chain_errors_total.inc()
            log.error("execution.chain_error", order_id=order.order_id, reason=exc.reason)
            return
        except PonsPalError as exc:
            log.error("execution.failed", order_id=order.order_id, error=exc.message)
            return
        await self._bus.publish(FillEvent(fill=fill))

    async def _on_fill(self, event: PonsEvent) -> None:
        if not isinstance(event, FillEvent):
            return
        fill = event.fill
        self._apply_fill(fill)
        self._store.record_fill(fill)
        self._fills.append(fill)
        self._metrics.fills_total.labels(
            side=fill.side.value, simulated=str(fill.simulated).lower()
        ).inc()
        started = self._order_started.pop(fill.order_id, None)
        if started is not None:
            self._metrics.order_latency_seconds.observe(time.monotonic() - started)
        await self._notifier.notify_fill(fill)
        log.info(
            "fill",
            pair_id=fill.pair_id,
            side=fill.side.value,
            amount_usd=round(fill.amount_usd, 2),
            simulated=fill.simulated,
        )

    # --- book -----------------------------------------------------------------------

    def _apply_fill(self, fill: PonsFill) -> None:
        pair = self._pairs.get(fill.pair_id)
        symbol = pair.stock.symbol if pair else "UNKNOWN"
        current = self._positions.get(fill.pair_id)
        if fill.side is Side.BUY:
            self._cash -= fill.amount_usd
            amount = (current.token_amount if current else 0.0) + fill.amount_token
            basis = (current.cost_basis_usd if current else 0.0) + fill.amount_usd
            self._positions[fill.pair_id] = PonsPosition(
                pair_id=fill.pair_id,
                stock_symbol=symbol,
                token_amount=amount,
                cost_basis_usd=basis,
                mark_usd=amount * fill.price_usd,
                stockback_accrued_units=current.stockback_accrued_units if current else 0.0,
            )
            return
        if current is None:
            return
        fraction = (
            min(1.0, fill.amount_token / current.token_amount) if current.token_amount > 0 else 1.0
        )
        self._cash += fill.amount_usd
        remaining = current.token_amount * (1.0 - fraction)
        if remaining <= 1e-12:
            del self._positions[fill.pair_id]
            return
        self._positions[fill.pair_id] = current.model_copy(
            update={
                "token_amount": remaining,
                "cost_basis_usd": current.cost_basis_usd * (1.0 - fraction),
                "mark_usd": remaining * fill.price_usd,
            }
        )

    def _mark_positions(self) -> None:
        for pair_id, position in list(self._positions.items()):
            price = self._cache.last_close(pair_id)
            if price is not None:
                self._positions[pair_id] = position.model_copy(
                    update={"mark_usd": position.token_amount * price}
                )
        self._peak_equity = max(self._peak_equity, self.equity_usd())

    def _observe_stockback(
        self, pairs: Sequence[PonsPair], limits: RiskLimits, now: datetime
    ) -> None:
        self._stockback = {}
        for pair in pairs:
            position_usd = (
                self._positions[pair.pair_id].mark_usd if pair.pair_id in self._positions else 0.0
            )
            record = stockback_for(pair, position_usd, limits.stockback_min_rate)
            self._stockback[pair.pair_id] = record
            reading = self._stock_readings.get(pair.stock.symbol)
            if reading is None or position_usd <= 0.0:
                self._ledger.observe(record, 0.0, now)
                continue
            units, usd = self._ledger.observe(record, reading.price_usd, now)
            if units > 0.0:
                self._store.record_stockback(pair.pair_id, pair.stock.symbol, units, usd, now)
                position = self._positions[pair.pair_id]
                self._positions[pair.pair_id] = position.model_copy(
                    update={"stockback_accrued_units": position.stockback_accrued_units + units}
                )

    async def _refresh_eth_balance(self) -> None:
        if self._eth_balance_reader is None:
            return
        try:
            self._eth_balance = await self._eth_balance_reader()
        except ChainError as exc:
            # Unknown balance is treated as zero so the gas-reserve check blocks.
            self._eth_balance = 0.0
            self._metrics.chain_errors_total.inc()
            log.error("engine.eth_balance_failed", reason=exc.reason)

    def _book(self) -> PonsBook:
        now = self._now
        equity = self.equity_usd()
        by_pair = {pair_id: p.mark_usd for pair_id, p in self._positions.items()}
        by_symbol: dict[str, float] = defaultdict(float)
        for position in self._positions.values():
            by_symbol[position.stock_symbol] += position.mark_usd
        return PonsBook(
            equity_usd=equity,
            pnl_intraday_usd=equity - self._equity_at(self._day_start(now), equity),
            pnl_weekly_usd=equity - self._equity_at(now - timedelta(days=7), equity),
            pnl_monthly_usd=equity - self._equity_at(now - timedelta(days=30), equity),
            chain_exposure_usd=sum(by_pair.values()),
            position_usd_by_pair=by_pair,
            stock_exposure_usd_by_symbol=dict(by_symbol),
            eth_balance=self._eth_balance,
            daily_notional_used_usd=self._store.notional_since(now - timedelta(hours=24)),
            min_order_usd=self._config.capital.min_order_usd,
        )

    def _pair_context(self, order: PonsOrder, pair: PonsPair) -> PonsPairContext:
        reading = self._stock_readings.get(pair.stock.symbol)
        history = self._stock_history.get(pair.stock.symbol, [])
        return_5d: float | None = None
        if len(history) >= 6 and history[-6] > 0.0:
            return_5d = (history[-1] / history[-6] - 1.0) * 100.0
        price = self._cache.last_close(pair.pair_id) or 0.0
        quote = self._router.quote(
            pair, order.side, order.notional_usd, price if price > 0 else 1.0
        )
        record = self._stockback.get(pair.pair_id)
        return PonsPairContext(
            pair_id=pair.pair_id,
            stock_symbol=pair.stock.symbol,
            volume_24h_usd=pair.volume_24h_usd,
            feed_age_s=None if reading is None else reading.age_s(self._now),
            stockback_rate_daily=record.accrual_rate_daily if record else 0.0,
            underlying_return_5d_pct=return_5d,
            expected_slippage_bps=quote.expected_slippage_bps,
            price_impact_bps=quote.price_impact_bps,
            gas_estimate_eth=quote.gas_estimate_eth,
        )

    @staticmethod
    def _day_start(now: datetime) -> datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _equity_at(self, since: datetime, fallback: float) -> float:
        value = self._store.first_equity_since(since)
        return fallback if value is None else value

    def _drawdown_pct(self, equity: float) -> float:
        if self._peak_equity <= 0.0:
            return 0.0
        return max(0.0, (self._peak_equity - equity) / self._peak_equity * 100.0)

    def _record_snapshot(self, now: datetime) -> None:
        equity = self.equity_usd()
        pnl_today = equity - self._equity_at(self._day_start(now), equity)
        self._store.record_snapshot(now, equity, pnl_today, self._drawdown_pct(equity))

    def _publish_metrics(self, now: datetime) -> None:
        state = self.state(now)
        self._metrics.equity_usd.set(state.equity_usd)
        self._metrics.pnl_today_usd.set(state.pnl_today_usd)
        self._metrics.drawdown_pct.set(state.drawdown_pct)
        self._metrics.wallet_balance_usd.set(self._cash)
        self._metrics.positions_count.set(state.positions_count)
        self._metrics.stockback_accrued_usd.set(state.stockback_accrued_usd)
        self._metrics.armed.set(1 if state.arm_state is ArmState.ARMED else 0)
        for breaker in state.breakers:
            self._metrics.breaker_tripped.labels(breaker=breaker.name).set(
                1 if breaker.tripped else 0
            )


def breakers_text(breakers: Sequence[PonsBreaker]) -> str:
    """One-line breaker summary for logs and the CLI."""
    return ", ".join(f"{b.name}={'tripped' if b.tripped else 'clear'}" for b in breakers)


def last_bar(bars: Sequence[PonsBar]) -> PonsBar | None:
    """The newest bar in a sequence, if any."""
    return bars[-1] if bars else None
