# SPDX-License-Identifier: MIT
# Pons Family - Robinhood retail sentiment reader for pons.family
"""Blend Twitter/X, Stocktwits, and news RSS into one score per stock symbol.

Every source is untrusted text. Payloads are bounded, parsed with a hardened
XML parser, reduced to a number per symbol, and the symbols themselves are
validated against the ticker pattern before they are used as keys. The blend
is a weighted mean over the sources that responded; with fewer than
``min_sources`` there is no score at all, because one noisy source is not a
signal.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import structlog
from defusedxml import ElementTree

from pons_pal.config import SentimentConfig
from pons_pal.errors import NetworkGuardError
from pons_pal.models import TICKER_RE, PonsModel, Signed, Ticker, utcnow
from pons_pal.net import SafeHttpClient

log = structlog.get_logger(__name__)

POSITIVE_WORDS = frozenset(
    {"bull", "bullish", "buy", "long", "moon", "breakout", "beat", "upgrade", "strong"}
)
NEGATIVE_WORDS = frozenset(
    {"bear", "bearish", "sell", "short", "dump", "miss", "downgrade", "weak", "crash"}
)
WORD_RE = re.compile(r"[a-z]+")
MAX_TEXT_LEN = 2000


class SentimentReading(PonsModel):
    """One source's score for one symbol."""

    symbol: Ticker
    score: Signed
    source: str
    items: int
    ts: datetime


def score_text(text: str) -> float | None:
    """Lexicon score of one message in [-1, 1]; ``None`` when it carries no sentiment words."""
    words = WORD_RE.findall(text[:MAX_TEXT_LEN].lower())
    positive = sum(1 for w in words if w in POSITIVE_WORDS)
    negative = sum(1 for w in words if w in NEGATIVE_WORDS)
    total = positive + negative
    if total == 0:
        return None
    return (positive - negative) / total


def blend(
    readings: Sequence[SentimentReading], weights: dict[str, float], min_sources: int
) -> dict[str, float]:
    """Weighted mean per symbol across sources; symbols with too few sources are omitted."""
    totals: dict[str, float] = {}
    weight_sum: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    for reading in readings:
        weight = weights.get(reading.source, 0.0)
        if weight <= 0.0:
            continue
        totals[reading.symbol] = totals.get(reading.symbol, 0.0) + weight * reading.score
        weight_sum[reading.symbol] = weight_sum.get(reading.symbol, 0.0) + weight
        sources.setdefault(reading.symbol, set()).add(reading.source)
    return {
        symbol: max(-1.0, min(1.0, totals[symbol] / weight_sum[symbol]))
        for symbol in totals
        if len(sources[symbol]) >= min_sources and weight_sum[symbol] > 0.0
    }


def parse_rss_titles(body: bytes, limit: int) -> list[str]:
    """Titles from an RSS or Atom document, bounded, via defusedxml."""
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return []
    titles: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if tag == "title" and element.text:
            titles.append(element.text[:MAX_TEXT_LEN])
            if len(titles) >= limit:
                break
    return titles


def parse_message_texts(body: bytes, limit: int) -> list[str]:
    """Texts from ``{"messages": [{"body": ...}]}`` or ``{"data": [{"text": ...}]}`` payloads."""
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    items: Any = payload.get("messages") or payload.get("data") or []
    if not isinstance(items, list):
        return []
    texts: list[str] = []
    for item in items[:limit]:
        if isinstance(item, dict):
            text = item.get("body") or item.get("text")
            if isinstance(text, str):
                texts.append(text[:MAX_TEXT_LEN])
    return texts


def readings_from_texts(
    symbol: str, texts: Iterable[str], source: str, now: datetime
) -> SentimentReading | None:
    """Reduce a batch of texts for one symbol to one reading."""
    if not TICKER_RE.match(symbol):
        return None
    scores = [s for s in (score_text(t) for t in texts) if s is not None]
    if not scores:
        return None
    return SentimentReading(
        symbol=symbol, score=sum(scores) / len(scores), source=source, items=len(scores), ts=now
    )


class RetailSentimentReader:
    """Fetches each enabled source through the guard and blends the results."""

    def __init__(self, config: SentimentConfig, http: SafeHttpClient) -> None:
        self._config = config
        self._http = http

    @property
    def weights(self) -> dict[str, float]:
        """Source weights for enabled sources."""
        return {name: src.weight for name, src in self._config.sources.items() if src.enabled}

    async def _fetch(self, url: str) -> bytes | None:
        try:
            status, body = await self._http.request_bytes("GET", url)
        except NetworkGuardError as exc:
            log.warning("sentiment.refused", reason=exc.reason)
            return None
        return body if status == 200 else None

    async def poll(self, symbols: Sequence[str]) -> list[SentimentReading]:
        """Collect one reading per (source, symbol) that returned usable text."""
        now = utcnow()
        limit = self._config.blend.max_items_per_source
        readings: list[SentimentReading] = []
        for name, source in self._config.sources.items():
            if not source.enabled:
                continue
            if name == "rss":
                titles: list[str] = []
                for feed_url in source.feeds:
                    body = await self._fetch(feed_url)
                    if body:
                        titles.extend(parse_rss_titles(body, limit))
                for symbol in symbols:
                    mentioned = [t for t in titles if symbol in t.upper()]
                    reading = readings_from_texts(symbol, mentioned, name, now)
                    if reading:
                        readings.append(reading)
                continue
            if not source.base_url:
                continue
            for symbol in symbols:
                if not TICKER_RE.match(symbol):
                    continue
                # TODO(pons): confirm each provider's path shape; this is the generic form.
                body = await self._fetch(f"{source.base_url.rstrip('/')}/symbol/{symbol}")
                if not body:
                    continue
                reading = readings_from_texts(symbol, parse_message_texts(body, limit), name, now)
                if reading:
                    readings.append(reading)
        return readings

    def blend(self, readings: Sequence[SentimentReading]) -> dict[str, float]:
        """Blend readings with the configured weights and minimum source count."""
        return blend(readings, self.weights, self._config.blend.min_sources)
