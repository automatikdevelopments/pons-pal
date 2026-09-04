# Signals

Every strategy returns `PonsSignal` records and nothing else.

| Field | Type | Meaning |
|---|---|---|
| `strategy` | str | Strategy name |
| `pair_id` | str | Pons pair |
| `score` | float | -1.0 to 1.0. Positive wants the pair, negative wants out |
| `confidence` | float | 0.0 to 1.0, usually how much history backed the score |
| `horizon_s` | int | How long the view is expected to hold |
| `rationale` | str | One line for the log and the webhook |

NaN never becomes a score. `Strategy.clip_score` returns 0.0 for anything
that is not finite.

## Strategies

**momentum.** Lookback log return divided by lookback volatility, squashed to
[-1, 1]. Volatility scaling matters more on launchpad tokens than on stocks:
a 20% move on a token that moves 20% an hour is noise.

**mean_reversion.** Z-score of price against its rolling mean. Silent inside
`z_entry`, full signal at the threshold, capped beyond it. A four-sigma
reading on a thin token is more often a repricing than an overshoot.

**pairs.** Residual of a rolling regression of token log price on the paired
stock's log price. Leans against the spread beyond `z_entry`. If the hedge
ratio is not positive the relationship has broken and the strategy says
nothing.

**event.** Blended retail sentiment on the paired stock, decayed by age with
`half_life_s`. An hour-old burst counts half, a day-old one counts nothing.

**stockback.** Pons-native. Ranks pairs by daily accrual rate times a trend
quality score for the paired stock, buys the top `top_k`, exits a held pair
whose accrual stopped. See the README for the rate formula.

## Blending

The portfolio builder takes every signal for a pair and computes a weighted
mean:

```
weight   = strategy_weight * confidence
score    = sum(weight * score) / sum(weight)      clipped to [-1, 1]
```

Strategy weights come from `config/default.yaml`. Then a correlation penalty:
each pair's score is divided by the sum of absolute correlations of its
returns with every other candidate. Five tokens that all track the same stock
are one bet and get sized like one.

## Sizing

```
max_position = equity * max_position_pct
target       = damped_score * max_position     if damped_score >= signal_floor
delta        = target - held
```

Long only. A negative score on a held pair is an exit, on an unheld pair it
is nothing. Orders below `min_order_usd` are dropped, orders above
`per_order_max_usd` are clipped. What the builder emits is a target, not a
fill: the risk gate still gets the last word.
