"""Tests for crypto Stage-0 paper battery (D-C12).

Rehomed from renquant-orchestrator PR #498 (rejected by codex -- broker-facing
checks belong in renquant-execution, not orchestrator).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from crypto_stage0_battery import (
    BatteryReport,
    StepResult,
    run_battery,
    step_buying_power,
    step_crypto_status,
    step_order_acceptance,
    step_pair_snapshot,
    step_stop_limit_acceptance,
)


# -- Fixtures ----------------------------------------------------------------


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


# -- step_crypto_status ------------------------------------------------------


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


# -- step_pair_snapshot ------------------------------------------------------


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


# -- step_order_acceptance ---------------------------------------------------


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


# -- step_stop_limit_acceptance ----------------------------------------------


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


# -- step_buying_power -------------------------------------------------------


class TestBuyingPower:
    def test_success(self):
        client = MagicMock()
        client.get_account.return_value = _fake_account()
        result = step_buying_power(client)
        assert result.status == "PASS"
        assert "crypto_bp" in result.detail


# -- run_battery -------------------------------------------------------------


class TestRunBattery:
    def test_live_blocked(self):
        report = run_battery(paper=False, dry_run=True)
        assert report.failed > 0
        assert report.steps[0].name == "safety"

    @patch("crypto_stage0_battery._get_trading_client")
    def test_dry_run_completes(self, mock_client_fn):
        client = MagicMock()
        client.get_account.return_value = _fake_account()
        client.get_all_assets.return_value = [
            _fake_crypto_asset("BTCUSD"),
            _fake_crypto_asset("ETHUSD"),
        ]
        mock_client_fn.return_value = client

        report = run_battery(paper=True, dry_run=True)
        assert report.environment == "paper"
        assert report.dry_run is True
        assert len(report.steps) == 7
        assert report.steps[0].status == "PASS"  # crypto_status
        assert report.steps[1].status == "PASS"  # pair_snapshot
        assert report.steps[2].status == "SKIP"  # order_acceptance (dry)
        assert report.steps[3].status == "SKIP"  # stop_limit (dry)
        assert report.steps[4].status == "SKIP"  # fee_from_fill (dry)
        assert report.steps[5].status == "PASS"  # buying_power

    @patch("crypto_stage0_battery._get_trading_client")
    def test_summary_json_serializable(self, mock_client_fn):
        client = MagicMock()
        client.get_account.return_value = _fake_account()
        client.get_all_assets.return_value = [_fake_crypto_asset("BTCUSD")]
        mock_client_fn.return_value = client

        report = run_battery(paper=True, dry_run=True)
        out = json.dumps(report.summary(), default=str)
        parsed = json.loads(out)
        assert parsed["environment"] == "paper"
        assert isinstance(parsed["steps"], list)


# -- BatteryReport -----------------------------------------------------------


class TestBatteryReport:
    def test_counts(self):
        report = BatteryReport(steps=[
            StepResult("a", "PASS"),
            StepResult("b", "FAIL"),
            StepResult("c", "SKIP"),
            StepResult("d", "PASS"),
        ])
        assert report.passed == 2
        assert report.failed == 1
        summary = report.summary()
        assert summary["total"] == 4
        assert summary["skipped"] == 1
