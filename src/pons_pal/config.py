# SPDX-License-Identifier: MIT
# Pons Family - environment and YAML configuration for pons.family
"""Settings from the environment, tunables from YAML, both validated before use.

Secrets live only in the environment and are read at call time through
``load_settings``; nothing here runs at import. YAML is parsed with
``yaml.safe_load`` only, because a config file is an input like any other and a
loader that can construct arbitrary objects is a code path, not a parser.

The risk file hot-reloads through ``HotReloader``. A file that fails validation
is ignored and the last good limits stay in force: a typo in ``risk.yaml`` must
never widen a limit, and "fall back to defaults" is a widening if the defaults
are looser than what the operator had set.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Generic, TypeVar

import structlog
import yaml
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from pons_pal.errors import ConfigError
from pons_pal.models import ADDRESS_RE, TICKER_RE, Mode, PonsModel, PonsPair

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=PonsModel)

DEFAULT_CONFIG_DIR = Path("config")


class Settings(PonsModel):
    """Everything read from the environment. Secrets are ``SecretStr`` so they never print."""

    trading_key: SecretStr | None = None
    budget_address: str | None = None
    mode: Mode = Mode.PAPER
    mode_explicit: bool = False
    rpc_http: str = "https://rpc.mainnet.chain.robinhood.com"
    rpc_ws: str | None = None
    chain_id: int = 4663
    router_address: str | None = None
    chainlink_feed_registry: str | None = None
    stock_data_api_key: SecretStr | None = None
    stock_data_api_base: str | None = None
    model_api_key: SecretStr | None = None
    cycle_secret: SecretStr | None = None
    webhook_url: str | None = None
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8765, ge=1, le=65535)
    metrics_host: str = "127.0.0.1"
    metrics_port: int = Field(default=8000, ge=1, le=65535)
    redis_url: str | None = None
    database_url: str = "sqlite:///data/pons_pal.db"

    @field_validator("budget_address", "router_address", "chainlink_feed_registry")
    @classmethod
    def _address_shape(cls, value: str | None) -> str | None:
        if value is not None and not ADDRESS_RE.match(value):
            raise ValueError("must be a 0x-prefixed 20-byte hex address")
        return value

    @field_validator("webhook_url", "stock_data_api_base", "rpc_http")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("must be an https URL")
        return value

    @field_validator("rpc_ws")
    @classmethod
    def _wss_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("wss://"):
            raise ValueError("must be a wss URL")
        return value

    @model_validator(mode="after")
    def _key_requires_address(self) -> Settings:
        # A key without a published address cannot be verified against anything,
        # and an unverifiable key is one that might belong to the wrong wallet.
        if self.trading_key is not None and self.budget_address is None:
            raise ValueError("PONS_PAL_BUDGET_ADDRESS is required when PONS_PAL_TRADING_KEY is set")
        return self

    @property
    def has_trading_key(self) -> bool:
        """True when a key is configured; says nothing about whether it is valid."""
        return self.trading_key is not None

    @property
    def sqlite_path(self) -> Path:
        """Filesystem path behind ``DATABASE_URL``; only SQLite is supported."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ConfigError("DATABASE_URL", "only sqlite:/// URLs are supported")
        return Path(self.database_url[len(prefix) :])


# (environment variable, settings field). A tuple of pairs rather than a dict so the
# variable names are data, not assignments that look like hardcoded secrets.
_ENV_FIELDS: tuple[tuple[str, str], ...] = (
    ("PONS_PAL_TRADING_KEY", "trading_key"),
    ("PONS_PAL_BUDGET_ADDRESS", "budget_address"),
    ("PONS_PAL_MODE", "mode"),
    ("RHC_RPC_HTTP", "rpc_http"),
    ("RHC_RPC_WS", "rpc_ws"),
    ("RHC_CHAIN_ID", "chain_id"),
    ("PONS_ROUTER_ADDRESS", "router_address"),
    ("CHAINLINK_FEED_REGISTRY", "chainlink_feed_registry"),
    ("STOCK_DATA_API_KEY", "stock_data_api_key"),
    ("STOCK_DATA_API_BASE", "stock_data_api_base"),
    ("PONS_PAL_MODEL_API_KEY", "model_api_key"),
    ("PONS_PAL_CYCLE_SECRET", "cycle_secret"),
    ("PONS_PAL_WEBHOOK_URL", "webhook_url"),
    ("PONS_PAL_MCP_HOST", "mcp_host"),
    ("PONS_PAL_MCP_PORT", "mcp_port"),
    ("PONS_PAL_METRICS_HOST", "metrics_host"),
    ("PONS_PAL_METRICS_PORT", "metrics_port"),
    ("REDIS_URL", "redis_url"),
    ("DATABASE_URL", "database_url"),
)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build ``Settings`` from the environment, treating blank values as unset.

    Args:
        env: Mapping to read; defaults to ``os.environ`` at call time.

    Raises:
        ConfigError: on the first invalid variable, naming the variable but never
            its value.
    """
    source = os.environ if env is None else env
    values: dict[str, Any] = {}
    for var, field_name in _ENV_FIELDS:
        raw = source.get(var, "")
        if raw.strip() == "":
            continue
        values[field_name] = raw.strip()
    if "mode" in values:
        values["mode_explicit"] = True
    try:
        return Settings(**values)
    except ValidationError as exc:
        first = exc.errors()[0]
        field_name = str(first["loc"][0]) if first["loc"] else "settings"
        var = next((k for k, v in _ENV_FIELDS if v == field_name), field_name)
        raise ConfigError(var, str(first["msg"])) from None


class StrategyConfig(PonsModel):
    """Per-strategy toggles and tunables; unknown keys are rejected."""

    enabled: bool = True
    weight: float = Field(default=1.0, ge=0.0, le=10.0)
    lookback_bars: int = Field(default=60, ge=5, le=10_000)
    z_entry: float = Field(default=2.0, gt=0.0, le=10.0)
    half_life_s: int = Field(default=3600, gt=0)
    top_k: int = Field(default=5, ge=1, le=100)


class EngineSection(PonsModel):
    """The ``engine`` block of ``config/default.yaml``."""

    mode: Mode = Mode.PAPER
    cycle_interval_s: int = Field(default=60, ge=1)
    bar_interval_s: int = Field(default=60, ge=1)
    history_bars: int = Field(default=240, ge=10, le=100_000)
    paper_equity_usd: float = Field(default=10_000.0, gt=0.0)


class CapitalSection(PonsModel):
    """The ``capital`` block: order-size floors and ceilings."""

    min_order_usd: float = Field(default=25.0, gt=0.0)
    per_order_max_usd: float = Field(default=500.0, gt=0.0)
    daily_notional_max_usd: float = Field(default=5000.0, gt=0.0)
    eth_gas_reserve: float = Field(default=0.02, ge=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> CapitalSection:
        if self.min_order_usd > self.per_order_max_usd:
            raise ValueError("min_order_usd exceeds per_order_max_usd")
        return self


class UniverseSection(PonsModel):
    """The ``universe`` block: which pairs are even considered."""

    allow_bonding_curve: bool = False
    min_liquidity_usd: float = Field(default=250_000.0, ge=0.0)
    min_volume_24h_usd: float = Field(default=1_000_000.0, ge=0.0)
    max_pairs: int = Field(default=25, ge=1, le=500)
    require_stock_feed: bool = True


class PortfolioSection(PonsModel):
    """The ``portfolio`` block."""

    correlation_lookback_bars: int = Field(default=120, ge=10)
    signal_floor: float = Field(default=0.15, ge=0.0, le=1.0)


class EngineConfig(PonsModel):
    """``config/default.yaml`` as a whole."""

    engine: EngineSection = EngineSection()
    capital: CapitalSection = CapitalSection()
    universe: UniverseSection = UniverseSection()
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)
    portfolio: PortfolioSection = PortfolioSection()


class RiskLimits(PonsModel):
    """``config/risk.yaml``: the seven checks, the Pons additions, and the execution floors.

    Every field is bounded so a reload cannot set a limit to zero, negative, or
    absurdly loose by accident.
    """

    hot_reload: bool = True
    intraday_loss_pct: float = Field(default=2.0, gt=0.0, le=100.0)
    weekly_loss_pct: float = Field(default=5.0, gt=0.0, le=100.0)
    monthly_loss_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    chain_exposure_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    max_position_pct: float = Field(default=5.0, gt=0.0, le=100.0)
    max_sector_pct: float = Field(default=25.0, gt=0.0, le=100.0)
    min_adv_usd: float = Field(default=1_000_000.0, ge=0.0)
    feed_max_age_s: float = Field(default=900.0, gt=0.0)
    feed_max_age_offhours_s: float = Field(default=86_400.0, gt=0.0)
    stockback_min_rate: float = Field(default=0.0005, ge=0.0, le=1.0)
    underlying_max_drawdown_pct: float = Field(default=15.0, gt=0.0, le=100.0)
    eth_gas_reserve: float = Field(default=0.02, ge=0.0)
    max_slippage_bps: int = Field(default=100, ge=0, le=10_000)
    max_price_impact_bps: int = Field(default=150, ge=0, le=10_000)
    per_order_max_usd: float = Field(default=500.0, gt=0.0)
    daily_notional_max_usd: float = Field(default=5000.0, gt=0.0)

    @model_validator(mode="after")
    def _loss_ladder(self) -> RiskLimits:
        # The breakers form a ladder: a weekly limit tighter than the intraday one
        # would make the intraday breaker unreachable, which is a silent hole.
        if not (self.intraday_loss_pct <= self.weekly_loss_pct <= self.monthly_loss_pct):
            raise ValueError("loss limits must satisfy intraday <= weekly <= monthly")
        if self.feed_max_age_s > self.feed_max_age_offhours_s:
            raise ValueError("feed_max_age_s must not exceed feed_max_age_offhours_s")
        return self


class SentimentSource(PonsModel):
    """One sentiment source in ``config/sentiment.yaml``."""

    enabled: bool = False
    weight: float = Field(default=1.0, ge=0.0, le=10.0)
    base_url: str = ""
    feeds: tuple[str, ...] = ()

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            raise ValueError("base_url must be https")
        return value


class SentimentBlend(PonsModel):
    """Blend parameters shared by the sentiment adapter and the event strategy."""

    min_sources: int = Field(default=1, ge=1)
    half_life_s: int = Field(default=3600, gt=0)
    max_items_per_source: int = Field(default=100, ge=1, le=10_000)


class SentimentConfig(PonsModel):
    """``config/sentiment.yaml``."""

    sources: dict[str, SentimentSource] = Field(default_factory=dict)
    blend: SentimentBlend = SentimentBlend()


class ChainSection(PonsModel):
    """The ``chain`` block of ``config/pons.yaml``."""

    name: str = "Robinhood Chain"
    chain_id: int = Field(default=4663, gt=0)
    rpc_http: str = "https://rpc.mainnet.chain.robinhood.com"
    rpc_ws: str = ""
    explorer: str = "https://robinhoodchain.blockscout.com"
    gas_token: str = "ETH"
    settlement_token: str = "USDG"
    settlement_token_address: str = ""
    max_fee_per_gas_gwei: float = Field(default=0.5, gt=0.0)
    max_priority_fee_per_gas_gwei: float = Field(default=0.01, ge=0.0)
    confirmations: int = Field(default=1, ge=1)
    request_timeout_s: float = Field(default=10.0, gt=0.0)


class RouterSection(PonsModel):
    """The ``router`` block. A blank address keeps on-chain execution unarmed."""

    address: str = ""
    abi_path: str = "config/abi/pons_router.json"
    stockback_share_default: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        if value and not ADDRESS_RE.match(value):
            raise ValueError("router address must be a 0x-prefixed 20-byte hex address")
        return value


class FeedsSection(PonsModel):
    """The ``feeds`` block: Chainlink registry and per-stock aggregator map."""

    chainlink_registry: str = ""
    stock_feeds: dict[str, str] = Field(default_factory=dict)
    feed_decimals_default: int = Field(default=8, ge=0, le=36)

    @field_validator("stock_feeds")
    @classmethod
    def _shapes(cls, value: dict[str, str]) -> dict[str, str]:
        for symbol, address in value.items():
            if not TICKER_RE.match(symbol):
                raise ValueError(f"invalid ticker in stock_feeds: {symbol!r}")
            if not ADDRESS_RE.match(address):
                raise ValueError(f"invalid feed address for {symbol}")
        return value


class PairsSection(PonsModel):
    """The ``pairs`` block: where the pair universe comes from."""

    index_url: str = ""
    static: tuple[PonsPair, ...] = ()

    @field_validator("index_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            raise ValueError("index_url must be https")
        return value


class OutboundSection(PonsModel):
    """The ``outbound`` block: the exhaustive host allowlist for the SSRF guard."""

    allowed_hosts: tuple[str, ...] = ()


class PonsChainConfig(PonsModel):
    """``config/pons.yaml`` as a whole."""

    chain: ChainSection = ChainSection()
    router: RouterSection = RouterSection()
    feeds: FeedsSection = FeedsSection()
    pairs: PairsSection = PairsSection()
    outbound: OutboundSection = OutboundSection()


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML file with ``safe_load`` and require a mapping at the top level.

    Raises:
        ConfigError: if the file is missing, unparseable, or not a mapping.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(str(path), f"cannot read: {exc.strerror}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(str(path), f"invalid YAML: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(str(path), "top level must be a mapping")
    return {str(k): v for k, v in data.items()}


def load_model(path: Path, model: type[T]) -> T:
    """Parse a YAML file into ``model``.

    Raises:
        ConfigError: naming the first failing field.
    """
    data = load_yaml_mapping(path)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"]) or model.__name__
        raise ConfigError(f"{path}:{loc}", str(first["msg"])) from None


def load_engine_config(path: Path = DEFAULT_CONFIG_DIR / "default.yaml") -> EngineConfig:
    """Load ``config/default.yaml``."""
    return load_model(path, EngineConfig)


def load_risk_limits(path: Path = DEFAULT_CONFIG_DIR / "risk.yaml") -> RiskLimits:
    """Load ``config/risk.yaml``."""
    return load_model(path, RiskLimits)


def load_sentiment_config(path: Path = DEFAULT_CONFIG_DIR / "sentiment.yaml") -> SentimentConfig:
    """Load ``config/sentiment.yaml``."""
    return load_model(path, SentimentConfig)


def load_pons_config(path: Path = DEFAULT_CONFIG_DIR / "pons.yaml") -> PonsChainConfig:
    """Load ``config/pons.yaml``."""
    return load_model(path, PonsChainConfig)


class HotReloader(Generic[T]):
    """Re-read a validated config file whenever its mtime changes.

    On a failed reload the previous value is kept and the failure is logged.
    Fail-closed here means "keep the limits you had", never "use defaults".
    """

    def __init__(self, path: Path, loader: Callable[[Path], T], *, enabled: bool = True) -> None:
        self._path = path
        self._loader = loader
        self._enabled = enabled
        self._value = loader(path)
        self._mtime = self._stat()

    def _stat(self) -> float:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return -1.0

    @property
    def path(self) -> Path:
        """The watched file."""
        return self._path

    def current(self) -> T:
        """Return the latest good value, reloading if the file changed."""
        if not self._enabled:
            return self._value
        mtime = self._stat()
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._value = self._loader(self._path)
                log.info("config.reloaded", path=str(self._path))
            except ConfigError as exc:
                log.error("config.reload_rejected", path=str(self._path), reason=exc.reason)
        return self._value
