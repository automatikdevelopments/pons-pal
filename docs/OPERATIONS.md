# Operations

How to run Pons Pal, arm it, halt it, and bring it back. Every command below
prints the runtime banner first; every command that widens what the agent may
do requires `--confirm`.

## Install

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Leave `.env` as it is for a paper run. `PONS_PAL_MODE` defaults to `paper`
and, with no trading key, the agent reports `unarmed`.

## Run in paper mode

```bash
pons-pal cycle
```

Runs one full pass (ingest, signal, portfolio, risk gate, execute) and
prints the cycle report as JSON. Fills are simulated against the modeled
quote and recorded with `simulated: true`.

```bash
pons-pal run
```

Runs cycles on `engine.cycle_interval_s` from `config/default.yaml` until
interrupted.

```bash
pons-pal replay --csv bars.csv
```

Paper-trades a CSV of bars (`pair_id, ts, open, high, low, close, volume_usd`)
through the same pipeline. A replay is always paper, whatever `.env` says.

## Inspect

```bash
pons-pal state
```

The machine self-report as JSON: mode, arm state, equity, P&L, drawdown,
positions, breakers, feed freshness, and the 24h notional used.

```bash
pons-pal disclosure
```

The same facts as text, with the limits in force and the disclaimer.

Prometheus metrics are served on `PONS_PAL_METRICS_HOST:PONS_PAL_METRICS_PORT`
(default `127.0.0.1:8000`) while `pons-pal mcp` is running.

## Arm for live trading

Do this only after the on-chain values in `config/pons.yaml` are confirmed
against [ponsfamily.com](https://ponsfamily.com) and the router ABI is in
place at `router.abi_path`.

1. Create a **dedicated budget wallet**. Fund it with only what the agent may
   lose, plus the ETH gas reserve.
2. Put its private key in `PONS_PAL_TRADING_KEY` and its address in
   `PONS_PAL_BUDGET_ADDRESS`. The agent aborts at startup if the key does not
   derive the address.
3. Set `PONS_PAL_MODE=live` and `PONS_ROUTER_ADDRESS`.
4. Run `pons-pal state` and confirm `arm_state` is `armed`. If it says
   `unarmed`, one of the four requirements (mode, key, router address, ABI) is
   missing; nothing will be submitted.
5. Run `pons-pal cycle` once and read the report before running `pons-pal run`.

## Halt

```bash
pons-pal disarm
```

Stops execution immediately. The flag is in memory and in the database, so a
restart comes back disarmed. Every cycle still runs ingest, signals, and the
gate; every approved order is refused at the router and recorded as a block.

Through MCP: `{"tool": "disarm"}` with the bearer secret.

To lift the kill switch:

```bash
pons-pal arm --confirm
```

## Circuit breakers

When intraday, weekly, or monthly P&L breaches its limit the matching breaker
trips, the block is persisted, and every subsequent order is refused until a
person resumes it:

```bash
pons-pal resume --breaker intraday_loss --confirm
```

Resuming does not re-evaluate the loss. If the book is still below the limit
the next order trips the breaker again, which is the intended behavior: a
breaker is a pause for a person to look, not a retry counter.

## Tune limits

Edit `config/risk.yaml`. The gate re-reads it when the file's mtime changes.
A file that fails validation is ignored and the previous limits stay in
force; check the log for `config.reload_rejected`.

## Serve the MCP endpoint

```bash
PONS_PAL_CYCLE_SECRET=<random secret> pons-pal mcp
```

Serves `POST /mcp` and `GET /mcp/tools` on `PONS_PAL_MCP_HOST:PONS_PAL_MCP_PORT`
(default `127.0.0.1:8765`). Every request needs `Authorization: Bearer
<secret>`. Keep it on loopback or behind an authenticating proxy; the
endpoint can run a trading cycle.

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer $PONS_PAL_CYCLE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"tool": "get_state"}'
```

## Notifications

Set `PONS_PAL_WEBHOOK_URL` to an https URL and add its host to
`outbound.allowed_hosts` in `config/pons.yaml`. Every gate decision (including
refusals) and every fill posts a JSON body with both `content` and `text`
fields, so Discord-, Telegram-, and X-shaped receivers can read it. A failed
post is logged and dropped; it never blocks the loop.

## Docker

```bash
docker compose up --build
```

Ports are bound to `127.0.0.1` on the host. Secrets come from `.env`.
