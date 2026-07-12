"""Tests for the crypto Stage-0 battery checks.

All tests mock the AlpacaBroker methods -- the battery module never imports
alpaca-py directly, so these tests work without broker SDK credentials or
the alpaca-py package installed in the test environment (except where a
test directly exercises ``AlpacaBroker.place_crypto_*``, which construct
real alpaca-py request objects; those already require alpaca-py per the
existing broker thin-wrapper tests below).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from renquant_execution.alpaca_broker import AlpacaBroker, CryptoQuoteSnapshot
from renquant_execution.crypto import CryptoAssetSpec
from renquant_execution.crypto_stage0_checks import (
    DEFAULT_CANARY_PAIRS,
    BatteryReport,
    StepResult,
    StepStatus,
    _check_gtc_order_acceptance,
    _check_stop_limit_acceptance,
    check_buying_power_behavior,
    check_crypto_account_status,
    check_data_parity,
    check_pair_snapshot,
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

#: Fake reference prices used by every test unless a test explicitly passes
#: its own ``reference_prices=`` override -- keeps the quote-derived canary
#: pricing (Codex review 2026-07-12 finding 3) hermetic: no test hits a real
#: market-data endpoint.
DEFAULT_REFERENCE_PRICES: dict[str, float] = {BTC: 60_000.0, ETH: 3_000.0, SOL: 150.0}


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
    """Fake TradingClient for battery tests -- no alpaca-py required to
    construct (though ``AlpacaBroker.place_crypto_*`` do construct real
    alpaca-py request objects and pass them in here as ``order_data``).

    ``submit_order`` echoes ``order_data``'s own type/side/time_in_force/
    price fields back onto the returned fake order -- mirroring a real
    broker's acceptance echo -- so the default happy-path tests naturally
    satisfy the field-validation Codex added (2026-07-12 finding 2) without
    every test needing to hand-construct a matching response.

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
        positions: dict[str, float] | None = None,
    ) -> None:
        self._account = account or _FakeAccount()
        self._assets = assets or {}
        self._orders_by_id: dict[str, SimpleNamespace] = {}
        self._order_status_sequence = {
            k: list(v) for k, v in (order_status_sequence or {}).items()
        }
        self._positions = dict(positions or {})
        self.submitted: list[Any] = []
        self.cancelled: list[str] = []
        self.get_order_by_id_calls: list[str] = []

    def get_account(self):
        return self._account

    def get_open_position(self, symbol: str):
        if symbol not in self._positions:
            raise RuntimeError(f"position does not exist for {symbol}")
        return SimpleNamespace(qty=self._positions[symbol])

    def get_asset(self, symbol: str):
        if symbol not in self._assets:
            raise RuntimeError(f"unknown asset {symbol}")
        return self._assets[symbol]

    @staticmethod
    def _enum_str(raw: Any) -> str:
        return str(getattr(raw, "value", raw) or "")

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        qty = float(getattr(order_data, "qty", 0.0) or 0.0)
        side = self._enum_str(getattr(order_data, "side", "")).upper()
        order_type = self._enum_str(getattr(order_data, "type", ""))
        tif = self._enum_str(getattr(order_data, "time_in_force", ""))
        limit_price = getattr(order_data, "limit_price", None)
        stop_price = getattr(order_data, "stop_price", None)
        order_id = f"ord-{len(self.submitted)}"
        order = SimpleNamespace(
            id=order_id,
            status="accepted",
            symbol=getattr(order_data, "symbol", ""),
            side=side or "BUY",
            order_type=order_type,
            time_in_force=tif,
            limit_price=limit_price,
            stop_price=stop_price,
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
    reference_prices: dict[str, float] | None = None,
    **kwargs,
) -> AlpacaBroker:
    """Build a battery-testable broker with injected fake client.

    Also monkeypatches ``get_crypto_reference_quote`` with a fake, hermetic,
    always-fresh lookup (Codex review 2026-07-12 finding 3: canary prices are
    now derived from a "real" reference quote rather than universal fixed
    constants) -- no test hits a real market-data endpoint. Pass
    ``reference_prices={}`` to simulate every pair's reference-quote lookup
    failing.
    """
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    if client is None:
        client = _FakeTradingClient(assets=assets)
    broker = AlpacaBroker(paper=paper, label="alpaca-battery-test", **kwargs)
    broker._trading_client = client  # noqa: SLF001
    broker._account = client.get_account()  # noqa: SLF001

    prices = DEFAULT_REFERENCE_PRICES if reference_prices is None else reference_prices

    def _fake_reference_quote(symbol: str) -> CryptoQuoteSnapshot:
        if symbol not in prices:
            raise RuntimeError(f"no fake reference price configured for {symbol!r}")
        mid = prices[symbol]
        return CryptoQuoteSnapshot(
            symbol=symbol,
            bid_price=mid,
            ask_price=mid,
            mid_price=mid,
            timestamp=dt.datetime.now(dt.timezone.utc),
            age_seconds=0.1,
        )

    broker.get_crypto_reference_quote = _fake_reference_quote  # noqa: SLF001
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


# ── _check_gtc_order_acceptance ─────────────────────────────────────────────


def test_gtc_acceptance_pass_all_pairs() -> None:
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker = _broker(client, crypto_asset_specs=SPECS)
    result = _check_gtc_order_acceptance(broker, CANARY)
    assert result.status == StepStatus.PASS
    assert len(client.submitted) == 3
    assert len(client.cancelled) == 3
    assert "3 GTC limit-buy orders placed+cancelled" in result.detail
    # Prices are quote-derived (finding 3), not the old fixed $0.01.
    for pair in CANARY:
        assert result.data["orders"][pair]["cancel_confirmed"] is True


def test_gtc_acceptance_prices_are_quote_derived_not_fixed() -> None:
    """Canary limit price is ~half the fake reference price, not a
    universal fixed constant (Codex review 2026-07-12 finding 3)."""
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(broker, (BTC,))
    assert result.status == StepStatus.PASS
    limit_price = result.data["orders"][BTC]["confirmed_limit_price"]
    # Reference price is 60_000.0 (DEFAULT_REFERENCE_PRICES); the probe
    # should be roughly half of that, not $0.01.
    assert 25_000.0 < limit_price < 35_000.0


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
    result = _check_gtc_order_acceptance(broker, CANARY)
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
    result = _check_gtc_order_acceptance(broker, CANARY)
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
    result = _check_gtc_order_acceptance(
        broker,
        (BTC,),
        cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
        cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
    )
    assert result.status == StepStatus.FAIL
    assert BTC in result.detail
    assert "ord-1" in result.detail
    assert "not confirmed" in result.detail
    # cancel_order() was called (and did not raise) -- the order was placed
    # and a cancel was requested, but never confirmed as terminally canceled.
    assert client.cancelled == ["ord-1"]
    assert result.data["orders"][BTC]["cancel_confirmed"] is False


def test_gtc_acceptance_queries_residual_position_when_cancel_unconfirmed() -> None:
    """An unconfirmed cancellation is ambiguous (still resting, or filled
    during the cancel-confirm window) -- residual position must be queried
    as durable evidence, same discipline as the initial-FILLED-status path
    (the race a same-package concurrent fix flagged: execution#36)."""
    client = _FakeTradingClient(
        assets={BTC: _crypto_asset()},
        order_status_sequence={"ord-1": ["accepted"]},
        positions={BTC: 0.0001},
    )
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(
        broker,
        (BTC,),
        cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
        cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
    )
    assert result.status == StepStatus.FAIL
    assert "residual_position_qty" in result.detail
    assert result.data["orders"][BTC]["residual_position_qty"] == pytest.approx(0.0001)


def test_gtc_acceptance_fails_when_order_fills_instead_of_resting() -> None:
    """A probe order that FILLS is a more severe Tier-1 condition than a
    merely-rejected one: real paper inventory was acquired. No cancel is
    attempted against a filled order (there's nothing resting to cancel).
    """
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    original_submit = client.submit_order

    def _filled(order_data):
        order = original_submit(order_data)
        order.status = "filled"
        return order

    client.submit_order = _filled
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(broker, (BTC,))
    assert result.status == StepStatus.FAIL
    assert "FILLED" in result.detail
    assert "Tier-1" in result.detail
    assert client.cancelled == []


def test_gtc_acceptance_records_residual_position_on_fill() -> None:
    """Codex round-2 review finding 3: a bare FILLED status is not durable
    evidence of how much inventory now exists -- the residual position must
    be queried and recorded, not just the order's own status field."""
    client = _FakeTradingClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 0.0001},
    )
    original_submit = client.submit_order

    def _filled(order_data):
        order = original_submit(order_data)
        order.status = "filled"
        return order

    client.submit_order = _filled
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(broker, (BTC,))
    assert result.status == StepStatus.FAIL
    assert "residual_position_qty" in result.detail
    assert result.data["orders"][BTC]["residual_position_qty"] == pytest.approx(0.0001)


def test_gtc_acceptance_fails_on_missing_order_type_field() -> None:
    """Codex round-2 review finding 2: an EMPTY/missing broker-confirmed
    field must FAIL, never be silently skipped -- the original `if field and
    field != expected` pattern let a blank field slip past validation."""
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    original_submit = client.submit_order

    def _blank_order_type(order_data):
        order = original_submit(order_data)
        order.order_type = ""
        return order

    client.submit_order = _blank_order_type
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(broker, (BTC,))
    assert result.status == StepStatus.FAIL
    assert "order_type" in result.detail
    # Cleanup still happened despite the missing field.
    assert client.cancelled == ["ord-1"]


def test_gtc_acceptance_fails_on_quantity_mismatch() -> None:
    """Codex round-2 review finding 2: quantity was not validated at all --
    a broker-confirmed quantity that doesn't match the request must FAIL."""
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    original_submit = client.submit_order

    def _wrong_qty(order_data):
        order = original_submit(order_data)
        order.qty = order.qty * 2.0  # broker echoed a different quantity
        return order

    client.submit_order = _wrong_qty
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(broker, (BTC,))
    assert result.status == StepStatus.FAIL
    assert "confirmed_quantity" in result.detail
    assert client.cancelled == ["ord-1"]


def test_gtc_acceptance_confirms_cancel_via_poll_despite_cancel_order_exception() -> None:
    """Codex round-2 review finding 5: a cancel_order() call that RAISES is
    not proof the order is still there -- the request may have reached the
    broker despite a transport-level exception. Must still poll for the
    actual terminal state and PASS if it's genuinely confirmed canceled."""
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    original_cancel = client.cancel_order_by_id

    def _raise_then_actually_cancel(order_id: str) -> None:
        # The broker DID process the cancellation (order reaches "canceled"
        # in the fake's internal state via the real cancel path) but the
        # transport call back to us raises anyway.
        original_cancel(order_id)
        raise RuntimeError("simulated transport timeout after broker ack")

    client.cancel_order_by_id = _raise_then_actually_cancel
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(
        broker,
        (BTC,),
        cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
        cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
    )
    assert result.status == StepStatus.PASS
    assert result.data["orders"][BTC]["cancel_confirmed"] is True
    assert result.data["orders"][BTC]["cancel_exception"] is not None
    assert "transport timeout" in result.data["orders"][BTC]["cancel_exception"]


def test_gtc_acceptance_fails_when_cancel_raises_and_poll_never_confirms() -> None:
    """The counterpart to the above: cancel_order() raises AND the poll
    genuinely never observes a terminal canceled state -- must FAIL, with
    both the exception and the unconfirmed poll result recorded."""
    client = _FakeTradingClient(
        assets={BTC: _crypto_asset()},
        order_status_sequence={"ord-1": ["accepted"]},
    )

    def _raise_only(order_id: str) -> None:
        raise RuntimeError("simulated hard transport failure")

    client.cancel_order_by_id = _raise_only
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(
        broker,
        (BTC,),
        cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
        cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
    )
    assert result.status == StepStatus.FAIL
    assert "simulated hard transport failure" in result.detail
    assert "not subsequently confirmed" in result.detail
    assert result.data["orders"][BTC]["cancel_confirmed"] is False


def test_gtc_acceptance_fails_on_side_mismatch() -> None:
    """Acceptance must be confirmed from genuinely matching order fields,
    not just a nonempty order_id (Codex review 2026-07-12 finding 2). The
    order is still cleaned up (cancelled+confirmed) even though the
    mismatch itself is reported as a failure.
    """
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    original_submit = client.submit_order

    def _wrong_side(order_data):
        order = original_submit(order_data)
        order.side = "SELL"  # broker echoed the wrong side
        return order

    client.submit_order = _wrong_side
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_gtc_order_acceptance(broker, (BTC,))
    assert result.status == StepStatus.FAIL
    assert "side" in result.detail
    assert "SELL" in result.detail
    # Cleanup still happened despite the field mismatch.
    assert client.cancelled == ["ord-1"]
    assert result.data["orders"][BTC]["cancel_confirmed"] is True


def test_gtc_acceptance_fails_when_reference_price_lookup_fails() -> None:
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC}, reference_prices={})
    result = _check_gtc_order_acceptance(broker, (BTC,))
    assert result.status == StepStatus.FAIL
    assert "reference quote lookup failed" in result.detail
    # Never even attempted to place an order without a reference price.
    assert client.submitted == []


# ── _check_stop_limit_acceptance ────────────────────────────────────────────


def test_stop_limit_acceptance_pass() -> None:
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker = _broker(client, crypto_asset_specs=SPECS)
    result = _check_stop_limit_acceptance(broker, CANARY)
    assert result.status == StepStatus.PASS
    assert len(client.submitted) == 3
    assert len(client.cancelled) == 3
    assert "stop-limit BUY" in result.detail


def test_stop_limit_acceptance_prices_are_quote_derived() -> None:
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_stop_limit_acceptance(broker, (BTC,))
    assert result.status == StepStatus.PASS
    stop_price = result.data["orders"][BTC]["confirmed_stop_price"]
    # Reference price is 60_000.0; the stop probe should be ~3x that
    # (180_000.0), not the old fixed $999,999,999 constant.
    assert 150_000.0 < stop_price < 210_000.0


def test_stop_limit_acceptance_fail_on_spec_lookup() -> None:
    """If a pair's spec can't be resolved, the step fails."""
    client = _FakeTradingClient(assets={})
    broker = _broker(client)
    result = _check_stop_limit_acceptance(broker, ("BTC/USD",))
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
    result = _check_stop_limit_acceptance(
        broker,
        (BTC,),
        cancel_confirm_timeout_seconds=_FAST_CANCEL_TIMEOUT_SECONDS,
        cancel_confirm_poll_interval_seconds=_FAST_CANCEL_POLL_INTERVAL_SECONDS,
    )
    assert result.status == StepStatus.FAIL
    assert BTC in result.detail
    assert "ord-1" in result.detail
    assert "not confirmed" in result.detail
    assert client.cancelled == ["ord-1"]
    assert result.data["orders"][BTC]["cancel_confirmed"] is False


def test_stop_limit_acceptance_fails_when_order_fills() -> None:
    client = _FakeTradingClient(assets={BTC: _crypto_asset()})
    original_submit = client.submit_order

    def _filled(order_data):
        order = original_submit(order_data)
        order.status = "filled"
        return order

    client.submit_order = _filled
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = _check_stop_limit_acceptance(broker, (BTC,))
    assert result.status == StepStatus.FAIL
    assert "FILLED" in result.detail
    assert client.cancelled == []


# ── check_buying_power_behavior ─────────────────────────────────────────────


def test_buying_power_pass_is_observational_and_not_required() -> None:
    result = check_buying_power_behavior(_broker())
    assert result.status == StepStatus.PASS
    assert "non_marginable_buying_power=100000.0" in result.detail
    assert "observational only" in result.detail
    assert result.required is False


def test_buying_power_fail_when_nmbp_zero() -> None:
    acct = _FakeAccount()
    acct.non_marginable_buying_power = 0.0
    acct.cash = 50_000.0
    client = _FakeTradingClient(account=acct)
    result = check_buying_power_behavior(_broker(client))
    assert result.status == StepStatus.FAIL
    assert "misconfigured" in result.detail
    assert result.required is False


# ── check_data_parity ──────────────────────────────────────────────────────


def test_data_parity_skips_with_reason_and_is_not_required() -> None:
    result = check_data_parity(CANARY)
    assert result.status == StepStatus.SKIP
    assert "placeholder" in result.detail
    assert result.data["reason"] == "no_data_source"
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
    # Every REQUIRED step passed, and the two required=False steps (SKIP
    # data_parity, observational buying_power) don't block overall success
    # (Codex review 2026-07-12 finding 5).
    assert report.all_passed is True


def test_full_battery_summary_format() -> None:
    broker = _broker(crypto_asset_specs=SPECS)
    report = run_full_battery(broker, dry_run=True)
    summary = report.summary
    assert "6 steps" in summary
    # dry_run: 2 SKIP (gtc + stop-limit, required=True but skipped in
    # dry-run) + 1 SKIP (data_parity, required=False) = 3 SKIP; PASS =
    # crypto_account_status + pair_snapshot + buying_power_behavior = 3.
    assert "SKIP=3" in summary
    assert "PASS=3" in summary


def test_battery_report_all_passed_property() -> None:
    broker = _broker(crypto_asset_specs=SPECS)
    assets = {BTC: _crypto_asset(), ETH: _crypto_asset(), SOL: _crypto_asset()}
    client = _FakeTradingClient(assets=assets)
    broker._trading_client = client  # noqa: SLF001
    report = run_full_battery(broker, dry_run=False)
    # data_parity (SKIP) and buying_power_behavior (observational) are both
    # required=False, so an otherwise fully-passing run reports all_passed
    # True (Codex review 2026-07-12 finding 5 -- a required=False SKIP must
    # not make a clean battery run structurally impossible to report as
    # passing).
    assert report.all_passed is True

    required_steps = [s for s in report.steps if s.required]
    assert required_steps  # sanity: at least one required step exists
    assert all(s.status == StepStatus.PASS for s in required_steps)


def test_battery_report_all_passed_false_when_a_required_step_fails() -> None:
    client = _FakeTradingClient(assets={})  # no assets -- pair_snapshot fails
    broker = _broker(client, crypto_asset_specs={})
    report = run_full_battery(broker, dry_run=True)
    pair_step = next(s for s in report.steps if s.name == "pair_snapshot")
    assert pair_step.required is True
    assert pair_step.status == StepStatus.FAIL
    assert report.all_passed is False


# ── environment verification fail-closed (Codex review finding 4) ──────────


def test_full_battery_fails_closed_when_account_lookup_errors() -> None:
    broker = AlpacaBroker(paper=True)  # not connected -- get_account_info() raises
    report = run_full_battery(broker, dry_run=True)
    assert report.environment == "unverified"
    assert report.all_passed is False
    names = [s.name for s in report.steps]
    assert "environment_verification" in names
    env_step = next(s for s in report.steps if s.name == "environment_verification")
    assert env_step.status == StepStatus.ERROR
    # No transactional steps were ever reached.
    assert "gtc_order_acceptance" not in names


def test_full_battery_fails_closed_when_paper_flag_mismatches_account() -> None:
    """broker.paper=True alone is not sufficient evidence (Codex review
    2026-07-12 finding 4): if the account-identity lookup itself reports
    paper=False, the battery must refuse to run, never silently default to
    environment='paper'.
    """
    client = _FakeTradingClient()
    broker = _broker(client)
    broker.get_account_info = lambda: {
        "account_id": "PA-FAKE-001",
        "status": "ACTIVE",
        "crypto_status": "ACTIVE",
        "buying_power": 100_000.0,
        "non_marginable_buying_power": 100_000.0,
        "cash": 100_000.0,
        "portfolio_value": 100_000.0,
        "paper": False,
    }
    report = run_full_battery(broker, dry_run=True)
    assert report.environment == "unverified"
    assert report.all_passed is False
    env_step = next(s for s in report.steps if s.name == "environment_verification")
    assert env_step.status == StepStatus.ERROR
    assert "paper=False" in env_step.detail
    names = [s.name for s in report.steps]
    assert "gtc_order_acceptance" not in names


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
    # Normalized confirmation fields (Codex review 2026-07-12 finding 2).
    assert result["status"] == "accepted"
    assert result["order_type"] == "limit"
    assert result["side"] == "BUY"
    assert result["confirmed_time_in_force"] == "gtc"
    assert result["confirmed_limit_price"] == pytest.approx(60000.0)


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
    assert result["order_type"] == "stop_limit"
    assert result["side"] == "BUY"
    assert result["confirmed_stop_price"] == pytest.approx(70000.0)
    assert result["confirmed_limit_price"] == pytest.approx(70500.0)


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


# ── AlpacaBroker.get_crypto_reference_price/_quote (Codex finding 3, round-2 finding 4) ──


def _fresh_ts() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def test_get_crypto_reference_price_mid_of_bid_ask() -> None:
    broker = AlpacaBroker(paper=True)
    fake_quote = SimpleNamespace(
        bid_price=59_900.0, ask_price=60_100.0, timestamp=_fresh_ts(), symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        price = broker.get_crypto_reference_price(BTC)
    assert price == pytest.approx(60_000.0)


def test_get_crypto_reference_price_falls_back_to_ask_only() -> None:
    broker = AlpacaBroker(paper=True)
    fake_quote = SimpleNamespace(
        bid_price=0.0, ask_price=60_100.0, timestamp=_fresh_ts(), symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        price = broker.get_crypto_reference_price(BTC)
    assert price == pytest.approx(60_100.0)


def test_get_crypto_reference_price_raises_on_lookup_failure() -> None:
    broker = AlpacaBroker(paper=True)
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.side_effect = (
            RuntimeError("boom")
        )
        with pytest.raises(RuntimeError, match="latest-quote lookup"):
            broker.get_crypto_reference_price(BTC)


def test_get_crypto_reference_price_raises_when_no_usable_quote() -> None:
    broker = AlpacaBroker(paper=True)
    fake_quote = SimpleNamespace(
        bid_price=0.0, ask_price=0.0, timestamp=_fresh_ts(), symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        with pytest.raises(RuntimeError, match="no usable bid/ask quote"):
            broker.get_crypto_reference_price(BTC)


def test_get_crypto_reference_quote_returns_typed_snapshot_with_provenance() -> None:
    """Codex round-2 finding 4: a bare float drops quote timestamp/source/
    symbol identity. get_crypto_reference_quote must return all of it."""
    broker = AlpacaBroker(paper=True)
    ts = _fresh_ts()
    fake_quote = SimpleNamespace(
        bid_price=59_900.0, ask_price=60_100.0, timestamp=ts, symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        snapshot = broker.get_crypto_reference_quote(BTC)
    assert snapshot.symbol == BTC
    assert snapshot.bid_price == pytest.approx(59_900.0)
    assert snapshot.ask_price == pytest.approx(60_100.0)
    assert snapshot.mid_price == pytest.approx(60_000.0)
    assert snapshot.timestamp == ts
    assert snapshot.age_seconds < 1.0


def test_get_crypto_reference_quote_rejects_missing_timestamp() -> None:
    broker = AlpacaBroker(paper=True)
    fake_quote = SimpleNamespace(
        bid_price=59_900.0, ask_price=60_100.0, timestamp=None, symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        with pytest.raises(RuntimeError, match="no timestamp"):
            broker.get_crypto_reference_quote(BTC)


def test_get_crypto_reference_quote_rejects_stale_quote() -> None:
    broker = AlpacaBroker(paper=True)
    stale_ts = _fresh_ts() - dt.timedelta(seconds=120)
    fake_quote = SimpleNamespace(
        bid_price=59_900.0, ask_price=60_100.0, timestamp=stale_ts, symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        with pytest.raises(RuntimeError, match="stale"):
            broker.get_crypto_reference_quote(BTC, max_staleness_seconds=60.0)


def test_get_crypto_reference_quote_rejects_implausible_future_timestamp() -> None:
    broker = AlpacaBroker(paper=True)
    future_ts = _fresh_ts() + dt.timedelta(seconds=30)
    fake_quote = SimpleNamespace(
        bid_price=59_900.0, ask_price=60_100.0, timestamp=future_ts, symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        with pytest.raises(RuntimeError, match="future timestamp"):
            broker.get_crypto_reference_quote(BTC)


def test_get_crypto_reference_quote_accepts_fresh_quote_within_staleness_bound() -> None:
    broker = AlpacaBroker(paper=True)
    almost_stale_ts = _fresh_ts() - dt.timedelta(seconds=59)
    fake_quote = SimpleNamespace(
        bid_price=59_900.0, ask_price=60_100.0, timestamp=almost_stale_ts, symbol=BTC,
    )
    with patch(
        "alpaca.data.historical.crypto.CryptoHistoricalDataClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.get_crypto_latest_quote.return_value = {
            BTC: fake_quote,
        }
        snapshot = broker.get_crypto_reference_quote(BTC, max_staleness_seconds=60.0)
    assert snapshot.mid_price == pytest.approx(60_000.0)
