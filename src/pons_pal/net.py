# SPDX-License-Identifier: MIT
# Pons Family - the outbound request guard for pons.family
"""One HTTP client for every outbound call, with SSRF defenses built in.

Sentiment feeds, the stock-data provider, the pair index, the webhook, and the
RPC all leave the process through ``SafeHttpClient``. It enforces https, an
exhaustive host allowlist, DNS resolution that rejects private, loopback,
link-local, and metadata addresses, no redirects, bounded response size, and a
timeout. Redirects are refused rather than followed because a redirect is an
attacker's way of turning an allowed host into a forbidden one after the check.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from pons_pal.errors import NetworkGuardError

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
METADATA_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal", "metadata"})


def is_public_address(address: str) -> bool:
    """True when ``address`` is a globally routable IP; everything else is refused."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def resolve_public(host: str) -> list[str]:
    """Resolve ``host`` and return its addresses, refusing any non-public one.

    Every address is checked, not just the first: a host that resolves to one
    public and one private address is a rebinding attempt, not a valid target.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise NetworkGuardError(host, "hostname does not resolve") from None
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise NetworkGuardError(host, "hostname resolved to nothing")
    for address in addresses:
        if not is_public_address(address):
            raise NetworkGuardError(host, "hostname resolves to a non-public address")
    return addresses


def check_url(url: str, allowed_hosts: Iterable[str], *, resolve: bool = True) -> str:
    """Validate an outbound URL against the guard rules and return its host.

    Raises:
        NetworkGuardError: on any violation, naming the rule.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise NetworkGuardError(url, "only https is allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise NetworkGuardError(url, "missing host")
    if parts.username or parts.password:
        raise NetworkGuardError(url, "credentials in URL are not allowed")
    if host in METADATA_HOSTS:
        raise NetworkGuardError(url, "metadata endpoints are never allowed")
    allowed = {entry.lower() for entry in allowed_hosts}
    if host not in allowed:
        raise NetworkGuardError(url, "host is not on the outbound allowlist")
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_address(host):
            raise NetworkGuardError(url, "IP literal is not public")
    elif resolve:
        resolve_public(host)
    return host


class SafeHttpClient:
    """An ``aiohttp`` wrapper that applies ``check_url`` to every request.

    Args:
        allowed_hosts: The exhaustive host allowlist from ``config/pons.yaml``.
        timeout_s: Per-request total timeout.
        max_bytes: Responses larger than this are refused; a provider that
            streams gigabytes is a denial of service, not data.
        resolve: Disable DNS checks only in tests.
    """

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        resolve: bool = True,
    ) -> None:
        self._allowed = tuple(allowed_hosts)
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._max_bytes = max_bytes
        self._resolve = resolve
        self._session: aiohttp.ClientSession | None = None

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """The configured allowlist."""
        return self._allowed

    def check(self, url: str) -> str:
        """Run the guard without making a request."""
        return check_url(url, self._allowed, resolve=self._resolve)

    async def _session_or_open(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout, raise_for_status=False)
        return self._session

    async def close(self) -> None:
        """Close the underlying session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, bytes]:
        """Perform a guarded request and return ``(status, body)``.

        Raises:
            NetworkGuardError: if the URL fails the guard, the server redirects,
                or the body exceeds ``max_bytes``.
        """
        self.check(url)
        session = await self._session_or_open()
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                allow_redirects=False,
            ) as response:
                if 300 <= response.status < 400:
                    raise NetworkGuardError(url, "redirects are refused")
                body = await response.content.read(self._max_bytes + 1)
                if len(body) > self._max_bytes:
                    raise NetworkGuardError(url, "response exceeds the size limit")
                return response.status, body
        except aiohttp.ClientError as exc:
            raise NetworkGuardError(url, f"transport error: {type(exc).__name__}") from None
