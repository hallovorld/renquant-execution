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
    REPORT_SCHEMA_VERSION,
    BatteryReport,
    StepResult,
    StepStatus,
    _check_residual_exposure,
    _validate_order_acceptance,
    check_buying_power_behavior,
    check_crypto_account_status,
    check_data_parity,
    check_gtc_order_acceptance,
    check_pair_snapshot,
    check_stop_limit_acceptance,
    run_full_battery,
)

#: Cancellation-confirmation timeout/poll interval for tests that exercise
#: the "cancellation never confirms" path -- small enough that the real
#: ``time.sleep`` calls inside ``AlpacaBroker._wait_for_order_terminal_cancel``
#: cost a few tens of milliseconds, not the 5s production default.
_FAST_CANCEL_TIMEOUT_SECONDS = 0.05
_FAST_CANCEL_POLL_INTERVAL_SECONDS = 0.01

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
    """Fake TradingClient for battery tests -- no alpaca-py required.

    ``order_status_sequence`` mirrors the convention already established in
    ``tests/test_crypto_order_semantics.py`` for the PR #31 confirmed-cancel
    tests: a mapping of order_id -> list of statuses that
    ``get_order_by_id`` reports on each successive poll (a single-element
    list repeats forever -- e.g. to simulate a cancellation that never
    confirms within the timeout). Omitting an id entirely gets the default
    "cancel_order_by_id marks it canceled immediately" behavior, so existing
    happy-path tests don't need to know about polling at all.
    """

    def __init__(
        self,
        account: Any | None = None,
        assets: dict[str, Any] | None = None,
        order_status_sequence: dict[str, list[str]] | None = None,
        positions: dict[str, SimpleNamespace] | None = None,
        base_url: str = "https://paper-api.alpaca.markets",
    ) -> None:
        self._account = account or _FakeAccount()
        self._assets = assets or {}
        self._orders_by_id: dict[str, SimpleNamespace] = {}
        self._order_status_sequence = {
            k: list(v) for k, v in (order_status_sequence or {}).items()
        }
        self._positions: dict[str, SimpleNamespace] = dict(positions or {})
        self._base_url = base_url
        self.submitted: list[Any] = []
        self.cancelled: list[str] = []
        self.get_order_by_id_calls: list[str] = []

    def get_account(self):
        return self._account

    def get_asset(self, symbol: str):
        if symbol not in self._assets:
            raise RuntimeError(f"unknown asset {symbol}")
        return self._assets[symbol]

    def get_open_position(self, symbol: str):
        if symbol in self._positions:
            return self._positions[symbol]
        raise RuntimeError(f"position does not exist for {symbol}")

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        qty = float(getattr(order_data, "qty", 0.0) or 0.0)
        side = str(
            getattr(getattr(order_data, "side", ""), "value", "") or ""
        ).upper()
        order_id = f"ord-{len(self.submitted)}"
        order = SimpleNamespace(
            id=order_id,
            status="accepted",
            symbol=getattr(order_data, "symbol", ""),
            side=side or "BUY",
            qty=qty,
            filled_qty=0.0,
            filled_avg_price=0.0,
        )
        self._orders_by_id[order_id] = order
        return order

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        # Default (no explicit order_status_sequence for this id): the
        # cancellation reaches a confirmed terminal CANCELED state right
        # away -- the common happy-path test shape.
        if order_id not in self._order_status_sequence:
            order = self._orders_by_id.get(order_id)
            if order is not None:
                order.status = "canceled"

    def get_order_by_id(self, order_id: str):
        self.get_order_by_id_calls.append(order_id)
        sequence = self._order_status_sequence.get(order_id)
        if sequence:
            status = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return SimpleNamespace(id=order_id, status=status)
        order = self._orders_by_id.get(order_id)
        if order is not None:
            return order
        return SimpleNamespace(id=order_id, status="canceled")


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


def test_gtc_acceptance_fails_when_cancellation_not_confirmed() -> None:
    """A cancel_order() call that doesn't raise is NOT proof the order is
    gone -- if AlpacaBroker.wait_for_order_terminal_cancel() never observes
    a confirmed terminal ``canceled`` status within the timeout, the step
    must report FAIL (naming the affected pair/order), never a silent PASS.
    """
    assets = {BTC: _crypto_asset()}
    # ord-1 (the only order, for BTC) is reported "accepted" (a resting,
    # non-terminal, non-cancel status) on every poll -- the cancellation
    # request is accepted (no exception) but never actually confirmed.
    client = _FakeTradingClient(
        assets=assets, order_status_sequence={"ord-1": ["accepted"]},
    )
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = check_gtc_order_acceptance(
        broker,
        (BTC,),
        cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
        cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
    )
    assert result.status == StepStatus.FAIL
    assert BTC in result.detail
    assert "ord-1" in result.detail
    assert "NOT confirmed" in result.detail
    # cancel_order() was called (and did not raise) -- the order was placed
    # and a cancel was requested, but never confirmed as terminally canceled.
    assert client.cancelled == ["ord-1"]
    assert result.data["orders"][BTC]["cancel_confirmed"] is False


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
    # Review item #3: labelled as diagnostic capability check (non-gating).
    assert "DIAGNOSTIC" in result.detail
    assert result.data["check_type"] == "diagnostic_capability"
    assert result.required is False


def test_stop_limit_acceptance_fail_on_spec_lookup() -> None:
    """If a pair's spec can't be resolved, the step fails."""
    client = _FakeTradingClient(assets={})
    broker = _broker(client)
    result = check_stop_limit_acceptance(broker, ("BTC/USD",))
    assert result.status == StepStatus.FAIL
    assert "spec lookup failed" in result.detail


def test_stop_limit_acceptance_fails_when_cancellation_not_confirmed() -> None:
    """Same confirmed-cancel discipline as the GTC limit-order step: a
    cancel request that didn't raise is not proof the resting BUY
    stop-limit is actually gone.
    """
    assets = {BTC: _crypto_asset()}
    client = _FakeTradingClient(
        assets=assets, order_status_sequence={"ord-1": ["accepted"]},
    )
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = check_stop_limit_acceptance(
        broker,
        (BTC,),
        cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
        cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
    )
    assert result.status == StepStatus.FAIL
    assert BTC in result.detail
    assert "ord-1" in result.detail
    assert "NOT confirmed" in result.detail
    assert client.cancelled == ["ord-1"]
    assert result.data["orders"][BTC]["cancel_confirmed"] is False


# ── check_buying_power_behavior ─────────────────────────────────────────────


def test_buying_power_pass() -> None:
    result = check_buying_power_behavior(_broker())
    assert result.status == StepStatus.PASS
    assert "OBSERVATIONAL" in result.detail
    assert "non_marginable_buying_power=100000.0" in result.detail
    assert result.data["check_type"] == "observational"


def test_buying_power_observational_reports_nmbp_zero() -> None:
    """Buying-power check is OBSERVATIONAL — it always reports PASS with the
    values for operator inspection, even when NMBP is zero.  It does not
    gate on the crypto non-marginable invariant (review item #6).
    """
    acct = _FakeAccount()
    acct.non_marginable_buying_power = 0.0
    acct.cash = 50_000.0
    client = _FakeTradingClient(account=acct)
    result = check_buying_power_behavior(_broker(client))
    assert result.status == StepStatus.PASS
    assert "OBSERVATIONAL" in result.detail
    assert result.data["check_type"] == "observational"
    assert result.data["non_marginable_buying_power"] == 0.0
    assert result.data["cash"] == 50_000.0


# ── check_data_parity ──────────────────────────────────────────────────────


def test_data_parity_skips_with_reason() -> None:
    result = check_data_parity(CANARY)
    assert result.status == StepStatus.SKIP
    assert "placeholder" in result.detail
    assert result.data["reason"] == "no_data_source"
    # Review item #5: data_parity is optional so it doesn't block all_passed.
    assert result.required is False


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
    """all_passed is True when all REQUIRED steps PASS.  data_parity is
    optional (required=False), so its structural SKIP does not block
    the overall battery verdict (review item #5).
    """
    broker = _broker(crypto_asset_specs=SPECS)
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker._trading_client = client  # noqa: SLF001
    report = run_full_battery(broker, dry_run=False)
    # data_parity is SKIP but required=False, so all_passed is True.
    assert report.all_passed is True

    # Verify data_parity is optional and SKIP.
    dp = next(s for s in report.steps if s.name == "data_parity")
    assert dp.status == StepStatus.SKIP
    assert dp.required is False

    # All required steps should be PASS.
    required = [s for s in report.steps if s.required]
    assert all(s.status == StepStatus.PASS for s in required)


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


# ── adversarial tests (review items #1-#6) ────────────────────────────────


class TestCancelSucceedsButNeverReachesTerminal:
    """Review item #1: cancel succeeds (no exception) but terminal state
    never reaches ``canceled`` within the timeout — the step must FAIL,
    not silently PASS.
    """

    def test_gtc_cancel_accepted_but_stays_resting(self) -> None:
        """Cancel request is accepted but the order stays 'accepted' forever."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(
            assets=assets,
            order_status_sequence={"ord-1": ["accepted"]},
        )
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(
            broker, (BTC,),
            cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
            cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
        )
        assert result.status == StepStatus.FAIL
        assert "NOT confirmed" in result.detail
        assert result.data["orders"][BTC]["cancel_confirmed"] is False

    def test_stop_limit_cancel_accepted_but_stays_resting(self) -> None:
        """Same for stop-limit: order stays resting after cancel request."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(
            assets=assets,
            order_status_sequence={"ord-1": ["accepted"]},
        )
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_stop_limit_acceptance(
            broker, (BTC,),
            cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
            cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
        )
        assert result.status == StepStatus.FAIL
        assert "NOT confirmed" in result.detail

    def test_cancel_raises_but_poll_finds_canceled(self) -> None:
        """Cancel raises but the order reaches canceled anyway — should PASS.

        Review item #1 fix: always poll even after cancel_order raises.
        """
        assets = {BTC: _crypto_asset()}
        # After cancel_order raises, polling finds the order canceled.
        client = _FakeTradingClient(
            assets=assets,
            order_status_sequence={"ord-1": ["canceled"]},
        )
        # Make cancel_order raise.
        original_cancel = client.cancel_order_by_id

        def _raise_on_cancel(order_id: str) -> None:
            original_cancel(order_id)
            raise RuntimeError("cancel request failed (network)")

        client.cancel_order_by_id = _raise_on_cancel
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(
            broker, (BTC,),
            cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
            cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
        )
        # Despite cancel raising, the poll found canceled — PASS.
        assert result.status == StepStatus.PASS
        assert result.data["orders"][BTC]["cancel_confirmed"] is True
        assert "cancel_exception" in result.data["orders"][BTC]


class TestImmediateFillOnProbeOrder:
    """Review item #2: if the probe order fills immediately (should never
    happen at $0.01 limit, but adversarial), the step must FAIL because
    a filled order is terminal-but-not-canceled.
    """

    def test_gtc_immediate_fill_is_fail(self) -> None:
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        # Override submit_order to return 'filled' status.
        original_submit = client.submit_order

        def _fill_immediately(order_data):
            order = original_submit(order_data)
            order.status = "filled"
            order.filled_qty = float(getattr(order_data, "qty", 0.0) or 0.0)
            return order

        client.submit_order = _fill_immediately
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(
            broker, (BTC,),
            cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
            cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
        )
        assert result.status == StepStatus.FAIL
        # Should fail on the validation of the returned status.
        assert "terminal/rejected" in result.detail or "NOT confirmed" in result.detail

    def test_stop_limit_immediate_fill_is_fail(self) -> None:
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        original_submit = client.submit_order

        def _fill_immediately(order_data):
            order = original_submit(order_data)
            order.status = "filled"
            return order

        client.submit_order = _fill_immediately
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_stop_limit_acceptance(
            broker, (BTC,),
            cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
            cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
        )
        assert result.status == StepStatus.FAIL
        assert "terminal/rejected" in result.detail or "NOT confirmed" in result.detail


class TestReturnedRejectionOrNoOrder:
    """Review item #2: if the broker returns a rejected status or missing
    order_id, the step must FAIL.
    """

    def test_gtc_rejected_status_is_fail(self) -> None:
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        original_submit = client.submit_order

        def _reject(order_data):
            order = original_submit(order_data)
            order.status = "rejected"
            return order

        client.submit_order = _reject
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(broker, (BTC,))
        assert result.status == StepStatus.FAIL
        assert "terminal/rejected" in result.detail

    def test_gtc_expired_status_is_fail(self) -> None:
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        original_submit = client.submit_order

        def _expire(order_data):
            order = original_submit(order_data)
            order.status = "expired"
            return order

        client.submit_order = _expire
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(broker, (BTC,))
        assert result.status == StepStatus.FAIL
        assert "terminal/rejected" in result.detail

    def test_gtc_no_order_id_is_fail(self) -> None:
        """submit_order returns an order with empty id."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        original_submit = client.submit_order

        def _no_id(order_data):
            order = original_submit(order_data)
            order.id = ""
            return order

        client.submit_order = _no_id
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(broker, (BTC,))
        assert result.status == StepStatus.FAIL
        assert "no order_id" in result.detail

    def test_gtc_wrong_side_is_fail(self) -> None:
        """Broker returns a SELL order when we requested BUY."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        original_submit = client.submit_order

        def _wrong_side(order_data):
            order = original_submit(order_data)
            order.side = "SELL"
            return order

        client.submit_order = _wrong_side
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(broker, (BTC,))
        assert result.status == StepStatus.FAIL
        assert "side=" in result.detail


class TestValidateOrderAcceptance:
    """Direct unit tests for _validate_order_acceptance (review item #2)."""

    def test_accepted_order_passes(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        assert _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        ) is None

    def test_new_status_passes(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "new",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        assert _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        ) is None

    def test_rejected_status_fails(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "rejected",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "terminal/rejected" in failure

    def test_filled_status_fails(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "filled",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "terminal/rejected" in failure

    def test_empty_order_id_fails(self) -> None:
        result = {"order_id": "", "status": "accepted"}
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "no order_id" in failure

    def test_wrong_side_fails(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "SELL",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "side=" in failure

    def test_wrong_tif_fails(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "day",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "time_in_force=" in failure


class TestUnverifiedEnvironment:
    """Review item #4: failed or unknown environment must be FAIL, never
    silently default to paper.
    """

    def test_failed_account_lookup_is_fail(self) -> None:
        """get_account_info() raises — battery should FAIL, not default paper."""
        broker = AlpacaBroker(paper=True, label="test-env-fail")
        # Not connected, so get_account_info will raise.
        # But _assert_paper_mode checks broker.paper attribute directly.
        # We need a broker whose paper=True but get_account_info fails.
        broker._trading_client = None  # noqa: SLF001
        # _assert_paper_mode will pass (paper=True), but get_account_info
        # will raise RuntimeError("AlpacaBroker is not connected").
        report = run_full_battery(broker, dry_run=True)
        assert report.environment == "unknown"
        assert report.all_passed is False
        assert any(
            s.name == "environment_verification" and s.status == StepStatus.FAIL
            for s in report.steps
        )

    def test_unknown_paper_flag_is_fail(self) -> None:
        """Paper flag is None/missing — battery should FAIL, not default."""
        acct = _FakeAccount()
        client = _FakeTradingClient(account=acct)
        broker = _broker(client, crypto_asset_specs=SPECS)
        # Monkey-patch paper to a non-bool.
        broker._paper = None  # noqa: SLF001

        # Override get_account_info to return paper=None.
        original_get = broker.get_account_info

        def _no_paper_flag():
            info = original_get()
            info["paper"] = None
            return info

        broker.get_account_info = _no_paper_flag
        report = run_full_battery(broker, dry_run=True)
        assert report.environment == "unknown"
        assert report.all_passed is False
        assert any(
            s.name == "environment_verification" and s.status == StepStatus.FAIL
            for s in report.steps
        )

    def test_live_environment_reported_is_fail(self) -> None:
        """Paper flag is False — battery should refuse to proceed."""
        acct = _FakeAccount()
        client = _FakeTradingClient(account=acct)
        broker = _broker(client, crypto_asset_specs=SPECS)

        original_get = broker.get_account_info

        def _live_env():
            info = original_get()
            info["paper"] = False
            return info

        broker.get_account_info = _live_env
        report = run_full_battery(broker, dry_run=True)
        assert report.environment == "live"
        assert report.all_passed is False
        assert any(
            s.name == "environment_verification" for s in report.steps
        )


class TestStopLimitDiagnosticCapabilityLabel:
    """Review item #3: stop-limit check is a non-gating diagnostic
    (required=False), not a capability proof.
    """

    def test_pass_result_has_diagnostic_label(self) -> None:
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_stop_limit_acceptance(broker, (BTC,))
        assert result.status == StepStatus.PASS
        assert "DIAGNOSTIC" in result.detail
        assert "not empirical fill proof" in result.detail
        assert result.data["check_type"] == "diagnostic_capability"
        assert result.required is False

    def test_fail_result_has_diagnostic_label(self) -> None:
        client = _FakeTradingClient(assets={})
        broker = _broker(client)
        result = check_stop_limit_acceptance(broker, (BTC,))
        assert result.status == StepStatus.FAIL
        assert result.data["check_type"] == "diagnostic_capability"
        assert result.required is False


class TestOptionalStepGating:
    """Review item #5: optional steps (required=False) don't block
    all_passed.
    """

    def test_all_passed_with_optional_skip(self) -> None:
        """A report with all required=PASS and one optional=SKIP passes."""
        steps = [
            StepResult(name="a", status=StepStatus.PASS, detail="ok", required=True),
            StepResult(name="b", status=StepStatus.PASS, detail="ok", required=True),
            StepResult(
                name="c", status=StepStatus.SKIP, detail="skip", required=False,
            ),
        ]
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps,
        )
        assert report.all_passed is True

    def test_all_passed_fails_when_required_fails(self) -> None:
        steps = [
            StepResult(name="a", status=StepStatus.PASS, detail="ok", required=True),
            StepResult(name="b", status=StepStatus.FAIL, detail="bad", required=True),
            StepResult(
                name="c", status=StepStatus.SKIP, detail="skip", required=False,
            ),
        ]
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps,
        )
        assert report.all_passed is False

    def test_all_passed_ignores_optional_fail(self) -> None:
        """An optional step that FAILs doesn't block the battery."""
        steps = [
            StepResult(name="a", status=StepStatus.PASS, detail="ok", required=True),
            StepResult(name="b", status=StepStatus.FAIL, detail="bad", required=False),
        ]
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps,
        )
        assert report.all_passed is True


# ── review item #1 hardening: unknown/missing order fields ───────────────


class TestUnknownMissingOrderFields:
    """Unknown or missing order fields must cause rejection."""

    def test_unknown_status_rejected(self) -> None:
        """An unknown/pending status is NOT accepted."""
        result = {
            "order_id": "ord-1",
            "status": "pending_new",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "unknown/unacceptable" in failure

    def test_partially_filled_rejected(self) -> None:
        """partially_filled is not a truly resting status — reject it."""
        result = {
            "order_id": "ord-1",
            "status": "partially_filled",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "unknown/unacceptable" in failure

    def test_held_status_rejected(self) -> None:
        """held is ambiguous — reject it."""
        result = {
            "order_id": "ord-1",
            "status": "held",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "unknown/unacceptable" in failure

    def test_empty_status_rejected(self) -> None:
        """Empty/absent status is rejected."""
        result = {
            "order_id": "ord-1",
            "status": "",
            "side": "BUY",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None

    def test_missing_side_rejected(self) -> None:
        """Absent side field is rejected (not silently accepted)."""
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "",
            "time_in_force": "gtc",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "missing side" in failure

    def test_missing_tif_rejected(self) -> None:
        """Absent time_in_force field is rejected."""
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "",
        }
        failure = _validate_order_acceptance(
            result, expected_side="BUY", expected_tif="gtc", pair=BTC,
        )
        assert failure is not None
        assert "missing time_in_force" in failure


# ── review item #1 hardening: wrong order type/asset class ───────────────


class TestWrongOrderTypeAssetClass:
    """Mismatch in order_type or asset_class must cause rejection."""

    def test_wrong_order_type_rejected(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "gtc",
            "order_type": "market",
            "asset_class": "crypto",
        }
        failure = _validate_order_acceptance(
            result,
            expected_side="BUY",
            expected_tif="gtc",
            expected_order_type="limit",
            expected_asset_class="crypto",
            pair=BTC,
        )
        assert failure is not None
        assert "order_type=" in failure
        assert "market" in failure

    def test_missing_order_type_rejected(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "gtc",
            "order_type": "",
            "asset_class": "crypto",
        }
        failure = _validate_order_acceptance(
            result,
            expected_side="BUY",
            expected_tif="gtc",
            expected_order_type="limit",
            expected_asset_class="crypto",
            pair=BTC,
        )
        assert failure is not None
        assert "missing order_type" in failure

    def test_wrong_asset_class_rejected(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "gtc",
            "order_type": "limit",
            "asset_class": "us_equity",
        }
        failure = _validate_order_acceptance(
            result,
            expected_side="BUY",
            expected_tif="gtc",
            expected_order_type="limit",
            expected_asset_class="crypto",
            pair=BTC,
        )
        assert failure is not None
        assert "asset_class=" in failure
        assert "us_equity" in failure

    def test_missing_asset_class_rejected(self) -> None:
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "gtc",
            "order_type": "limit",
            "asset_class": "",
        }
        failure = _validate_order_acceptance(
            result,
            expected_side="BUY",
            expected_tif="gtc",
            expected_order_type="limit",
            expected_asset_class="crypto",
            pair=BTC,
        )
        assert failure is not None
        assert "missing asset_class" in failure

    def test_correct_type_and_class_pass(self) -> None:
        """When all fields match including order_type and asset_class, pass."""
        result = {
            "order_id": "ord-1",
            "status": "accepted",
            "side": "BUY",
            "time_in_force": "gtc",
            "order_type": "limit",
            "asset_class": "crypto",
        }
        failure = _validate_order_acceptance(
            result,
            expected_side="BUY",
            expected_tif="gtc",
            expected_order_type="limit",
            expected_asset_class="crypto",
            pair=BTC,
        )
        assert failure is None


# ── review item #2: residual position/order audit ────────────────────────


class TestResidualExposureAudit:
    """Partial fill + confirmed cancel with residual position is Tier-1."""

    def test_clean_probe_no_residual(self) -> None:
        """No fills, no position — residual check passes."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        order_details: dict[str, Any] = {}
        clean, failure = _check_residual_exposure(
            broker, "ord-1", BTC, order_details=order_details,
        )
        assert clean is True
        assert failure is None
        assert order_details["final_order_state"]["filled_qty"] == 0.0
        assert order_details["residual_position_qty"] == 0.0

    def test_partial_fill_detected(self) -> None:
        """Order with filled_qty > 0 is a Tier-1 failure."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        # Simulate partial fill on the order
        order = SimpleNamespace(
            id="ord-1", status="canceled", qty=1.0,
            filled_qty=0.05, filled_avg_price=65000.0,
            side="BUY", symbol=BTC,
            created_at="", submitted_at="", filled_at="",
        )
        client._orders_by_id["ord-1"] = order
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        order_details: dict[str, Any] = {}
        clean, failure = _check_residual_exposure(
            broker, "ord-1", BTC, order_details=order_details,
        )
        assert clean is False
        assert failure is not None
        assert "residual fill" in failure
        assert "Tier-1" in failure
        assert "filled_qty=0.05" in failure

    def test_residual_position_detected(self) -> None:
        """Nonzero position after probe is a Tier-1 failure."""
        assets = {BTC: _crypto_asset()}
        # Set up a position that exists for BTC/USD
        positions = {BTC: SimpleNamespace(qty=0.001)}
        client = _FakeTradingClient(assets=assets, positions=positions)
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        order_details: dict[str, Any] = {}
        clean, failure = _check_residual_exposure(
            broker, "ord-1", BTC, order_details=order_details,
        )
        assert clean is False
        assert failure is not None
        assert "residual position" in failure
        assert "Tier-1" in failure

    def test_partial_fill_with_cancel_is_step_fail(self) -> None:
        """GTC acceptance step fails when cancel succeeds but order filled."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        # Override submit_order to produce an order that will show fills
        original_submit = client.submit_order

        def _submit_with_fill(order_data):
            order = original_submit(order_data)
            # After submission, set filled_qty on the order so
            # get_order_by_id returns it with a fill.
            order.filled_qty = 0.01
            return order

        client.submit_order = _submit_with_fill
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_gtc_order_acceptance(broker, (BTC,))
        assert result.status == StepStatus.FAIL
        assert "residual fill" in result.detail or "Tier-1" in result.detail


# ── review item #3: price-band rejection as diagnostic ───────────────────


class TestPriceBandRejectionDiagnostic:
    """Price-band rejection on stop-limit is classified as diagnostic
    (non-gating) rather than a false capability failure.
    """

    def test_price_band_rejection_does_not_block_battery(self) -> None:
        """A stop-limit failure due to price bands doesn't block all_passed."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(assets=assets)
        # Make stop-limit submission raise (simulating broker price-band rejection)
        original_submit = client.submit_order

        def _reject_stop_limit(order_data):
            raise RuntimeError("price outside allowed band")

        client.submit_order = _reject_stop_limit
        broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
        result = check_stop_limit_acceptance(broker, (BTC,))
        assert result.status == StepStatus.FAIL
        assert result.required is False  # non-gating diagnostic
        # The battery should still pass if all required steps pass.
        steps = [
            StepResult(name="a", status=StepStatus.PASS, detail="ok", required=True),
            result,  # FAIL but required=False
        ]
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps,
        )
        assert report.all_passed is True


# ── review item #4: environment / base_url verification ──────────────────


class TestEnvironmentBaseUrlVerification:
    """Base URL must be consistent with paper flag; exposed in report."""

    def test_paper_url_passes(self) -> None:
        """Paper flag + paper URL = consistent, passes."""
        broker = _broker(crypto_asset_specs=SPECS)
        report = run_full_battery(broker, dry_run=True)
        assert report.base_url == "https://paper-api.alpaca.markets"
        assert report.environment == "paper"

    def test_production_url_with_paper_flag_fails(self) -> None:
        """Paper flag + production URL = inconsistent, FAIL."""
        assets = {BTC: _crypto_asset()}
        client = _FakeTradingClient(
            assets=assets,
            base_url="https://api.alpaca.markets",
        )
        broker = _broker(client, crypto_asset_specs=SPECS)
        report = run_full_battery(broker, dry_run=True)
        assert report.environment == "inconsistent"
        assert report.all_passed is False
        assert any(
            s.name == "environment_verification"
            and s.status == StepStatus.FAIL
            and "inconsistency" in s.detail
            for s in report.steps
        )

    def test_base_url_in_report(self) -> None:
        """base_url is an immutable report field."""
        broker = _broker(crypto_asset_specs=SPECS)
        report = run_full_battery(broker, dry_run=True)
        assert hasattr(report, "base_url")
        assert report.base_url == "https://paper-api.alpaca.markets"

    def test_account_id_in_report(self) -> None:
        """account_id is an immutable report field."""
        broker = _broker(crypto_asset_specs=SPECS)
        report = run_full_battery(broker, dry_run=True)
        assert report.account_id == "PA-FAKE-001"


# ── review item #5: nonempty required-gate set + schema/hash ─────────────


class TestNonemptyRequiredGateSet:
    """all_passed must require a nonempty required-gate set."""

    def test_empty_steps_is_not_passed(self) -> None:
        """No steps at all — all_passed is False (vacuous truth rejected)."""
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=[],
        )
        assert report.all_passed is False

    def test_only_optional_steps_is_not_passed(self) -> None:
        """Only optional steps (all PASS) — all_passed is False."""
        steps = [
            StepResult(
                name="a", status=StepStatus.PASS, detail="ok", required=False,
            ),
            StepResult(
                name="b", status=StepStatus.PASS, detail="ok", required=False,
            ),
        ]
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps,
        )
        assert report.all_passed is False


class TestReportSchemaVersionAndHash:
    """Report carries schema version and content hash."""

    def test_report_schema_version_present(self) -> None:
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False,
        )
        assert report.report_schema_version == REPORT_SCHEMA_VERSION
        assert report.report_schema_version == "1.0.0"

    def test_content_hash_is_sha256(self) -> None:
        """content_hash returns a 64-char hex SHA-256 digest."""
        steps = [
            StepResult(name="a", status=StepStatus.PASS, detail="ok"),
        ]
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps,
        )
        h = report.content_hash()
        assert isinstance(h, str)
        assert len(h) == 64
        # Must be valid hex.
        int(h, 16)

    def test_content_hash_deterministic(self) -> None:
        """Same report produces the same hash."""
        steps = [
            StepResult(name="a", status=StepStatus.PASS, detail="ok"),
        ]
        report = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps,
        )
        assert report.content_hash() == report.content_hash()

    def test_content_hash_changes_on_different_data(self) -> None:
        """Different step data produces a different hash."""
        steps1 = [StepResult(name="a", status=StepStatus.PASS, detail="ok")]
        steps2 = [StepResult(name="a", status=StepStatus.FAIL, detail="bad")]
        r1 = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps1,
        )
        r2 = BatteryReport(
            timestamp="t", account_id="x", environment="paper",
            dry_run=False, steps=steps2,
        )
        assert r1.content_hash() != r2.content_hash()
