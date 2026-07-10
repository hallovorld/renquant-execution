"""Acceptance tests for the §5.3 account-scoped cash reservation ledger.

Crypto RFC (renquant-orchestrator ``doc/design/2026-07-10-crypto-trading-rfc.md``
§5.3 CORRECTED, deliverable D-C4): atomic account-wide reserve/release over
SQLite WAL; idempotent retries on the ``parent_intent_id`` key; TTL +
orphan-sweep surfacing (never silent auto-release); broker-cash recheck
immediately before submit; fail-closed-for-new-entries across EVERY sleeve on
unknown open buys / reconciliation mismatch; exits never blocked; flag-gated
default OFF = byte-identical.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import threading
import textwrap
from concurrent.futures import ThreadPoolExecutor

import pytest

from renquant_execution.account_cash_ledger import (
    ACCOUNT_CASH_LEDGER_FLAG,
    DEFAULT_RESERVATION_TTL_SECONDS,
    HALT_REASON_RECHECK_MISMATCH,
    HALT_REASON_UNKNOWN_OPEN_BUY,
    RESERVATION_GRACE_SECONDS,
    AccountCashLedger,
    AccountCashLedgerError,
    account_cash_ledger_db_path,
    account_cash_ledger_enabled,
    maybe_build_account_cash_ledger,
)
from renquant_execution.order_state_machine import (
    ACCOUNT_CASH_RECONCILE_MISMATCH_REASON,
    MAX_PENDING_AGE_SECONDS,
    ChildOrderState,
    EntryBlockedError,
    LifecycleState,
    OrderStateBook,
    parent_intent_id_from_client_order_id,
    reconcile_on_restart,
    submit_remainder,
)

T0 = dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.timezone.utc)
ACCOUNT = "PA3XXXX1"  # real brokerage account id, NEVER a broker tag


class _BrokerCash:
    """Fake broker-cash feed: mutable balance + fetch counter.

    ``queue`` (optional) overrides the balance per call, so a test can make
    the balance move BETWEEN reserve() and the pre-submit recheck.
    """

    def __init__(self, balance: float, queue: list[float] | None = None):
        self.balance = float(balance)
        self.queue = list(queue) if queue is not None else None
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self.queue:
            self.balance = self.queue.pop(0)
        return self.balance


def _ledger(
    tmp_path, cash: "_BrokerCash | float" = 1_000.0, **kwargs
) -> "tuple[AccountCashLedger, _BrokerCash]":
    feed = cash if isinstance(cash, _BrokerCash) else _BrokerCash(cash)
    ledger = AccountCashLedger(
        tmp_path / f"account_cash_ledger.{ACCOUNT}.db",
        account_id=ACCOUNT,
        broker_cash_fn=feed,
        **kwargs,
    )
    return ledger, feed


class FakeBroker:
    """Minimal BrokerPort double (mirrors test_order_state_machine)."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.submits: list[str] = []

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

    def cancel_order(self, client_order_id):
        row = self.orders[client_order_id]
        row["status"] = "canceled"
        return {"status": "canceled", "filled_qty": row["filled_qty"]}

    def open_orders(self):
        return {
            cid: row["qty"] - row["filled_qty"]
            for cid, row in self.orders.items()
            if row["status"] == "open"
        }

    def order_status(self, client_order_id):
        row = self.orders[client_order_id]
        return {"status": row["status"], "filled_qty": row["filled_qty"]}


def _book(tag: str = "alpaca", ledger: AccountCashLedger | None = None) -> OrderStateBook:
    return OrderStateBook(account=tag, trading_day="2026-07-10", cash_ledger=ledger)


def _buy(book: OrderStateBook, symbol: str = "NVDA", target: float = 10.0):
    return book.register_intent(
        symbol=symbol, side="BUY", signal_version="sig-v1", target_qty=target
    )


def _sell(book: OrderStateBook, symbol: str = "NVDA", target: float = 10.0):
    return book.register_intent(
        symbol=symbol, side="SELL", signal_version="sig-v1", target_qty=target
    )


# ---------------------------------------------------------------------------
# Core ledger protocol
# ---------------------------------------------------------------------------


def test_reserve_grants_and_persists_row_with_ttl(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=400.0, now=T0
    )
    row = ledger.reservation("pi-1")
    assert row is not None
    assert row.status == "active"
    assert row.sleeve_tag == "alpaca"
    assert row.account_id == ACCOUNT
    assert row.amount == 400.0
    assert row.reserved_at == pytest.approx(T0.timestamp())
    assert row.expires_at == pytest.approx(
        T0.timestamp() + DEFAULT_RESERVATION_TTL_SECONDS
    )
    assert ledger.active_reserved_total(now=T0) == pytest.approx(400.0)


def test_ttl_default_reuses_order_timeout_convention():
    # RFC §5.3: "order timeout budget + a fixed grace margin — reuse whatever
    # order-submission timeout convention already exists", not a fresh number.
    assert DEFAULT_RESERVATION_TTL_SECONDS == (
        MAX_PENDING_AGE_SECONDS + RESERVATION_GRACE_SECONDS
    )
    assert RESERVATION_GRACE_SECONDS == MAX_PENDING_AGE_SECONDS / 2.0


def test_reserve_refuses_beyond_headroom_across_tags(tmp_path):
    # THE §5.3 bug: two sleeves sizing against the same real account. The SUM
    # is across ALL tags, so the second sleeve may not double-spend.
    ledger, _ = _ledger(tmp_path, 100.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-a", amount=60.0)
    assert not ledger.reserve(
        sleeve_tag="alpaca_crypto", parent_intent_id="pi-b", amount=60.0
    )
    # the refused attempt reserved nothing
    assert ledger.reservation("pi-b") is None
    assert ledger.active_reserved_total() == pytest.approx(60.0)


def test_reserve_exact_headroom_boundary(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-a", amount=100.0)
    assert not ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-b", amount=0.01
    )


def test_reserve_idempotent_retry_never_doubles(tmp_path):
    ledger, feed = _ledger(tmp_path, 100.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    # Retried call (timeout retry / crash-and-resubmit): no-op, same result,
    # never a second reservation of the same cash — even though a SECOND
    # 60.0 could not possibly fit in the remaining 40.0 headroom.
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    assert ledger.active_reserved_total() == pytest.approx(60.0)
    # A retry with a different amount is still a no-op on the ACTIVE row.
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=10.0)
    row = ledger.reservation("pi-1")
    assert row is not None and row.amount == 60.0


def test_refused_reserve_retry_reevaluates_fresh(tmp_path):
    ledger, feed = _ledger(tmp_path, 100.0)
    assert not ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=150.0
    )
    feed.balance = 200.0  # broker cash moved (a sell filled)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=150.0)


def test_reserve_after_release_is_fresh_headroom_check(tmp_path):
    # Ambiguity #1 (module docstring): a RELEASED row holds no cash, so a
    # re-emit's reserve() must re-check headroom and re-activate, never
    # blind-no-op True with no active reservation behind it.
    ledger, feed = _ledger(tmp_path, 100.0)
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0, now=T0
    )
    assert ledger.release("pi-1", reason="canceled")
    assert ledger.active_reserved_total() == pytest.approx(0.0)
    feed.balance = 50.0
    assert not ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0
    )
    feed.balance = 100.0
    t1 = T0 + dt.timedelta(seconds=30)
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0, now=t1
    )
    row = ledger.reservation("pi-1")
    assert row is not None
    assert row.status == "active"
    assert row.reserved_at == pytest.approx(t1.timestamp())  # fresh timestamps


def test_broker_cash_refetched_every_reserve_never_cached(tmp_path):
    ledger, feed = _ledger(tmp_path, 100.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    assert feed.calls == 1
    feed.balance = 70.0  # a fill on the OTHER sleeve moved real cash
    assert not ledger.reserve(
        sleeve_tag="alpaca_crypto", parent_intent_id="pi-2", amount=20.0
    )
    assert feed.calls == 2
    # idempotent no-op retries do NOT need (and do not make) a broker fetch
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    assert feed.calls == 2


def test_release_is_idempotent_and_unknown_is_noop(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    assert not ledger.release("pi-unknown", reason="canceled")  # no-op, no error
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    assert ledger.release("pi-1", reason="filled")
    assert not ledger.release("pi-1", reason="filled")  # racing hook: no-op
    row = ledger.reservation("pi-1")
    assert row is not None
    assert row.status == "released"
    assert row.release_reason == "filled"
    assert row.released_at is not None


def test_reserve_validates_inputs(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(AccountCashLedgerError):
            ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=bad)
    with pytest.raises(AccountCashLedgerError):
        ledger.reserve(sleeve_tag="", parent_intent_id="pi-1", amount=1.0)
    with pytest.raises(AccountCashLedgerError):
        ledger.reserve(sleeve_tag="alpaca", parent_intent_id="", amount=1.0)


# ---------------------------------------------------------------------------
# Atomicity under concurrency (threads + a second process)
# ---------------------------------------------------------------------------


def test_concurrent_reserves_exactly_one_wins(tmp_path):
    # Two sleeves race for headroom only one can have: 100 cash, 60 + 60.
    ledger, _ = _ledger(tmp_path, 100.0)
    barrier = threading.Barrier(2)

    def attempt(pid: str) -> bool:
        barrier.wait()
        return ledger.reserve(sleeve_tag=f"tag-{pid}", parent_intent_id=pid, amount=60.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["pi-a", "pi-b"]))
    assert sorted(results) == [False, True]
    assert ledger.active_reserved_total() == pytest.approx(60.0)


def test_concurrent_idempotent_retries_reserve_once(tmp_path):
    # Many racing retries of the SAME intent must all be granted but reserve
    # the cash exactly once (UPSERT-then-check).
    ledger, _ = _ledger(tmp_path, 100.0)
    barrier = threading.Barrier(4)

    def attempt(_: int) -> bool:
        barrier.wait()
        return ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=80.0)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results == [True, True, True, True]
    assert ledger.active_reserved_total() == pytest.approx(80.0)


def test_reservation_visible_to_second_process(tmp_path):
    # The 104 batch process and the crypto 24/7 loop are separate PROCESSES
    # sharing the db file (WAL): a reservation made here must refuse the
    # other process's over-committing reserve.
    ledger, _ = _ledger(tmp_path, 100.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-a", amount=60.0)
    script = textwrap.dedent(
        """
        import json, sys
        from renquant_execution.account_cash_ledger import AccountCashLedger
        ledger = AccountCashLedger(
            sys.argv[1], account_id=sys.argv[2], broker_cash_fn=lambda: 100.0
        )
        print(json.dumps({
            "over": ledger.reserve(
                sleeve_tag="alpaca_crypto", parent_intent_id="pi-b", amount=60.0
            ),
            "fits": ledger.reserve(
                sleeve_tag="alpaca_crypto", parent_intent_id="pi-c", amount=40.0
            ),
        }))
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(ledger.db_path), ACCOUNT],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    outcome = json.loads(proc.stdout)
    assert outcome == {"over": False, "fits": True}
    # ...and the second process's grant is visible back here.
    assert ledger.active_reserved_total() == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# TTL + orphan sweep
# ---------------------------------------------------------------------------


def test_expired_reservation_stops_debiting_but_stays_active(tmp_path):
    # RFC §5.3 check formula: SUM(active, NON-EXPIRED) — the TTL bounds how
    # long a crash can hold phantom headroom — but the row is NEVER silently
    # auto-released.
    ledger, _ = _ledger(tmp_path, 100.0)
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0, now=T0
    )
    later = T0 + dt.timedelta(seconds=DEFAULT_RESERVATION_TTL_SECONDS + 1)
    assert ledger.active_reserved_total(now=later) == pytest.approx(0.0)
    row = ledger.reservation("pi-1")
    assert row is not None and row.status == "active"  # NOT auto-released
    assert ledger.reserve(
        sleeve_tag="alpaca_crypto", parent_intent_id="pi-2", amount=90.0, now=later
    )


def test_sweep_surfaces_expired_unreleased_without_releasing(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0, now=T0
    )
    later = T0 + dt.timedelta(seconds=DEFAULT_RESERVATION_TTL_SECONDS + 1)
    # the broker order is still open and locally in flight: expired-but-live
    result = ledger.sweep(
        broker_open_buy_intents={"pi-1"},
        local_inflight_intents={"pi-1"},
        now=later,
    )
    assert result.expired_unreleased == ("pi-1",)
    assert result.orphans_released == ()
    assert not result.clean
    assert not result.halted
    row = ledger.reservation("pi-1")
    assert row is not None and row.status == "active"  # surfaced, not released


def test_sweep_releases_orphans_counted_and_reported(tmp_path):
    # Crashed between reserve and submit (or a missed release): no broker
    # order, no local lifecycle state -> released + counted, a reportable
    # defect — never a silent cleanup.
    ledger, _ = _ledger(tmp_path, 1_000.0)
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-orphan", amount=60.0, now=T0
    )
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-live", amount=40.0, now=T0
    )
    result = ledger.sweep(
        broker_open_buy_intents={"pi-live"},
        local_inflight_intents={"pi-live"},
        now=T0 + dt.timedelta(seconds=1),
    )
    assert result.orphans_released == ("pi-orphan",)
    assert not result.clean
    assert not result.halted  # orphans alone don't halt (sustained = Tier-1,
    # the caller's alerting policy) — unknown open buys DO halt (below)
    row = ledger.reservation("pi-orphan")
    assert row is not None
    assert row.status == "released"
    assert row.release_reason == "orphan_sweep"
    assert ledger.active_reserved_total(
        now=T0 + dt.timedelta(seconds=1)
    ) == pytest.approx(40.0)


def test_sweep_unknown_open_buy_fail_closes_every_sleeve(tmp_path):
    # The graver defect: a broker open BUY the ledger never reserved for —
    # an external/manual order or a path that submitted without reserving.
    # New entries halt for EVERY sleeve sharing the account, not just the
    # sleeve whose reconcile pass noticed.
    ledger, _ = _ledger(tmp_path, 1_000.0)
    result = ledger.sweep(
        broker_open_buy_intents={"manual-broker-uuid"},
        local_inflight_intents=set(),
    )
    assert result.unknown_open_buys == ("manual-broker-uuid",)
    assert result.halted
    assert result.halt_reason == HALT_REASON_UNKNOWN_OPEN_BUY
    assert not result.clean
    # fail-closed across sleeves: plenty of headroom, still refused
    assert not ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-1", amount=1.0
    )
    assert not ledger.reserve(
        sleeve_tag="alpaca_crypto", parent_intent_id="pi-2", amount=1.0
    )
    assert ledger.halt_state() == (True, HALT_REASON_UNKNOWN_OPEN_BUY)
    # explicit operator/reconciliation action re-opens
    ledger.clear_halt()
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=1.0)


def test_sweep_released_reservation_does_not_cover_open_buy(tmp_path):
    # Ambiguity #3: "no ledger reservation" means no ACTIVE one — an open
    # buy whose reservation was already released is the same headroom leak.
    ledger, _ = _ledger(tmp_path, 1_000.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    assert ledger.release("pi-1", reason="canceled")
    result = ledger.sweep(
        broker_open_buy_intents={"pi-1"}, local_inflight_intents=set()
    )
    assert result.unknown_open_buys == ("pi-1",)
    assert result.halted


def test_halt_preserves_first_reason(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    ledger.halt("first_reason")
    ledger.halt("second_reason")
    assert ledger.halt_state() == (True, "first_reason")


# ---------------------------------------------------------------------------
# Broker-cash recheck immediately before submit
# ---------------------------------------------------------------------------


def test_recheck_passes_when_ledger_covered(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    assert ledger.recheck_before_submit()
    assert ledger.halt_state() == (False, None)


def test_recheck_mismatch_halts_account_wide(tmp_path):
    ledger, feed = _ledger(tmp_path, 100.0)
    assert ledger.reserve(sleeve_tag="alpaca", parent_intent_id="pi-1", amount=60.0)
    feed.balance = 40.0  # something outside the ledger moved real cash
    assert not ledger.recheck_before_submit()
    assert ledger.halt_state() == (True, HALT_REASON_RECHECK_MISMATCH)
    # every sleeve is now fail-closed for NEW entries
    assert not ledger.reserve(
        sleeve_tag="alpaca_crypto", parent_intent_id="pi-2", amount=1.0
    )


# ---------------------------------------------------------------------------
# Ledger identity / storage contract
# ---------------------------------------------------------------------------


def test_db_is_wal_mode_and_canonical_path(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert account_cash_ledger_db_path(tmp_path, ACCOUNT) == (
        tmp_path / f"account_cash_ledger.{ACCOUNT}.db"
    )
    with pytest.raises(AccountCashLedgerError):
        account_cash_ledger_db_path(tmp_path, "../escape")


def test_account_mismatch_fails_loud(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    with pytest.raises(AccountCashLedgerError):
        AccountCashLedger(
            ledger.db_path, account_id="OTHER-ACCOUNT", broker_cash_fn=lambda: 1.0
        )


def test_halt_state_shared_between_instances(tmp_path):
    # Two ledger INSTANCES (as two processes would hold) share halt state
    # through the db, not through Python object state.
    ledger_a, _ = _ledger(tmp_path, 100.0)
    ledger_b = AccountCashLedger(
        ledger_a.db_path, account_id=ACCOUNT, broker_cash_fn=lambda: 100.0
    )
    ledger_a.halt("account_cash_unknown_open_buy")
    assert ledger_b.halt_state() == (True, "account_cash_unknown_open_buy")
    assert not ledger_b.reserve(
        sleeve_tag="alpaca_crypto", parent_intent_id="pi-x", amount=1.0
    )


# ---------------------------------------------------------------------------
# Flag gating: default OFF = byte-identical
# ---------------------------------------------------------------------------


def test_flag_default_off_builds_nothing(tmp_path):
    assert not account_cash_ledger_enabled(env={})
    assert (
        maybe_build_account_cash_ledger(
            data_dir=tmp_path,
            account_id=ACCOUNT,
            broker_cash_fn=lambda: 1.0,
            env={},
        )
        is None
    )
    for off_value in ("", "0", "false", "off", "no"):
        assert not account_cash_ledger_enabled(env={ACCOUNT_CASH_LEDGER_FLAG: off_value})
    assert not list(tmp_path.iterdir())  # flag OFF writes NOTHING


def test_flag_on_builds_ledger_at_canonical_path(tmp_path):
    ledger = maybe_build_account_cash_ledger(
        data_dir=tmp_path,
        account_id=ACCOUNT,
        broker_cash_fn=lambda: 1.0,
        env={ACCOUNT_CASH_LEDGER_FLAG: "1"},
    )
    assert isinstance(ledger, AccountCashLedger)
    assert ledger.db_path == account_cash_ledger_db_path(tmp_path, ACCOUNT)
    assert ledger.db_path.exists()


def test_flag_off_book_and_submit_path_identical(tmp_path):
    # Byte-identity pin: with no ledger attached (the default), the book and
    # the submit path behave exactly as before this feature existed — same
    # snapshot schema (no new keys), no ledger consultation anywhere.
    book = _book()
    assert book.cash_ledger is None
    broker = FakeBroker()
    parent = _buy(book)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert child is not None
    book.on_fill(child.child_order_id, 10.0)
    snapshot = book.to_snapshot()
    assert set(snapshot) == {
        "schema_version",
        "account",
        "trading_day",
        "entries_halted",
        "halt_reason",
        "parents",
    }
    restored = OrderStateBook.from_snapshot(snapshot)
    assert restored.cash_ledger is None  # runtime wiring, never state
    assert restored.to_snapshot() == snapshot


# ---------------------------------------------------------------------------
# order_state_machine wiring: reserve on submit, release on fill/cancel/reject
# ---------------------------------------------------------------------------


def test_submit_remainder_reserves_entry_notional(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert child is not None and broker.submits == [child.child_order_id]
    row = ledger.reservation(parent.parent_intent_id)
    assert row is not None
    assert row.status == "active"
    assert row.amount == pytest.approx(500.0)  # qty 10 x price 50
    assert row.sleeve_tag == "alpaca"  # the broker TAG records WHO reserved


def test_submit_remainder_refused_reserve_blocks_entry(tmp_path):
    ledger, _ = _ledger(tmp_path, 100.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)  # 10 x 50 = 500 > 100 cash
    with pytest.raises(EntryBlockedError) as excinfo:
        submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    # the existing A2 reason string, reused not duplicated (RFC §5.3)
    assert excinfo.value.reason == "insufficient_buying_power_headroom"
    assert broker.submits == []  # order placement did not proceed
    assert parent.children == []
    assert not book.entries_halted  # a refusal is not a halt


def test_cross_sleeve_double_reservation_prevented(tmp_path):
    # THE RFC §5.3 scenario verbatim: two books (broker tags) over ONE real
    # account; cadence overlap lets both size "broker_cash - reserved_cash"
    # locally. With the shared ledger the second sleeve is refused.
    ledger, _ = _ledger(tmp_path, 1_000.0)
    equity_book = _book("alpaca", ledger)
    crypto_book = _book("alpaca_crypto", ledger)
    broker = FakeBroker()
    equity = _buy(equity_book, symbol="NVDA", target=10.0)  # 600
    crypto = _buy(crypto_book, symbol="BTC/USD", target=0.01)  # 600
    assert (
        submit_remainder(equity_book, broker, equity.parent_intent_id, price=60.0, now=T0)
        is not None
    )
    with pytest.raises(EntryBlockedError) as excinfo:
        submit_remainder(
            crypto_book, broker, crypto.parent_intent_id, price=60_000.0, now=T0
        )
    assert excinfo.value.reason == "insufficient_buying_power_headroom"
    assert ledger.active_reserved_total() == pytest.approx(600.0)


@pytest.mark.parametrize(
    ("transition", "expected_reason"),
    [
        ("fill", "filled"),
        ("cancel", "canceled"),
        ("reject", "rejected"),
        ("expire", "expired"),
    ],
)
def test_release_wired_to_lifecycle_transitions(tmp_path, transition, expected_reason):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert child is not None
    assert ledger.reservation(parent.parent_intent_id).status == "active"
    if transition == "fill":
        book.on_fill(child.child_order_id, 10.0)
        assert book.lifecycle_state(parent.parent_intent_id) is LifecycleState.FILLED
    elif transition == "cancel":
        book.on_cancel(child.child_order_id)
    elif transition == "reject":
        book.on_reject(child.child_order_id)
    else:
        book.on_expire(child.child_order_id)
    row = ledger.reservation(parent.parent_intent_id)
    assert row.status == "released"
    assert row.release_reason == expected_reason
    assert ledger.active_reserved_total() == pytest.approx(0.0)


def test_partial_fill_keeps_reservation_until_terminal(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    book.on_fill(child.child_order_id, 4.0)  # partial: order still live
    assert ledger.reservation(parent.parent_intent_id).status == "active"
    book.apply_terminal_status(child.child_order_id, status="canceled", filled_qty=4.0)
    row = ledger.reservation(parent.parent_intent_id)
    assert row.status == "released"
    assert row.release_reason == "canceled"


def test_reemit_after_cancel_re_reserves(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)
    c1 = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    book.on_cancel(c1.child_order_id)
    assert ledger.reservation(parent.parent_intent_id).status == "released"
    c2 = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert c2 is not None and c2.attempt_n == 2
    row = ledger.reservation(parent.parent_intent_id)
    assert row.status == "active"  # re-activated against fresh headroom


def test_broker_submit_failure_releases_reservation(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    parent = _buy(book, target=10.0)

    class ExplodingBroker:
        def submit_order(self, **kwargs):
            raise RuntimeError("broker down")

    with pytest.raises(RuntimeError):
        submit_remainder(
            book, ExplodingBroker(), parent.parent_intent_id, price=50.0, now=T0
        )
    row = ledger.reservation(parent.parent_intent_id)
    assert row.status == "released"  # via the on_reject lifecycle hook
    assert row.release_reason == "rejected"


def test_recheck_mismatch_refuses_entry_and_fail_closes_all_sleeves(tmp_path):
    # Broker cash moves BETWEEN reserve() and the submit call: first fetch
    # (reserve) sees 600, second fetch (pre-submit recheck) sees 40.
    feed = _BrokerCash(600.0, queue=[600.0, 40.0])
    ledger, _ = _ledger(tmp_path, feed)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)  # 500 notional
    with pytest.raises(EntryBlockedError) as excinfo:
        submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert excinfo.value.reason == HALT_REASON_RECHECK_MISMATCH
    assert broker.submits == []  # the submit API call never happened
    # this order's child is REJECTED and its reservation released
    assert parent.children[-1].state is ChildOrderState.REJECTED
    assert ledger.reservation(parent.parent_intent_id).status == "released"
    # fail-closed across EVERY sleeve + mirrored on this session book
    assert book.entries_halted
    assert book.halt_reason == HALT_REASON_RECHECK_MISMATCH
    assert ledger.halt_state() == (True, HALT_REASON_RECHECK_MISMATCH)
    other_book = _book("alpaca_crypto", ledger)
    other = _buy(other_book, symbol="BTC/USD", target=0.001)
    with pytest.raises(EntryBlockedError) as other_exc:
        submit_remainder(other_book, broker, other.parent_intent_id, price=100.0, now=T0)
    assert other_exc.value.reason == HALT_REASON_RECHECK_MISMATCH


def test_exits_never_blocked_by_halted_ledger(tmp_path):
    # §5.4 precedence: every ledger halt binds ENTRIES only. Exits (SELL)
    # and protective-stop maintenance never consult reserve().
    ledger, feed = _ledger(tmp_path, 100.0)
    ledger.halt(HALT_REASON_UNKNOWN_OPEN_BUY)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    fetches_before = feed.calls
    exit_parent = _sell(book, target=10.0)
    child = submit_remainder(book, broker, exit_parent.parent_intent_id, price=50.0, now=T0)
    assert child is not None
    assert broker.submits == [child.child_order_id]
    assert feed.calls == fetches_before  # the ledger was never even consulted


# ---------------------------------------------------------------------------
# reconcile_on_restart: ledger sweep + account-wide escalation
# ---------------------------------------------------------------------------


def _snapshot_roundtrip(book: OrderStateBook, ledger: AccountCashLedger) -> OrderStateBook:
    restored = OrderStateBook.from_snapshot(book.to_snapshot())
    restored.attach_cash_ledger(ledger)
    return restored


def test_reconcile_clean_runs_sweep_and_keeps_entries_open(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)
    submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    restored = _snapshot_roundtrip(book, ledger)
    result = reconcile_on_restart(restored, broker)
    assert result.clean
    assert not restored.entries_halted
    assert restored.last_ledger_sweep is not None
    assert restored.last_ledger_sweep.clean
    assert ledger.halt_state() == (False, None)
    # the open buy keeps its reservation
    assert ledger.reservation(parent.parent_intent_id).status == "active"


def test_reconcile_releases_orphan_from_crashed_process(tmp_path):
    # A reservation left behind by a crash between reserve and submit: no
    # broker order, no in-flight local state -> released + counted.
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    assert ledger.reserve(
        sleeve_tag="alpaca", parent_intent_id="pi-crashed", amount=60.0
    )
    restored = _snapshot_roundtrip(book, ledger)
    result = reconcile_on_restart(restored, broker)
    assert result.clean  # the BOOK is clean; the LEDGER defect is surfaced:
    assert restored.last_ledger_sweep.orphans_released == ("pi-crashed",)
    assert ledger.reservation("pi-crashed").status == "released"
    assert ledger.reservation("pi-crashed").release_reason == "orphan_sweep"


def test_reconcile_unknown_broker_order_fail_closes_every_sleeve(tmp_path):
    # An external/manual open order: session mismatch halt (existing SS7
    # behavior) AND the account-wide §5.3 fail-closed halt.
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    broker.submit_order(
        client_order_id="manual-uuid-1", symbol="TSLA", side="BUY", qty=5.0
    )
    restored = _snapshot_roundtrip(book, ledger)
    result = reconcile_on_restart(restored, broker)
    assert not result.clean
    assert result.mismatches[0].kind == "unknown_broker_order"
    assert restored.entries_halted
    halted, reason = ledger.halt_state()
    assert halted
    assert reason == HALT_REASON_UNKNOWN_OPEN_BUY  # sweep noticed it first
    # every OTHER sleeve is refused too
    assert not ledger.reserve(
        sleeve_tag="alpaca_crypto", parent_intent_id="pi-z", amount=1.0
    )


def test_reconcile_mismatch_without_unknown_buy_still_escalates(tmp_path):
    # A missing-at-broker mismatch (book thinks a child is open, broker does
    # not list it and reports it non-terminal): no unknown open buy, but the
    # task's fail-closed rule still applies account-wide.
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    parent = _buy(book, target=10.0)
    child = submit_remainder(book, broker, parent.parent_intent_id, price=50.0, now=T0)
    assert child is not None
    restored = _snapshot_roundtrip(book, ledger)

    # broker "loses" the order but reports a NON-terminal status, so the
    # child stays open in the book and mismatches broker open-orders.
    class _Port:
        def open_orders(self):
            return {}

        def order_status(self, client_order_id):
            return {"status": "pending_new", "filled_qty": 0.0}

    result = reconcile_on_restart(restored, _Port())
    assert not result.clean
    assert restored.entries_halted
    halted, reason = ledger.halt_state()
    assert halted
    assert reason == ACCOUNT_CASH_RECONCILE_MISMATCH_REASON


def test_reconcile_known_sell_orders_do_not_trip_the_sweep(tmp_path):
    ledger, _ = _ledger(tmp_path, 1_000.0)
    book = _book("alpaca", ledger)
    broker = FakeBroker()
    exit_parent = _sell(book, target=10.0)
    submit_remainder(book, broker, exit_parent.parent_intent_id, price=50.0, now=T0)
    restored = _snapshot_roundtrip(book, ledger)
    result = reconcile_on_restart(restored, broker)
    assert result.clean
    assert restored.last_ledger_sweep.clean  # a known SELL reserves no cash
    assert ledger.halt_state() == (False, None)


def test_parent_intent_id_from_client_order_id_roundtrip():
    assert parent_intent_id_from_client_order_id("pi-abc123:7") == "pi-abc123"
    assert parent_intent_id_from_client_order_id("pi-abc123:12") == "pi-abc123"
    # external/manual ids come back verbatim -> can never match a reservation
    for manual in ("8f2c9e-broker-uuid", "manual:order:extra", "no-colon", ":3"):
        assert parent_intent_id_from_client_order_id(manual) == manual
