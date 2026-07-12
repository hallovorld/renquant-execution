"""Tests for the crypto Stage-0 battery checks.

All tests mock the AlpacaBroker methods -- the battery module never imports
alpaca-py directly, so these tests work without broker SDK credentials or
the alpaca-py package installed in the test environment.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from renquant_execution.alpaca_broker import AlpacaBroker
from renquant_execution.crypto import CryptoAssetSpec
from renquant_execution.crypto_stage0_checks import (
    DEFAULT_CANARY_PAIRS,
    BatteryReport,
    StepResult,
    StepStatus,
    check_buying_power_behavior,
    check_crypto_account_status,
    check_data_parity,
    check_gtc_order_acceptance,
    check_pair_snapshot,
    check_stop_limit_acceptance,
    run_full_battery,
)

BTC = "BTC/USD"
ETH = "ETH/USD"
SOL = "SOL/USD"
CANARY = (BTC, ETH, SOL)

BTC_SPEC = CryptoAssetSpec(
    symbol=BTC, min_order_size=0.0001,
    min_trade_increment=0.0001, price_increment=0.01,
)
ETH_SPEC = CryptoAssetSpec(
    symbol=ETH, min_order_size=0.001,
    min_trade_increment=0.001, price_increment=0.01,
)
SOL_SPEC = CryptoAssetSpec(
    symbol=SOL, min_order_size=0.01,
    min_trade_increment=0.01, price_increment=0.01,
)
SPECS = {BTC: BTC_SPEC, ETH: ETH_SPEC, SOL: SOL_SPEC}


# ── fake broker (no alpaca-py dependency) ───────────────────────────────────


class _FakeAccount:
    account_number = "PA-FAKE-001"
    status = "ACTIVE"
    crypto_status = "ACTIVE"
    buying_power = 100_000.0
    non_marginable_buying_power = 100_000.0
    cash = 100_000.0
    portfolio_value = 100_000.0


class _FakeTradingClient:
    """Fake TradingClient for battery tests -- no alpaca-py required."""

    def __init__(
        self,
        account: Any | None = None,
        assets: dict[str, Any] | None = None,
    ) -> None:
        self._account = account or _FakeAccount()
        self._assets = assets or {}
        self.submitted: list[Any] = []
        self.cancelled: list[str] = []

    def get_account(self):
        return self._account

    def get_asset(self, symbol: str):
        if symbol not in self._assets:
            raise RuntimeError(f"unknown asset {symbol}")
        return self._assets[symbol]

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        qty = float(getattr(order_data, "qty", 0.0) or 0.0)
        side = str(
            getattr(getattr(order_data, "side", ""), "value", "") or ""
        ).upper()
        return SimpleNamespace(
            id=f"ord-{len(self.submitted)}",
            status="accepted",
            symbol=getattr(order_data, "symbol", ""),
            side=side or "BUY",
            qty=qty,
            filled_qty=0.0,
            filled_avg_price=0.0,
        )

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)


def _crypto_asset(**overrides) -> SimpleNamespace:
    fields = {
        "fractionable": False,
        "min_order_size": 0.0001,
        "min_trade_increment": 0.0001,
        "price_increment": 0.01,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _broker(
    client: _FakeTradingClient | None = None,
    paper: bool = True,
    **kwargs,
) -> AlpacaBroker:
    """Build a battery-testable broker with injected fake client."""
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    if client is None:
        client = _FakeTradingClient(assets=assets)
    broker = AlpacaBroker(paper=paper, label="alpaca-battery-test", **kwargs)
    broker._trading_client = client  # noqa: SLF001
    broker._account = client.get_account()  # noqa: SLF001
    return broker


# ── check_crypto_account_status ─────────────────────────────────────────────


def test_account_status_pass_when_active() -> None:
    result = check_crypto_account_status(_broker())
    assert result.status == StepStatus.PASS
    assert "ACTIVE" in result.detail
    assert result.data["account_id"] == "PA-FAKE-001"


def test_account_status_fail_when_inactive_account() -> None:
    acct = _FakeAccount()
    acct.status = "DISABLED"
    client = _FakeTradingClient(account=acct)
    result = check_crypto_account_status(_broker(client))
    assert result.status == StepStatus.FAIL
    assert "DISABLED" in result.detail


def test_account_status_fail_when_crypto_inactive() -> None:
    acct = _FakeAccount()
    acct.crypto_status = "INACTIVE"
    client = _FakeTradingClient(account=acct)
    result = check_crypto_account_status(_broker(client))
    assert result.status == StepStatus.FAIL
    assert "INACTIVE" in result.detail


def test_account_status_pass_when_crypto_status_empty() -> None:
    """An absent crypto_status is PASS (the order acceptance is the real test)."""
    acct = _FakeAccount()
    acct.crypto_status = ""
    client = _FakeTradingClient(account=acct)
    result = check_crypto_account_status(_broker(client))
    assert result.status == StepStatus.PASS


def test_account_status_error_on_exception() -> None:
    broker = AlpacaBroker(paper=True)
    # Not connected -- get_account_info will raise RuntimeError.
    result = check_crypto_account_status(broker)
    assert result.status == StepStatus.ERROR


# ── check_pair_snapshot ─────────────────────────────────────────────────────


def test_pair_snapshot_pass_all_pairs_resolved() -> None:
    broker = _broker(crypto_asset_specs=SPECS)
    result = check_pair_snapshot(broker, CANARY)
    assert result.status == StepStatus.PASS
    assert "3 pairs resolved" in result.detail
    assert result.data["pairs"]["BTC/USD"]["tradable"] is True


def test_pair_snapshot_fail_on_missing_pair() -> None:
    # Only BTC available, ETH/SOL will fail.
    broker = _broker(crypto_asset_specs={BTC: BTC_SPEC})
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    broker._trading_client = client  # noqa: SLF001
    result = check_pair_snapshot(broker, CANARY)
    assert result.status == StepStatus.FAIL
    assert "2/3" in result.detail


def test_pair_snapshot_rejects_non_pair_symbol() -> None:
    broker = _broker()
    result = check_pair_snapshot(broker, ("AAPL",))
    assert result.status == StepStatus.FAIL
    assert "not a valid pair-form" in result.detail


# ── check_gtc_order_acceptance ──────────────────────────────────────────────


def test_gtc_acceptance_pass_all_pairs() -> None:
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker = _broker(client, crypto_asset_specs=SPECS)
    result = check_gtc_order_acceptance(broker, CANARY)
    assert result.status == StepStatus.PASS
    assert len(client.submitted) == 3
    assert len(client.cancelled) == 3
    assert "3 GTC limit-buy orders placed+cancelled" in result.detail


def test_gtc_acceptance_fail_on_order_reject() -> None:
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    # Sabotage submit_order to raise.
    original_submit = client.submit_order

    def _fail_on_eth(order_data):
        sym = getattr(order_data, "symbol", "")
        if sym == ETH:
            raise RuntimeError("ETH order rejected by broker")
        return original_submit(order_data)

    client.submit_order = _fail_on_eth
    broker = _broker(client, crypto_asset_specs=SPECS)
    result = check_gtc_order_acceptance(broker, CANARY)
    assert result.status == StepStatus.FAIL
    assert "1/3" in result.detail


def test_gtc_acceptance_cancels_even_on_partial_failure() -> None:
    """Orders that succeed are still cancelled even if a later pair fails."""
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    call_count = 0
    original_submit = client.submit_order

    def _fail_on_third(order_data):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise RuntimeError("third order fails")
        return original_submit(order_data)

    client.submit_order = _fail_on_third
    broker = _broker(client, crypto_asset_specs=SPECS)
    result = check_gtc_order_acceptance(broker, CANARY)
    assert result.status == StepStatus.FAIL
    # The first 2 orders were placed and cancelled.
    assert len(client.cancelled) == 2


# ── check_stop_limit_acceptance ─────────────────────────────────────────────


def test_stop_limit_acceptance_pass() -> None:
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker = _broker(client, crypto_asset_specs=SPECS)
    result = check_stop_limit_acceptance(broker, CANARY)
    assert result.status == StepStatus.PASS
    assert len(client.submitted) == 3
    assert len(client.cancelled) == 3
    assert "stop-limit BUY" in result.detail


def test_stop_limit_acceptance_fail_on_spec_lookup() -> None:
    """If a pair's spec can't be resolved, the step fails."""
    client = _FakeTradingClient(assets={})
    broker = _broker(client)
    result = check_stop_limit_acceptance(broker, ("BTC/USD",))
    assert result.status == StepStatus.FAIL
    assert "spec lookup failed" in result.detail


# ── check_buying_power_behavior ─────────────────────────────────────────────


def test_buying_power_pass() -> None:
    result = check_buying_power_behavior(_broker())
    assert result.status == StepStatus.PASS
    assert "non_marginable_buying_power=100000.0" in result.detail


def test_buying_power_fail_when_nmbp_zero() -> None:
    acct = _FakeAccount()
    acct.non_marginable_buying_power = 0.0
    acct.cash = 50_000.0
    client = _FakeTradingClient(account=acct)
    result = check_buying_power_behavior(_broker(client))
    assert result.status == StepStatus.FAIL
    assert "misconfigured" in result.detail


# ── check_data_parity ──────────────────────────────────────────────────────


def test_data_parity_skips_with_reason() -> None:
    result = check_data_parity(CANARY)
    assert result.status == StepStatus.SKIP
    assert "placeholder" in result.detail
    assert result.data["reason"] == "no_data_source"


# ── run_full_battery ────────────────────────────────────────────────────────


def test_full_battery_refuses_live_broker() -> None:
    broker = _broker(paper=False)
    with pytest.raises(RuntimeError, match="REFUSES.*non-paper"):
        run_full_battery(broker)


def test_full_battery_dry_run_skips_order_steps() -> None:
    broker = _broker(crypto_asset_specs=SPECS)
    report = run_full_battery(broker, dry_run=True)
    assert isinstance(report, BatteryReport)
    assert report.dry_run is True
    assert report.account_id == "PA-FAKE-001"
    assert report.environment == "paper"

    step_names = [s.name for s in report.steps]
    assert "crypto_account_status" in step_names
    assert "pair_snapshot" in step_names
    assert "gtc_order_acceptance" in step_names
    assert "stop_limit_acceptance" in step_names
    assert "buying_power_behavior" in step_names
    assert "data_parity" in step_names

    # Order steps are SKIP in dry_run.
    gtc = next(s for s in report.steps if s.name == "gtc_order_acceptance")
    assert gtc.status == StepStatus.SKIP
    sl = next(s for s in report.steps if s.name == "stop_limit_acceptance")
    assert sl.status == StepStatus.SKIP


def test_full_battery_live_run_places_orders() -> None:
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker = _broker(client, crypto_asset_specs=SPECS)
    report = run_full_battery(broker, dry_run=False)
    assert isinstance(report, BatteryReport)
    assert report.dry_run is False

    # 6 orders total: 3 GTC limits + 3 stop-limits.
    assert len(client.submitted) == 6
    assert len(client.cancelled) == 6

    # Account + pair + buying-power steps pass; data parity is SKIP.
    status_map = {s.name: s.status for s in report.steps}
    assert status_map["crypto_account_status"] == StepStatus.PASS
    assert status_map["pair_snapshot"] == StepStatus.PASS
    assert status_map["gtc_order_acceptance"] == StepStatus.PASS
    assert status_map["stop_limit_acceptance"] == StepStatus.PASS
    assert status_map["buying_power_behavior"] == StepStatus.PASS
    assert status_map["data_parity"] == StepStatus.SKIP


def test_full_battery_summary_format() -> None:
    broker = _broker(crypto_asset_specs=SPECS)
    report = run_full_battery(broker, dry_run=True)
    summary = report.summary
    assert "6 steps" in summary
    # dry_run: 2 SKIP (gtc + stop-limit) + 1 SKIP (data parity) = 3 SKIP
    assert "SKIP=3" in summary


def test_battery_report_all_passed_property() -> None:
    broker = _broker(crypto_asset_specs=SPECS)
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker._trading_client = client  # noqa: SLF001
    report = run_full_battery(broker, dry_run=False)
    # data_parity is SKIP, so all_passed is False.
    assert report.all_passed is False

    # If we filter to only non-SKIP steps, they should all be PASS.
    non_skip = [s for s in report.steps if s.status != StepStatus.SKIP]
    assert all(s.status == StepStatus.PASS for s in non_skip)


# ── broker thin-wrapper unit tests ──────────────────────────────────────────


def test_get_account_info_returns_expected_fields() -> None:
    broker = _broker()
    info = broker.get_account_info()
    assert info["account_id"] == "PA-FAKE-001"
    assert info["status"] == "ACTIVE"
    assert info["crypto_status"] == "ACTIVE"
    assert info["paper"] is True
    assert info["buying_power"] == 100_000.0
    assert info["non_marginable_buying_power"] == 100_000.0


def test_get_crypto_asset_spec_public_wrapper() -> None:
    broker = _broker(crypto_asset_specs={BTC: BTC_SPEC})
    spec = broker.get_crypto_asset_spec(BTC)
    assert spec.symbol == BTC
    assert spec.min_order_size == 0.0001


def test_get_crypto_asset_spec_raises_on_failure() -> None:
    client = _FakeTradingClient(assets={})
    broker = _broker(client)
    with pytest.raises(RuntimeError, match="spec lookup.*failed"):
        broker.get_crypto_asset_spec(BTC)


def test_place_crypto_limit_order_basic() -> None:
    assets = {BTC: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = broker.place_crypto_limit_order(
        symbol=BTC, action="BUY", qty=0.5, limit_price=60000.0,
    )
    assert result["skipped"] is False
    assert result["asset_class"] == "crypto"
    assert result["time_in_force"] == "gtc"
    assert len(client.submitted) == 1


def test_place_crypto_limit_order_rejects_equity() -> None:
    broker = _broker()
    with pytest.raises(ValueError, match="crypto-only"):
        broker.place_crypto_limit_order("AAPL", "BUY", 1.0, 100.0)


def test_place_crypto_stop_limit_order_buy_side() -> None:
    assets = {BTC: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = broker.place_crypto_stop_limit_order(
        symbol=BTC, action="BUY", qty=0.5,
        stop_price=70000.0, limit_price=70500.0,
    )
    assert result["skipped"] is False
    assert result["action"] == "BUY"
    assert result["stop_price"] == 70000.0
    assert result["limit_price"] == 70500.0


def test_place_crypto_stop_limit_order_buy_rejects_limit_below_stop() -> None:
    broker = _broker(crypto_asset_specs={BTC: BTC_SPEC})
    with pytest.raises(ValueError, match="limit >= stop"):
        broker.place_crypto_stop_limit_order(
            BTC, "BUY", 0.5, stop_price=70000.0, limit_price=69000.0,
        )


def test_place_crypto_stop_limit_order_sell_rejects_limit_above_stop() -> None:
    broker = _broker(crypto_asset_specs={BTC: BTC_SPEC})
    with pytest.raises(ValueError, match="limit <= stop"):
        broker.place_crypto_stop_limit_order(
            BTC, "SELL", 0.5, stop_price=60000.0, limit_price=61000.0,
        )


def test_place_crypto_stop_limit_order_rejects_equity() -> None:
    broker = _broker()
    with pytest.raises(ValueError, match="crypto-only"):
        broker.place_crypto_stop_limit_order("AAPL", "BUY", 1.0, 100.0, 101.0)
