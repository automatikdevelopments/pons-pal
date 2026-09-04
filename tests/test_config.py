# SPDX-License-Identifier: MIT
# Pons Family - configuration and key tests for pons.family
"""Settings parse from the environment, YAML validates, bad config raises ``ConfigError``."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pons_pal.config import (
    HotReloader,
    RiskLimits,
    load_engine_config,
    load_pons_config,
    load_risk_limits,
    load_sentiment_config,
    load_settings,
)
from pons_pal.errors import ConfigError
from pons_pal.keys import load_budget_account
from pons_pal.models import Mode
from tests.conftest import CONFIG_DIR, DEV_ADDRESS, DEV_KEY, OTHER_ADDRESS


def test_blank_environment_is_paper_and_unarmed() -> None:
    settings = load_settings({})
    assert settings.mode is Mode.PAPER
    assert not settings.has_trading_key
    assert settings.mode_explicit is False
    assert load_budget_account(settings) is None


def test_blank_values_are_unset() -> None:
    settings = load_settings({"PONS_PAL_TRADING_KEY": "   ", "PONS_PAL_MODE": "paper"})
    assert not settings.has_trading_key
    assert settings.mode_explicit is True


def test_key_without_budget_address_is_refused() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({"PONS_PAL_TRADING_KEY": DEV_KEY})
    assert "PONS_PAL_BUDGET_ADDRESS" in str(info.value) or "PONS_PAL_TRADING_KEY" in str(info.value)


def test_key_must_derive_published_address() -> None:
    settings = load_settings(
        {"PONS_PAL_TRADING_KEY": DEV_KEY, "PONS_PAL_BUDGET_ADDRESS": OTHER_ADDRESS}
    )
    with pytest.raises(ConfigError) as info:
        load_budget_account(settings)
    assert info.value.field == "PONS_PAL_TRADING_KEY"
    assert DEV_KEY not in str(info.value)


def test_matching_key_loads_and_never_prints() -> None:
    settings = load_settings(
        {"PONS_PAL_TRADING_KEY": DEV_KEY, "PONS_PAL_BUDGET_ADDRESS": DEV_ADDRESS}
    )
    account = load_budget_account(settings)
    assert account is not None
    assert account.address.lower() == DEV_ADDRESS.lower()
    assert DEV_KEY not in repr(settings)
    assert DEV_KEY not in str(settings.trading_key)


def test_malformed_key_is_refused_without_echo() -> None:
    settings = load_settings(
        {"PONS_PAL_TRADING_KEY": "not-a-key", "PONS_PAL_BUDGET_ADDRESS": DEV_ADDRESS}
    )
    with pytest.raises(ConfigError) as info:
        load_budget_account(settings)
    assert "not-a-key" not in str(info.value)


def test_invalid_address_shape() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({"PONS_ROUTER_ADDRESS": "0x1234"})
    assert info.value.field == "PONS_ROUTER_ADDRESS"


def test_webhook_must_be_https() -> None:
    with pytest.raises(ConfigError):
        load_settings({"PONS_PAL_WEBHOOK_URL": "http://example.com/hook"})


def test_repo_configs_validate() -> None:
    assert load_engine_config(CONFIG_DIR / "default.yaml").engine.mode is Mode.PAPER
    limits = load_risk_limits(CONFIG_DIR / "risk.yaml")
    assert limits.intraday_loss_pct == 2.0
    assert limits.hot_reload is True
    assert load_sentiment_config(CONFIG_DIR / "sentiment.yaml").blend.min_sources >= 1
    pons = load_pons_config(CONFIG_DIR / "pons.yaml")
    assert pons.chain.chain_id == 4663
    assert pons.router.address == ""


def test_risk_yaml_rejects_unknown_and_bad_values(tmp_path: Path) -> None:
    bad = tmp_path / "risk.yaml"
    bad.write_text("intraday_loss_pct: 0\n")
    with pytest.raises(ConfigError):
        load_risk_limits(bad)
    bad.write_text("surprise: 1\n")
    with pytest.raises(ConfigError):
        load_risk_limits(bad)
    bad.write_text("intraday_loss_pct: 8\nweekly_loss_pct: 5\n")
    with pytest.raises(ConfigError):
        load_risk_limits(bad)
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_risk_limits(bad)


def test_yaml_is_safe_loaded(tmp_path: Path) -> None:
    evil = tmp_path / "risk.yaml"
    evil.write_text("intraday_loss_pct: !!python/object/apply:os.system ['echo pwned']\n")
    with pytest.raises(ConfigError):
        load_risk_limits(evil)


def test_hot_reloader_keeps_last_good_on_bad_file(tmp_path: Path) -> None:
    path = tmp_path / "risk.yaml"
    path.write_text("intraday_loss_pct: 1.0\n")
    reloader = HotReloader(path, load_risk_limits)
    assert reloader.current().intraday_loss_pct == 1.0
    time.sleep(0.01)
    path.write_text("intraday_loss_pct: 1.5\n")
    _bump_mtime(path)
    assert reloader.current().intraday_loss_pct == 1.5
    path.write_text("intraday_loss_pct: -3\n")
    _bump_mtime(path)
    assert reloader.current().intraday_loss_pct == 1.5


def test_hot_reload_disabled_ignores_changes(tmp_path: Path) -> None:
    path = tmp_path / "risk.yaml"
    path.write_text("intraday_loss_pct: 1.0\n")
    reloader = HotReloader(path, load_risk_limits, enabled=False)
    path.write_text("intraday_loss_pct: 1.5\n")
    _bump_mtime(path)
    assert reloader.current().intraday_loss_pct == 1.0


def test_limits_bounds() -> None:
    with pytest.raises(ValueError):
        RiskLimits(max_slippage_bps=20_000)


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    import os

    os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))
