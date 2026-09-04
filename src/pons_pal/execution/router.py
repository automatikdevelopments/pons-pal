# SPDX-License-Identifier: MIT
# Pons Family - execution router for pons.family
"""Quote a pair, decide whether the fill is simulated or real, and carry it out.

The router is the last line before value moves, so it re-checks what the gate
already decided rather than trusting it: the decision must be approved, the
kill switch must be off, the mode must be live and the signer armed for a real
swap, and the quote must still clear the slippage and impact limits at the
moment of submission. Anything else is a simulated fill in paper mode or a
refusal in live mode. There is no path that submits without every check.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from pons_pal.adapters.chain import RobinhoodChainClient, load_abi
from pons_pal.config import RiskLimits, RouterSection
from pons_pal.errors import ChainError, ExecutionError, NotArmedError, RiskBlocked
from pons_pal.execution.fills import confirm_fill, simulated_fill
from pons_pal.execution.slippage import expected_slippage_bps, min_amount_out, price_impact_bps
from pons_pal.models import Mode, PonsFill, PonsModel, PonsOrder, PonsPair, PonsRiskDecision, Side
from pons_pal.signer import PonsSigner

log = structlog.get_logger(__name__)

DEFAULT_GAS_ESTIMATE_ETH = 0.0005


class PonsQuote(PonsModel):
    """What a swap of ``notional_usd`` in a pair is expected to cost."""

    pair_id: str
    side: Side
    notional_usd: float
    price_usd: float
    expected_slippage_bps: float
    price_impact_bps: float
    gas_estimate_eth: float


class ExecutionRouter:
    """Routes approved orders to a simulated or on-chain fill.

    Args:
        mode: Paper or live.
        signer: The signing boundary; unarmed in paper mode.
        limits: Current risk limits, re-checked at submission.
        router_config: Router address and ABI path; blank keeps execution unarmed.
        chain: Chain client, required only for live execution.
        kill_switch: Returns True when the operator has disarmed the agent.
    """

    def __init__(
        self,
        *,
        mode: Mode,
        signer: PonsSigner,
        limits: Callable[[], RiskLimits],
        router_config: RouterSection,
        chain: RobinhoodChainClient | None = None,
        kill_switch: Callable[[], bool] = lambda: False,
    ) -> None:
        self._mode = mode
        self._signer = signer
        self._limits = limits
        self._router = router_config
        self._chain = chain
        self._kill_switch = kill_switch
        self._abi: list[dict[str, Any]] | None = None

    @property
    def mode(self) -> Mode:
        """Configured mode."""
        return self._mode

    @property
    def live_capable(self) -> bool:
        """True only when mode, signer, router address, ABI, and chain client all line up."""
        if self._mode is not Mode.LIVE or not self._signer.armed or self._chain is None:
            return False
        if not self._router.address:
            return False
        if self._abi is None:
            try:
                self._abi = load_abi(Path(self._router.abi_path))
            except ChainError:
                return False
        return True

    def quote(self, pair: PonsPair, side: Side, notional_usd: float, price_usd: float) -> PonsQuote:
        """Model the cost of a swap from pool liquidity; the live quote is confirmed on-chain."""
        return PonsQuote(
            pair_id=pair.pair_id,
            side=side,
            notional_usd=notional_usd,
            price_usd=price_usd,
            expected_slippage_bps=expected_slippage_bps(notional_usd, pair.liquidity_usd),
            price_impact_bps=price_impact_bps(notional_usd, pair.liquidity_usd),
            gas_estimate_eth=DEFAULT_GAS_ESTIMATE_ETH,
        )

    def _final_checks(
        self, order: PonsOrder, decision: PonsRiskDecision, quote: PonsQuote
    ) -> float:
        """Re-verify at the boundary; return the notional to execute."""
        if not decision.approved or decision.adjusted_notional_usd is None:
            raise RiskBlocked(
                decision.check or "gate", decision.value or 0.0, decision.limit or 0.0
            )
        if self._kill_switch():
            raise NotArmedError("disarmed")
        limits = self._limits()
        if quote.expected_slippage_bps > limits.max_slippage_bps:
            raise RiskBlocked(
                "slippage", quote.expected_slippage_bps, float(limits.max_slippage_bps)
            )
        if quote.price_impact_bps > limits.max_price_impact_bps:
            raise RiskBlocked(
                "price_impact", quote.price_impact_bps, float(limits.max_price_impact_bps)
            )
        notional = min(decision.adjusted_notional_usd, order.notional_usd, limits.per_order_max_usd)
        if notional <= 0.0:
            raise RiskBlocked("notional", notional, 0.0)
        return notional

    async def execute(
        self, order: PonsOrder, decision: PonsRiskDecision, pair: PonsPair, quote: PonsQuote
    ) -> PonsFill:
        """Fill the order: simulated in paper mode, on-chain in live mode.

        Raises:
            RiskBlocked: if the decision or the quote fails the final checks.
            NotArmedError: in live mode without an armed signer and router.
            ExecutionError: if the swap cannot be built, sent, or confirmed.
        """
        notional = self._final_checks(order, decision, quote)
        if self._mode is Mode.PAPER:
            return simulated_fill(order, notional, quote.price_usd, quote.price_impact_bps)
        if not self.live_capable or self._chain is None or self._abi is None:
            raise NotArmedError("unarmed")
        return await self._execute_live(order, pair, quote, notional)

    async def _execute_live(
        self, order: PonsOrder, pair: PonsPair, quote: PonsQuote, notional: float
    ) -> PonsFill:
        if self._chain is None or self._abi is None:
            raise NotArmedError("unarmed")
        sender = self._signer.address
        if sender is None:
            raise NotArmedError("unarmed")
        expected_tokens = notional / quote.price_usd
        floor = min_amount_out(expected_tokens, order.max_slippage_bps)
        # TODO(pons): confirm the router's swap function name and argument order
        # from the published ABI. The call below is the shape, not the fact.
        function = "swapExactIn" if order.side is Side.BUY else "swapExactOut"
        args: list[Any] = [
            pair.pool_address,
            int(notional * 10**6),
            int(floor * 10**pair.stock.decimals),
            sender,
        ]
        data = self._chain.encode_swap(self._abi, self._router.address, function, args)
        tx = await self._chain.build_eip1559_tx(sender, self._router.address, data)
        signed = self._signer.sign(tx)
        tx_hash = await self._chain.send_raw(bytes(signed))
        log.info(
            "execution.submitted", pair_id=pair.pair_id, side=order.side.value, tx_hash=tx_hash
        )
        receipt = await self._chain.wait_receipt(tx_hash)
        if receipt.get("status") != 1:
            raise ExecutionError("confirm", "transaction reverted")
        # TODO(pons): decode the actual amounts from the router's swap event once
        # the ABI is known; until then the fill is recorded at the quoted price.
        return confirm_fill(
            order,
            receipt,
            amount_token=expected_tokens,
            amount_usd=notional,
            price_usd=quote.price_usd,
        )
