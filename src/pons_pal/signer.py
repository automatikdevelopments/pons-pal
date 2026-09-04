# SPDX-License-Identifier: MIT
# Pons Family - the signing boundary for pons.family
"""The one place a transaction is signed.

Everything above this module works with unsigned transaction dicts; everything
below it works with raw bytes. The signer holds the account object and exposes
only its address and a ``sign`` method. Its ``repr`` shows the address alone, so
a signer that lands in a traceback or a debug dump reveals nothing.

``UnarmedSigner`` is the default. It has no key and refuses every signature, so
a code path that reaches it in paper mode fails loudly instead of silently
sending nothing.
"""

from __future__ import annotations

from typing import Any, Protocol

from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes

from pons_pal.errors import NotArmedError, SignerError


class PonsSigner(Protocol):
    """What the execution router needs from a signer."""

    @property
    def address(self) -> str | None:
        """Checksummed address the signer signs for, or ``None`` when unarmed."""
        ...

    @property
    def armed(self) -> bool:
        """True when ``sign`` can produce a signature."""
        ...

    def sign(self, tx: dict[str, Any]) -> HexBytes:
        """Return the raw signed transaction bytes."""
        ...


class UnarmedSigner:
    """A signer with no key. Refuses to sign; reports ``armed = False``."""

    @property
    def address(self) -> str | None:
        """Always ``None``."""
        return None

    @property
    def armed(self) -> bool:
        """Always ``False``."""
        return False

    def sign(self, tx: dict[str, Any]) -> HexBytes:
        """Raise ``NotArmedError``; there is nothing to sign with."""
        raise NotArmedError("unarmed")

    def __repr__(self) -> str:
        return "<UnarmedSigner>"


class LocalSigner:
    """Signs with the in-process budget-wallet account."""

    def __init__(self, account: LocalAccount) -> None:
        self._account = account

    @property
    def address(self) -> str:
        """The budget wallet address."""
        return str(self._account.address)

    @property
    def armed(self) -> bool:
        """Always ``True``; the arm state of the agent is decided elsewhere."""
        return True

    def sign(self, tx: dict[str, Any]) -> HexBytes:
        """Sign an EIP-1559 transaction dict.

        Raises:
            SignerError: if the account rejects the transaction shape.
        """
        if tx.get("from", self.address).lower() != self.address.lower():
            raise SignerError("transaction sender does not match the budget wallet")
        try:
            signed = self._account.sign_transaction(tx)
        except (ValueError, TypeError) as exc:
            raise SignerError(f"cannot sign transaction: {type(exc).__name__}") from None
        return HexBytes(signed.raw_transaction)

    def __repr__(self) -> str:
        return f"<LocalSigner address={self.address}>"
