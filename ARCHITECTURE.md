# Architecture

Pons Pal is a single-process, single-loop trading agent. This document
explains the event bus, the data layer, and the boundaries between them, and
why each boundary is where it is.

## The event bus

`core/engine.EventBus` is a priority-ordered dispatcher over one
`asyncio.Queue`. Six frozen dataclasses flow through it, in pipeline order:

| event | produced by | consumed by |
| --- | --- | --- |
| `TickEvent` | the normalizer, from feed ticks | the rolling cache |
| `SentimentEvent` | the sentiment reader | the engine's sentiment map |
| `SignalEvent` | each strategy | the engine's signal list |
| `OrderEvent` | the portfolio builder | the risk gate |
| `RiskEvent` | the risk gate | the execution router (if approved) |
| `FillEvent` | the router | the book, the store, metrics, notifications |

A cycle (`PonsPalEngine.run_cycle`) publishes ticks and drains the bus, runs
the strategies and drains, builds orders and drains. Draining between stages
is what makes "every signal is seen before any order is built" a property of
the loop rather than of handler ordering.

Events are frozen so no handler can edit what a later handler sees. The
concrete threat is a strategy that edits an order between the builder and the
gate; with frozen events it cannot.

## Strategy isolation

Strategies receive a `PonsStrategyContext` whose portfolio view is a deep
copy. They return `PonsSignal` records and nothing else. They cannot place
orders, reach the book, or see another strategy's output. One strategy
raising an arithmetic error is logged and skipped; it does not take the cycle
down. Only the `PortfolioBuilder` turns signals into orders, and only after
the `RiskGate` has judged an order does a `FillEvent` change the book.

## The data layer

- `data/feeds.py` defines three protocols (`MarketFeed`, `StockDataSource`,
  `PairSource`) and the implementations behind them: replay and static
  sources for paper runs and tests, Chainlink- and index-backed sources for
  production. The engine depends on the protocols only.
- `data/normalizer.py` folds validated `RawTick` records into canonical
  `PonsBar` records so every strategy sees one shape regardless of the feed.
- `data/cache.py` keeps a bounded deque of bars per pair, with an optional
  Redis mirror that can warm a restart but can never block a cycle.
- `data/historical.py` loads CSV bars for `pons-pal replay`, dropping any
  row that fails validation rather than coercing it.

Every value from outside is parsed into a frozen pydantic model that forbids
unknown fields and rejects NaN. The alternative, trusting decoded chain data,
means a NaN pool read becomes a NaN order becomes an unbounded one.

## The risk gate

`core/risk.RiskGate.evaluate` runs sixteen checks in a fixed order: the three
loss breakers, chain exposure, position notional (REDUCE), pair
concentration, 24h volume, then the Pons-native checks (feed freshness,
stock-back liveness, underlying health, gas reserve), then the execution
floors (per-order and daily ceilings, slippage, impact). The first BLOCK
wins; REDUCE outcomes compose to the smallest allowed size. Limits come from
`config/risk.yaml` through a `HotReloader` that keeps the last good file if a
reload fails validation.

`CircuitBreakers` persists to SQLite before returning the block, blocks every
subsequent order including sells, and clears only with an explicit
`confirm=True` that nothing in the trading loop ever passes.

## The execution boundary

`execution/router.ExecutionRouter` is the last code before value moves. It
re-checks what the gate decided (approved, kill switch off, slippage and
impact within limits, notional under the ceiling) at the moment of
submission, then either simulates a fill (paper) or builds an EIP-1559
transaction through `adapters/chain.RobinhoodChainClient`, signs it through
`signer.LocalSigner`, broadcasts, waits for the receipt, and decodes the fill.
Any of: paper mode, an unarmed signer, a blank router address, or a missing
router ABI makes the live path unreachable.

`signer.py` is the only module that touches the private key. It exposes an
address and a `sign` method; its `repr` shows the address alone.

## The agent surfaces

- `agent/mcp.py` is an aiohttp tool server (`get_state`, `get_positions`,
  `get_disclosure`, `run_cycle`, `disarm`, `resume_breaker`) that requires the
  cycle secret on every request and refuses to start without one.
- `agent/notify.py` posts per-decision and per-fill JSON to one operator-configured
  webhook, through the outbound guard, never blocking the loop.
- `agent/disclosure.py` is the self-report: mode, arm state, limits, breakers,
  feed freshness, and the disclaimer, built from the same state object the
  engine uses.
- `monitoring/metrics.py` holds Prometheus gauges, counters, and a latency
  histogram on a private registry.

## Persistence

`store.PonsStore` wraps one SQLite file with parameterized statements:
`trades`, `pnl_snapshots`, `risk_events`, `stockback_ledger`, `breakers`,
`controls`. The chain remains the durable record for fills; the database
holds what the chain does not: refusals, breaker state, the kill switch.

## Where the lines are

| boundary | module | what crosses it |
| --- | --- | --- |
| environment to settings | `config.py` | validated `Settings`, secrets as `SecretStr` |
| YAML to tunables | `config.py` | validated models via `yaml.safe_load` |
| chain to engine | `adapters/chain.py` | `PonsFeedReading`, receipts as dicts |
| providers to engine | `net.py` + `data/feeds.py` | validated `PonsPair`, closes |
| strategies to builder | `strategies/base.py` | `PonsSignal` only |
| builder to gate | `core/portfolio.py` | `PonsOrder` |
| gate to router | `core/risk.py` | `PonsRiskDecision` |
| router to chain | `signer.py` | raw signed bytes |
