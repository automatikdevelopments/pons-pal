# SPDX-License-Identifier: MIT
# Pons Family - per-trade notifications for pons.family
"""Post a JSON summary of every order decision and fill to the operator's webhook.

The webhook URL comes from operator configuration and nowhere else: never from
a pair record, a token name, or a model response. It goes through the same
outbound guard as everything else. A failed notification is logged and
dropped; it must never block or retry into the trading loop, because a
notification channel that can stall execution is a channel an attacker can use
to stall execution.
"""

from __future__ import annotations

from typing import Any

import structlog

from pons_pal.errors import NetworkGuardError
from pons_pal.models import PonsFill, PonsOrder, PonsRiskDecision
from pons_pal.net import SafeHttpClient

log = structlog.get_logger(__name__)


class Notifier:
    """Sends Discord/Telegram/X-shaped JSON to one webhook."""

    def __init__(self, http: SafeHttpClient | None, webhook_url: str | None) -> None:
        self._http = http
        self._url = webhook_url
        if self._http is not None and self._url is not None:
            # Fail at startup, not at the first trade, if the URL is not allowed.
            self._http.check(self._url)

    @property
    def enabled(self) -> bool:
        """True when a webhook is configured."""
        return self._http is not None and self._url is not None

    async def _post(self, payload: dict[str, Any]) -> None:
        if self._http is None or self._url is None:
            return
        try:
            status, _ = await self._http.request_bytes("POST", self._url, json_body=payload)
        except NetworkGuardError as exc:
            log.warning("notify.refused", reason=exc.reason)
            return
        if status >= 300:
            log.warning("notify.status", status=status)

    async def notify_decision(self, order: PonsOrder, decision: PonsRiskDecision) -> None:
        """Announce a gate decision, including refusals."""
        text = (
            f"Pons Pal {decision.action.value}: {order.side.value} {order.pair_id} "
            f"{order.notional_usd:,.2f} USD"
        )
        if decision.check:
            text += f" [{decision.check}: {decision.reason}]"
        await self._post(
            {
                "content": text,
                "text": text,
                "event": "decision",
                "order_id": order.order_id,
                "pair_id": order.pair_id,
                "side": order.side.value,
                "notional_usd": order.notional_usd,
                "action": decision.action.value,
                "check": decision.check,
                "reason": decision.reason,
            }
        )

    async def notify_fill(self, fill: PonsFill) -> None:
        """Announce a fill."""
        kind = "paper fill" if fill.simulated else "fill"
        text = (
            f"Pons Pal {kind}: {fill.side.value} {fill.pair_id} {fill.amount_usd:,.2f} USD "
            f"at {fill.price_usd:,.6f}"
        )
        if fill.tx_hash:
            text += f" tx {fill.tx_hash}"
        await self._post(
            {
                "content": text,
                "text": text,
                "event": "fill",
                "fill_id": fill.fill_id,
                "pair_id": fill.pair_id,
                "side": fill.side.value,
                "amount_usd": fill.amount_usd,
                "price_usd": fill.price_usd,
                "simulated": fill.simulated,
                "tx_hash": fill.tx_hash,
            }
        )
