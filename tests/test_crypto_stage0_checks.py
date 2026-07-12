"""Tests for crypto Stage-0 paper battery step checks (D-C12).

Covers all 5 codex review items:
  1. run_battery hard-rejects paper=False
  2. step_fee_from_fill: round-trip with compensating sell, fill polling,
     residual-position audit, Tier-1 cleanup failure
  3. step_stop_limit_acceptance: terminal-state confirmation after cancel
  4. step_order_acceptance: terminal-state verification after cancel
  5. run_battery(transactional=False) runs only passive checks
"""
from __future__ import annotations

import unittest.mock as um
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from renquant_execution.crypto_stage0_checks import (
    StepResult,
    _poll_order_terminal,
    run_battery,
    step_buying_power,
    step_crypto_status,
    step_fee_from_fill,
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


def _fake_order(order_id="order-abc-123", status="filled"):
    return SimpleNamespace(
        id=order_id,
        status=status,
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


# ── _poll_order_terminal ────────────────────────────────────────────────────


class TestPollOrderTerminal:
    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_immediate_terminal(self, mock_sleep):
        client = MagicMock()
        client.get_order_by_id.return_value = _fake_order(status="canceled")
        reached, order = _poll_order_terminal(client, "order-1")
        assert reached is True
        assert str(order.status) == "canceled"
        mock_sleep.assert_not_called()

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_reaches_terminal_after_retries(self, mock_sleep):
        client = MagicMock()
        client.get_order_by_id.side_effect = [
            _fake_order(status="pending"),
            _fake_order(status="accepted"),
            _fake_order(status="canceled"),
        ]
        reached, order = _poll_order_terminal(client, "order-1")
        assert reached is True
        assert str(order.status) == "canceled"
        assert mock_sleep.call_count == 2

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_timeout(self, mock_sleep):
        client = MagicMock()
        client.get_order_by_id.return_value = _fake_order(status="pending")
        reached, order = _poll_order_terminal(
            client, "order-1", max_attempts=3
        )
        assert reached is False
        assert str(order.status) == "pending"
        assert mock_sleep.call_count == 3


# ── step_order_acceptance (review item 4: terminal-state verification) ──────


class TestOrderAcceptance:
    def test_dry_run_skips(self):
        client = MagicMock()
        result = step_order_acceptance(client, dry_run=True)
        assert result.status == "SKIP"

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_all_accepted_and_cancelled(self, mock_sleep):
        client = MagicMock()
        client.submit_order.return_value = _fake_order()
        client.get_order_by_id.return_value = _fake_order(status="canceled")
        result = step_order_acceptance(client, dry_run=False)
        assert result.status == "PASS"
        assert "3/3" in result.detail
        # Verify cancel_confirmed is True for all pairs
        for pair_result in result.data["results"].values():
            assert pair_result["cancel_confirmed"] is True

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_accepted_but_cancel_not_confirmed(self, mock_sleep):
        """Order accepted but cancel never reached terminal -> FAIL."""
        client = MagicMock()
        client.submit_order.return_value = _fake_order()
        # Always returns non-terminal status
        client.get_order_by_id.return_value = _fake_order(status="pending")
        result = step_order_acceptance(client, dry_run=False)
        assert result.status == "FAIL"
        assert "not confirmed terminal" in result.detail

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
        client.get_order_by_id.return_value = _fake_order(status="canceled")
        result = step_order_acceptance(client, dry_run=False)
        assert result.status == "FAIL"
        assert "2/3" in result.detail


# ── step_stop_limit_acceptance (review item 3: terminal-state confirm) ──────


class TestStopLimitAcceptance:
    def test_dry_run_skips(self):
        client = MagicMock()
        result = step_stop_limit_acceptance(client, dry_run=True)
        assert result.status == "SKIP"

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_all_accepted_and_cancelled(self, mock_sleep):
        client = MagicMock()
        client.submit_order.return_value = _fake_order()
        client.get_order_by_id.return_value = _fake_order(status="canceled")
        result = step_stop_limit_acceptance(client, dry_run=False)
        assert result.status == "PASS"
        for pair_result in result.data["results"].values():
            assert pair_result["cancel_confirmed"] is True

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_accepted_but_cancel_not_confirmed(self, mock_sleep):
        """Order accepted but cancel never reached terminal -> FAIL."""
        client = MagicMock()
        client.submit_order.return_value = _fake_order()
        client.get_order_by_id.return_value = _fake_order(status="pending")
        result = step_stop_limit_acceptance(client, dry_run=False)
        assert result.status == "FAIL"
        assert "not confirmed terminal" in result.detail


# ── step_fee_from_fill (review item 2: round-trip) ──────────────────────────


class TestFeeFromFill:
    def test_dry_run_skips(self):
        client = MagicMock()
        result = step_fee_from_fill(client, dry_run=True)
        assert result.status == "SKIP"

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_full_round_trip_pass(self, mock_sleep):
        """BUY fills, compensating SELL fills, no residual -> PASS."""
        client = MagicMock()
        buy_order = _fake_order("buy-1", status="new")
        buy_filled = _fake_order("buy-1", status="filled")
        sell_order = _fake_order("sell-1", status="new")
        sell_filled = _fake_order("sell-1", status="filled")

        client.submit_order.side_effect = [buy_order, sell_order]
        client.get_order_by_id.side_effect = [buy_filled, sell_filled]
        client.get_all_positions.return_value = []  # no residual

        result = step_fee_from_fill(client, dry_run=False)
        assert result.status == "PASS"
        assert "Round-trip complete" in result.detail
        assert result.data["buy_order_id"] == "buy-1"
        assert result.data["sell_order_id"] == "sell-1"
        assert "cleanup_failure" not in result.data

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_buy_does_not_fill(self, mock_sleep):
        """BUY never fills -> FAIL."""
        client = MagicMock()
        buy_order = _fake_order("buy-1", status="new")
        client.submit_order.return_value = buy_order
        # All polls return non-filled, non-terminal
        client.get_order_by_id.return_value = _fake_order(
            "buy-1", status="pending"
        )

        result = step_fee_from_fill(client, dry_run=False)
        assert result.status == "FAIL"
        assert "BUY did not fill" in result.detail

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_sell_does_not_fill_cleanup_failure(self, mock_sleep):
        """BUY fills but compensating SELL does not -> FAIL + cleanup_failure."""
        client = MagicMock()
        buy_order = _fake_order("buy-1", status="new")
        sell_order = _fake_order("sell-1", status="new")

        client.submit_order.side_effect = [buy_order, sell_order]
        # First poll (BUY) returns filled, second poll (SELL) returns pending
        client.get_order_by_id.side_effect = [
            _fake_order("buy-1", status="filled"),
        ] + [_fake_order("sell-1", status="pending")] * 10

        result = step_fee_from_fill(client, dry_run=False)
        assert result.status == "FAIL"
        assert "Compensating SELL did not fill" in result.detail
        assert result.data["cleanup_failure"] is True

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_residual_position_detected(self, mock_sleep):
        """Round-trip orders fill but residual position remains -> FAIL."""
        client = MagicMock()
        buy_order = _fake_order("buy-1", status="new")
        sell_order = _fake_order("sell-1", status="new")

        client.submit_order.side_effect = [buy_order, sell_order]
        client.get_order_by_id.side_effect = [
            _fake_order("buy-1", status="filled"),
            _fake_order("sell-1", status="filled"),
        ]
        # Residual position exists
        client.get_all_positions.return_value = [
            SimpleNamespace(symbol="BTCUSD", qty="0.0001")
        ]

        result = step_fee_from_fill(client, dry_run=False)
        assert result.status == "FAIL"
        assert "residual position remains" in result.detail
        assert result.data["cleanup_failure"] is True
        assert result.data["residual_qty"] == "0.0001"

    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_no_filled_qty_cleanup_failure(self, mock_sleep):
        """BUY fills but filled_qty is missing -> FAIL + cleanup_failure."""
        client = MagicMock()
        buy_order = SimpleNamespace(id="buy-1", status="new")
        buy_filled = SimpleNamespace(
            id="buy-1",
            status="filled",
            filled_avg_price="60000",
            notional="1.10",
            # filled_qty intentionally omitted
        )

        client.submit_order.return_value = buy_order
        client.get_order_by_id.return_value = buy_filled

        result = step_fee_from_fill(client, dry_run=False)
        assert result.status == "FAIL"
        assert "filled_qty unavailable" in result.detail
        assert result.data["cleanup_failure"] is True


# ── step_buying_power ────────────────────────────────────────────────────────


class TestBuyingPower:
    def test_success(self):
        client = MagicMock()
        client.get_account.return_value = _fake_account()
        result = step_buying_power(client)
        assert result.status == "PASS"
        assert "crypto_bp" in result.detail


# ── run_battery (review items 1 + 5) ────────────────────────────────────────


class TestRunBattery:
    def test_rejects_paper_false(self):
        """Review item 1: run_battery hard-rejects paper=False."""
        with pytest.raises(ValueError, match="only supports paper=True"):
            run_battery(paper=False)

    @patch(
        "renquant_execution.crypto_stage0_checks.get_trading_client"
    )
    def test_passive_only_by_default(self, mock_get_client):
        """Review item 5: transactional=False skips order probes."""
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_account.return_value = _fake_account()
        client.get_all_assets.return_value = [
            _fake_crypto_asset("BTCUSD"),
        ]

        results = run_battery(paper=True, dry_run=True, transactional=False)

        # Should have 7 results total
        assert len(results) == 7
        names = [r.name for r in results]
        assert names == [
            "crypto_status",
            "pair_snapshot",
            "buying_power",
            "data_parity",
            "order_acceptance",
            "stop_limit_acceptance",
            "fee_from_fill",
        ]

        # Passive checks ran (not SKIP due to transactional)
        assert results[0].status == "PASS"  # crypto_status
        assert results[1].status == "PASS"  # pair_snapshot
        assert results[2].status == "PASS"  # buying_power

        # Transactional checks skipped with the right reason
        for r in results[4:]:
            assert r.status == "SKIP"
            assert "transactional=False" in r.detail

        # No orders were placed
        client.submit_order.assert_not_called()

    @patch(
        "renquant_execution.crypto_stage0_checks.get_trading_client"
    )
    @patch("renquant_execution.crypto_stage0_checks.time.sleep")
    def test_transactional_runs_probes(self, mock_sleep, mock_get_client):
        """transactional=True runs the order probes."""
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_account.return_value = _fake_account()
        client.get_all_assets.return_value = [
            _fake_crypto_asset("BTCUSD"),
        ]
        # For order acceptance + stop-limit: submit then cancel confirmed
        client.submit_order.return_value = _fake_order()
        client.get_order_by_id.return_value = _fake_order(status="canceled")
        client.get_all_positions.return_value = []

        results = run_battery(
            paper=True, dry_run=False, transactional=True
        )

        assert len(results) == 7
        # Transactional checks should have run (not "transactional=False" skip)
        for r in results[4:]:
            assert "transactional=False" not in r.detail

    @patch(
        "renquant_execution.crypto_stage0_checks.get_trading_client"
    )
    def test_always_creates_paper_client(self, mock_get_client):
        """run_battery always passes paper=True to get_trading_client."""
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_account.return_value = _fake_account()
        client.get_all_assets.return_value = []

        run_battery(paper=True, dry_run=True)
        mock_get_client.assert_called_once_with(paper=True)
