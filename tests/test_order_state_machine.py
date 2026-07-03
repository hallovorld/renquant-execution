"""Acceptance tests for the Stage-1 order-lifecycle state machine.

RFC #208 SS8 row 1: state-machine unit tests; duplicate child-id rejected;
partial-fill + remainder accounting (including the SS7 worked example:
10-request / 4-fill / 6-cancel sums gross to 16 while economic stays <= 10);
restart reconcile; watchdog cancel fires between ticks. Plus the #223 A2
amendments: exits-always-allowed precedence and the verified intraday-margin
regime (no PDT counting).
"""
from __future__ import annotations

import datetime as dt

import pytest

from renquant_execution.order_state_machine import (
    BrokerRegimeSnapshot,
    ChildOrderState,
    DuplicateChildOrderError,
    EconomicInvariantError,
    EntryBlockedError,
    IntradayEntryEnvelope,
    LifecycleError,
    LifecycleState,
    MAX_PENDING_AGE_SECONDS,
    OrderStateBook,
    child_order_id,
    compute_parent_intent_id,
    evaluate_entry_headroom,
    reconcile_on_restart,
    run_stale_watchdog,
    submit_remainder,
)

T0 = dt.datetime(2026, 7, 2, 14, 0, tzinfo=dt.timezone.utc)


def _book() -> OrderStateBook:
    return OrderStateBook(account="acct-1", trading_day="2026-07-02")


def _buy(book: OrderStateBook, symbol: str = "NVDA", target: float = 10, version: str = "sig-v1"):
    return book.register_intent(
        symbol=symbol, side="BUY", signal_version=version, target_qty=target
    )


class FakeBroker:
    """In-memory BrokerPort double; enforces unique client-order-ids."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.submits: list[str] = []
        self.cancels: list[str] = []

    def submit_order(self, *, client_order_id, symbol, side, qty):
        if client_order_id in self.orders:
            raise ValueError(f"duplicate client_order_id: {client_order_id}")
        self.orders[client_order_id] = {
            "status": "open",
            "symbol": symbol,
            "side": side,
            "qty": float(qty),
            "filled_qty": 0.0,
        }
        self.submits.append(client_order_id)
        return {"status": "accepted", "client_order_id": client_order_id}

    def fill(self, client_order_id: str, qty: float) -> None:
        row = self.orders[client_order_id]
        row["filled_qty"] += qty
        if row["filled_qty"] >= row["qty"]:
            row["status"] = "filled"

    def set_status(self, client_order_id: str, status: str) -> None:
        self.orders[client_order_id]["status"] = status

    def cancel_order(self, client_order_id):
        row = self.orders[client_order_id]
        self.cancels.append(client_order_id)
        if row["status"] == "open":
            row["status"] = "canceled"
        return {"status": row["status"], "filled_qty": row["filled_qty"]}

    def open_orders(self):
        return {
            cid: row["qty"] - row["filled_qty"]
            for cid, row in self.orders.items()
            if row["status"] == "open"
        }

    def order_status(self, client_order_id):
        row = self.orders[client_order_id]
        return {"status": row["status"], "filled_qty": row["filled_qty"]}


# ---------------------------------------------------------------------------
# Two-level id
# ---------------------------------------------------------------------------


def test_parent_intent_id_deterministic_and_field_sensitive():
    base = dict(
        account="acct-1",
        symbol="NVDA",
        trading_day="2026-07-02",
        side="BUY",
        signal_version="sig-v1",
    )
    pid = compute_parent_intent_id(**base)
    assert pid == compute_parent_intent_id(**base)
    assert pid == compute_parent_intent_id(**{**base, "symbol": "nvda"})  # normalized
    for key, other in [
        ("account", "acct-2"),
        ("symbol", "AMD"),
        ("trading_day", "2026-07-03"),
        ("side", "SELL"),
        ("signal_version", "sig-v2"),
    ]:
        assert pid != compute_parent_intent_id(**{**base, key: other})


def test_child_order_id_is_parent_plus_attempt():
    assert child_order_id("pi-abc", 3) == "pi-abc:3"


# ---------------------------------------------------------------------------
# State machine walk + dedup
# ---------------------------------------------------------------------------


def test_lifecycle_happy_path_states():
    book = _book()
    unknown = compute_parent_intent_id(
        account="acct-1", symbol="NVDA", trading_day="2026-07-02",
        side="BUY", signal_version="sig-v1",
    )
    assert book.lifecycle_state(unknown) is LifecycleState.NONE

    parent = _buy(book)
    assert parent.state is LifecycleState.INTENDED

    child = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    assert child.child_order_id == f"{parent.parent_intent_id}:1"
    assert parent.state is LifecycleState.SUBMITTED

    book.on_broker_ack(child.child_order_id)
    assert parent.state is LifecycleState.ACCEPTED

    book.on_fill(child.child_order_id, 4)
    assert parent.state is LifecycleState.PARTIALLY_FILLED
    assert parent.cum_filled == 4
    assert parent.open_qty == 6

    book.on_fill(child.child_order_id, 6)
    assert parent.state is LifecycleState.FILLED
    assert child.state is ChildOrderState.FILLED
    assert parent.remaining_unsubmitted == 0
    assert not book.can_emit_remainder(parent.parent_intent_id)


def test_register_intent_dedup_idempotent_and_target_immutable():
    book = _book()
    a = _buy(book, target=10)
    b = _buy(book, target=10)
    assert a is b
    with pytest.raises(LifecycleError, match="different target_qty"):
        _buy(book, target=12)


def test_one_filled_position_per_name_per_session():
    book = _book()
    first = _buy(book, version="sig-v1")
    book.submit_child(first.parent_intent_id, qty=10, price=50.0, now=T0)
    cid = first.children[0].child_order_id
    book.on_fill(cid, 4)
    # second BUY decision for the same name, different signal_version
    with pytest.raises(LifecycleError, match="one filled position per name"):
        _buy(book, version="sig-v2")
    # but a name whose only parent had zero economic effect can re-decide
    book2 = _book()
    ghost = _buy(book2, version="sig-v1")
    child = book2.submit_child(ghost.parent_intent_id, qty=10, price=50.0, now=T0)
    book2.on_reject(child.child_order_id)
    assert _buy(book2, version="sig-v2") is not ghost


def test_rejected_branch_and_states():
    book = _book()
    parent = _buy(book)
    child = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    book.on_reject(child.child_order_id)
    assert parent.state is LifecycleState.REJECTED
    assert parent.cum_rejected == 10
    assert parent.gross_submitted_qty == 10
    # rejected remainder stays re-emit eligible (fresh child id)
    assert book.can_emit_remainder(parent.parent_intent_id)
    retry = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    assert retry.attempt_n == 2
    assert retry.child_order_id != child.child_order_id


# ---------------------------------------------------------------------------
# Economic + audit invariants (incl. the SS7 worked example)
# ---------------------------------------------------------------------------


def test_worked_example_10_request_4_fill_6_cancel_gross_16_economic_le_10():
    """RFC SS7: target 10; child1 requests 10, fills 4, cancels 6; child2
    requests the remainder 6 -> gross sums to 16 while economic stays <= 10."""
    book = _book()
    parent = _buy(book, target=10)
    gross_seen = []

    def economic():
        return parent.cum_filled + parent.open_qty

    c1 = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    gross_seen.append(parent.gross_submitted_qty)
    assert economic() <= 10

    book.on_broker_ack(c1.child_order_id)
    book.on_fill(c1.child_order_id, 4)
    gross_seen.append(parent.gross_submitted_qty)
    assert economic() <= 10

    book.on_cancel(c1.child_order_id)
    gross_seen.append(parent.gross_submitted_qty)
    assert parent.cum_canceled == 6
    # canceled qty does NOT reduce target: recovered via remaining_unsubmitted
    assert parent.remaining_unsubmitted == 6
    assert economic() <= 10

    c2 = book.submit_child(parent.parent_intent_id, qty=6, price=50.0, now=T0)
    assert c2.child_order_id == f"{parent.parent_intent_id}:2"
    gross_seen.append(parent.gross_submitted_qty)
    assert parent.gross_submitted_qty == 16  # audit invariant MAY exceed target
    assert economic() == 10  # economic invariant at its cap, never above

    book.on_fill(c2.child_order_id, 6)
    gross_seen.append(parent.gross_submitted_qty)
    assert parent.cum_filled == 10  # retries never overfill
    assert parent.state is LifecycleState.FILLED
    assert parent.gross_submitted_qty == 16
    # audit invariant is monotone non-decreasing at every step
    assert gross_seen == sorted(gross_seen)
    assert gross_seen == [10, 10, 10, 16, 16]


def test_hard_assertion_before_every_submit():
    book = _book()
    parent = _buy(book, target=10)
    c1 = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    # one OPEN child max per parent
    with pytest.raises(LifecycleError, match="one OPEN child"):
        book.submit_child(parent.parent_intent_id, qty=1, price=50.0, now=T0)
    book.on_fill(c1.child_order_id, 4)
    book.on_cancel(c1.child_order_id)
    # a child larger than remaining_unsubmitted would overfill -> refused
    with pytest.raises(EconomicInvariantError, match="remaining_unsubmitted"):
        book.submit_child(parent.parent_intent_id, qty=7, price=50.0, now=T0)
    # target reached -> parent stops
    c2 = book.submit_child(parent.parent_intent_id, qty=6, price=50.0, now=T0)
    book.on_fill(c2.child_order_id, 6)
    with pytest.raises(LifecycleError, match="target already reached"):
        book.submit_child(parent.parent_intent_id, qty=1, price=50.0, now=T0)


def test_overfill_on_child_rejected():
    book = _book()
    parent = _buy(book, target=10)
    child = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    with pytest.raises(EconomicInvariantError, match="exceed requested"):
        book.on_fill(child.child_order_id, 11)


def test_canceled_remainder_eligibility_requires_gate_admission():
    book = _book()
    parent = _buy(book, target=10)
    child = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    book.on_fill(child.child_order_id, 4)
    book.on_cancel(child.child_order_id)
    assert parent.state is LifecycleState.CANCELED
    # eligible iff still gate-admitted (SS7 canceled-remainder policy)
    assert book.can_emit_remainder(parent.parent_intent_id, gate_admitted=True)
    assert not book.can_emit_remainder(parent.parent_intent_id, gate_admitted=False)
    with pytest.raises(EntryBlockedError, match="gate_stack_rejected"):
        book.submit_child(
            parent.parent_intent_id, qty=6, price=50.0, now=T0, gate_admitted=False
        )


# ---------------------------------------------------------------------------
# Duplicate child-id rejected
# ---------------------------------------------------------------------------


def test_every_submission_gets_fresh_unique_child_id_at_broker():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    c1 = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    book.on_fill(c1.child_order_id, 4)
    book.on_cancel(c1.child_order_id)
    broker.set_status(c1.child_order_id, "canceled")
    c2 = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert c2.child_order_id != c1.child_order_id
    assert broker.submits == [c1.child_order_id, c2.child_order_id]


def test_broker_duplicate_client_order_id_rejected_and_audited():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    c1 = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    # simulate a double-fire replaying the same client-order-id at the broker
    with pytest.raises(ValueError, match="duplicate client_order_id"):
        broker.submit_order(
            client_order_id=c1.child_order_id, symbol="NVDA", side="BUY", qty=10
        )
    # and a broker-side submit failure is recorded as a REJECTED child
    book.on_cancel(c1.child_order_id)
    broker.set_status(c1.child_order_id, "canceled")

    class ExplodingBroker(FakeBroker):
        def submit_order(self, **kwargs):
            raise ValueError("duplicate client_order_id (simulated)")

    with pytest.raises(ValueError):
        submit_remainder(book, ExplodingBroker(), parent.parent_intent_id, price=50.0, now=T0)
    assert parent.children[-1].state is ChildOrderState.REJECTED
    assert parent.cum_rejected == 10


def test_snapshot_integrity_duplicate_child_id_refused():
    book = _book()
    parent = _buy(book, target=10)
    book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    snap = book.to_snapshot()
    snap["parents"][0]["children"].append(dict(snap["parents"][0]["children"][0]))
    with pytest.raises(DuplicateChildOrderError):
        OrderStateBook.from_snapshot(snap)


# ---------------------------------------------------------------------------
# Reserved-cash accounting
# ---------------------------------------------------------------------------


def test_reserved_cash_tracks_open_buy_children_only():
    book = _book()
    buy = _buy(book, symbol="NVDA", target=10)
    sell = book.register_intent(
        symbol="MU", side="SELL", signal_version="exit-v1", target_qty=4
    )
    assert book.reserved_cash() == 0.0
    book.submit_child(buy.parent_intent_id, qty=10, price=50.0, now=T0)
    book.submit_child(sell.parent_intent_id, qty=4, price=100.0, now=T0)
    # sells free exposure, they never reserve cash
    assert book.reserved_cash() == 10 * 50.0
    # a partial keeps its unfilled remainder reserved
    book.on_fill(buy.children[0].child_order_id, 4)
    assert book.reserved_cash() == 6 * 50.0
    # unsettled buys stack on top; available never uses raw broker cash
    assert book.reserved_cash(unsettled_buys=100.0) == 6 * 50.0 + 100.0
    assert book.available_cash(1000.0) == 1000.0 - 300.0
    # cancel releases the reservation
    book.on_cancel(buy.children[0].child_order_id)
    assert book.reserved_cash() == 0.0


# ---------------------------------------------------------------------------
# Watchdog: timer-driven stale-pending cancel between ticks
# ---------------------------------------------------------------------------


def test_watchdog_cancels_stale_pending_between_ticks():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)

    # inside max age: nothing to do
    assert run_stale_watchdog(book, broker, now=T0 + dt.timedelta(seconds=540)) == []
    assert child.is_open

    # 11 min > the 10-min SS10 default: watchdog (not the next tick) cancels
    resolved = run_stale_watchdog(book, broker, now=T0 + dt.timedelta(seconds=660))
    assert [c.child_order_id for c in resolved] == [child.child_order_id]
    assert child.state is ChildOrderState.CANCELED
    assert broker.cancels == [child.child_order_id]
    assert parent.cum_canceled == 10
    # the next tick inherits NO overdue order and may re-emit the remainder
    assert book.open_children() == []
    assert book.can_emit_remainder(parent.parent_intent_id)
    assert MAX_PENDING_AGE_SECONDS == 600.0


def test_watchdog_stale_cancel_racing_fill_resolves_to_filled():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    # fills arrive at the broker before the cancel lands
    broker.fill(child.child_order_id, 10)
    resolved = run_stale_watchdog(book, broker, now=T0 + dt.timedelta(seconds=700))
    assert [c.child_order_id for c in resolved] == [child.child_order_id]
    assert child.state is ChildOrderState.FILLED
    assert parent.state is LifecycleState.FILLED
    assert parent.cum_filled == 10
    assert parent.cum_canceled == 0


def test_stale_pending_partial_fill_then_cancel_keeps_accounting():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    broker.fill(child.child_order_id, 4)
    resolved = run_stale_watchdog(book, broker, now=T0 + dt.timedelta(seconds=700))
    assert resolved[0].state is ChildOrderState.CANCELED
    assert parent.cum_filled == 4
    assert parent.cum_canceled == 6
    assert parent.remaining_unsubmitted == 6
    assert parent.gross_submitted_qty == 10


# ---------------------------------------------------------------------------
# Restart reconcile (reconcile-before-emit)
# ---------------------------------------------------------------------------


def test_restored_book_refuses_all_emits_until_reconciled():
    book = _book()
    parent = _buy(book, target=10)
    book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    restored = OrderStateBook.from_snapshot(book.to_snapshot())
    assert restored.needs_reconcile
    sell = restored.register_intent(
        symbol="MU", side="SELL", signal_version="exit-v1", target_qty=4
    )
    # reconcile-before-emit blocks BOTH sides: the in-flight set is unknown
    with pytest.raises(LifecycleError, match="reconcile-before-emit"):
        restored.submit_child(sell.parent_intent_id, qty=4, price=100.0, now=T0)
    pid = parent.parent_intent_id
    assert not restored.can_emit_remainder(pid)


def test_restart_reconcile_resolves_terminal_outcomes_then_allows_emit():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    snap = book.to_snapshot()

    # while the process was down: 4 filled, then the order was canceled
    broker.fill(child.child_order_id, 4)
    broker.set_status(child.child_order_id, "canceled")

    restored = OrderStateBook.from_snapshot(snap)
    result = reconcile_on_restart(restored, broker)
    assert result.clean
    assert not restored.needs_reconcile
    assert not restored.entries_halted
    rparent = restored.parent(parent.parent_intent_id)
    assert rparent.cum_filled == 4
    assert rparent.cum_canceled == 6
    assert rparent.remaining_unsubmitted == 6
    # in-flight set rebuilt; remainder emit now allowed with a fresh child id
    c2 = submit_remainder(restored, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert c2.attempt_n == 2


def test_restart_reconcile_open_order_survives_and_matches():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    restored = OrderStateBook.from_snapshot(book.to_snapshot())
    result = reconcile_on_restart(restored, broker)
    assert result.clean
    rparent = restored.parent(parent.parent_intent_id)
    assert rparent.open_qty == 10
    assert rparent.open_child.child_order_id == child.child_order_id
    # dedup on parent_intent_id: no second open child may be emitted
    with pytest.raises(LifecycleError, match="one OPEN child"):
        restored.submit_child(parent.parent_intent_id, qty=1, price=50.0, now=T0)


def test_reconcile_mismatch_halts_entries_exits_still_allowed():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    restored = OrderStateBook.from_snapshot(book.to_snapshot())
    # an order at the broker the ledger does not know about
    broker.submit_order(client_order_id="rogue-1", symbol="AMD", side="BUY", qty=5)
    result = reconcile_on_restart(restored, broker)
    assert not result.clean
    kinds = {m.kind for m in result.mismatches}
    assert "unknown_broker_order" in kinds
    assert restored.entries_halted
    assert restored.halt_reason == "reconcile_mismatch"
    # new entries are refused for the session...
    buy2 = restored.register_intent(
        symbol="AMD", side="BUY", signal_version="sig-v9", target_qty=3
    )
    with pytest.raises(EntryBlockedError, match="reconcile_mismatch"):
        restored.submit_child(buy2.parent_intent_id, qty=3, price=10.0, now=T0)
    # ...but exits remain allowed (exits-always-allowed precedence)
    sell = restored.register_intent(
        symbol="MU", side="SELL", signal_version="exit-v1", target_qty=4
    )
    child = restored.submit_child(sell.parent_intent_id, qty=4, price=100.0, now=T0)
    assert child.state is ChildOrderState.SUBMITTED


def test_reconcile_qty_mismatch_detected():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    restored = OrderStateBook.from_snapshot(book.to_snapshot())
    # broker shows a different unfilled quantity than the ledger
    broker.orders[child.child_order_id]["filled_qty"] = 3.0
    result = restored.reconcile(broker.open_orders())
    assert not result.clean
    assert result.mismatches[0].kind == "qty_mismatch"
    assert restored.entries_halted


# ---------------------------------------------------------------------------
# A2: exits-always-allowed + verified intraday-margin regime (no PDT)
# ---------------------------------------------------------------------------


def test_entries_halted_never_blocks_exits():
    book = _book()
    book.halt_entries("intraday_margin_deficit")
    buy = _buy(book, target=10)
    with pytest.raises(EntryBlockedError, match="intraday_margin_deficit"):
        book.submit_child(buy.parent_intent_id, qty=10, price=50.0, now=T0)
    sell = book.register_intent(
        symbol="NVDA", side="SELL", signal_version="exit-v1", target_qty=4
    )
    child = book.submit_child(sell.parent_intent_id, qty=4, price=50.0, now=T0)
    assert child.state is ChildOrderState.SUBMITTED
    # gate admission is likewise an entry-only constraint: a halted book with
    # gates dropped still lets a fresh exit emit
    sell2 = book.register_intent(
        symbol="MU", side="SELL", signal_version="exit-v1", target_qty=2
    )
    assert book.can_emit_remainder(sell2.parent_intent_id, gate_admitted=False)


def test_entry_headroom_binds_on_buying_power_fields_not_pdt():
    envelope = IntradayEntryEnvelope(designed_account_type="margin", max_entry_fraction=0.15)
    regime = BrokerRegimeSnapshot(
        account_type="margin",
        non_marginable_buying_power=8300.0,
        pattern_day_trader=False,
        daytrade_count=0,
    )
    headroom = 0.15 * 8300.0
    ok = evaluate_entry_headroom(envelope, regime, entry_notional=headroom - 1, reserved_cash=0.0)
    assert ok.allowed and ok.reason == "ok"
    # open/pending buys consume headroom (consistent with reserved_cash)
    blocked = evaluate_entry_headroom(
        envelope, regime, entry_notional=headroom - 1, reserved_cash=500.0
    )
    assert not blocked.allowed
    assert blocked.reason == "insufficient_buying_power_headroom"
    # deprecated PDT fields are recorded but NEVER gate: decisions identical
    pdt_regime = BrokerRegimeSnapshot(
        account_type="margin",
        non_marginable_buying_power=8300.0,
        pattern_day_trader=True,
        daytrade_count=99,
    )
    for notional in (100.0, headroom - 1, headroom + 1):
        a = evaluate_entry_headroom(envelope, regime, entry_notional=notional, reserved_cash=0.0)
        b = evaluate_entry_headroom(envelope, pdt_regime, entry_notional=notional, reserved_cash=0.0)
        assert (a.allowed, a.reason) == (b.allowed, b.reason)


def test_regime_mismatch_and_margin_deficit_are_tier1_session_halts():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=10)
    envelope = IntradayEntryEnvelope(designed_account_type="margin", max_entry_fraction=0.15)
    cash_regime = BrokerRegimeSnapshot(account_type="cash", non_marginable_buying_power=8300.0)
    with pytest.raises(EntryBlockedError, match="broker_rule_regime_mismatch"):
        submit_remainder(
            book, broker, parent.parent_intent_id,
            price=50.0, now=T0, envelope=envelope, regime=cash_regime,
        )
    assert book.entries_halted  # verify-then-bind: session aborts entries
    assert book.halt_reason == "broker_rule_regime_mismatch"

    book2 = _book()
    parent2 = _buy(book2, target=10)
    deficit_regime = BrokerRegimeSnapshot(
        account_type="margin",
        non_marginable_buying_power=8300.0,
        intraday_margin_deficit=120.0,
    )
    with pytest.raises(EntryBlockedError, match="intraday_margin_deficit"):
        submit_remainder(
            book2, broker, parent2.parent_intent_id,
            price=50.0, now=T0, envelope=envelope, regime=deficit_regime,
        )
    assert book2.entries_halted


def test_insufficient_headroom_blocks_order_but_not_session():
    book = _book()
    broker = FakeBroker()
    parent = _buy(book, target=1000)
    envelope = IntradayEntryEnvelope(designed_account_type="margin", max_entry_fraction=0.15)
    regime = BrokerRegimeSnapshot(account_type="margin", non_marginable_buying_power=8300.0)
    with pytest.raises(EntryBlockedError, match="insufficient_buying_power_headroom"):
        submit_remainder(
            book, broker, parent.parent_intent_id,
            price=50.0, now=T0, envelope=envelope, regime=regime,
        )
    assert not book.entries_halted  # per-order refusal, not a Tier-1 halt
    # a small enough entry on the same book still goes through
    small = _buy(book, symbol="AMD", target=10, version="sig-v1")
    child = submit_remainder(
        book, broker, small.parent_intent_id,
        price=50.0, now=T0, envelope=envelope, regime=regime,
    )
    assert child is not None and child.state is ChildOrderState.SUBMITTED


def test_exits_never_routed_through_entry_envelope():
    book = _book()
    broker = FakeBroker()
    sell = book.register_intent(
        symbol="NVDA", side="SELL", signal_version="exit-v1", target_qty=100
    )
    envelope = IntradayEntryEnvelope(designed_account_type="margin", max_entry_fraction=0.15)
    # a regime that would hard-block any entry must not touch an exit
    bad_regime = BrokerRegimeSnapshot(
        account_type="cash",
        non_marginable_buying_power=0.0,
        intraday_margin_deficit=999.0,
    )
    child = submit_remainder(
        book, broker, sell.parent_intent_id,
        price=50.0, now=T0, envelope=envelope, regime=bad_regime,
    )
    assert child is not None and child.state is ChildOrderState.SUBMITTED
    assert not book.entries_halted


def test_regime_snapshot_run_bundle_record_marks_pdt_fields_deprecated():
    regime = BrokerRegimeSnapshot(
        account_type="margin",
        non_marginable_buying_power=8300.0,
        pattern_day_trader=False,
        daytrade_count=0,
    )
    record = regime.to_record()
    assert record["account_type"] == "margin"
    assert record["pattern_day_trader_deprecated"] is False
    assert record["daytrade_count_deprecated"] == 0
    assert "pattern_day_trader" not in record  # only the deprecated-marked keys


# ---------------------------------------------------------------------------
# Snapshot round-trip
# ---------------------------------------------------------------------------


def test_snapshot_round_trip_preserves_accounting():
    import json

    book = _book()
    parent = _buy(book, target=10)
    c1 = book.submit_child(parent.parent_intent_id, qty=10, price=50.0, now=T0)
    book.on_broker_ack(c1.child_order_id)
    book.on_fill(c1.child_order_id, 4)
    book.on_cancel(c1.child_order_id)
    book.submit_child(parent.parent_intent_id, qty=6, price=51.0, now=T0)

    snap = json.loads(json.dumps(book.to_snapshot()))  # JSON-serializable
    restored = OrderStateBook.from_snapshot(snap)
    rparent = restored.parent(parent.parent_intent_id)
    assert rparent.cum_filled == 4
    assert rparent.cum_canceled == 6
    assert rparent.open_qty == 6
    assert rparent.gross_submitted_qty == 16
    assert rparent.remaining_unsubmitted == 0
    assert restored.reserved_cash() == 6 * 51.0
    assert rparent.children[1].state is ChildOrderState.SUBMITTED
