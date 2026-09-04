# SPDX-License-Identifier: MIT
# Pons Family - typed models for everything that crosses a boundary for pons.family
"""Pydantic models for config, chain data, signals, orders, fills, and state.

Everything that enters the engine from outside (a decoded pool read, a provider
response, a model number) is parsed into one of these before it is used. The
models are frozen and forbid unknown fields, and they reject NaN and infinity,
because a NaN that reaches position sizing becomes a NaN order and a NaN order
is an unbounded one. Names and tickers are attacker-controlled: anyone can launch
a pair, so they are bounded and restricted to printable characters before they
are stored or displayed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
PAIR_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")
PRINTABLE_RE = re.compile(r"^[\x20-\x7e]*$")

MAX_NAME_LEN = 64
MAX_RATIONALE_LEN = 512


def utcnow() -> datetime:
    """Return an aware UTC timestamp; naive datetimes are never used in the engine."""
    return datetime.now(tz=UTC)


class PonsModel(BaseModel):
    """Base for every first-party model: frozen, strict about fields, rejects NaN."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, str_strip_whitespace=True
    )


class Mode(StrEnum):
    """Execution mode. ``paper`` simulates fills against live quotes; ``live`` submits swaps."""

    PAPER = "paper"
    LIVE = "live"


class ArmState(StrEnum):
    """Whether the agent may submit on-chain transactions.

    ``UNARMED`` means no trading key (or no router) is configured. ``ARMED`` means
    live execution is possible. ``DISARMED`` is the operator kill switch.
    ``HALTED`` means a circuit breaker is tripped and awaits a manual resume.
    """

    UNARMED = "unarmed"
    ARMED = "armed"
    DISARMED = "disarmed"
    HALTED = "halted"


class Side(StrEnum):
    """Order direction relative to the Pons token of the pair."""

    BUY = "buy"
    SELL = "sell"


class RiskAction(StrEnum):
    """Outcome of the risk gate for one order. ``ALLOW`` is recorded as ``PASS``."""

    ALLOW = "PASS"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"


class PairStage(StrEnum):
    """Where a pair trades: still on the bonding curve, or graduated into a Uniswap V4 pool."""

    CURVE = "curve"
    GRADUATED = "graduated"


Address = Annotated[str, Field(pattern=ADDRESS_RE.pattern)]
TxHash = Annotated[str, Field(pattern=TX_HASH_RE.pattern)]
Ticker = Annotated[str, Field(pattern=TICKER_RE.pattern)]
PairId = Annotated[str, Field(pattern=PAIR_ID_RE.pattern)]
DisplayName = Annotated[
    str, Field(min_length=1, max_length=MAX_NAME_LEN, pattern=PRINTABLE_RE.pattern)
]
Rationale = Annotated[str, Field(max_length=MAX_RATIONALE_LEN, pattern=PRINTABLE_RE.pattern)]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]
Signed = Annotated[float, Field(ge=-1.0, le=1.0)]
NonNegative = Annotated[float, Field(ge=0.0)]
Positive = Annotated[float, Field(gt=0.0)]


def sanitize_display(text: str, limit: int = MAX_NAME_LEN) -> str:
    """Bound and strip a token-supplied string to printable ASCII.

    Token names come from whoever launched the pair. They are never trusted to
    be short, printable, or free of control characters, so the untrusted string
    is reduced to something safe to log or render rather than rejected outright
    (a rejected pair would still need a name in the refusal record).
    """
    cleaned = "".join(ch for ch in text if 0x20 <= ord(ch) <= 0x7E)
    cleaned = cleaned.strip()
    return cleaned[:limit] or "unnamed"


class PonsStockToken(PonsModel):
    """A tokenized stock on Robinhood Chain: an ERC-20 with economic exposure to the underlying."""

    symbol: Ticker
    name: DisplayName
    address: Address
    feed_address: Address | None = None
    decimals: int = Field(default=18, ge=0, le=36)


class PonsPair(PonsModel):
    """A Pons V2 token-stock pair and the pool facts the strategies and gate need."""

    pair_id: PairId
    token_symbol: DisplayName
    token_name: DisplayName
    token_address: Address
    pool_address: Address
    stock: PonsStockToken
    stage: PairStage
    stockback_share: UnitInterval
    volume_24h_usd: NonNegative
    fee_flow_24h_usd: NonNegative
    liquidity_usd: NonNegative
    created_at: datetime

    @field_validator("token_symbol", "token_name", mode="before")
    @classmethod
    def _sanitize(cls, value: object) -> str:
        return sanitize_display(str(value))


class PonsBar(PonsModel):
    """A canonical OHLCV bar for one pair, in USD."""

    pair_id: PairId
    ts: datetime
    open: Positive
    high: Positive
    low: Positive
    close: Positive
    volume_usd: NonNegative

    @model_validator(mode="after")
    def _ordered(self) -> PonsBar:
        if self.low > self.high:
            raise ValueError("bar low exceeds high")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("bar open/close outside low/high")
        return self


class PonsFeedReading(PonsModel):
    """A decoded Chainlink round for a stock feed.

    ``answer`` must be positive: a zero or negative stock price from a feed is
    a broken feed, not a price, and a broken feed must block rather than size.
    """

    feed_address: Address
    symbol: Ticker
    price_usd: Positive
    updated_at: datetime
    round_id: int = Field(ge=0)
    decimals: int = Field(ge=0, le=36)

    def age_s(self, now: datetime | None = None) -> float:
        """Seconds since the feed last updated, never negative."""
        current = now or utcnow()
        return max(0.0, (current - self.updated_at).total_seconds())


class PonsSignal(PonsModel):
    """One strategy's view on one pair.

    ``score`` is signed: positive favors the token leg, negative favors exiting.
    ``confidence`` scales how much of the score the portfolio builder will act on.
    """

    strategy: DisplayName
    pair_id: PairId
    score: Signed
    confidence: UnitInterval
    horizon_s: int = Field(gt=0)
    rationale: Rationale = ""
    ts: datetime = Field(default_factory=utcnow)

    @field_validator("rationale", mode="before")
    @classmethod
    def _bound_rationale(cls, value: object) -> str:
        return sanitize_display(str(value), MAX_RATIONALE_LEN) if value else ""


class PonsOrder(PonsModel):
    """An intended trade, before the risk gate has seen it."""

    order_id: PairId
    pair_id: PairId
    side: Side
    notional_usd: Positive
    max_slippage_bps: int = Field(ge=0, le=10_000)
    strategies: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)

    def with_notional(self, notional_usd: float) -> PonsOrder:
        """Return a copy resized by the risk gate."""
        return self.model_copy(update={"notional_usd": notional_usd})


class PonsRiskDecision(PonsModel):
    """The gate's verdict on one order, recorded whether it passed or not."""

    order_id: PairId
    action: RiskAction
    check: str | None = None
    value: float | None = None
    limit: float | None = None
    reason: Rationale = ""
    adjusted_notional_usd: NonNegative | None = None
    ts: datetime = Field(default_factory=utcnow)

    @property
    def approved(self) -> bool:
        """True for PASS and REDUCE; the order may proceed at ``adjusted_notional_usd``."""
        return self.action is not RiskAction.BLOCK


class PonsFill(PonsModel):
    """A confirmed (or simulated) execution."""

    fill_id: PairId
    order_id: PairId
    pair_id: PairId
    side: Side
    amount_token: NonNegative
    amount_usd: NonNegative
    price_usd: Positive
    gas_eth: NonNegative = 0.0
    tx_hash: TxHash | None = None
    simulated: bool = True
    ts: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _live_fills_have_hashes(self) -> PonsFill:
        if not self.simulated and self.tx_hash is None:
            raise ValueError("a live fill must carry its transaction hash")
        return self


class PonsPosition(PonsModel):
    """An open holding in one pair's token, marked in USD."""

    pair_id: PairId
    stock_symbol: Ticker
    token_amount: NonNegative
    cost_basis_usd: NonNegative
    mark_usd: NonNegative
    stockback_accrued_units: NonNegative = 0.0

    @property
    def unrealized_pnl_usd(self) -> float:
        """Mark minus cost."""
        return self.mark_usd - self.cost_basis_usd


class PonsStockBack(PonsModel):
    """Stock-back accrual facts for one pair and the position held in it."""

    pair_id: PairId
    stock_symbol: Ticker
    fee_flow_24h_usd: NonNegative
    stockback_share: UnitInterval
    liquidity_usd: NonNegative
    position_usd: NonNegative
    accrual_rate_daily: NonNegative
    live: bool


class PonsBreaker(PonsModel):
    """The persisted state of one circuit breaker."""

    name: str
    tripped: bool
    value: float | None = None
    limit: float | None = None
    tripped_at: datetime | None = None


class PonsFeedStatus(PonsModel):
    """Freshness of one ingest feed as reported on the disclosure surface."""

    name: str
    age_s: NonNegative | None
    fresh: bool


class PonsPalState(PonsModel):
    """The machine self-report: what the agent is, what it holds, and whether it may trade."""

    mode: Mode
    arm_state: ArmState
    budget_address: Address | None
    equity_usd: NonNegative
    pnl_today_usd: float
    drawdown_pct: NonNegative
    positions_count: int = Field(ge=0)
    breakers: tuple[PonsBreaker, ...]
    feeds: tuple[PonsFeedStatus, ...]
    daily_notional_used_usd: NonNegative
    stockback_accrued_usd: NonNegative
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def can_execute_live(self) -> bool:
        """Only an armed agent in live mode with no tripped breaker submits a swap."""
        return self.mode is Mode.LIVE and self.arm_state is ArmState.ARMED
