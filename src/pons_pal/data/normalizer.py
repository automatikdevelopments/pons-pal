# SPDX-License-Identifier: MIT
# Pons Family - tick-to-bar normalizer for pons.family
"""Fold raw ticks from any feed into canonical ``PonsBar`` records.

Every feed speaks a different dialect (a pool swap event, a WebSocket trade,
a provider quote). The normalizer is the one place that knows the canonical
shape, so strategies only ever see bars. A tick is validated on the way in and
a bar on the way out; a tick with a zero or negative price is dropped and
counted, not turned into a bar with a zero low.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import Field, ValidationError

from pons_pal.models import NonNegative, PairId, PonsBar, PonsModel, Positive


class RawTick(PonsModel):
    """A single validated trade or quote observation."""

    pair_id: PairId
    ts: datetime
    price_usd: Positive
    volume_usd: NonNegative = 0.0
    source: str = Field(default="unknown", max_length=32)


@dataclass
class _OpenBar:
    """A bar still accumulating ticks."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class TickNormalizer:
    """Accumulates ticks into fixed-width bars.

    Args:
        bar_interval_s: Width of each bar in seconds.
    """

    def __init__(self, bar_interval_s: int) -> None:
        if bar_interval_s <= 0:
            raise ValueError("bar_interval_s must be positive")
        self._interval = bar_interval_s
        self._open: dict[str, _OpenBar] = {}
        self.dropped = 0

    def bucket(self, ts: datetime) -> datetime:
        """Floor ``ts`` to the start of its bar."""
        epoch = int(ts.astimezone(UTC).timestamp())
        start = epoch - (epoch % self._interval)
        return datetime.fromtimestamp(start, tz=UTC)

    def parse(self, raw: dict[str, object]) -> RawTick | None:
        """Validate one raw mapping into a ``RawTick``; ``None`` if it is malformed."""
        try:
            return RawTick.model_validate(raw)
        except ValidationError:
            self.dropped += 1
            return None

    def ingest(self, ticks: Iterable[RawTick]) -> list[PonsBar]:
        """Fold ticks in; return bars that closed because a newer bucket arrived."""
        closed: list[PonsBar] = []
        for tick in sorted(ticks, key=lambda t: t.ts):
            start = self.bucket(tick.ts)
            current = self._open.get(tick.pair_id)
            if current is not None and current.ts != start:
                closed.append(self._to_bar(tick.pair_id, current))
                current = None
            if current is None:
                self._open[tick.pair_id] = _OpenBar(
                    ts=start,
                    open=tick.price_usd,
                    high=tick.price_usd,
                    low=tick.price_usd,
                    close=tick.price_usd,
                    volume=tick.volume_usd,
                )
                continue
            current.high = max(current.high, tick.price_usd)
            current.low = min(current.low, tick.price_usd)
            current.close = tick.price_usd
            current.volume += tick.volume_usd
        return closed

    def flush(self) -> list[PonsBar]:
        """Close every open bar, for the end of a cycle or a replay."""
        bars = [self._to_bar(pair_id, state) for pair_id, state in self._open.items()]
        self._open.clear()
        return bars

    @staticmethod
    def _to_bar(pair_id: str, state: _OpenBar) -> PonsBar:
        return PonsBar(
            pair_id=pair_id,
            ts=state.ts,
            open=state.open,
            high=state.high,
            low=state.low,
            close=state.close,
            volume_usd=state.volume,
        )
