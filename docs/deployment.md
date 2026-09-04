# Deployment

## Docker

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f pons-pal
```

Ports bind to `127.0.0.1` on the host. Secrets come from `.env`. The image
has no secrets baked in and starts unarmed in paper mode unless the
environment says otherwise.

## Systemd

`/etc/systemd/system/pons-pal.service`:

```ini
[Unit]
Description=Pons Pal trading agent
After=network.target

[Service]
User=pons
WorkingDirectory=/opt/pons-pal
ExecStart=/opt/pons-pal/.venv/bin/pons-pal run
Restart=on-failure
RestartSec=10
EnvironmentFile=/opt/pons-pal/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable pons-pal
systemctl start pons-pal
journalctl -u pons-pal -f
```

A restart comes back in whatever state it was in: a disarmed agent stays
disarmed, a tripped breaker stays tripped.

## Monitoring

Prometheus scrapes `127.0.0.1:8000/metrics` while `pons-pal mcp` is running.
Put an authenticating proxy in front of it before exposing it anywhere.

Alerts worth having:

- `pons_pal_drawdown_pct` above 1.5 (the breaker fires at 2)
- rate of `pons_pal_risk_blocks_total` above 10/min
- `pons_pal_feed_age_seconds` above 900 during market hours
- `pons_pal_breaker_tripped` at 1 for longer than you expect a person to
  take to look
- `pons_pal_armed` changing value at all

## Before arming

1. Confirm every `TODO(pons)` in `config/pons.yaml` against ponsfamily.com
2. Run `pons-pal state` and read it
3. Run `pons-pal cycle` once and read the report
4. Fund the budget wallet with only what the agent may lose, plus the gas
   reserve
5. Set the webhook so every decision reaches you
