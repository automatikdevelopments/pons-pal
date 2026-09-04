# SPDX-License-Identifier: MIT
# Pons Family - budget-wallet key loading and assertion for pons.family
"""Load the trading key from settings and prove it belongs to the budget wallet.

The key is accepted only if it derives the published ``PONS_PAL_BUDGET_ADDRESS``.
A key that derives anything else is refused at startup, because the address is
the operator's declaration of which wallet the agent may spend from; a key for
a different wallet is a key for the wrong wallet, however it got there. The key
material never appears in an exception message or a log line.
"""

from __future__ import annotations

from eth_account import Account
from eth_account.signers.local import LocalAccount

from pons_pal.config import Settings
from pons_pal.errors import ConfigError


def load_budget_account(settings: Settings) -> LocalAccount | None:
    """Return the budget-wallet account, or ``None`` when no key is configured.

    Raises:
        ConfigError: if the key is malformed or derives a different address.
    """
    if settings.trading_key is None:
        return None
    if settings.budget_address is None:
        raise ConfigError("PONS_PAL_BUDGET_ADDRESS", "required when a trading key is set")
    try:
        account: LocalAccount = Account.from_key(settings.trading_key.get_secret_value())
    except (ValueError, TypeError):
        raise ConfigError("PONS_PAL_TRADING_KEY", "not a valid EVM private key") from None
    if account.address.lower() != settings.budget_address.lower():
        raise ConfigError(
            "PONS_PAL_TRADING_KEY", "does not derive the published PONS_PAL_BUDGET_ADDRESS"
        )
    return account
