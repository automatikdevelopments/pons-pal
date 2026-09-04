# SPDX-License-Identifier: MIT
# Pons Family - market, stock, and pair feeds for pons.family
"""Sources of ticks, stock readings, and pair records, behind small protocols.

Each feed does one thing and returns validated models. The engine depends on
the protocols, not the classes, so a test or a paper run can replace the chain
with a replay. Live feeds fetch through the outbound guard; there is no code
path that reaches the network without it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

import structlog
from pydantic import ValidationError

from pons_pal.adapters.chain import RobinhoodChainClient
from pons_pal.data.normalizer import RawTick
from pons_pal.errors import ChainError, DecodeError, NetworkGuardError
from pons_pal.models import PonsFeedReading, PonsPair, utcnow
from pons_pal.net import SafeHttpClient

log = structlog.get_logger(__name__)


class MarketFeed(Protocol):
    """Yields pair ticks."""

    async def poll(self) -> list[RawTick]:
        """Return new ticks since the last poll."""
        ...


class StockDataSource(Protocol):
    """Yields stock prices and recent history for the paired stocks."""

    async def readings(self, symbols: Sequence[str]) -> dict[str, PonsFeedReading]:
        """Latest reading per symbol; missing symbols are simply absent."""
        ...

    async def history(self, symbol: str, bars: int) -> list[float]:
        """Recent closes, oldest first; empty if unavailable."""
        ...


class PairSource(Protocol):
    """Yields the current set of Pons pairs."""

    async def pairs(self) -> list[PonsPair]:
        """Every known pair; the universe filter decides which are tradeable."""
        ...


class ReplayFeed:
    """Serve a fixed list of ticks in batches; the paper-mode and test feed."""

    def __init__(self, ticks: Iterable[RawTick], batch: int = 500) -> None:
        self._ticks = list(ticks)
        self._batch = batch
        self._cursor = 0

    async def poll(self) -> list[RawTick]:
        """Return the next batch."""
        chunk = self._ticks[self._cursor : self._cursor + self._batch]
        self._cursor += len(chunk)
        return chunk

    @property
    def exhausted(self) -> bool:
        """True when every tick has been served."""
        return self._cursor >= len(self._ticks)


class StaticPairSource:
    """Serve the pairs listed inline in ``config/pons.yaml``."""

    def __init__(self, pairs: Iterable[PonsPair]) -> None:
        self._pairs = list(pairs)

    async def pairs(self) -> list[PonsPair]:
        """The configured pairs."""
        return list(self._pairs)


class StaticStockData:
    """Stock readings and history supplied up front, for paper runs and tests."""

    def __init__(
        self,
        readings: dict[str, PonsFeedReading],
        history: dict[str, Sequence[float]] | None = None,
    ) -> None:
        self._readings = dict(readings)
        self._history = {k: list(v) for k, v in (history or {}).items()}

    async def readings(self, symbols: Sequence[str]) -> dict[str, PonsFeedReading]:
        """Readings for the requested symbols."""
        return {s: self._readings[s] for s in symbols if s in self._readings}

    async def history(self, symbol: str, bars: int) -> list[float]:
        """Recent closes."""
        return list(self._history.get(symbol, []))[-bars:]


class ChainlinkStockData:
    """Stock prices from Chainlink feeds on Robinhood Chain, history from a provider.

    The feed map comes from ``config/pons.yaml``. History is optional: without a
    provider the strategies that need it stay quiet, and the gate's freshness
    check still runs on the on-chain reading.
    """

    def __init__(
        self,
        chain: RobinhoodChainClient,
        feed_map: dict[str, str],
        http: SafeHttpClient | None = None,
        provider_base: str | None = None,
        provider_key: str | None = None,
    ) -> None:
        self._chain = chain
        self._feed_map = dict(feed_map)
        self._http = http
        self._base = provider_base.rstrip("/") if provider_base else None
        self._key = provider_key

    async def readings(self, symbols: Sequence[str]) -> dict[str, PonsFeedReading]:
        """Read each symbol's aggregator; a failing feed is absent, not stale-by-default."""
        out: dict[str, PonsFeedReading] = {}
        for symbol in symbols:
            address = self._feed_map.get(symbol)
            if address is None:
                continue
            try:
                out[symbol] = await self._chain.read_chainlink(address, symbol)
            except (ChainError, DecodeError) as exc:
                log.warning("stock_feed.read_failed", symbol=symbol, reason=exc.message)
        return out

    async def history(self, symbol: str, bars: int) -> list[float]:
        """Fetch recent closes from the provider; empty on any failure."""
        if self._http is None or self._base is None:
            return []
        # TODO(pons): confirm the provider's path shape; this is the generic form.
        url = f"{self._base}/v1/bars/{symbol}?limit={int(bars)}"
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else None
        try:
            status, body = await self._http.request_bytes("GET", url, headers=headers)
        except NetworkGuardError as exc:
            log.warning("stock_history.refused", symbol=symbol, reason=exc.reason)
            return []
        if status != 200:
            return []
        return decode_close_series(body)


def decode_close_series(body: bytes) -> list[float]:
    """Parse ``{"bars": [{"c": <close>}, ...]}`` into finite positive closes."""
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    bars = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(bars, list):
        return []
    closes: list[float] = []
    for item in bars:
        if not isinstance(item, dict):
            continue
        value = item.get("c")
        if (
            isinstance(value, int | float)
            and value > 0
            and value == value
            and value != float("inf")
        ):
            closes.append(float(value))
    return closes


class PairIndexSource:
    """Fetch pairs from the Pons V2 index endpoint and validate each record."""

    def __init__(self, http: SafeHttpClient, index_url: str) -> None:
        self._http = http
        self._url = index_url

    async def pairs(self) -> list[PonsPair]:
        """Every valid pair in the index; malformed records are dropped and counted."""
        try:
            status, body = await self._http.request_bytes("GET", self._url)
        except NetworkGuardError as exc:
            log.warning("pair_index.refused", reason=exc.reason)
            return []
        if status != 200:
            log.warning("pair_index.status", status=status)
            return []
        return decode_pairs(body)


def decode_pairs(body: bytes, now: datetime | None = None) -> list[PonsPair]:
    """Validate an index payload of ``{"pairs": [...]}`` into ``PonsPair`` records."""
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    items = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out: list[PonsPair] = []
    dropped = 0
    for item in items[:2000]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        record = dict(item)
        record.setdefault("created_at", (now or utcnow()).isoformat())
        try:
            out.append(PonsPair.model_validate(record))
        except ValidationError:
            dropped += 1
    if dropped:
        log.warning("pair_index.dropped", count=dropped)
    return out


class ChainPoolFeed:
    """Ticks from Pons pool reads on Robinhood Chain.

    Reading a pool's spot price needs the pool ABI, which is not published
    here (see ``config/pons.yaml``). Until the operator provides it this feed
    returns nothing and says so once: an agent with no bars emits no signals,
    which is the safe failure.
    """

    def __init__(self, chain: RobinhoodChainClient, pairs: PairSource, abi_path: str) -> None:
        self._chain = chain
        self._pairs = pairs
        self._abi_path = abi_path
        self._warned = False

    async def poll(self) -> list[RawTick]:
        """Return spot ticks for every known pair, or nothing without an ABI."""
        # TODO(pons): decode pool reserves with the published pool ABI and
        # emit one RawTick per pair from the implied spot price.
        if not self._warned:
            log.warning(
                "market_feed.unavailable", reason="pool ABI not configured", abi_path=self._abi_path
            )
            self._warned = True
        return []
