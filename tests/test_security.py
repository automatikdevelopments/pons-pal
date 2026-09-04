# SPDX-License-Identifier: MIT
# Pons Family - security boundary tests for pons.family
"""The SSRF guard, chain decoding, input sanitization, log scrubbing, and MCP auth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr, ValidationError

from pons_pal.adapters.chain import decode_feed_round
from pons_pal.adapters.sentiment import parse_message_texts, parse_rss_titles, score_text
from pons_pal.agent.mcp import McpServer, constant_time_equal
from pons_pal.data.feeds import decode_close_series, decode_pairs
from pons_pal.data.normalizer import RawTick, TickNormalizer
from pons_pal.errors import ConfigError, DecodeError, NetworkGuardError
from pons_pal.execution.fills import confirm_fill, simulated_fill
from pons_pal.log import scrub_secrets
from pons_pal.models import PonsPair, PonsSignal, sanitize_display
from pons_pal.net import SafeHttpClient, check_url, is_public_address
from pons_pal.signer import UnarmedSigner
from tests.conftest import FEED_ADDRESS, NOW, make_order, make_pair

ALLOWED = ["rpc.mainnet.chain.robinhood.com", "example.com"]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.com/x", "https"),
        ("https://user:pw@example.com/x", "credentials"),
        ("https://169.254.169.254/latest/meta-data", "metadata"),
        ("https://metadata.google.internal/x", "metadata"),
        ("https://evil.example.net/x", "allowlist"),
        ("https://127.0.0.1/x", "allowlist"),
        ("https:///x", "host"),
    ],
)
def test_guard_refuses(url: str, reason: str) -> None:
    with pytest.raises(NetworkGuardError) as info:
        check_url(url, ALLOWED, resolve=False)
    assert reason in info.value.reason


def test_guard_refuses_private_ip_literal_even_if_allowlisted() -> None:
    with pytest.raises(NetworkGuardError):
        check_url("https://10.0.0.5/x", ["10.0.0.5"], resolve=False)
    with pytest.raises(NetworkGuardError):
        check_url("https://[::1]/x", ["::1"], resolve=False)


def test_guard_accepts_allowlisted_https() -> None:
    assert check_url("https://Example.com/path?q=1", ALLOWED, resolve=False) == "example.com"


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),
        ("2606:4700::1111", True),
        ("not-an-ip", False),
    ],
)
def test_public_address_classification(address: str, public: bool) -> None:
    assert is_public_address(address) is public


def test_client_check_without_network() -> None:
    client = SafeHttpClient(ALLOWED, resolve=False)
    assert client.check("https://example.com/") == "example.com"
    with pytest.raises(NetworkGuardError):
        client.check("https://other.example.org/")


def test_feed_round_decoding() -> None:
    now = NOW
    raw = (1, 200_00000000, int(now.timestamp()) - 60, int(now.timestamp()) - 60, 1)
    reading = decode_feed_round(FEED_ADDRESS, "AAPL", raw, 8, now)
    assert reading.price_usd == pytest.approx(200.0)
    assert reading.age_s(now) == pytest.approx(60.0)
    with pytest.raises(DecodeError):
        decode_feed_round(FEED_ADDRESS, "AAPL", (1, 0, 0, int(now.timestamp()), 1), 8, now)
    with pytest.raises(DecodeError):
        decode_feed_round(FEED_ADDRESS, "AAPL", (1, -5, 0, int(now.timestamp()), 1), 8, now)
    with pytest.raises(DecodeError):
        decode_feed_round(FEED_ADDRESS, "AAPL", (1, 5, 0), 8, now)
    with pytest.raises(DecodeError):
        decode_feed_round(FEED_ADDRESS, "AAPL", (1, 5, 0, int(now.timestamp()) + 3_600, 1), 8, now)
    with pytest.raises(DecodeError):
        decode_feed_round(FEED_ADDRESS, "AAPL", (1, 5, 0, int(now.timestamp()), 1), 99, now)
    with pytest.raises(DecodeError):
        decode_feed_round(FEED_ADDRESS, "AAPL", ("1", 5, 0, int(now.timestamp()), 1), 8, now)


def test_pair_index_decoding_drops_malformed() -> None:
    good = make_pair().model_dump(mode="json")
    bad = dict(good, liquidity_usd=-1.0)
    nan = dict(good, volume_24h_usd="NaN")
    body = (
        '{"pairs": ['
        + ",".join(__import__("json").dumps(x) for x in (good, bad, nan))
        + ', 5, "x"]}'
    ).encode()
    pairs = decode_pairs(body)
    assert len(pairs) == 1 and pairs[0].pair_id == "PONS-AAPL"
    assert decode_pairs(b"not json") == []
    assert decode_pairs(b'{"pairs": {}}') == []


def test_close_series_decoding() -> None:
    assert decode_close_series(
        b'{"bars": [{"c": 1.5}, {"c": -1}, {"c": "x"}, {"c": null}, 7]}'
    ) == [1.5]
    assert decode_close_series(b"[]") == []


def test_token_names_are_bounded_and_printable() -> None:
    assert sanitize_display("A" * 500) == "A" * 64
    assert sanitize_display("evil\x00\x1b[31mname\n") == "evil[31mname"
    assert sanitize_display("\x00\x01") == "unnamed"
    pair = make_pair().model_copy(update={"token_name": "x"})
    assert PonsPair.model_validate(
        dict(pair.model_dump(), token_name="bad\x00name" * 20)
    ).token_name.startswith("badname")
    with pytest.raises(ValidationError):
        PonsPair.model_validate(dict(pair.model_dump(), token_address="0xnothex"))


def test_models_reject_nan_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PonsSignal(strategy="s", pair_id="p", score=float("nan"), confidence=1.0, horizon_s=1)
    with pytest.raises(ValidationError):
        PonsSignal(strategy="s", pair_id="p", score=0.0, confidence=1.0, horizon_s=1, extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RawTick(pair_id="p", ts=NOW, price_usd=0.0)


def test_normalizer_drops_malformed_and_builds_bars() -> None:
    normalizer = TickNormalizer(60)
    assert normalizer.parse({"pair_id": "p", "ts": NOW.isoformat(), "price_usd": -1}) is None
    assert normalizer.dropped == 1
    ticks = [
        RawTick(pair_id="p", ts=NOW, price_usd=1.0, volume_usd=10.0),
        RawTick(pair_id="p", ts=NOW + timedelta(seconds=30), price_usd=2.0, volume_usd=10.0),
        RawTick(pair_id="p", ts=NOW + timedelta(seconds=61), price_usd=1.5),
    ]
    closed = normalizer.ingest(ticks)
    assert len(closed) == 1 and closed[0].high == 2.0 and closed[0].volume_usd == 20.0
    assert len(normalizer.flush()) == 1


def test_fills_refuse_reverts_and_zero_amounts() -> None:
    order = make_order()
    receipt = {
        "status": 1,
        "transactionHash": bytes.fromhex("ab" * 32),
        "gasUsed": 21_000,
        "effectiveGasPrice": 10**9,
    }
    fill = confirm_fill(order, receipt, amount_token=10.0, amount_usd=100.0, price_usd=10.0)
    assert (
        not fill.simulated
        and fill.tx_hash == "0x" + "ab" * 32
        and fill.gas_eth == pytest.approx(2.1e-5)
    )
    with pytest.raises(Exception, match="reverted"):
        confirm_fill(
            order, dict(receipt, status=0), amount_token=10.0, amount_usd=100.0, price_usd=10.0
        )
    with pytest.raises(DecodeError):
        confirm_fill(order, receipt, amount_token=0.0, amount_usd=100.0, price_usd=10.0)
    with pytest.raises(DecodeError):
        simulated_fill(order, 100.0, float("nan"), 0.0)
    paper = simulated_fill(order, 100.0, 10.0, 100.0)
    assert paper.simulated and paper.price_usd == pytest.approx(10.1)


def test_log_scrubber_redacts_keys_and_hex() -> None:
    event = scrub_secrets(
        None, "info", {"event": "x", "trading_key": "abc", "note": "sent 0x" + "ab" * 32 + " ok"}
    )
    assert event["trading_key"] == "[redacted]"
    assert "ab" * 32 not in str(event["note"])
    assert event["event"] == "x"


def test_unarmed_signer_refuses() -> None:
    signer = UnarmedSigner()
    assert signer.address is None and not signer.armed
    with pytest.raises(Exception, match="unarmed"):
        signer.sign({})


def test_sentiment_parsing_is_bounded() -> None:
    assert score_text("very bullish breakout, buy") == 1.0
    assert score_text("nothing here") is None
    rss = b"<rss><channel><title>Feed</title><item><title>AAPL beat</title></item></channel></rss>"
    assert parse_rss_titles(rss, 1) == ["Feed"]
    assert parse_rss_titles(b"<not xml", 5) == []
    assert parse_message_texts(b'{"messages": [{"body": "buy"}, {"nope": 1}, "x"]}', 10) == ["buy"]


def test_constant_time_compare() -> None:
    assert constant_time_equal("abc", "abc")
    assert not constant_time_equal("abc", "abd")
    assert not constant_time_equal("", "abc")


class _StubEngine:
    def state(self) -> object:
        from pons_pal.models import ArmState, Mode, PonsPalState

        return PonsPalState(
            mode=Mode.PAPER,
            arm_state=ArmState.UNARMED,
            budget_address=None,
            equity_usd=1.0,
            pnl_today_usd=0.0,
            drawdown_pct=0.0,
            positions_count=0,
            breakers=(),
            feeds=(),
            daily_notional_used_usd=0.0,
            stockback_accrued_usd=0.0,
            updated_at=datetime.now(tz=UTC),
        )

    @property
    def positions(self) -> dict[str, object]:
        return {}


def test_mcp_requires_secret_to_start() -> None:
    with pytest.raises(ConfigError):
        McpServer(_StubEngine(), None, "127.0.0.1", 0)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        McpServer(_StubEngine(), SecretStr(""), "127.0.0.1", 0)  # type: ignore[arg-type]


async def test_mcp_authenticates_every_request() -> None:
    server = McpServer(_StubEngine(), SecretStr("s3cret-value"), "127.0.0.1", 0)  # type: ignore[arg-type]
    async with TestClient(TestServer(server.app())) as client:
        assert (await client.get("/mcp/tools")).status == 401
        assert (await client.post("/mcp", json={"tool": "get_state"})).status == 401
        wrong = {"Authorization": "Bearer wrong"}
        assert (await client.post("/mcp", json={"tool": "get_state"}, headers=wrong)).status == 401
        right = {"Authorization": "Bearer s3cret-value"}
        response = await client.post("/mcp", json={"tool": "get_state"}, headers=right)
        assert response.status == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        body = await response.json()
        assert body["result"]["arm_state"] == "unarmed"
        assert (await client.post("/mcp", json={"tool": "nope"}, headers=right)).status == 404
        assert (await client.post("/mcp", data=b"{bad", headers=right)).status == 400
        big = await client.post("/mcp", data=b"x" * (70 * 1024), headers=right)
        assert big.status == 413
