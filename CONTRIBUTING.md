# Contributing

## Strategies

A new strategy must:

1. Extend `Strategy` in `src/pons_pal/strategies/base.py`
2. Implement `generate(ctx: PonsStrategyContext) -> list[PonsSignal]` and set `name`
3. Return signals only. No orders, no book access, no network
4. Register in `src/pons_pal/strategies/__init__.py` and add a block to
   `config/default.yaml` under `strategies:`
5. Ship tests in `tests/test_strategies.py`, including one that feeds it NaN
   and one that feeds it too little history
6. Get a paragraph in `docs/signals.md`

Run it through `pons-pal replay --csv <bars.csv>` before opening a PR and put
the numbers in the description.

## Code

- Python 3.11+, typed throughout, `mypy --strict` clean
- `ruff check` and `ruff format` clean, line length 100
- Every file starts with the SPDX line and the
  `Pons Family - <what this file is> for pons.family` banner
- Every public function has a docstring. If a decision is not obvious, say
  why, not what
- No new dependencies without a reason in the PR. Pin the version
- No `# noqa` on a security rule, no `# type: ignore` in `src/`, no `# nosec`
- No emoji, American spelling

## Pull requests

Open against `main`. `make check` runs the same lint, types, tests, and
security gate as CI. A red check on your laptop is cheaper than a red check in
the PR.

Do not put a real key, address, or RPC URL in a test. The synthetic keys in
`tests/conftest.py` control nothing.

## Reporting a security problem

Privately, through the contact on [ponsfamily.com](https://ponsfamily.com).
Not as a public issue. See [SECURITY.md](SECURITY.md).
