# Execution

`ExecutionRouter` is the last code before value moves.

## Quote

Before anything is submitted the router builds a `PonsQuote` from the pool's
liquidity and a constant-product impact estimate:

```
impact_bps   = q / (L/2 + q) * 10_000        q = order notional, L = pool liquidity
slippage_bps = impact_bps + pool_fee_bps
```

It is an estimate. It exists so an order that could never clear the impact
floor is refused in the gate, before any RPC call is spent. The on-chain
`min_amount_out` is derived from `max_slippage_bps` so a worse fill reverts
instead of filling.

## Final checks

The router does not trust the gate. At submission it checks again:

- the decision is PASS or REDUCE
- the kill switch is off
- slippage and impact still clear the limits on the live quote
- notional is under `per_order_max_usd`

Then one of three things happens.

| Condition | Result |
|---|---|
| Paper mode | Simulated fill at the quoted price, `simulated: true` |
| Live, armed, router configured | EIP-1559 swap built, signed locally, broadcast, receipt decoded |
| Live but unarmed, blank router, or missing ABI | Refusal, recorded as a block |

## The live path

1. `adapters/chain.RobinhoodChainClient` builds the swap against the router
   in `config/pons.yaml`
2. `signer.LocalSigner` signs it. This is the only module that touches the key
3. The transaction is broadcast and the router waits `chain.confirmations`
   blocks
4. `execution/fills.py` decodes the receipt into a `PonsFill`
5. The book, the store, the metrics, and the webhook see the fill, in that
   order

## Errors

A rejected or timed-out submission is logged with full context, counted in
`pons_pal_chain_errors_total`, and posted to the webhook. It is not retried.
Retrying is a strategy decision, and the next cycle will make it if the
signal still stands.

There is no venue fallback. If Robinhood Chain is unreachable the agent does
nothing, which is the correct amount.
