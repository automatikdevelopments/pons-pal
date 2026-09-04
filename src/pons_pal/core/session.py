# SPDX-License-Identifier: MIT
# Pons Family - trading session and market hours for pons.family
"""Robinhood Chain trades around the clock; the stocks behind it do not.

A Chainlink stock feed idles when the underlying market is closed, so the
staleness limit that is right at 14:00 New York time is wrong at 03:00. The
session tells the risk gate which limit applies. It is deliberately simple:
weekday regular hours only, no holiday calendar, because a missing holiday
makes the gate stricter (it expects a fresh feed and blocks), never looser.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from pons_pal.models import utcnow

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


class TradingSession:
    """Answers "is the stock market open right now" for the feed-freshness check."""

    def __init__(self, *, chain_always_open: bool = True) -> None:
        self._chain_always_open = chain_always_open

    def chain_open(self, _now: datetime | None = None) -> bool:
        """The chain venue is always open unless configured otherwise."""
        return self._chain_always_open

    def stock_market_open(self, now: datetime | None = None) -> bool:
        """True during regular NYSE hours on a weekday (no holiday calendar; see module note)."""
        current = (now or utcnow()).astimezone(NEW_YORK)
        if current.weekday() >= 5:
            return False
        return REGULAR_OPEN <= current.time() < REGULAR_CLOSE

    def feed_max_age_s(
        self, market_hours_s: float, off_hours_s: float, now: datetime | None = None
    ) -> float:
        """Pick the applicable feed staleness limit for ``now``."""
        return market_hours_s if self.stock_market_open(now) else off_hours_s
