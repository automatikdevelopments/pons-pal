# SPDX-License-Identifier: MIT
# Pons Family - historical bar loading for backtests for pons.family
"""Load bars from CSV into validated ``PonsBar`` records.

The loader goes through pandas for parsing and through the model for
validation. A row that fails validation is dropped and counted rather than
coerced, because a backtest that quietly accepts a zero low is a backtest that
will report a drawdown that never happened.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import ValidationError

from pons_pal.data.normalizer import RawTick
from pons_pal.errors import ConfigError
from pons_pal.models import PonsBar

REQUIRED_COLUMNS = ("pair_id", "ts", "open", "high", "low", "close", "volume_usd")


def load_bars_csv(path: Path) -> tuple[list[PonsBar], int]:
    """Read a CSV of bars; return ``(bars, dropped)``.

    Raises:
        ConfigError: if the file is unreadable or lacks a required column.
    """
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(str(path), f"cannot read CSV: {type(exc).__name__}") from None
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ConfigError(str(path), f"missing columns: {', '.join(missing)}")
    bars: list[PonsBar] = []
    dropped = 0
    records: list[dict[str, Any]] = [
        {str(k): v for k, v in row.items()} for row in frame.to_dict(orient="records")
    ]
    for row in records:
        try:
            ts = pd.Timestamp(row["ts"]).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            bars.append(
                PonsBar(
                    pair_id=str(row["pair_id"]),
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume_usd=float(row["volume_usd"]),
                )
            )
        except (ValidationError, ValueError, TypeError, KeyError):
            dropped += 1
    bars.sort(key=lambda bar: (bar.ts, bar.pair_id))
    return bars, dropped


def bars_to_ticks(bars: list[PonsBar]) -> list[RawTick]:
    """Replay bars as close-price ticks for the ``ReplayFeed``."""
    return [
        RawTick(
            pair_id=bar.pair_id,
            ts=bar.ts,
            price_usd=bar.close,
            volume_usd=bar.volume_usd,
            source="replay",
        )
        for bar in bars
    ]
