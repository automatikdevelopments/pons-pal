# Changelog

## v0.1.0

- Event bus engine with six event types and drain-between-stages cycles
- Strategies: momentum, mean reversion, statistical pairs, event drift,
  stock-back
- Portfolio builder with strategy weight, confidence, and correlation damping
- Risk gate: seven ordered checks, Pons-native checks, execution floors,
  hot-reload from `config/risk.yaml`
- Circuit breakers persisted to SQLite, manual resume only
- Execution router with constant-product impact model, paper fills, and a
  live path through `web3.py` on Robinhood Chain
- Budget-wallet key assertion and a local signer that is the only module
  touching the key
- Outbound SSRF guard for every remote call
- MCP endpoint, per-trade webhook notifications, disclosure surface
- Prometheus metrics
- CI: ruff, mypy, pytest, pip-audit, bandit, gitleaks
