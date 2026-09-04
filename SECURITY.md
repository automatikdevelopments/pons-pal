# Security

Pons Pal holds a key that can move value and exposes three network surfaces
(Prometheus, the MCP endpoint, and outbound webhooks). This document is the
threat model, the invariants the code enforces, and how to report a problem.

## Threat model

Assume the operator, the host, and the database are all fallible.

- **The operator** can misconfigure a limit, paste the wrong address, or
  point the agent at the wrong RPC. The code validates every field, refuses a
  key that does not derive the published budget wallet, and refuses an RPC
  that does not serve the configured chain id.
- **The host** can be compromised, its logs read, its environment dumped. The
  signing key is read from the environment into a `SecretStr`, is never
  logged (a scrubbing processor runs first in the `structlog` chain), and
  never appears in an exception message or an HTTP response.
- **The database** can be tampered with. It is a cache of decisions and
  breaker state; the chain is the durable record of fills. A tampered
  database can at most make the agent stricter (a phantom tripped breaker
  halts trading). Every widening action needs a person and a `--confirm`.
- **The market** is adversarial. Anyone can launch a pair, so pair records,
  token names, and tickers are attacker-controlled input: bounded, validated,
  and never interpolated into SQL, shell, or HTML.
- **Every remote** is untrusted: pair index, sentiment sources, stock-data
  provider, webhook. All leave the process through one outbound guard.

Out of scope: market risk, token quality, and trading losses are not
vulnerabilities. An agent that follows its limits and still loses money is
working as designed. The disclaimer in the README is not decorative.

## Invariants (enforced in code, tested in `tests/`)

1. **Paper by default, unarmed without a key.** `PONS_PAL_MODE` defaults to
   `paper`. Without `PONS_PAL_TRADING_KEY` the signer is `UnarmedSigner` and
   refuses to sign; without a router address and ABI the router reports
   `live_capable = False`. Both are required for a real swap.
2. **The key must derive the budget wallet.** `keys.load_budget_account`
   aborts at startup if the key does not derive `PONS_PAL_BUDGET_ADDRESS`.
3. **Secrets never leave the process.** Read at call time, never at import;
   `SecretStr` everywhere; `log.scrub_secrets` redacts secret-named fields and
   any 32-byte hex value; `.env` is git-ignored; CI runs `gitleaks` over the
   full history.
4. **Risk limits run at the execution boundary.** `core/risk.RiskGate` runs
   sixteen ordered checks before every order; `execution/router.ExecutionRouter`
   re-checks the decision, the kill switch, the slippage cap, the impact
   floor, and the per-order ceiling at submission. There is no code path from
   a signal to a swap that skips either.
5. **Breakers halt and require a person.** A tripped breaker is persisted
   before the block is returned, blocks every subsequent order including
   sells, survives restarts, and clears only through `resume --confirm` or
   the secret-gated MCP tool with `confirm: true`.
6. **The kill switch is immediate and persistent.** `disarm` sets an in-memory
   flag the router checks on every execution and a database control row so a
   restart comes back disarmed. Lifting it requires `arm --confirm`.
7. **The ETH gas reserve is never spent.** `eth_reserve` blocks any order
   whose gas estimate would take the balance below `eth_gas_reserve`. An
   unreadable balance is treated as zero, which blocks.
8. **Malformed input is a recorded refusal, never a silent trade.** Every
   decoded chain value, provider payload, and model number is parsed into a
   frozen pydantic model that forbids unknown fields and rejects NaN and
   infinity. Stock-back arithmetic guards every division.
9. **Parameterized SQL only.** `store.PonsStore` builds no SQL from strings.
10. **`yaml.safe_load` only.** No `pickle`, `eval`, `exec`, `os.system`, or
    `shell=True` anywhere in `src/`.
11. **One outbound guard.** `net.SafeHttpClient` enforces https, an exhaustive
    host allowlist from `config/pons.yaml`, DNS resolution that rejects
    private, loopback, link-local, multicast, and metadata addresses (every
    resolved address, not just the first), no redirects, a response size cap,
    and a timeout. The webhook URL is operator configuration only.
12. **The MCP endpoint authenticates its own caller.** Every request needs
    `PONS_PAL_CYCLE_SECRET` as a bearer token compared with
    `hmac.compare_digest`; the server refuses to start without a secret;
    request bodies are capped; responses carry a strict CSP and `nosniff`.
    It binds to loopback by default.
13. **Metrics expose no secrets.** Only numbers and the already-public
    budget address; loopback by default; put a scrape-authenticating proxy in
    front of it.
14. **No fabricated on-chain facts.** Router, feed, and settlement addresses
    are blank with `TODO(pons)` until confirmed against
    [ponsfamily.com](https://ponsfamily.com). A blank router keeps execution
    unarmed.

## Scanning

CI (`.github/workflows/ci.yml`) is the local mirror of the zauth Vector
gate: `ruff`, `mypy --strict`, `pytest`, then `pip-audit --strict`,
`bandit -r src`, and `gitleaks` over the full history. Every job blocks. A
finding is fixed, never suppressed: the source contains no `# nosec`, no
`# noqa` for a security rule, and no `# type: ignore` in `src/`.

Run Vector's RepoScan against the repository, and a web scan against any
deployment that exposes the MCP or metrics ports, before arming a wallet.

## Reporting

Report vulnerabilities privately through the contact on
[ponsfamily.com](https://ponsfamily.com). Do not open a public issue for a
security problem. Include the commit, the reproduction, and the impact; a
report that shows a path from untrusted input to a signature is the highest
priority this project has.
