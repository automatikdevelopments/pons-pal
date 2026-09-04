# SPDX-License-Identifier: MIT
# Pons Family - engine assembly for pons.family
"""Wire settings, configuration, adapters, and the engine together.

Construction is separate from the engine so the engine never reads the
environment: it receives a signer, a store, and feeds, and does not know or
care where they came from. That is also what makes the whole pipeline testable
with replay feeds and an unarmed signer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

from pons_pal.adapters.chain import RobinhoodChainClient
from pons_pal.adapters.sentiment import RetailSentimentReader
from pons_pal.agent.notify import Notifier
from pons_pal.config import (
    DEFAULT_CONFIG_DIR,
    HotReloader,
    RiskLimits,
    Settings,
    StrategyConfig,
    load_engine_config,
    load_pons_config,
    load_risk_limits,
    load_sentiment_config,
)
from pons_pal.core.engine import PonsPalEngine
from pons_pal.core.portfolio import PortfolioBuilder
from pons_pal.core.risk import CircuitBreakers, RiskGate
from pons_pal.core.session import TradingSession
from pons_pal.core.universe import PonsUniverse
from pons_pal.data.feeds import (
    ChainlinkStockData,
    ChainPoolFeed,
    MarketFeed,
    PairIndexSource,
    PairSource,
    ReplayFeed,
    StaticPairSource,
    StaticStockData,
    StockDataSource,
)
from pons_pal.execution.router import ExecutionRouter
from pons_pal.keys import load_budget_account
from pons_pal.models import Mode
from pons_pal.monitoring.metrics import PonsMetrics
from pons_pal.net import SafeHttpClient
from pons_pal.signer import LocalSigner, PonsSigner, UnarmedSigner
from pons_pal.store import PonsStore
from pons_pal.strategies import (
    EventDriftStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    StatisticalPairsStrategy,
    StockBackStrategy,
    Strategy,
)

log = structlog.get_logger(__name__)

STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "pairs": StatisticalPairsStrategy,
    "event": EventDriftStrategy,
    "stockback": StockBackStrategy,
}


def build_strategies(configs: dict[str, StrategyConfig]) -> list[Strategy]:
    """Instantiate every enabled strategy named in ``config/default.yaml``."""
    strategies: list[Strategy] = []
    for name, cls in STRATEGY_CLASSES.items():
        config = configs.get(name, StrategyConfig())
        if config.enabled:
            strategies.append(cls(config))
    return strategies


def assemble_engine(
    settings: Settings,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    *,
    market_feed: MarketFeed | None = None,
    stock_data: StockDataSource | None = None,
    pair_source: PairSource | None = None,
    store: PonsStore | None = None,
    metrics: PonsMetrics | None = None,
) -> PonsPalEngine:
    """Build a fully wired engine from settings and the config directory.

    Injected feeds and store are for paper replays and tests; production runs
    with the chain-backed defaults.
    """
    engine_config = load_engine_config(config_dir / "default.yaml")
    pons_config = load_pons_config(config_dir / "pons.yaml")
    sentiment_config = load_sentiment_config(config_dir / "sentiment.yaml")
    limits = HotReloader(config_dir / "risk.yaml", load_risk_limits)
    limits_fn: Callable[[], RiskLimits] = limits.current
    if not limits.current().hot_reload:
        limits = HotReloader(config_dir / "risk.yaml", load_risk_limits, enabled=False)
        limits_fn = limits.current

    mode = settings.mode if settings.mode_explicit else engine_config.engine.mode
    account = load_budget_account(settings)
    signer: PonsSigner = LocalSigner(account) if account is not None else UnarmedSigner()
    budget_address = signer.address or settings.budget_address

    http = SafeHttpClient(
        pons_config.outbound.allowed_hosts, timeout_s=pons_config.chain.request_timeout_s
    )
    chain = RobinhoodChainClient(pons_config.chain, settings.rpc_http)
    store = store or PonsStore(settings.sqlite_path)
    metrics = metrics or PonsMetrics()

    if pair_source is None:
        pair_source = (
            PairIndexSource(http, pons_config.pairs.index_url)
            if pons_config.pairs.index_url
            else StaticPairSource(pons_config.pairs.static)
        )
    if stock_data is None:
        stock_data = (
            ChainlinkStockData(
                chain,
                pons_config.feeds.stock_feeds,
                http,
                settings.stock_data_api_base,
                settings.stock_data_api_key.get_secret_value()
                if settings.stock_data_api_key
                else None,
            )
            if pons_config.feeds.stock_feeds
            else StaticStockData({})
        )
    if market_feed is None:
        market_feed = (
            ChainPoolFeed(chain, pair_source, pons_config.router.abi_path)
            if mode is Mode.LIVE or pons_config.pairs.index_url
            else ReplayFeed([])
        )

    router_config = pons_config.router
    if settings.router_address:
        router_config = router_config.model_copy(update={"address": settings.router_address})

    breakers = CircuitBreakers(store)
    risk = RiskGate(limits_fn, breakers, TradingSession())
    weights = {name: cfg.weight for name, cfg in engine_config.strategies.items()}
    builder = PortfolioBuilder(
        engine_config.portfolio,
        engine_config.capital,
        weights,
        max_slippage_bps=limits_fn().max_slippage_bps,
    )

    engine_holder: list[PonsPalEngine] = []
    router = ExecutionRouter(
        mode=mode,
        signer=signer,
        limits=limits_fn,
        router_config=router_config,
        chain=chain if mode is Mode.LIVE else None,
        kill_switch=lambda: bool(engine_holder) and engine_holder[0].is_disarmed(),
    )
    notifier = Notifier(http if settings.webhook_url else None, settings.webhook_url)
    sentiment = (
        RetailSentimentReader(sentiment_config, http)
        if any(src.enabled for src in sentiment_config.sources.values())
        else None
    )

    eth_reader: Callable[[], Awaitable[float]] | None = None
    if mode is Mode.LIVE and budget_address is not None:
        address = budget_address

        async def read_balance() -> float:
            """ETH balance of the budget wallet, read every cycle for the gas-reserve check."""
            return await chain.eth_balance(address)

        eth_reader = read_balance

    engine = PonsPalEngine(
        mode=mode,
        config=engine_config,
        limits=limits_fn,
        universe=PonsUniverse(engine_config.universe, pons_config.feeds),
        market_feed=market_feed,
        stock_data=stock_data,
        pair_source=pair_source,
        strategies=build_strategies(engine_config.strategies),
        builder=builder,
        risk=risk,
        router=router,
        store=store,
        metrics=metrics,
        notifier=notifier,
        budget_address=budget_address,
        sentiment=sentiment,
        eth_balance=eth_reader,
    )
    engine_holder.append(engine)
    log.info(
        "engine.assembled",
        mode=mode.value,
        arm_state=engine.arm_state().value,
        strategies=len(engine_config.strategies),
    )
    return engine
