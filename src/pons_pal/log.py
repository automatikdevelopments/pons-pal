# SPDX-License-Identifier: MIT
# Pons Family - structured logging with secret scrubbing for pons.family
"""``structlog`` configuration that cannot emit a key.

The scrubber runs as the first processor. Any event field whose name looks like
a secret is replaced, and any value that looks like a 32-byte hex string is
redacted wherever it appears. This is defense in depth on top of ``SecretStr``:
the key should never reach a log call, and if it does anyway it still does not
reach the log.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

SECRET_FIELD_RE = re.compile(r"(key|secret|token|password|authorization|mnemonic)", re.IGNORECASE)
HEX_SECRET_RE = re.compile(r"0x[0-9a-fA-F]{64}")
REDACTED = "[redacted]"


def scrub_secrets(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact secret-looking fields and values in one log event."""
    for field_name in list(event_dict):
        value = event_dict[field_name]
        if SECRET_FIELD_RE.search(field_name) and field_name != "event":
            event_dict[field_name] = REDACTED
        elif isinstance(value, str) and HEX_SECRET_RE.search(value):
            event_dict[field_name] = HEX_SECRET_RE.sub(REDACTED, value)
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Install the scrubbing processor chain. Safe to call more than once."""
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())
    structlog.configure(
        processors=[
            scrub_secrets,
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(key_order=["timestamp", "level", "event"]),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
