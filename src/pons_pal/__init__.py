# SPDX-License-Identifier: MIT
# Pons Family - package root for pons.family
"""Pons Pal: an autonomous trader over Pons RWA token-stock pairs on Robinhood Chain.

The pipeline is ``ingest -> signal -> portfolio -> risk gate -> execute`` over a
typed event bus. The package is non-custodial, runs in paper mode by default,
and stays unarmed for on-chain execution until a trading key that derives the
published budget wallet is present.
"""

from __future__ import annotations

__version__ = "0.1.0"

BANNER = (
    "Pons Pal: agentic trading over Pons RWA pairs on Robinhood Chain.\n"
    "Non-custodial, paper by default. Risk limits and circuit breakers are enforced in code.\n"
    "Significant risk including total loss. An AI agent can err. Not financial advice."
)

__all__ = ["BANNER", "__version__"]
