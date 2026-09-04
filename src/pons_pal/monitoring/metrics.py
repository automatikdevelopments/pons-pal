# SPDX-License-Identifier: MIT
# Pons Family - Prometheus metrics for pons.family
"""Counters, gauges, and a histogram for the agent's health and book.

Only non-sensitive numbers are exported: equity, P&L, drawdown, counts,
latencies. No key, no address beyond what is already public, no per-trade
detail that would let a scraper front-run the book. The server binds to
loopback by default; expose it through a scrape-authenticating proxy.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server


class PonsMetrics:
    """The metric set, bound to its own registry so tests can instantiate it freely."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        reg = self.registry
        self.equity_usd = Gauge("pons_pal_equity_usd", "Book equity in USD", registry=reg)
        self.pnl_today_usd = Gauge("pons_pal_pnl_today_usd", "Intraday P&L in USD", registry=reg)
        self.drawdown_pct = Gauge(
            "pons_pal_drawdown_pct", "Drawdown from peak equity, percent", registry=reg
        )
        self.wallet_balance_usd = Gauge(
            "pons_pal_wallet_balance_usd", "Budget wallet balance in USD", registry=reg
        )
        self.positions_count = Gauge("pons_pal_positions_count", "Open positions", registry=reg)
        self.fills_total = Counter(
            "pons_pal_fills_total",
            "Fills by side and whether simulated",
            ["side", "simulated"],
            registry=reg,
        )
        self.chain_errors_total = Counter(
            "pons_pal_chain_errors_total", "Chain RPC failures", registry=reg
        )
        self.risk_blocks_total = Counter(
            "pons_pal_risk_blocks_total",
            "Orders blocked by the risk gate, by check",
            ["check"],
            registry=reg,
        )
        self.stockback_accrued_usd = Gauge(
            "pons_pal_stockback_accrued_usd", "Stock-back accrued across pairs in USD", registry=reg
        )
        self.order_latency_seconds = Histogram(
            "pons_pal_order_latency_seconds",
            "Seconds from order to fill",
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
            registry=reg,
        )
        self.armed = Gauge("pons_pal_armed", "1 when live execution is possible", registry=reg)
        self.breaker_tripped = Gauge(
            "pons_pal_breaker_tripped",
            "1 when a circuit breaker is tripped",
            ["breaker"],
            registry=reg,
        )
        self.feed_age_seconds = Gauge(
            "pons_pal_feed_age_seconds",
            "Age of the latest reading per feed",
            ["feed"],
            registry=reg,
        )

    def serve(self, host: str, port: int) -> None:
        """Start the HTTP exporter. Loopback by default; see the module note."""
        start_http_server(port, addr=host, registry=self.registry)
