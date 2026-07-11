"""Fee-inclusive reservation against the REAL canonical cost contract.

These tests run only where ``renquant_common.cost_model`` (common#28, D-C8a)
is importable — i.e. after the coordinated renquant-common release lands (or
against a checkout of its branch). They re-prove, against the REAL module,
exactly what tests/test_account_cash_ledger.py proves against the frozen-API
stub — including sha PARITY (both must equal the same hand-computed
canonical-JSON sha), so the stub can never silently drift from the contract.

Until that release is installed everywhere, the default environment still
proves the OTHER half of the requirement for real: the contract-absent paths
fail closed (test_account_cash_ledger.py::test_cost_contract_absent_*).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

cost_model = pytest.importorskip(
    "renquant_common.cost_model",
    reason="requires the renquant-common release carrying the D-C8a cost contract",
)

from renquant_execution.account_cash_ledger import (  # noqa: E402
    REQUIRED_COST_MODEL_FINGERPRINT_SCHEMA_VERSION,
    AccountCashLedger,
    load_cost_contract,
    worst_case_entry_debit,
)
from renquant_execution.order_state_machine import (  # noqa: E402
    EntryBlockedError,
    OrderStateBook,
    submit_remainder,
)

T0 = dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.timezone.utc)

FEE_SPEC_DICT = {"fee_bps": 25.0, "spread_bps": 10.0}
FULL_SPEC_DICT = {
    "fee_bps": 25.0,
    "spread_bps": 10.0,
    "slippage_bps": 0.0,
    "increment_rounding_bps": 0.0,
}
PER_SIDE_RATE = 30.0 / 1e4  # fee 25 + spread/2 5


def _hand_computed_sha(spec_dict: dict) -> str:
    blob = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class _FakeBroker:
    def __init__(self, client_reject: bool = False):
        self.submits: list[str] = []

    def submit_order(self, *, client_order_id, symbol, side, qty):
        self.submits.append(client_order_id)
        return {"status": "accepted"}


def test_real_contract_loads_and_pins_required_schema_version():
    contract = load_cost_contract()
    assert contract is cost_model
    assert (
        cost_model.COST_MODEL_FINGERPRINT_SCHEMA_VERSION
        == REQUIRED_COST_MODEL_FINGERPRINT_SCHEMA_VERSION
        == 1
    )


def test_real_contract_debit_and_sha_match_hand_computed_values():
    debit, sha, params_json = worst_case_entry_debit(10_000.0, FEE_SPEC_DICT)
    assert debit == pytest.approx(10_000.0 * (1.0 + PER_SIDE_RATE))  # 10030
    # sha PARITY: the real module's fingerprint equals the same
    # hand-computed canonical-JSON sha the stub tests pin — no drift room.
    assert sha == _hand_computed_sha(FULL_SPEC_DICT)
    assert sha == cost_model.cost_model_content_sha256(
        cost_model.cost_model_spec_from_dict(FEE_SPEC_DICT)
    )
    assert json.loads(params_json) == FULL_SPEC_DICT


def test_real_contract_accepts_spec_instance_and_dict_identically():
    spec = cost_model.CostModelSpec(fee_bps=25.0, spread_bps=10.0)
    d1 = worst_case_entry_debit(500.0, spec)
    d2 = worst_case_entry_debit(500.0, FEE_SPEC_DICT)
    assert d1 == d2


def test_real_contract_boundary_notional_fits_fees_do_not(tmp_path):
    # THE Codex boundary case against the REAL contract, end-to-end through
    # submit_remainder: 100.00 cash, 99.80 notional fits, 99.80 * 1.003 =
    # 100.0994 worst-case debit does not.
    ledger = AccountCashLedger(
        tmp_path / "account_cash_ledger.PA3REAL0001.db",
        account_id="PA3REAL0001",
        broker_cash_fn=lambda: 100.0,
    )
    book = OrderStateBook(
        account="alpaca",
        trading_day="2026-07-10",
        cash_ledger=ledger,
        cost_model_spec=dict(FEE_SPEC_DICT),
    )
    broker = _FakeBroker()
    parent = book.register_intent(
        symbol="NVDA", side="BUY", signal_version="sig-v1", target_qty=1.0
    )
    with pytest.raises(EntryBlockedError) as excinfo:
        submit_remainder(book, broker, parent.parent_intent_id, price=99.80, now=T0)
    assert excinfo.value.reason == "insufficient_buying_power_headroom"
    assert broker.submits == []
    assert ledger.reservation(parent.parent_intent_id) is None
    # zero-cost control: the SAME notional is granted, isolating the fees
    # as the only reason for the refusal above
    control = AccountCashLedger(
        tmp_path / "account_cash_ledger.PA3CTRL0001.db",
        account_id="PA3CTRL0001",
        broker_cash_fn=lambda: 100.0,
    )
    assert control.reserve_entry(
        sleeve_tag="alpaca",
        parent_intent_id=parent.parent_intent_id,
        notional=99.80,
        cost_spec=cost_model.CostModelSpec(),
    )


def test_real_contract_reservation_row_stamped(tmp_path):
    ledger = AccountCashLedger(
        tmp_path / "account_cash_ledger.PA3REAL0001.db",
        account_id="PA3REAL0001",
        broker_cash_fn=lambda: 1_000.0,
    )
    assert ledger.reserve_entry(
        sleeve_tag="alpaca_crypto",
        parent_intent_id="pi-real",
        notional=500.0,
        cost_spec=FEE_SPEC_DICT,
        now=T0,
    )
    row = ledger.reservation("pi-real")
    assert row.amount == pytest.approx(500.0 * (1.0 + PER_SIDE_RATE))  # 501.5
    assert row.cost_model_sha256 == _hand_computed_sha(FULL_SPEC_DICT)
    assert json.loads(row.cost_model_params) == FULL_SPEC_DICT
