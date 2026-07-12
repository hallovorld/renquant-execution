"""Tests for crypto Stage-0 paper battery step checks (D-C12).

Moved verbatim (test logic/mocking/assertions unchanged, imports adjusted)
from renquant-orchestrator's ``tests/test_crypto_stage0_battery.py``
(orchestrator PR #498) — see ``src/renquant_execution/crypto_stage0_checks.py``
module docstring for why these step-check functions and their tests moved
into this repo.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from renquant_execution.crypto_stage0_checks import (
    StepResult,
    step_buying_power,
    step_crypto_status,
    step_order_acceptance,
    step_pair_snapshot,
    step_stop_limit_acceptance,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _fake_account(*, crypto_status="ACTIVE", **kwargs):
    defaults = {
        "id": "test-account-123",
        "buying_power": "10000.00",
        "cash": "10000.00",
        "non_marginable_buying_power": "10000.00",
        "crypto_buying_power": "10000.00",
    }
    defaults.update(kwargs)
    defaults["crypto_status"] = crypto_status
    return SimpleNamespace(**defaults)


def _fake_crypto_asset(symbol, *, tradable=True):
    return SimpleNamespace(
        symbol=symbol,
        name=f"Test {symbol}",
        tradable=tradable,
        min_order_size="0.0001",
        min_trade_increment="0.0001",
        price_increment="0.01",
        fractionable=True,
        marginable=False,
        shortable=False,
    )


def _fake_order(order_id="order-abc-123"):
    return SimpleNamespace(
        id=order_id,
        status="filled",
        filled_avg_price="60000.50",
        filled_qty="0.0001",
        notional="6.00",
    )


# ── step_crypto_status ───────────────────────────────────────────────────────


class TestCryptoStatus:
    def test_active(self):
        client = MagicMock()
        client.get_account.return_value = _fake_account(crypto_status="ACTIVE")
        result = step_crypto_status(client)
        assert result.status == "PASS"
        assert "ACTIVE" in result.detail

    def test_inactive(self):
        client = MagicMock()
        client.get_account.return_value = _fake_account(crypto_status="INACTIVE")
        result = step_crypto_status(client)
        assert result.status == "FAIL"

    def test_no_attribute(self):
        client = MagicMock()
        acct = SimpleNamespace(id="x")
        client.get_account.return_value = acct
        result = step_crypto_status(client)
        assert result.status == "FAIL"
        assert "no crypto_status" in result.detail

    def test_api_error(self):
        client = MagicMock()
        client.get_account.side_effect = RuntimeError("API down")
        result = step_crypto_status(client)
        assert result.status == "ERROR"


# ── step_pair_snapshot ───────────────────────────────────────────────────────


class TestPairSnapshot:
    def test_success(self):
        client = MagicMock()
        client.get_all_assets.return_value = [
            _fake_crypto_asset("BTCUSD"),
            _fake_crypto_asset("ETHUSD"),
            _fake_crypto_asset("DOGEUSD", tradable=False),
        ]
        result = step_pair_snapshot(client)
        assert result.status == "PASS"
        assert result.data["pair_count"] == 2
        assert "BTCUSD" in result.data["pairs"]

    def test_no_tradable(self):
        client = MagicMock()
        client.get_all_assets.return_value = [
            _fake_crypto_asset("BTCUSD", tradable=False),
        ]
        result = step_pair_snapshot(client)
        assert result.status == "FAIL"


# ── step_order_acceptance ────────────────────────────────────────────────────


class TestOrderAcceptance:
    def test_dry_run_skips(self):
        client = MagicMock()
        result = step_order_acceptance(client, dry_run=True)
        assert result.status == "SKIP"

    def test_all_accepted(self):
        client = MagicMock()
        client.submit_order.return_value = _fake_order()
        result = step_order_acceptance(client, dry_run=False)
        assert result.status == "PASS"
        assert "3/3" in result.detail

    def test_partial_failure(self):
        client = MagicMock()
        call_count = 0

        def _side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("rejected")
            return _fake_order(f"order-{call_count}")

        client.submit_order.side_effect = _side_effect
        result = step_order_acceptance(client, dry_run=False)
        assert result.status == "FAIL"
        assert "2/3" in result.detail


# ── step_stop_limit_acceptance ───────────────────────────────────────────────


class TestStopLimitAcceptance:
    def test_dry_run_skips(self):
        client = MagicMock()
        result = step_stop_limit_acceptance(client, dry_run=True)
        assert result.status == "SKIP"

    def test_all_accepted(self):
        client = MagicMock()
        client.submit_order.return_value = _fake_order()
        result = step_stop_limit_acceptance(client, dry_run=False)
        assert result.status == "PASS"


# ── step_buying_power ────────────────────────────────────────────────────────


class TestBuyingPower:
    def test_success(self):
        client = MagicMock()
        client.get_account.return_value = _fake_account()
        result = step_buying_power(client)
        assert result.status == "PASS"
        assert "crypto_bp" in result.detail
