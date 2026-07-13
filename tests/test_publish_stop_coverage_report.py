"""Integration-style tests for ``AlpacaBroker.publish_stop_coverage_report()``
— the ONLY execution-owned path to a genuine ``CoverageReport`` (Codex review
2026-07-13T00:16:11Z, PR #37).

Uses a fake alpaca-py TradingClient (pattern mirrors
``tests/test_crypto_order_semantics.py``'s ``_FakeCryptoClient`` and
``tests/test_alpaca_broker_account_id.py``'s ``_FakeAccount``) so these tests
run with no network and no real broker credentials, while still exercising
the REAL ``check_crypto_stop_coverage`` / ``get_all_positions`` /
``get_open_orders_detailed`` code paths end-to-end — nothing about violation
detection is mocked or bypassed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from renquant_execution.alpaca_broker import AlpacaBroker
from renquant_execution.coverage_report import (
    CoverageReport,
    compute_snapshot_hash,
    verify_coverage_report,
)

BTC = "BTC/USD"
ETH = "ETH/USD"


class _FakeAccount:
    def __init__(self, account_number: str = "PA-TEST-0001"):
        self.account_number = account_number
        self.status = "ACTIVE"


class _FakePublishClient:
    """Fake alpaca-py TradingClient exposing exactly the surface
    ``publish_stop_coverage_report()``'s call chain needs: ``get_account``,
    ``get_all_positions``, ``get_asset``, ``get_orders``."""

    def __init__(
        self,
        account_number: str = "PA-TEST-0001",
        assets: dict[str, object] | None = None,
        positions: dict[str, float] | None = None,
        orders: list[object] | None = None,
    ) -> None:
        self._account = _FakeAccount(account_number)
        self._assets = assets or {}
        self._positions = positions or {}
        self._orders = orders or []
        self.get_orders_calls = 0
        self.get_all_positions_calls = 0

    def get_account(self):
        return self._account

    def get_asset(self, symbol: str):
        if symbol not in self._assets:
            raise RuntimeError(f"unknown asset {symbol}")
        return self._assets[symbol]

    def get_all_positions(self):
        self.get_all_positions_calls += 1
        return [
            SimpleNamespace(
                symbol=sym,
                qty=qty,
                qty_available=qty,
                market_value=qty * 60000.0,
                avg_entry_price=60000.0,
                unrealized_pl=0.0,
            )
            for sym, qty in self._positions.items()
            if qty > 0
        ]

    def get_orders(self, filter=None):  # noqa: A002 — SDK argument name
        self.get_orders_calls += 1
        return list(self._orders)


def _crypto_asset(**overrides) -> SimpleNamespace:
    fields = {
        "fractionable": False,
        "min_order_size": 0.0001,
        "min_trade_increment": 0.0001,
        "price_increment": 0.01,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _broker(client: _FakePublishClient, paper: bool = True) -> AlpacaBroker:
    broker = AlpacaBroker(paper=paper, label="alpaca-crypto-test")
    broker._trading_client = client  # noqa: SLF001 — inject fake, skip connect()
    broker._account = client.get_account()  # noqa: SLF001
    return broker


def _stop_order(
    order_id: str,
    symbol: str,
    qty: float,
    stop_price: float = 59000.0,
    limit_price: float = 58500.0,
    status: str = "accepted",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        status=status,
        symbol=symbol,
        side="SELL",
        qty=qty,
        filled_qty=0.0,
        filled_avg_price=0.0,
        order_type="stop_limit",
        time_in_force="gtc",
        stop_price=stop_price,
        limit_price=limit_price,
        created_at="",
        submitted_at="",
        filled_at=None,
        asset_class="crypto",
    )


# ---------------------------------------------------------------------------
# All positions covered
# ---------------------------------------------------------------------------


def test_all_covered_yields_zero_violations():
    client = _FakePublishClient(
        assets={BTC: _crypto_asset(), ETH: _crypto_asset()},
        positions={BTC: 0.5, ETH: 2.0},
        orders=[
            _stop_order("stop-btc", BTC, 0.5),
            _stop_order("stop-eth", ETH, 2.0),
        ],
    )
    broker = _broker(client)
    report = broker.publish_stop_coverage_report()

    assert isinstance(report, CoverageReport)
    assert verify_coverage_report(report)
    assert report.violations == 0
    assert report.positions_total == 2
    assert report.positions_covered == 2
    assert set(report.order_ids) == {"stop-btc", "stop-eth"}
    assert report.account_id == "PA-TEST-0001"
    assert report.environment == "paper"
    assert report.execution_version  # auto-derived, non-empty
    assert report.observation_timestamp_utc is not None


def test_covered_report_hashes_bind_to_the_real_observation():
    """position_snapshot_hash / order_snapshot_hash must equal
    compute_snapshot_hash() of the ACTUAL observed positions/orders --
    proving they are derived from the real broker query, not arbitrary."""
    client = _FakePublishClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[_stop_order("stop-btc", BTC, 0.5)],
    )
    broker = _broker(client)
    report = broker.publish_stop_coverage_report()

    expected_positions_hash = compute_snapshot_hash({BTC: 0.5})
    assert report.position_snapshot_hash == expected_positions_hash

    observed_order = client._orders[0]  # noqa: SLF001 - test introspection
    from renquant_execution.alpaca_broker import _order_to_dict

    order_dict = _order_to_dict(observed_order)
    order_dict["status"] = "accepted"
    order_dict["side"] = "SELL"
    order_dict["order_type"] = "stop_limit"
    order_dict["time_in_force"] = "gtc"
    order_dict["stop_price"] = 59000.0
    order_dict["limit_price"] = 58500.0
    expected_orders_hash = compute_snapshot_hash({BTC: [order_dict]})
    assert report.order_snapshot_hash == expected_orders_hash


# ---------------------------------------------------------------------------
# Uncovered position
# ---------------------------------------------------------------------------


def test_uncovered_position_yields_real_violation():
    client = _FakePublishClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[],
    )
    broker = _broker(client)
    report = broker.publish_stop_coverage_report()

    assert verify_coverage_report(report)
    assert report.violations == 1
    assert report.positions_total == 1
    assert report.positions_covered == 0
    assert report.order_ids == ()


def test_partial_coverage_shortfall_counts_as_one_violation():
    client = _FakePublishClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[_stop_order("stop-partial", BTC, 0.3)],
    )
    broker = _broker(client)
    report = broker.publish_stop_coverage_report()

    assert verify_coverage_report(report)
    assert report.violations == 1
    assert report.positions_covered == 0
    # The partial (insufficient) stop IS still a "qualifying" order shape,
    # so its id is captured in the observation even though it doesn't cover.
    assert report.order_ids == ("stop-partial",)


# ---------------------------------------------------------------------------
# Duplicate / racing protective stops (PR #31 "count not sum" semantics)
# ---------------------------------------------------------------------------


def test_duplicate_competing_stops_count_as_one_violation_not_two():
    client = _FakePublishClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[
            _stop_order("stop-a", BTC, 0.5),
            _stop_order("stop-b", BTC, 0.5),
        ],
    )
    broker = _broker(client)
    report = broker.publish_stop_coverage_report()

    assert verify_coverage_report(report)
    # ONE symbol with a "duplicate" violation, not two violations — the
    # violations == positions_total - positions_covered invariant
    # (CoverageReport.__post_init__) forces this to be counted per-SYMBOL.
    assert report.positions_total == 1
    assert report.violations == 1
    assert report.positions_covered == 0
    # Both competing order ids are still captured as evidence of what was
    # actually observed (ambiguous coverage, ids not silently dropped).
    assert set(report.order_ids) == {"stop-a", "stop-b"}


# ---------------------------------------------------------------------------
# Zero positions
# ---------------------------------------------------------------------------


def test_zero_crypto_positions_yields_empty_report():
    client = _FakePublishClient(positions={}, orders=[])
    broker = _broker(client)
    report = broker.publish_stop_coverage_report()

    assert verify_coverage_report(report)
    assert report.positions_total == 0
    assert report.positions_covered == 0
    assert report.violations == 0
    assert report.order_ids == ()
    # No crypto positions -> the open-orders query is never even issued
    # (mirrors check_crypto_stop_coverage()'s existing early-return).
    assert client.get_orders_calls == 0


# ---------------------------------------------------------------------------
# check_crypto_stop_coverage() and publish_stop_coverage_report() share ONE
# bounded observation (Codex finding 2) — not two independent round-trips.
# ---------------------------------------------------------------------------


def test_check_crypto_stop_coverage_and_publish_share_one_query_per_call():
    client = _FakePublishClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[_stop_order("stop-btc", BTC, 0.5)],
    )
    broker = _broker(client)

    violations = broker.check_crypto_stop_coverage()
    assert violations == []
    assert client.get_all_positions_calls == 1
    assert client.get_orders_calls == 1

    report = broker.publish_stop_coverage_report()
    assert report.violations == 0
    # publish_stop_coverage_report() issued exactly one MORE position query
    # and one MORE order query (its own independent call), not two extra —
    # i.e. it does not double-query within a single call.
    assert client.get_all_positions_calls == 2
    assert client.get_orders_calls == 2


# ---------------------------------------------------------------------------
# account_id / environment are broker-derived, never caller-supplied
# ---------------------------------------------------------------------------


def test_account_id_is_broker_derived_by_default():
    client = _FakePublishClient(account_number="PA-REAL-9999", positions={}, orders=[])
    broker = _broker(client)
    report = broker.publish_stop_coverage_report()
    assert report.account_id == "PA-REAL-9999"


def test_account_id_argument_is_a_consistency_assertion_not_an_override():
    client = _FakePublishClient(account_number="PA-REAL-9999", positions={}, orders=[])
    broker = _broker(client)

    # Passing the CORRECT account_id is a harmless no-op assertion.
    report = broker.publish_stop_coverage_report(account_id="PA-REAL-9999")
    assert report.account_id == "PA-REAL-9999"

    # Passing a DIFFERENT account_id must fail loud, never silently adopt
    # the caller's value.
    with pytest.raises(ValueError, match="account_id mismatch"):
        broker.publish_stop_coverage_report(account_id="SOME-OTHER-ACCOUNT")


def test_environment_reflects_broker_paper_flag():
    client = _FakePublishClient(positions={}, orders=[])
    paper_report = _broker(client, paper=True).publish_stop_coverage_report()
    assert paper_report.environment == "paper"

    live_client = _FakePublishClient(positions={}, orders=[])
    live_report = _broker(live_client, paper=False).publish_stop_coverage_report()
    assert live_report.environment == "live"


# ---------------------------------------------------------------------------
# The core Codex-review guarantee: a caller cannot authorize a zero-
# violation report by hand-picking field values through a PUBLIC function.
# ---------------------------------------------------------------------------


def test_build_coverage_report_removed_from_public_api():
    """``build_coverage_report`` -- the general caller-populated builder
    Codex's review flagged as the fabrication vector -- does not exist
    anywhere, at the package level or the module level; only the private
    ``_build_coverage_report`` does. ``CoverageObservation`` is likewise
    excluded from ``renquant_execution``'s package exports and from this
    module's ``__all__``."""
    import renquant_execution

    assert not hasattr(renquant_execution, "build_coverage_report")
    assert not hasattr(renquant_execution, "CoverageObservation")

    import renquant_execution.coverage_report as cr_module

    assert not hasattr(cr_module, "build_coverage_report")
    assert "build_coverage_report" not in cr_module.__all__
    assert "CoverageObservation" not in cr_module.__all__
    # verify_coverage_report/CoverageReport remain the legitimate public
    # surface for a consumer that loads a serialized report.
    assert "CoverageReport" in cr_module.__all__
    assert "verify_coverage_report" in cr_module.__all__

    with pytest.raises(ImportError):
        from renquant_execution.coverage_report import (  # noqa: F401
            build_coverage_report,
        )
    assert hasattr(cr_module, "_build_coverage_report")


def test_publish_stop_coverage_report_signature_has_no_field_overrides():
    """The only parameter accepted is a same-account consistency assertion
    -- there is no keyword to inject violations/positions_covered/
    positions_total/order_ids."""
    import inspect

    sig = inspect.signature(AlpacaBroker.publish_stop_coverage_report)
    params = set(sig.parameters) - {"self"}
    assert params == {"account_id"}


def test_caller_cannot_authorize_zero_violations_when_broker_state_is_uncovered():
    """The real (uncovered) broker state is truthfully reported every time
    -- there is no argument, override, or repeated call that flips it to
    zero violations while the fake broker's position/order state is
    unchanged."""
    client = _FakePublishClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[],
    )
    broker = _broker(client)

    report = broker.publish_stop_coverage_report()
    assert report.violations == 1
    assert report.positions_covered == 0

    # Calling again (idempotent w.r.t. broker state) still reports the truth.
    again = broker.publish_stop_coverage_report()
    assert again.violations == 1
    assert again.report_id != report.report_id  # fresh identity each call

    # A caller CAN still hand-construct a bare CoverageReport (the dataclass
    # is legitimately public -- a consumer needs it to deserialize a stored
    # report), but doing so requires supplying every field by hand,
    # including a self-consistent SHA-256 integrity_hash it must reimplement
    # from scratch; there is no public one-call function (like the old
    # build_coverage_report) that hands them a ready-made "0 violations"
    # report. Constructing one by hand doesn't change what the REAL broker
    # query returns, and is trivially distinguishable: a placeholder hash
    # fails verification immediately.
    forged = CoverageReport(
        report_id="00000000-0000-0000-0000-000000000000",
        timestamp_utc=report.timestamp_utc,
        observation_timestamp_utc=report.observation_timestamp_utc,
        account_id=report.account_id,
        environment=report.environment,
        positions_covered=1,
        positions_total=1,
        violations=0,
        order_ids=(),
        source_version=report.source_version,
        execution_version=report.execution_version,
        position_snapshot_hash=compute_snapshot_hash({BTC: 0.5}),
        order_snapshot_hash=compute_snapshot_hash({}),
        integrity_hash="0" * 64,  # caller has no way to derive the real hash
    )
    assert not verify_coverage_report(forged)
    # Even if a caller went further and correctly recomputed the hash
    # themselves, that would still never be what
    # publish_stop_coverage_report() -- the only broker-owned path --
    # actually returns for this account's real, still-uncovered state:
    assert broker.publish_stop_coverage_report().violations == 1
