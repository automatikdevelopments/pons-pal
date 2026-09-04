# SPDX-License-Identifier: MIT
# Pons Family - rolling bar cache for pons.family
"""A bounded in-memory history per pair, with an optional Redis mirror.

Strategies need the last N bars and nothing older, so the cache is a deque per
pair and forgets on its own. Redis is a mirror, not a source of truth: if it is
configured the bars are also written there so a restarted process can warm up,
but a Redis failure never blocks a cycle. An agent that cannot trade because
its cache is down is safe; one that trades on a half-warm cache is not, which
is why a cold cache simply yields fewer signals rather than stale ones.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from pons_pal.models import PonsBar

log = structlog.get_logger(__name__)


class BarMirror(Protocol):
    """A key-value mirror with the two operations the cache uses."""

    def rpush(self, key: str, value: str) -> Any:
        """Append ``value`` to the list at ``key``."""
        ...

    def ltrim(self, key: str, start: int, end: int) -> Any:
        """Keep only the slice ``[start, end]`` of the list at ``key``."""
        ...

    def lrange(self, key: str, start: int, end: int) -> Any:
        """Return the slice ``[start, end]`` of the list at ``key``."""
        ...


class RollingCache:
    """Per-pair bar history bounded at ``max_bars``."""

    def __init__(
        self, max_bars: int, mirror: BarMirror | None = None, namespace: str = "pons_pal"
    ) -> None:
        if max_bars <= 0:
            raise ValueError("max_bars must be positive")
        self._max = max_bars
        self._bars: dict[str, deque[PonsBar]] = defaultdict(lambda: deque(maxlen=max_bars))
        self._mirror = mirror
        self._namespace = namespace

    def _key(self, pair_id: str) -> str:
        return f"{self._namespace}:bars:{pair_id}"

    def append(self, bar: PonsBar) -> None:
        """Add a bar, evicting the oldest when full, and mirror it if configured."""
        self._bars[bar.pair_id].append(bar)
        if self._mirror is None:
            return
        try:
            key = self._key(bar.pair_id)
            self._mirror.rpush(key, bar.model_dump_json())
            self._mirror.ltrim(key, -self._max, -1)
        except Exception as exc:
            log.warning("cache.mirror_failed", error=type(exc).__name__)

    def extend(self, bars: Sequence[PonsBar]) -> None:
        """Append many bars."""
        for bar in bars:
            self.append(bar)

    def history(self, pair_id: str) -> tuple[PonsBar, ...]:
        """Oldest-to-newest bars for ``pair_id``."""
        return tuple(self._bars.get(pair_id, ()))

    def snapshot(self) -> Mapping[str, tuple[PonsBar, ...]]:
        """Immutable copy of every pair's history, for the strategies."""
        return {pair_id: tuple(bars) for pair_id, bars in self._bars.items()}

    def last_close(self, pair_id: str) -> float | None:
        """Most recent close for ``pair_id``."""
        bars = self._bars.get(pair_id)
        return bars[-1].close if bars else None

    def warm(self, pair_id: str) -> int:
        """Load bars for ``pair_id`` from the mirror; returns how many were accepted."""
        if self._mirror is None:
            return 0
        try:
            raw = self._mirror.lrange(self._key(pair_id), -self._max, -1)
        except Exception as exc:
            log.warning("cache.warm_failed", error=type(exc).__name__)
            return 0
        accepted = 0
        for item in raw or []:
            try:
                text = item.decode() if isinstance(item, bytes) else str(item)
                self._bars[pair_id].append(PonsBar.model_validate(json.loads(text)))
                accepted += 1
            except (ValidationError, ValueError, UnicodeDecodeError):
                continue
        return accepted
