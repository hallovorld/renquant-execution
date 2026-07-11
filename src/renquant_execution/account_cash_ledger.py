"""Account-scoped cash reservation ledger (crypto RFC §5.3 CORRECTED, D-C4).

Implements the execution-owned ``AccountCashLedger`` of the merged crypto
trading RFC (renquant-orchestrator
``doc/design/2026-07-10-crypto-trading-rfc.md`` §5.3): one SQLite ledger,
keyed by the REAL brokerage account (never by broker tag), tracking the SUM
of all open buy-order cash reservations across every sleeve/tag sharing that
account. It exists to close the concurrent double-reservation hole the RFC
documents: two per-tag ``OrderStateBook`` instances (``alpaca`` equity,
``alpaca_crypto`` sleeve) each sizing ``broker_cash - reserved_cash()`` from
their own LOCAL view can both believe headroom exists that the other has
already spent.

Contract (all RFC §5.3, quoted where binding):

- **Storage**: one SQLite table per account
  (``data/account_cash_ledger.<account>.db``), WAL mode "for concurrent
  readers/writers from the 104 batch process and the crypto 24/7 loop", with
  a single-writer-transaction reserve/release protocol (``BEGIN IMMEDIATE``).
- **reserve(sleeve_tag, parent_intent_id, amount) -> bool**:
  ``parent_intent_id`` IS the idempotency key; reserve is UPSERT-then-check —
  a retried call for an ACTIVE row is a no-op returning the same ``True`` the
  original returned, never a second reservation of the same cash. Otherwise
  it atomically checks ``broker_cash - SUM(all active, non-expired
  reservations across all tags) - amount >= 0`` and inserts the reservation
  row in the SAME transaction; ``False`` means the caller's order placement
  MUST NOT proceed.
- **broker_cash** is re-fetched (never cached) at the start of each
  ``reserve()`` transaction, inside the write lock, so a real balance change
  is visible to the next reservation attempt from EITHER sleeve.
- **TTL**: every reservation carries ``reserved_at`` + ``expires_at``.  The
  default TTL reuses the existing order-timeout convention
  (:data:`~renquant_execution.order_state_machine.MAX_PENDING_AGE_SECONDS`)
  plus a fixed grace margin — not a fresh number. An EXPIRED reservation is
  NEVER silently auto-released: it stops counting toward the headroom SUM
  (the RFC check formula is explicit: "all active, non-expired") — that is
  how the TTL "bounds how long a crash can hold phantom headroom" — but the
  row stays ACTIVE until the orphan sweep surfaces it as a reportable event.
- **release(parent_intent_id)**: wired to the fill/cancel/reject lifecycle
  transitions the ``OrderStateBook`` already owns (see
  ``order_state_machine``); idempotent — releasing an already-released or
  unknown intent is a no-op, "lifecycle hooks may legitimately race to
  release the same intent".
- **Pre-submit broker-cash recheck**: immediately before the actual
  order-submit API call, ``recheck_before_submit()`` re-fetches broker cash
  and re-verifies ``broker_cash - SUM(active, non-expired) >= 0`` for the
  whole account. A failure is a REAL reconciliation mismatch: the entry is
  refused AND the ledger fail-closes new entries across EVERY sleeve
  (account-wide sticky halt), "never just the sleeve that happened to
  notice".
- **Orphan sweep** (:meth:`AccountCashLedger.sweep`): an ACTIVE reservation
  with no corresponding broker open order AND no in-flight local lifecycle
  state is an ORPHAN — released, counted, and alerted (a reportable defect,
  never a silent cleanup). A broker open BUY order with NO active ledger
  reservation (external/manual order, or a path that submitted without
  reserving) is the graver defect: it halts new entries for EVERY sleeve
  sharing the account. Expired-but-unreleased ACTIVE rows are surfaced as
  reportable events (and only released when they are also orphans).
- **Fail-closed scope**: every halt binds NEW ENTRIES only. Exits and
  protective-stop maintenance are never routed through ``reserve()`` and can
  never be blocked by this ledger (the §5.4 exits-always-allowed precedence).

Flag-gated (default OFF = byte-identical): nothing constructs a ledger unless
the ``RENQUANT_ACCOUNT_CASH_LEDGER`` environment flag is truthy —
:func:`maybe_build_account_cash_ledger` returns ``None`` when OFF, and every
``order_state_machine`` seam treats ``None`` as "behave exactly as before".

Ambiguities resolved against the RFC text (mirrored in the progress doc):

1. A reserve retry against a RELEASED row is a FRESH reservation attempt
   (full headroom re-check, row re-activated with new timestamps), not a
   no-op ``True``: the RFC's no-op clause exists so a retry can "never
   double-reserve the same cash", and a released row holds no cash — while a
   blind no-op ``True`` after release would let a re-emitted remainder size
   a buy with NO active reservation (exactly the leak this ledger exists to
   prevent). The named retry scenarios (timeout retry, crash-and-resubmit)
   all hit the ACTIVE-row no-op path.
2. A REFUSED reserve inserts no row, so a later retry re-evaluates fresh
   headroom (it may then succeed). Refusal reserved nothing, so this cannot
   double-reserve; recording refusals forever would instead wedge an intent
   for the whole session even after headroom frees.
3. "No ledger reservation" for the unknown-open-buy check means no ACTIVE
   reservation: a broker open buy whose reservation was already released is
   the same headroom leak (committed cash the SUM no longer covers).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import math
import os
import sqlite3
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .broker import BaseBroker
from .order_state_machine import (
    ACCOUNT_CASH_RECONCILE_MISMATCH_REASON,
    MAX_PENDING_AGE_SECONDS,
    parent_intent_id_from_client_order_id,
)

ACCOUNT_CASH_LEDGER_SCHEMA_VERSION = "account-cash-ledger-v1"

#: Default-OFF feature flag (RFC §5.3 lands flag-gated; byte-identical when
#: OFF). Truthy values: "1", "true", "on", "yes" (case-insensitive).
ACCOUNT_CASH_LEDGER_FLAG = "RENQUANT_ACCOUNT_CASH_LEDGER"

#: RFC §5.3 TTL: "order timeout budget + a fixed grace margin — reuse
#: whatever order-submission timeout convention already exists in
#: order_state_machine.py, not a fresh number". The budget is the SS10
#: stale-pending watchdog age (600s); the grace margin covers one full
#: watchdog cycle after the order itself is overdue.
RESERVATION_GRACE_SECONDS = MAX_PENDING_AGE_SECONDS / 2.0
DEFAULT_RESERVATION_TTL_SECONDS = MAX_PENDING_AGE_SECONDS + RESERVATION_GRACE_SECONDS

#: Sticky account-wide halt reasons (fail-closed for new entries across
#: EVERY sleeve sharing the account; exits are never routed through here).
#: The reconcile-mismatch reason is single-sourced in order_state_machine
#: (its reconcile pass is what escalates a session mismatch account-wide).
HALT_REASON_UNKNOWN_OPEN_BUY = "account_cash_unknown_open_buy"
HALT_REASON_RECHECK_MISMATCH = "account_cash_recheck_mismatch"
HALT_REASON_RECONCILE_MISMATCH = ACCOUNT_CASH_RECONCILE_MISMATCH_REASON

_SQLITE_BUSY_TIMEOUT_MS = 30_000

_TRUTHY = frozenset({"1", "true", "on", "yes"})


class AccountCashLedgerError(RuntimeError):
    """Contract violation inside the account cash ledger."""


def account_cash_ledger_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the default-OFF §5.3 flag is explicitly enabled."""
    source = os.environ if env is None else env
    return str(source.get(ACCOUNT_CASH_LEDGER_FLAG, "")).strip().lower() in _TRUTHY


def account_cash_ledger_db_path(data_dir: "str | Path", account_id: str) -> Path:
    """RFC §5.3 canonical path: ``<data_dir>/account_cash_ledger.<account>.db``."""
    account = str(account_id).strip()
    if not account:
        raise AccountCashLedgerError("account_id must be non-empty")
    if "/" in account or "\\" in account:
        raise AccountCashLedgerError(f"account_id must be path-safe: {account_id!r}")
    return Path(data_dir) / f"account_cash_ledger.{account}.db"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _epoch(ts: Optional[dt.datetime]) -> float:
    moment = _utc_now() if ts is None else ts
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


@dataclass(frozen=True)
class ReservationRow:
    """One reservation row (audit view of the SQLite record)."""

    parent_intent_id: str
    account_id: str
    sleeve_tag: str
    amount: float
    status: str  # "active" | "released"
    reserved_at: float  # unix epoch seconds, UTC
    expires_at: float
    released_at: Optional[float] = None
    release_reason: Optional[str] = None

    def is_expired(self, *, now_epoch: float) -> bool:
        return self.status == "active" and now_epoch >= self.expires_at


@dataclass(frozen=True)
class LedgerSweepResult:
    """Outcome of one ledger reconciliation pass (RFC §5.3 orphan sweep).

    ``orphans_released``: ACTIVE reservations with no broker open order and
    no in-flight local lifecycle state — released by this sweep, counted,
    and returned for alerting (>0 is a reportable defect: a lifecycle hook
    missed a terminal path).

    ``expired_unreleased``: ACTIVE rows past ``expires_at`` at sweep time —
    surfaced, never auto-released for being expired (only released when they
    are also orphans; such rows appear in BOTH tuples).

    ``unknown_open_buys``: broker open BUY intents with no ACTIVE
    reservation — the graver defect (headroom leak / external or manual
    order); triggers the account-wide fail-closed halt.
    """

    clean: bool
    orphans_released: tuple[str, ...] = ()
    expired_unreleased: tuple[str, ...] = ()
    unknown_open_buys: tuple[str, ...] = ()
    halted: bool = False
    halt_reason: Optional[str] = None


class AccountCashLedger:
    """RFC §5.3 account-scoped cash reservation ledger (SQLite WAL).

    ``account_id`` is the REAL brokerage account identifier (e.g. the Alpaca
    account number), NEVER a broker tag — the whole point is that multiple
    tags (``alpaca``, ``alpaca_crypto``) share one ledger. ``broker_cash_fn``
    is called fresh inside every ``reserve()`` / ``recheck_before_submit()``
    write transaction; it must return the CURRENT broker cash for the
    account (never a cached number).

    Every method opens its own short-lived connection, so one instance is
    safe to share across threads, and independent instances in independent
    processes (the 104 batch and the crypto 24/7 loop) serialize through
    SQLite's write lock on the shared db file.
    """

    def __init__(
        self,
        db_path: "str | Path",
        *,
        account_id: str,
        broker_cash_fn: Callable[[], float],
        ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
    ):
        self.db_path = Path(db_path)
        self.account_id = str(account_id)
        if not self.account_id:
            raise AccountCashLedgerError("account_id must be non-empty")
        self._broker_cash_fn = broker_cash_fn
        self.ttl_seconds = float(ttl_seconds)
        if not (self.ttl_seconds > 0):
            raise AccountCashLedgerError(
                f"ttl_seconds must be positive: {ttl_seconds!r}"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)

    # -- connection / schema --------------------------------------------------
    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Short-lived per-operation connection, ALWAYS closed on exit.

        (``sqlite3.Connection``'s own context manager commits/rolls back but
        never closes — using it directly would leak one handle per call.)
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,  # explicit BEGIN IMMEDIATE, no implicit txns
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ledger_meta (
                       key   TEXT PRIMARY KEY,
                       value TEXT NOT NULL
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cash_reservations (
                       parent_intent_id TEXT PRIMARY KEY,
                       account_id       TEXT NOT NULL,
                       sleeve_tag       TEXT NOT NULL,
                       amount           REAL NOT NULL CHECK (amount > 0),
                       status           TEXT NOT NULL
                           CHECK (status IN ('active', 'released')),
                       reserved_at      REAL NOT NULL,
                       expires_at       REAL NOT NULL,
                       released_at      REAL,
                       release_reason   TEXT
                   )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_cash_res_active
                       ON cash_reservations(status, expires_at)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ledger_control (
                       account_id  TEXT PRIMARY KEY,
                       halted      INTEGER NOT NULL DEFAULT 0,
                       halt_reason TEXT,
                       halted_at   REAL
                   )"""
            )
            for key, expected in (
                ("schema_version", ACCOUNT_CASH_LEDGER_SCHEMA_VERSION),
                ("account_id", self.account_id),
            ):
                row = conn.execute(
                    "SELECT value FROM ledger_meta WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO ledger_meta (key, value) VALUES (?, ?)",
                        (key, expected),
                    )
                elif str(row["value"]) != expected:
                    raise AccountCashLedgerError(
                        f"ledger db {self.db_path} has {key}={row['value']!r}, "
                        f"expected {expected!r} — refusing to mix ledgers"
                    )
            conn.execute(
                "INSERT OR IGNORE INTO ledger_control (account_id, halted) VALUES (?, 0)",
                (self.account_id,),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # -- introspection ---------------------------------------------------------
    def reservation(self, parent_intent_id: str) -> Optional[ReservationRow]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cash_reservations WHERE parent_intent_id = ?",
                (str(parent_intent_id),),
            ).fetchone()
        return self._row_to_dataclass(row) if row is not None else None

    def reservations(self, *, status: Optional[str] = None) -> list[ReservationRow]:
        query = "SELECT * FROM cash_reservations"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (str(status),)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY reserved_at", params).fetchall()
        return [self._row_to_dataclass(row) for row in rows]

    @staticmethod
    def _row_to_dataclass(row: sqlite3.Row) -> ReservationRow:
        return ReservationRow(
            parent_intent_id=str(row["parent_intent_id"]),
            account_id=str(row["account_id"]),
            sleeve_tag=str(row["sleeve_tag"]),
            amount=float(row["amount"]),
            status=str(row["status"]),
            reserved_at=float(row["reserved_at"]),
            expires_at=float(row["expires_at"]),
            released_at=(
                float(row["released_at"]) if row["released_at"] is not None else None
            ),
            release_reason=(
                str(row["release_reason"])
                if row["release_reason"] is not None
                else None
            ),
        )

    @staticmethod
    def _active_sum(conn: sqlite3.Connection, now_epoch: float) -> float:
        """SUM of all ACTIVE, NON-EXPIRED reservations across all tags (§5.3)."""
        row = conn.execute(
            """SELECT COALESCE(SUM(amount), 0.0) AS total
                   FROM cash_reservations
                  WHERE status = 'active' AND expires_at > ?""",
            (now_epoch,),
        ).fetchone()
        return float(row["total"])

    def active_reserved_total(self, *, now: Optional[dt.datetime] = None) -> float:
        """Account-wide headroom debit: SUM(active, non-expired) (§5.3)."""
        with self._connect() as conn:
            return self._active_sum(conn, _epoch(now))

    # -- halt state (fail-closed across EVERY sleeve) --------------------------
    def halt_state(self) -> "tuple[bool, Optional[str]]":
        with self._connect() as conn:
            row = self._halt_row(conn)
        return row

    def _halt_row(self, conn: sqlite3.Connection) -> "tuple[bool, Optional[str]]":
        row = conn.execute(
            "SELECT halted, halt_reason FROM ledger_control WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        if row is None:
            return (False, None)
        reason = row["halt_reason"]
        return (bool(row["halted"]), str(reason) if reason is not None else None)

    def halt(self, reason: str, *, now: Optional[dt.datetime] = None) -> None:
        """Sticky account-wide fail-closed: every sleeve's reserve() refuses.

        Binds NEW ENTRIES only — exits/stop maintenance never consult the
        ledger. The FIRST halt reason is preserved (most-restrictive/earliest
        wins); repeated halts do not overwrite the original evidence.
        """
        reason_s = str(reason).strip()
        if not reason_s:
            raise AccountCashLedgerError("halt reason must be non-empty")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._halt_in_txn(conn, reason_s, _epoch(now))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _halt_in_txn(
        self, conn: sqlite3.Connection, reason: str, now_epoch: float
    ) -> None:
        conn.execute(
            """INSERT INTO ledger_control (account_id, halted, halt_reason, halted_at)
                   VALUES (?, 1, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET
                       halted = 1,
                       halt_reason = COALESCE(ledger_control.halt_reason, excluded.halt_reason),
                       halted_at = COALESCE(ledger_control.halted_at, excluded.halted_at)""",
            (self.account_id, reason, now_epoch),
        )

    def clear_halt(self) -> None:
        """Operator/reconciliation action: re-open new entries account-wide.

        Deliberately explicit and manual — nothing in the automated paths
        calls this; fail-closed states are cleared only after the mismatch
        that caused them is actually reconciled.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """UPDATE ledger_control
                           SET halted = 0, halt_reason = NULL, halted_at = NULL
                         WHERE account_id = ?""",
                    (self.account_id,),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # -- the §5.3 protocol ------------------------------------------------------
    def reserve(
        self,
        *,
        sleeve_tag: str,
        parent_intent_id: str,
        amount: float,
        now: Optional[dt.datetime] = None,
    ) -> bool:
        """Atomically reserve ``amount`` against the shared account headroom.

        UPSERT-then-check (§5.3): an existing ACTIVE row for this
        ``parent_intent_id`` is a retried call — no-op, returns ``True`` (the
        original result), never a second reservation. Otherwise, inside ONE
        ``BEGIN IMMEDIATE`` transaction: fetch fresh ``broker_cash``, check
        ``broker_cash - SUM(active, non-expired, all tags) - amount >= 0``,
        and insert (or re-activate a released row) with ``reserved_at`` /
        ``expires_at``. ``False`` = reservation refused; the caller's order
        placement MUST NOT proceed. When the account is fail-closed (halted),
        every reserve refuses regardless of headroom.
        """
        pid = str(parent_intent_id).strip()
        if not pid:
            raise AccountCashLedgerError("parent_intent_id must be non-empty")
        tag = str(sleeve_tag).strip()
        if not tag:
            raise AccountCashLedgerError("sleeve_tag must be non-empty")
        amount_f = float(amount)
        if not math.isfinite(amount_f) or amount_f <= 0:
            raise AccountCashLedgerError(
                f"reservation amount must be finite and positive: {amount!r}"
            )
        now_epoch = _epoch(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                halted, _ = self._halt_row(conn)
                if halted:
                    conn.execute("ROLLBACK")
                    return False
                existing = conn.execute(
                    "SELECT status FROM cash_reservations WHERE parent_intent_id = ?",
                    (pid,),
                ).fetchone()
                if existing is not None and str(existing["status"]) == "active":
                    # Idempotent retry (timeout retry / crash-and-resubmit):
                    # no-op, same result as the original call.
                    conn.execute("ROLLBACK")
                    return True
                # Fresh attempt (no row, or a released row being re-activated
                # for a re-emitted remainder). broker_cash is re-fetched INSIDE
                # the write lock — never cached (§5.3).
                broker_cash = float(self._broker_cash_fn())
                reserved = self._active_sum(conn, now_epoch)
                if broker_cash - reserved - amount_f < 0:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    """INSERT INTO cash_reservations
                           (parent_intent_id, account_id, sleeve_tag, amount,
                            status, reserved_at, expires_at, released_at,
                            release_reason)
                       VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, NULL)
                       ON CONFLICT(parent_intent_id) DO UPDATE SET
                           sleeve_tag = excluded.sleeve_tag,
                           amount = excluded.amount,
                           status = 'active',
                           reserved_at = excluded.reserved_at,
                           expires_at = excluded.expires_at,
                           released_at = NULL,
                           release_reason = NULL""",
                    (
                        pid,
                        self.account_id,
                        tag,
                        amount_f,
                        now_epoch,
                        now_epoch + self.ttl_seconds,
                    ),
                )
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def release(
        self,
        parent_intent_id: str,
        *,
        reason: str,
        now: Optional[dt.datetime] = None,
    ) -> bool:
        """Release one reservation on its fill/cancel/reject transition.

        Idempotent (§5.3): an unknown or already-released
        ``parent_intent_id`` is a no-op returning ``False`` — lifecycle hooks
        may legitimately race to release the same intent. Returns ``True``
        only when this call performed the active -> released transition.
        """
        pid = str(parent_intent_id).strip()
        reason_s = str(reason).strip()
        if not reason_s:
            raise AccountCashLedgerError("release reason must be non-empty")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """UPDATE cash_reservations
                           SET status = 'released',
                               released_at = ?,
                               release_reason = ?
                         WHERE parent_intent_id = ? AND status = 'active'""",
                    (_epoch(now), reason_s, pid),
                )
                conn.execute("COMMIT")
                return cursor.rowcount > 0
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def recheck_before_submit(self, *, now: Optional[dt.datetime] = None) -> bool:
        """§5.3 broker-cash recheck IMMEDIATELY before the order-submit call.

        Re-fetches ``broker_cash`` and re-verifies ``broker_cash -
        SUM(active, non-expired reservations) >= 0`` for the account as a
        whole. A failure is a REAL reconciliation mismatch (some path is
        moving cash the ledger doesn't know about): this method fail-closes
        new entries across EVERY sleeve (sticky halt) and returns ``False``
        — the submitting sleeve's entry must be refused. Also returns
        ``False`` without rechecking when the account is already halted.
        """
        now_epoch = _epoch(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                halted, _ = self._halt_row(conn)
                if halted:
                    conn.execute("ROLLBACK")
                    return False
                broker_cash = float(self._broker_cash_fn())
                reserved = self._active_sum(conn, now_epoch)
                if broker_cash - reserved < 0:
                    self._halt_in_txn(conn, HALT_REASON_RECHECK_MISMATCH, now_epoch)
                    conn.execute("COMMIT")
                    return False
                conn.execute("ROLLBACK")  # read-only pass; nothing to write
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def sweep(
        self,
        *,
        broker_open_buy_intents: Collection[str],
        local_inflight_intents: Collection[str],
        now: Optional[dt.datetime] = None,
    ) -> LedgerSweepResult:
        """§5.3 ledger reconciliation (orphan sweep), one atomic pass.

        ``broker_open_buy_intents``: parent-intent keys derived from the
        broker's OPEN BUY orders (via
        :func:`parent_intent_id_from_client_order_id`; external/manual
        orders keep their raw broker id and therefore fail closed).
        ``local_inflight_intents``: BUY intents the calling process still
        holds in-flight lifecycle state for (open children or an unsubmitted
        remainder), which keep their reservations legitimate.
        """
        broker_open = {str(pid) for pid in broker_open_buy_intents}
        local_inflight = {str(pid) for pid in local_inflight_intents}
        now_epoch = _epoch(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = [
                    self._row_to_dataclass(row)
                    for row in conn.execute(
                        "SELECT * FROM cash_reservations WHERE status = 'active'"
                        " ORDER BY reserved_at"
                    ).fetchall()
                ]
                active_ids = {row.parent_intent_id for row in rows}
                expired = tuple(
                    row.parent_intent_id
                    for row in rows
                    if row.is_expired(now_epoch=now_epoch)
                )
                orphans = tuple(
                    row.parent_intent_id
                    for row in rows
                    if row.parent_intent_id not in broker_open
                    and row.parent_intent_id not in local_inflight
                )
                for pid in orphans:
                    conn.execute(
                        """UPDATE cash_reservations
                               SET status = 'released',
                                   released_at = ?,
                                   release_reason = 'orphan_sweep'
                             WHERE parent_intent_id = ? AND status = 'active'""",
                        (now_epoch, pid),
                    )
                unknown = tuple(
                    sorted(pid for pid in broker_open if pid not in active_ids)
                )
                halted = False
                halt_reason: Optional[str] = None
                if unknown:
                    # Graver defect: committed broker cash the SUM does not
                    # cover — fail closed for EVERY sleeve on this account.
                    self._halt_in_txn(
                        conn, HALT_REASON_UNKNOWN_OPEN_BUY, now_epoch
                    )
                    halted, halt_reason = True, HALT_REASON_UNKNOWN_OPEN_BUY
                else:
                    halted, halt_reason = self._halt_row(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return LedgerSweepResult(
            clean=not (orphans or expired or unknown),
            orphans_released=orphans,
            expired_unreleased=expired,
            unknown_open_buys=unknown,
            halted=halted,
            halt_reason=halt_reason,
        )


def maybe_build_account_cash_ledger(
    *,
    data_dir: "str | Path",
    account_id: str,
    broker_cash_fn: Callable[[], float],
    ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[AccountCashLedger]:
    """Flag-gated constructor: ``None`` unless the §5.3 flag is explicitly ON.

    Default OFF is the byte-identical path — with ``None`` every
    ``order_state_machine`` seam behaves exactly as it did before this
    module existed.
    """
    if not account_cash_ledger_enabled(env):
        return None
    return AccountCashLedger(
        account_cash_ledger_db_path(data_dir, account_id),
        account_id=account_id,
        broker_cash_fn=broker_cash_fn,
        ttl_seconds=ttl_seconds,
    )


#: THE single override hook for the canonical shared-ledger data root
#: (intended for tests / an explicit, ONE-TIME operator decision recorded
#: here — never a per-sleeve setting). Absent, the ledger lives at a FIXED
#: location independent of any per-process variable (RENQUANT_REPO_ROOT,
#: cwd, RENQUANT_SUBREPO_ROOT, ...) that could legitimately differ between
#: two sleeves' launch environments (Codex review, D-C4 round-2: the prior
#: design accepted an arbitrary caller-supplied ``data_dir``, which let two
#: sleeves silently create independent per-account databases).
ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE = "RENQUANT_ACCOUNT_CASH_LEDGER_DATA_DIR"


def account_cash_ledger_data_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """THE canonical, non-negotiable data root for the shared account-scoped
    cash ledger (Codex review, D-C4 round-2).

    Takes NO caller-supplied path argument and consults nothing that could
    vary sleeve-to-sleeve: exactly one override
    (:data:`ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE`, for tests / a single
    recorded operator decision — never set differently per sleeve) and
    otherwise a FIXED, machine-scoped default
    (``~/.renquant/account_cash_ledger``) that does not depend on
    ``RENQUANT_REPO_ROOT`` or any other per-deployment variable. Two
    sleeves sharing a real brokerage account run on the same machine (the
    whole point of a SQLite-file-coordinated ledger); this function's only
    job is to make sure they can never resolve to two different files.
    """
    source = os.environ if env is None else env
    raw = source.get(ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".renquant" / "account_cash_ledger"


def build_shared_account_cash_ledger_for_broker(
    broker: BaseBroker,
    *,
    ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[AccountCashLedger]:
    """THE execution-owned wiring contract every launch path (the 104 batch
    process, the crypto 24/7 loop) MUST build its ledger handle through
    (Codex review, D-C4 round-1/round-2): ``account_id`` is DERIVED from
    ``broker.get_account_id()`` — the broker's own verified real account
    identity — never accepted as a caller-supplied string, and the data
    root is resolved by :func:`account_cash_ledger_data_dir` — ALSO never
    a caller-supplied string. This function takes NO path/account
    argument at all (round-2: round-1 still accepted an arbitrary
    ``data_dir``, which Codex correctly flagged as still allowing two
    sleeves to silently diverge onto independent per-account databases).
    Two sleeves that connect to the SAME brokerage account through their
    own :class:`BaseBroker` instance therefore always resolve to the SAME
    ``account_cash_ledger_db_path``, regardless of which sleeve's process
    constructs it — there is no parameter through which a per-sleeve path
    could be threaded, by construction, not by convention. The shared-file
    property this enforces is verified end-to-end (two real OS processes,
    not two in-process instances) by
    ``tests/test_account_cash_ledger_shared_process.py``, including a
    positive proof that passing ``data_dir`` is now a ``TypeError``.

    Delegates to :func:`maybe_build_account_cash_ledger` for the flag gate
    and ``broker.get_cash`` for the fresh-every-transaction balance read.
    """
    if not account_cash_ledger_enabled(env):
        return None
    return maybe_build_account_cash_ledger(
        data_dir=account_cash_ledger_data_dir(env=env),
        account_id=broker.get_account_id(),
        broker_cash_fn=broker.get_cash,
        ttl_seconds=ttl_seconds,
        env=env,
    )


__all__ = [
    "ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE",
    "ACCOUNT_CASH_LEDGER_FLAG",
    "ACCOUNT_CASH_LEDGER_SCHEMA_VERSION",
    "AccountCashLedger",
    "AccountCashLedgerError",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "HALT_REASON_RECHECK_MISMATCH",
    "HALT_REASON_RECONCILE_MISMATCH",
    "HALT_REASON_UNKNOWN_OPEN_BUY",
    "LedgerSweepResult",
    "RESERVATION_GRACE_SECONDS",
    "ReservationRow",
    "account_cash_ledger_data_dir",
    "account_cash_ledger_db_path",
    "account_cash_ledger_enabled",
    "build_shared_account_cash_ledger_for_broker",
    "maybe_build_account_cash_ledger",
    "parent_intent_id_from_client_order_id",
]
