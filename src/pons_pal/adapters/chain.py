# SPDX-License-Identifier: MIT
# Pons Family - Robinhood Chain client for pons.family
"""``web3.py`` client for reads, Chainlink decoding, and EIP-1559 swap building.

Robinhood Chain is an Arbitrum Orbit L2, so standard Ethereum tooling is the
whole SDK. The client verifies the chain id on first use and refuses to run
against anything else: a key that signs for chain 4663 must never be pointed at
a look-alike RPC. Every value read from the chain is decoded through a pydantic
model before it is used.

Nothing here invents an address. The router ABI is loaded from a file the
operator provides; until it exists the client cannot build a swap, and the
agent stays unarmed for execution.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import Web3Exception

from pons_pal.config import ChainSection
from pons_pal.errors import ChainError, DecodeError, ExecutionError
from pons_pal.models import ADDRESS_RE, PonsFeedReading

log = structlog.get_logger(__name__)

# Chainlink's public AggregatorV3Interface. This is the standard, published
# interface every Chainlink price feed exposes, not a Pons-specific address.
AGGREGATOR_V3_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

MAX_FUTURE_SKEW_S = 300
GWEI = 10**9


def decode_feed_round(
    feed_address: str, symbol: str, raw: Sequence[Any], decimals: int, now: datetime | None = None
) -> PonsFeedReading:
    """Validate a ``latestRoundData`` tuple into a ``PonsFeedReading``.

    Raises:
        DecodeError: for a non-positive answer, a bad tuple, absurd decimals,
            or an ``updatedAt`` in the future.
    """
    if len(raw) != 5:
        raise DecodeError("chainlink", "latestRoundData", "expected a 5-tuple")
    round_id, answer, _started, updated, _answered = raw
    if not isinstance(round_id, int) or not isinstance(answer, int) or not isinstance(updated, int):
        raise DecodeError("chainlink", "latestRoundData", "non-integer field")
    if not 0 <= decimals <= 36:
        raise DecodeError("chainlink", "decimals", "out of range")
    if answer <= 0:
        raise DecodeError("chainlink", "answer", "non-positive price")
    if round_id < 0 or updated <= 0:
        raise DecodeError("chainlink", "round", "negative round or zero timestamp")
    current = now or datetime.now(tz=UTC)
    updated_at = datetime.fromtimestamp(updated, tz=UTC)
    if (updated_at - current).total_seconds() > MAX_FUTURE_SKEW_S:
        raise DecodeError("chainlink", "updatedAt", "timestamp is in the future")
    price = answer / (10**decimals)
    if not math.isfinite(price) or price <= 0.0:
        raise DecodeError("chainlink", "answer", "price is not finite")
    return PonsFeedReading(
        feed_address=feed_address,
        symbol=symbol,
        price_usd=price,
        updated_at=updated_at,
        round_id=round_id,
        decimals=decimals,
    )


def load_abi(path: Path) -> list[dict[str, Any]]:
    """Load a JSON ABI from disk.

    Raises:
        ChainError: if the file is missing or not a JSON list.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ChainError(
            str(path), "router ABI is missing or unreadable; execution stays unarmed"
        ) from None
    if not isinstance(data, list):
        raise ChainError(str(path), "ABI must be a JSON list")
    return [entry for entry in data if isinstance(entry, dict)]


class RobinhoodChainClient:
    """Async JSON-RPC client bound to one chain id.

    Args:
        config: The ``chain`` block of ``config/pons.yaml``.
        rpc_http: Override for the RPC URL (from the environment).
    """

    def __init__(self, config: ChainSection, rpc_http: str | None = None) -> None:
        self._config = config
        self._rpc = rpc_http or config.rpc_http
        self._w3 = AsyncWeb3(
            AsyncHTTPProvider(self._rpc, request_kwargs={"timeout": config.request_timeout_s})
        )
        self._verified = False

    @property
    def endpoint(self) -> str:
        """The RPC URL, for error context."""
        return self._rpc

    async def verify_chain(self) -> int:
        """Confirm the RPC serves the configured chain id.

        Raises:
            ChainError: on RPC failure or a mismatched chain id.
        """
        try:
            chain_id = await self._w3.eth.chain_id
        except (Web3Exception, OSError, ValueError) as exc:
            raise ChainError(self._rpc, f"cannot read chain id: {type(exc).__name__}") from None
        if chain_id != self._config.chain_id:
            raise ChainError(
                self._rpc, f"chain id {chain_id} does not match configured {self._config.chain_id}"
            )
        self._verified = True
        return int(chain_id)

    async def eth_balance(self, address: str) -> float:
        """ETH balance of ``address`` in ether."""
        if not ADDRESS_RE.match(address):
            raise DecodeError("config", "address", "not an EVM address")
        try:
            wei = await self._w3.eth.get_balance(self._w3.to_checksum_address(address))
        except (Web3Exception, OSError, ValueError) as exc:
            raise ChainError(self._rpc, f"get_balance failed: {type(exc).__name__}") from None
        return int(wei) / 10**18

    async def read_chainlink(self, feed_address: str, symbol: str) -> PonsFeedReading:
        """Read and decode a Chainlink aggregator."""
        if not ADDRESS_RE.match(feed_address):
            raise DecodeError("config", "feed_address", "not an EVM address")
        contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(feed_address), abi=AGGREGATOR_V3_ABI
        )
        try:
            decimals = await contract.functions.decimals().call()
            raw = await contract.functions.latestRoundData().call()
        except (Web3Exception, OSError, ValueError) as exc:
            raise ChainError(self._rpc, f"chainlink read failed: {type(exc).__name__}") from None
        return decode_feed_round(feed_address, symbol, cast(Sequence[Any], raw), int(decimals))

    async def build_eip1559_tx(
        self,
        sender: str,
        to: str,
        data: bytes,
        value_wei: int = 0,
        gas_limit: int | None = None,
    ) -> dict[str, Any]:
        """Assemble an EIP-1559 transaction dict with the configured fee caps.

        The fee caps come from config rather than from the node's suggestion so a
        misbehaving RPC cannot talk the agent into an expensive transaction.
        """
        if not self._verified:
            await self.verify_chain()
        try:
            nonce = await self._w3.eth.get_transaction_count(
                self._w3.to_checksum_address(sender), "pending"
            )
        except (Web3Exception, OSError, ValueError) as exc:
            raise ChainError(self._rpc, f"nonce lookup failed: {type(exc).__name__}") from None
        tx: dict[str, Any] = {
            "chainId": self._config.chain_id,
            "from": self._w3.to_checksum_address(sender),
            "to": self._w3.to_checksum_address(to),
            "data": data,
            "value": value_wei,
            "nonce": int(nonce),
            "maxFeePerGas": int(self._config.max_fee_per_gas_gwei * GWEI),
            "maxPriorityFeePerGas": int(self._config.max_priority_fee_per_gas_gwei * GWEI),
            "type": 2,
        }
        if gas_limit is None:
            try:
                estimate = await self._w3.eth.estimate_gas(cast(Any, tx))
            except (Web3Exception, OSError, ValueError) as exc:
                raise ExecutionError("estimate_gas", type(exc).__name__) from None
            gas_limit = int(estimate * 1.2)
        tx["gas"] = gas_limit
        return tx

    async def send_raw(self, signed: bytes) -> str:
        """Broadcast a signed transaction and return its hash."""
        try:
            tx_hash = await self._w3.eth.send_raw_transaction(signed)
        except (Web3Exception, OSError, ValueError) as exc:
            raise ExecutionError("send", type(exc).__name__) from None
        return "0x" + bytes(tx_hash).hex()

    async def wait_receipt(self, tx_hash: str, timeout_s: float = 120.0) -> dict[str, Any]:
        """Wait for a receipt and return it as a plain dict."""
        try:
            receipt = await self._w3.eth.wait_for_transaction_receipt(
                cast(Any, tx_hash), timeout=timeout_s
            )
        except (Web3Exception, OSError, ValueError, TimeoutError) as exc:
            raise ExecutionError("receipt", type(exc).__name__) from None
        return dict(receipt)

    def encode_swap(
        self, abi: list[dict[str, Any]], router: str, function: str, args: Sequence[Any]
    ) -> bytes:
        """ABI-encode a router call. The function name and args come from the operator's ABI.

        Raises:
            ExecutionError: if the ABI does not expose ``function``.
        """
        contract = self._w3.eth.contract(address=self._w3.to_checksum_address(router), abi=abi)
        try:
            fn = contract.get_function_by_name(function)
            encoded = fn(*args)._encode_transaction_data()
        except (ValueError, Web3Exception) as exc:
            raise ExecutionError("encode", f"{function}: {type(exc).__name__}") from None
        return bytes.fromhex(encoded[2:] if encoded.startswith("0x") else encoded)
