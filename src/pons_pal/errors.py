# SPDX-License-Identifier: MIT
# Pons Family - named exceptions for pons.family
"""Every failure in Pons Pal is a named exception that carries its context.

A trading agent fails in ways that must be diagnosable after the fact: which
check blocked the order, which feed went stale and by how much, which endpoint
refused. Bare exceptions lose that. Each class here stores the facts a person
would want in the incident note, and never a secret: messages are built from
field names and numbers, not from key material.
"""

from __future__ import annotations

from typing import Any


class PonsPalError(Exception):
    """Base class for every Pons Pal failure.

    Args:
        message: Human-readable summary. Must never contain key material.
        **context: Structured facts about the failure, kept for logging.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = ", ".join(f"{key}={value!r}" for key, value in self.context.items())
        return f"{self.message} ({detail})"


class ConfigError(PonsPalError):
    """A configuration field is missing, malformed, or inconsistent.

    Raised at startup so a bad configuration never reaches the trading loop.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"invalid configuration for {field}: {reason}", field=field, reason=reason)
        self.field = field
        self.reason = reason


class ChainError(PonsPalError):
    """A Robinhood Chain RPC call failed or returned something undecodable."""

    def __init__(self, endpoint: str, reason: str = "request failed") -> None:
        super().__init__(f"chain error at {endpoint}: {reason}", endpoint=endpoint, reason=reason)
        self.endpoint = endpoint
        self.reason = reason


class DecodeError(PonsPalError):
    """External data (chain, model, provider) did not match its expected shape.

    Distinct from ``ChainError`` because the transport succeeded; the payload is
    what cannot be trusted.
    """

    def __init__(self, source: str, field: str, reason: str) -> None:
        super().__init__(
            f"cannot decode {field} from {source}: {reason}",
            source=source,
            field=field,
            reason=reason,
        )
        self.source = source
        self.field = field
        self.reason = reason


class FeedStaleError(PonsPalError):
    """A price feed is older than the configured maximum age."""

    def __init__(self, feed: str, age_s: float, max_age_s: float) -> None:
        super().__init__(
            f"feed {feed} is stale: {age_s:.0f}s old, limit {max_age_s:.0f}s",
            feed=feed,
            age_s=age_s,
            max_age_s=max_age_s,
        )
        self.feed = feed
        self.age_s = age_s
        self.max_age_s = max_age_s


class RiskBlocked(PonsPalError):
    """The risk gate refused an order.

    Carries the check that fired, the observed value, and the limit it breached
    so the refusal can be published exactly like a fill.
    """

    def __init__(self, check: str, value: float, limit: float) -> None:
        super().__init__(
            f"risk check {check} blocked the order: value {value:g}, limit {limit:g}",
            check=check,
            value=value,
            limit=limit,
        )
        self.check = check
        self.value = value
        self.limit = limit


class BreakerTripped(RiskBlocked):
    """A circuit breaker is tripped; all trading is halted until a manual resume."""

    def __init__(self, breaker: str, value: float, limit: float) -> None:
        super().__init__(breaker, value, limit)
        self.breaker = breaker
        self.message = (
            f"circuit breaker {breaker} is tripped (value {value:g}, limit {limit:g}); "
            "manual resume required"
        )


class NotArmedError(PonsPalError):
    """Live execution was requested while the agent is unarmed, disarmed, or halted."""

    def __init__(self, state: str) -> None:
        super().__init__(f"execution refused: agent is {state}", state=state)
        self.state = state


class SignerError(PonsPalError):
    """The signing boundary refused or failed to sign a transaction."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"signer error: {reason}", reason=reason)
        self.reason = reason


class NetworkGuardError(PonsPalError):
    """An outbound request was refused by the SSRF guard."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"outbound request refused: {reason}", url=url, reason=reason)
        self.url = url
        self.reason = reason


class AuthError(PonsPalError):
    """A privileged action was requested without a valid secret."""

    def __init__(self, action: str) -> None:
        super().__init__(f"unauthorized: {action}", action=action)
        self.action = action


class ExecutionError(PonsPalError):
    """A swap could not be built, submitted, or confirmed."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(f"execution failed at {stage}: {reason}", stage=stage, reason=reason)
        self.stage = stage
        self.reason = reason
