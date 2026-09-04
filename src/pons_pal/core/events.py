# SPDX-License-Identifier: MIT
# Pons Family - typed events for the bus for pons.family
"""The six event types that flow through the engine, in pipeline order.

``TickEvent`` and ``SentimentEvent`` enter from ingestion; strategies emit
``SignalEvent``; the portfolio builder emits ``OrderEvent``; the risk gate
emits ``RiskEvent``; execution emits ``FillEvent``. Events are frozen so a
handler cannot mutate what a later handler sees: a strategy that could edit an
order on its way to the gate would be a strategy that bypasses the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pons_pal.models import (
    PonsBar,
    PonsFill,
    PonsOrder,
    PonsRiskDecision,
    PonsSignal,
    utcnow,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PonsEvent:
    """Base event. ``ts`` is when the event was created, not when it was handled."""

    ts: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True, kw_only=True)
class TickEvent(PonsEvent):
    """A canonical bar for one pair."""

    bar: PonsBar


@dataclass(frozen=True, slots=True, kw_only=True)
class SentimentEvent(PonsEvent):
    """A blended retail sentiment score for one stock symbol in [-1, 1]."""

    symbol: str
    score: float
    sources: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEvent(PonsEvent):
    """A strategy's signal for one pair."""

    signal: PonsSignal


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderEvent(PonsEvent):
    """An order proposed by the portfolio builder, not yet risk-checked."""

    order: PonsOrder


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskEvent(PonsEvent):
    """The gate's decision, paired with the order it judged."""

    order: PonsOrder
    decision: PonsRiskDecision


@dataclass(frozen=True, slots=True, kw_only=True)
class FillEvent(PonsEvent):
    """A confirmed or simulated fill."""

    fill: PonsFill
