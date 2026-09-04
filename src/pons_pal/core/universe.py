# SPDX-License-Identifier: MIT
# Pons Family - the tradeable pair universe for pons.family
"""Decide which Pons pairs the strategies are allowed to look at.

Anyone can launch a pair, so the universe is a filter, not a list. A pair is
admitted only when it has graduated (bonding-curve pricing is thin and
reflexive), has real liquidity and volume, and its stock has a price feed the
gate can check. The filter runs before any strategy so a strategy can never
develop a view on a pair the gate would refuse anyway.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from pons_pal.config import FeedsSection, UniverseSection
from pons_pal.models import PairStage, PonsPair

log = structlog.get_logger(__name__)


class PonsUniverse:
    """Filters raw pair records down to the tradeable set."""

    def __init__(self, config: UniverseSection, feeds: FeedsSection) -> None:
        self._config = config
        self._feeds = feeds

    def feed_address_for(self, pair: PonsPair) -> str | None:
        """The Chainlink aggregator for the pair's stock, from the pair or the config map."""
        if pair.stock.feed_address is not None:
            return pair.stock.feed_address
        return self._feeds.stock_feeds.get(pair.stock.symbol)

    def reject_reason(self, pair: PonsPair) -> str | None:
        """Why a pair is excluded, or ``None`` if it is admitted."""
        if pair.stage is PairStage.CURVE and not self._config.allow_bonding_curve:
            return "not graduated"
        if pair.liquidity_usd < self._config.min_liquidity_usd:
            return "liquidity below minimum"
        if pair.volume_24h_usd < self._config.min_volume_24h_usd:
            return "volume below minimum"
        if self._config.require_stock_feed and self.feed_address_for(pair) is None:
            return "no stock price feed"
        return None

    def select(self, pairs: Iterable[PonsPair]) -> list[PonsPair]:
        """Admit pairs that pass every rule, best volume first, capped at ``max_pairs``."""
        admitted: list[PonsPair] = []
        for pair in pairs:
            reason = self.reject_reason(pair)
            if reason is None:
                admitted.append(pair)
            else:
                log.debug("universe.rejected", pair_id=pair.pair_id, reason=reason)
        admitted.sort(key=lambda p: p.volume_24h_usd, reverse=True)
        return admitted[: self._config.max_pairs]
