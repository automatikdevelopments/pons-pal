# SPDX-License-Identifier: MIT
# Pons Family - the MCP endpoint for pons.family
"""A small tool server so an external agent can connect to Pons Pal the way an
agent connects to a brokerage: read state, read positions, run a cycle, disarm.

The handler authenticates its own caller. Every request must carry the cycle
secret as a bearer token, compared in constant time, and the server refuses to
start without one: an unauthenticated tool that can run a trading cycle is a
public trading button. It binds to loopback by default and caps the request
body, and the only writes it offers are the ones a person would want an agent
to have: run a cycle, and stop.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiohttp import web
from pydantic import SecretStr

from pons_pal.agent.disclosure import build_disclosure
from pons_pal.core.engine import PonsPalEngine
from pons_pal.errors import ConfigError, PonsPalError

log = structlog.get_logger(__name__)

MAX_BODY_BYTES = 64 * 1024
Tool = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def constant_time_equal(provided: str, expected: str) -> bool:
    """Compare two secrets without leaking their length through timing."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


class McpServer:
    """The tool server bound to one engine."""

    def __init__(
        self, engine: PonsPalEngine, secret: SecretStr | None, host: str, port: int
    ) -> None:
        if secret is None or not secret.get_secret_value():
            raise ConfigError("PONS_PAL_CYCLE_SECRET", "required to serve the MCP endpoint")
        self._engine = engine
        self._secret = secret
        self._host = host
        self._port = port
        self._tools: dict[str, Tool] = {
            "get_state": self._get_state,
            "get_positions": self._get_positions,
            "get_disclosure": self._get_disclosure,
            "run_cycle": self._run_cycle,
            "disarm": self._disarm,
            "resume_breaker": self._resume_breaker,
        }

    # --- tools -------------------------------------------------------------------

    async def _get_state(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._engine.state().model_dump(mode="json")

    async def _get_positions(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {pair_id: p.model_dump(mode="json") for pair_id, p in self._engine.positions.items()}

    async def _get_disclosure(self, _args: dict[str, Any]) -> dict[str, Any]:
        return build_disclosure(self._engine.state(), self._engine.current_limits()).model_dump(
            mode="json"
        )

    async def _run_cycle(self, _args: dict[str, Any]) -> dict[str, Any]:
        report = await self._engine.run_cycle()
        return report.model_dump(mode="json")

    async def _disarm(self, _args: dict[str, Any]) -> dict[str, Any]:
        self._engine.disarm()
        return {"arm_state": self._engine.arm_state().value}

    async def _resume_breaker(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("breaker")
        confirm = args.get("confirm") is True
        if not isinstance(name, str):
            raise ConfigError("breaker", "name required")
        breaker = self._engine.risk.breakers.resume(name, confirm=confirm)
        return breaker.model_dump(mode="json")

    # --- http -------------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return constant_time_equal(header[len("Bearer ") :], self._secret.get_secret_value())

    @staticmethod
    def _json(payload: dict[str, Any], status: int = 200) -> web.Response:
        return web.json_response(payload, status=status, headers=SECURITY_HEADERS)

    async def handle_tools(self, request: web.Request) -> web.Response:
        """GET /mcp/tools: list tool names. Authenticated like everything else."""
        if not self._authorized(request):
            return self._json({"error": "unauthorized"}, status=401)
        return self._json({"tools": sorted(self._tools)})

    async def handle_call(self, request: web.Request) -> web.Response:
        """POST /mcp: ``{"tool": name, "arguments": {...}}``."""
        if not self._authorized(request):
            return self._json({"error": "unauthorized"}, status=401)
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return self._json({"error": "body too large"}, status=413)
        raw = await request.content.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            return self._json({"error": "body too large"}, status=413)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return self._json({"error": "body must be an object"}, status=400)
        name = body.get("tool")
        args = body.get("arguments", {})
        if not isinstance(name, str) or name not in self._tools or not isinstance(args, dict):
            return self._json({"error": "unknown tool"}, status=404)
        try:
            result = await self._tools[name](args)
        except PonsPalError as exc:
            return self._json({"error": exc.message}, status=409)
        return self._json({"tool": name, "result": result})

    def app(self) -> web.Application:
        """Build the aiohttp application."""
        application = web.Application(client_max_size=MAX_BODY_BYTES)
        application.router.add_get("/mcp/tools", self.handle_tools)
        application.router.add_post("/mcp", self.handle_call)
        return application

    async def serve(self) -> web.AppRunner:
        """Start serving; returns the runner so the caller can stop it."""
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("mcp.listening", host=self._host, port=self._port)
        return runner
