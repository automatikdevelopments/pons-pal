# SPDX-License-Identifier: MIT
# Pons Family - command-line entry point for pons.family
"""``pons-pal``: run, cycle, state, disclosure, disarm, arm, resume, replay, mcp.

The banner prints on every start, colorless and without emoji, so an
operator's terminal says what this program is before it does anything. The
commands that widen what the agent may do (``arm``, ``resume``) require
``--confirm``; the one that narrows it (``disarm``) does not, because stopping
must never be the harder action.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import structlog

from pons_pal import BANNER, __version__
from pons_pal.agent.disclosure import build_disclosure, render_text
from pons_pal.agent.mcp import McpServer
from pons_pal.app import assemble_engine
from pons_pal.config import Settings, load_settings
from pons_pal.data.feeds import ReplayFeed
from pons_pal.data.historical import bars_to_ticks, load_bars_csv
from pons_pal.errors import PonsPalError
from pons_pal.log import configure_logging
from pons_pal.models import Mode

log = structlog.get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pons-pal", description="Agentic trading for ponsfamily.com"
    )
    parser.add_argument(
        "--config-dir", type=Path, default=Path("config"), help="directory of YAML config"
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--version", action="version", version=f"pons-pal {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run cycles on the configured interval")
    sub.add_parser("cycle", help="run one cycle and print the report")
    sub.add_parser("state", help="print the machine self-report as JSON")
    sub.add_parser("disclosure", help="print the disclosure surface as text")
    sub.add_parser("disarm", help="kill switch: stop execution immediately")

    arm = sub.add_parser("arm", help="lift the kill switch")
    arm.add_argument("--confirm", action="store_true", help="required")

    resume = sub.add_parser("resume", help="clear a tripped circuit breaker")
    resume.add_argument(
        "--breaker", required=True, choices=["intraday_loss", "weekly_loss", "monthly_loss"]
    )
    resume.add_argument("--confirm", action="store_true", help="required")

    replay = sub.add_parser("replay", help="paper-trade a CSV of bars")
    replay.add_argument("--csv", type=Path, required=True)
    replay.add_argument("--max-cycles", type=int, default=10_000)

    mcp = sub.add_parser("mcp", help="serve the MCP endpoint (and metrics) until interrupted")
    mcp.add_argument("--no-metrics", action="store_true")
    return parser


def _settings_for(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if args.command == "replay" and settings.mode is Mode.LIVE:
        # A replay is a simulation; running it live would submit swaps for history.
        settings = settings.model_copy(update={"mode": Mode.PAPER, "mode_explicit": True})
    return settings


async def _cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_for(args)
    engine = assemble_engine(settings, args.config_dir)
    _print_state_line(engine)
    stop = asyncio.Event()
    try:
        await engine.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


async def _cmd_cycle(args: argparse.Namespace) -> int:
    engine = assemble_engine(_settings_for(args), args.config_dir)
    report = await engine.run_cycle()
    print(report.model_dump_json(indent=2))
    return 0


async def _cmd_state(args: argparse.Namespace) -> int:
    engine = assemble_engine(_settings_for(args), args.config_dir)
    print(engine.state().model_dump_json(indent=2))
    return 0


async def _cmd_disclosure(args: argparse.Namespace) -> int:
    engine = assemble_engine(_settings_for(args), args.config_dir)
    print(render_text(build_disclosure(engine.state(), engine.current_limits())))
    return 0


async def _cmd_disarm(args: argparse.Namespace) -> int:
    engine = assemble_engine(_settings_for(args), args.config_dir)
    engine.disarm()
    print(json.dumps({"arm_state": engine.arm_state().value}))
    return 0


async def _cmd_arm(args: argparse.Namespace) -> int:
    engine = assemble_engine(_settings_for(args), args.config_dir)
    engine.clear_disarm(confirm=bool(args.confirm))
    print(json.dumps({"arm_state": engine.arm_state().value}))
    return 0


async def _cmd_resume(args: argparse.Namespace) -> int:
    engine = assemble_engine(_settings_for(args), args.config_dir)
    breaker = engine.risk.breakers.resume(args.breaker, confirm=bool(args.confirm))
    print(breaker.model_dump_json())
    return 0


async def _cmd_replay(args: argparse.Namespace) -> int:
    bars, dropped = load_bars_csv(args.csv)
    if dropped:
        log.warning("replay.dropped_rows", count=dropped)
    feed = ReplayFeed(bars_to_ticks(bars), batch=max(1, len({b.pair_id for b in bars})))
    engine = assemble_engine(_settings_for(args), args.config_dir, market_feed=feed)
    cycles = 0
    while not feed.exhausted and cycles < args.max_cycles:
        await engine.run_cycle()
        cycles += 1
    print(engine.state().model_dump_json(indent=2))
    return 0


async def _cmd_mcp(args: argparse.Namespace) -> int:
    settings = _settings_for(args)
    engine = assemble_engine(settings, args.config_dir)
    if not args.no_metrics:
        from pons_pal.monitoring.metrics import (
            PonsMetrics,
        )

        metrics = PonsMetrics()
        metrics.serve(settings.metrics_host, settings.metrics_port)
    server = McpServer(engine, settings.cycle_secret, settings.mcp_host, settings.mcp_port)
    runner = await server.serve()
    _print_state_line(engine)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
    return 0


def _print_state_line(engine: object) -> None:
    from pons_pal.core.engine import PonsPalEngine

    if isinstance(engine, PonsPalEngine):
        state = engine.state()
        print(
            f"mode={state.mode.value} arm_state={state.arm_state.value} "
            f"equity={state.equity_usd:,.2f} USD"
        )


COMMANDS = {
    "run": _cmd_run,
    "cycle": _cmd_cycle,
    "state": _cmd_state,
    "disclosure": _cmd_disclosure,
    "disarm": _cmd_disarm,
    "arm": _cmd_arm,
    "resume": _cmd_resume,
    "replay": _cmd_replay,
    "mcp": _cmd_mcp,
}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, print the banner, and dispatch."""
    args = _parser().parse_args(argv)
    configure_logging(args.log_level)
    print(BANNER, file=sys.stderr)
    try:
        return asyncio.run(COMMANDS[args.command](args))
    except PonsPalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
