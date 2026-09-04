# Risk model

Every `OrderEvent` goes through `RiskGate.evaluate` before it reaches the
router. Checks run in a fixed order. The first BLOCK wins. REDUCE outcomes
compose to the smallest allowed size.

## Order of checks

| Stage | Check | Outcome |
|---|---|---|
| 0 | Any breaker already tripped | BLOCK |
| 1 | Intraday P&L below `intraday_loss_pct` | BLOCK, trips breaker |
| 2 | Weekly P&L below `weekly_loss_pct` | BLOCK, trips breaker |
| 3 | Monthly P&L below `monthly_loss_pct` | BLOCK, trips breaker |
| 4 | Gross chain exposure above `chain_exposure_pct` | BLOCK |
| 5 | Position would exceed `max_position_pct` | REDUCE |
| 6 | Pairs on one stock above `max_sector_pct` | BLOCK |
| 7 | Pair 24h volume below `min_adv_usd` | BLOCK |
| 8 | Stock feed older than `feed_max_age_s` (market hours) | BLOCK |
| 9 | Buy on a pair with accrual below `stockback_min_rate` | BLOCK |
| 10 | Paired stock down more than `underlying_max_drawdown_pct` in 5 sessions | BLOCK buys |
| 11 | Swap would take ETH below `eth_gas_reserve` | BLOCK |
| 12 | Order above `per_order_max_usd` | REDUCE |
| 13 | 24h notional would pass `daily_notional_max_usd` | REDUCE or BLOCK |
| 14 | Expected slippage above `max_slippage_bps` | BLOCK |
| 15 | Modeled price impact above `max_price_impact_bps` | BLOCK |

Why this order: loss breakers first so a losing book blocks before any sizing
runs. Book-level checks before per-pair checks. Execution floors last because
they act on a size the earlier checks may already have cut.

Off hours the feed limit widens to `feed_max_age_offhours_s` (a day) because
Chainlink stock feeds idle when the market is closed. It never goes away.

## Breakers

Checks 1 to 3 are circuit breakers.

| Breaker | Trips when | Clears when |
|---|---|---|
| `intraday_loss` | Today's P&L < -2% of equity | `pons-pal resume --breaker intraday_loss --confirm` |
| `weekly_loss` | Week's P&L < -5% | same, `weekly_loss` |
| `monthly_loss` | Month's P&L < -10% | same, `monthly_loss` |

A trip is written to SQLite before the block is returned. It blocks sells as
well as buys. It survives a restart. Nothing in the trading loop ever passes
`confirm=True`; only the CLI and the MCP tool can, and both need a person.

Resuming does not re-evaluate the loss. If the book is still below the limit
the next order trips it again. That is the point: a breaker is a pause for a
person to look, not a retry counter.

## Hot reload

The gate re-reads `config/risk.yaml` when its mtime changes. A file that
fails validation is ignored and the last good limits stay in force, logged as
`config.reload_rejected`. A typo must never widen a limit.

## Every decision is recorded

PASS, REDUCE, and BLOCK all go to the `risk_events` table, the
`pons_pal_risk_blocks_total{check}` counter, and the webhook. The refusals
are the interesting part of the log.
