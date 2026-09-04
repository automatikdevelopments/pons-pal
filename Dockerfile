# SPDX-License-Identifier: MIT
# Pons Family - container image for pons.family
#
# Runs as an unprivileged user with no secrets baked in. Every secret arrives
# through the environment at run time, so an image pulled from a registry can
# never move funds on its own: without `PONS_PAL_TRADING_KEY` it starts unarmed
# in paper mode, which is the only mode a container should default to.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system pons && useradd --system --gid pons --home-dir /app pons

WORKDIR /app

COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config

RUN pip install -r requirements.txt && pip install --no-deps . \
    && mkdir -p /app/data && chown -R pons:pons /app

USER pons

# Prometheus metrics and the MCP endpoint bind to loopback by default. Publish
# them through an authenticating proxy, never directly.
EXPOSE 8000 8765

ENV PONS_PAL_MODE=paper \
    DATABASE_URL=sqlite:////app/data/pons_pal.db

ENTRYPOINT ["pons-pal"]
CMD ["run"]
