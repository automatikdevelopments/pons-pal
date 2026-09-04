# SPDX-License-Identifier: MIT
# Pons Family - SQLite persistence for pons.family
"""Trades, P&L snapshots, risk events, the stock-back ledger, and control state.

Every statement is parameterized; no SQL is ever built from a string. The
database is a cache of what the chain already records for fills, plus the
things the chain does not record: refusals, breaker state, and the kill switch.
Breaker state is persisted so a restart cannot clear a trip. An agent that
re-arms itself by crashing and coming back is not risk-managed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pons_pal.models import PonsBreaker, PonsFill, PonsRiskDecision, Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    side TEXT NOT NULL,
    amount_token REAL NOT NULL,
    amount_usd REAL NOT NULL,
    price_usd REAL NOT NULL,
    gas_eth REAL NOT NULL,
    tx_hash TEXT,
    simulated INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_ts ON trades (ts);
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    ts TEXT PRIMARY KEY,
    equity_usd REAL NOT NULL,
    pnl_today_usd REAL NOT NULL,
    drawdown_pct REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    action TEXT NOT NULL,
    check_name TEXT,
    value REAL,
    limit_value REAL,
    reason TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stockback_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT NOT NULL,
    stock_symbol TEXT NOT NULL,
    accrued_units REAL NOT NULL,
    accrued_usd REAL NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS breakers (
    name TEXT PRIMARY KEY,
    tripped INTEGER NOT NULL,
    value REAL,
    limit_value REAL,
    tripped_at TEXT
);
CREATE TABLE IF NOT EXISTS controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class PonsStore:
    """A thin, parameterized wrapper over one SQLite file.

    Args:
        path: Database file; ``":memory:"`` for tests.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._conn.cursor()
        try:
            cursor.execute("BEGIN")
            yield cursor
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
        finally:
            cursor.close()

    # --- trades -----------------------------------------------------------------

    def record_fill(self, fill: PonsFill) -> None:
        """Insert a fill; duplicate fill ids are ignored so a retry is idempotent."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO trades
                (fill_id, order_id, pair_id, side, amount_token, amount_usd, price_usd,
                 gas_eth, tx_hash, simulated, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.pair_id,
                    fill.side.value,
                    fill.amount_token,
                    fill.amount_usd,
                    fill.price_usd,
                    fill.gas_eth,
                    fill.tx_hash,
                    int(fill.simulated),
                    _iso(fill.ts),
                ),
            )

    def fills_since(self, since: datetime) -> list[PonsFill]:
        """Fills at or after ``since``, oldest first."""
        rows = self._conn.execute(
            """
            SELECT fill_id, order_id, pair_id, side, amount_token, amount_usd, price_usd,
                   gas_eth, tx_hash, simulated, ts
            FROM trades WHERE ts >= ? ORDER BY ts ASC
            """,
            (_iso(since),),
        ).fetchall()
        return [
            PonsFill(
                fill_id=row[0],
                order_id=row[1],
                pair_id=row[2],
                side=Side(row[3]),
                amount_token=row[4],
                amount_usd=row[5],
                price_usd=row[6],
                gas_eth=row[7],
                tx_hash=row[8],
                simulated=bool(row[9]),
                ts=datetime.fromisoformat(row[10]),
            )
            for row in rows
        ]

    def notional_since(self, since: datetime) -> float:
        """Sum of fill notional at or after ``since``; feeds the 24h ceiling."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM trades WHERE ts >= ?", (_iso(since),)
        ).fetchone()
        return float(row[0]) if row else 0.0

    # --- p&l --------------------------------------------------------------------

    def record_snapshot(
        self, ts: datetime, equity_usd: float, pnl_today_usd: float, drawdown_pct: float
    ) -> None:
        """Upsert a P&L snapshot keyed by timestamp."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO pnl_snapshots (ts, equity_usd, pnl_today_usd, drawdown_pct)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                    equity_usd = excluded.equity_usd,
                    pnl_today_usd = excluded.pnl_today_usd,
                    drawdown_pct = excluded.drawdown_pct
                """,
                (_iso(ts), equity_usd, pnl_today_usd, drawdown_pct),
            )

    def latest_snapshot(self) -> tuple[datetime, float, float, float] | None:
        """The most recent snapshot as ``(ts, equity, pnl_today, drawdown)``."""
        row = self._conn.execute(
            "SELECT ts, equity_usd, pnl_today_usd, drawdown_pct FROM pnl_snapshots "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row[0]), float(row[1]), float(row[2]), float(row[3])

    def first_equity_since(self, since: datetime) -> float | None:
        """Equity of the earliest snapshot at or after ``since``; the anchor for period P&L."""
        row = self._conn.execute(
            "SELECT equity_usd FROM pnl_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
            (_iso(since),),
        ).fetchone()
        return float(row[0]) if row else None

    # --- risk -------------------------------------------------------------------

    def record_risk(self, decision: PonsRiskDecision) -> None:
        """Append a gate decision. Refusals are recorded exactly like passes."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO risk_events
                (order_id, action, check_name, value, limit_value, reason, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.order_id,
                    decision.action.value,
                    decision.check,
                    decision.value,
                    decision.limit,
                    decision.reason,
                    _iso(decision.ts),
                ),
            )

    def risk_counts(self) -> dict[str, int]:
        """Count of decisions by action."""
        rows = self._conn.execute(
            "SELECT action, COUNT(*) FROM risk_events GROUP BY action"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    # --- stock-back -------------------------------------------------------------

    def record_stockback(
        self,
        pair_id: str,
        stock_symbol: str,
        accrued_units: float,
        accrued_usd: float,
        ts: datetime,
    ) -> None:
        """Append an accrual observation."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO stockback_ledger (pair_id, stock_symbol, accrued_units, accrued_usd, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pair_id, stock_symbol, accrued_units, accrued_usd, _iso(ts)),
            )

    def stockback_total_usd(self) -> float:
        """Total stock-back accrued in USD across every pair."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(accrued_usd), 0) FROM stockback_ledger"
        ).fetchone()
        return float(row[0]) if row else 0.0

    # --- breakers ---------------------------------------------------------------

    def save_breaker(self, breaker: PonsBreaker) -> None:
        """Upsert one breaker's state."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO breakers (name, tripped, value, limit_value, tripped_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    tripped = excluded.tripped,
                    value = excluded.value,
                    limit_value = excluded.limit_value,
                    tripped_at = excluded.tripped_at
                """,
                (
                    breaker.name,
                    int(breaker.tripped),
                    breaker.value,
                    breaker.limit,
                    _iso(breaker.tripped_at) if breaker.tripped_at else None,
                ),
            )

    def load_breakers(self) -> dict[str, PonsBreaker]:
        """Every persisted breaker keyed by name."""
        rows = self._conn.execute(
            "SELECT name, tripped, value, limit_value, tripped_at FROM breakers"
        ).fetchall()
        return {
            str(row[0]): PonsBreaker(
                name=str(row[0]),
                tripped=bool(row[1]),
                value=row[2],
                limit=row[3],
                tripped_at=_parse(row[4]),
            )
            for row in rows
        }

    # --- controls ---------------------------------------------------------------

    def set_control(self, key: str, value: str, ts: datetime) -> None:
        """Upsert a control flag such as the kill switch."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO controls (key, value, ts) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, ts = excluded.ts
                """,
                (key, value, _iso(ts)),
            )

    def get_control(self, key: str) -> str | None:
        """Read a control flag."""
        row = self._conn.execute("SELECT value FROM controls WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None
