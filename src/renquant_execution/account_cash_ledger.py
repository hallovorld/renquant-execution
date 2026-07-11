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

DEPLOYMENT CONSTRAINT — SAME HOST, LOCAL FILESYSTEM ONLY (Codex review, D-C4
round-3): this ledger's cross-process coordination guarantee rests entirely
on SQLite's WAL-mode locking, which itself rests on the OS's POSIX advisory
file locking (``fcntl``) working correctly on the volume the db file lives
on. That holds for a local disk on ONE machine. It does NOT reliably hold
over NFS, SMB, or most cloud-mounted/network-attached filesystems — many
either lack working advisory locking entirely or emulate it in ways that can
silently fail to serialize concurrent writers. Running the 104 batch process
and the crypto 24/7 loop on DIFFERENT HOSTS sharing this db over a network
mount — or pointing :data:`ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE` at such a
mount "to simplify multi-host deployment" — would silently break the exact
serialization guarantee this whole ledger exists to provide: reservations
could race and double-book instead of correctly refusing. This is why
enabling the ledger requires BOTH the main flag AND an explicit same-host
acknowledgment (:data:`ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST`, the same
"operator must consciously affirm X" idiom ``alpaca_broker.py`` already uses
for ``RENQUANT_EXPECTED_LIVE_ACCOUNT``) — see
:func:`build_shared_account_cash_ledger_for_broker`. As a second,
best-effort check (not a substitute for the acknowledgment), the ledger
stamps the creating process's hostname into ``ledger_meta`` at first
construction and REFUSES to open the SAME db file from a different
hostname thereafter — the same "refuse to mix ledgers" mechanism already
used for ``schema_version``/``account_id`` consistency.

LIMIT OF THE HOSTNAME CHECK (Codex D-C4 round-4, do not overstate this
mechanism): it only detects a divergent host AFTER two processes already
happen to open the SAME db file. The actual dangerous cross-host failure
mode is the opposite — two hosts (or two containers, or two independently
configured processes on one host with different ``HOME``/override roots)
resolving to TWO DIFFERENT db files, each of which looks like a perfectly
valid, self-consistent single-host ledger from the inside. Neither process
observes a mismatch, because there is nothing to mismatch against; the
hostname stamp cannot see a file it never opened. No mechanism internal to
this SQLite-backed library can close that gap — it requires an external
party that can observe BOTH processes' resolved paths before either opens
its db and refuse to let them diverge. That external party is a
control-plane preflight, and it does not exist yet. Until it does, this
module is a SAME-FILESYSTEM-ONLY LIBRARY PRIMITIVE: safe and correct when
every process sharing an account's ledger is genuinely co-resident on one
host/filesystem, and NOT validated (by this module or anything else today)
for cross-host or cross-container deployment. Enabling
``RENQUANT_ACCOUNT_CASH_LEDGER`` for a real multi-host 104+24/7-crypto
deployment without that preflight in place is a deployment error this
library cannot detect for you.

Flag-gated (default OFF = byte-identical): nothing constructs a ledger unless
the ``RENQUANT_ACCOUNT_CASH_LEDGER`` environment flag is truthy —
:func:`build_shared_account_cash_ledger_for_broker` returns ``None`` when
OFF, and every ``order_state_machine`` seam treats ``None`` as "behave
exactly as before". When ON, the topology is non-negotiable by construction:
the account id comes from ``broker.get_account_id()`` (never a caller
string) and the db location comes from :func:`account_cash_ledger_data_dir`
(never a caller path) — see :func:`open_session_order_book`, the session
constructor both real launch paths MUST route through to get this
non-negotiable topology. No in-repo production entry point calls it yet
(renquant-execution is a library; wiring the 104 batch process and the
24/7 crypto loop onto it is owned by renquant-orchestrator and is tracked
as a separate follow-up — see the D-C4 progress doc). Reservations are the
fee-inclusive
WORST-CASE EXECUTABLE DEBIT computed through the REQUIRED canonical cost
contract (``renquant_common.cost_model``, coordinated floor
renquant-common>=0.12.0), sha-stamped per row; absent contract = new
entries fail closed.

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
import json
import math
import os
import socket
import sqlite3
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .broker import BaseBroker
from .order_state_machine import (
    ACCOUNT_CASH_RECONCILE_MISMATCH_REASON,
    CostContractUnavailableError,
    MAX_PENDING_AGE_SECONDS,
    OrderStateBook,
    parent_intent_id_from_client_order_id,
)

ACCOUNT_CASH_LEDGER_SCHEMA_VERSION = "account-cash-ledger-v1"

#: Default-OFF feature flag (RFC §5.3 lands flag-gated; byte-identical when
#: OFF). Truthy values: "1", "true", "on", "yes" (case-insensitive).
ACCOUNT_CASH_LEDGER_FLAG = "RENQUANT_ACCOUNT_CASH_LEDGER"

#: THE single override hook for the canonical shared-ledger data root
#: (intended for tests / an explicit, ONE-TIME operator decision recorded
#: here — never a per-sleeve setting). Absent, the ledger lives at a FIXED
#: location independent of any per-process variable (RENQUANT_REPO_ROOT,
#: cwd, RENQUANT_SUBREPO_ROOT, ...) that could legitimately differ between
#: two sleeves' launch environments (Codex review, D-C4 round-2: the prior
#: design accepted an arbitrary caller-supplied ``data_dir``, which let two
#: sleeves silently create independent per-account databases).
ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE = "RENQUANT_ACCOUNT_CASH_LEDGER_DATA_DIR"

#: Explicit operator acknowledgment that this deployment is SAME-HOST,
#: LOCAL-FILESYSTEM (Codex review, D-C4 round-3 — see the module docstring's
#: "DEPLOYMENT CONSTRAINT" section for why this matters). Required in
#: addition to :data:`ACCOUNT_CASH_LEDGER_FLAG`; same idiom as
#: ``alpaca_broker.py``'s ``RENQUANT_EXPECTED_LIVE_ACCOUNT`` — an operator
#: must consciously affirm the constraint, it is never assumed.
ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST = (
    "RENQUANT_ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST"
)

#: The REQUIRED canonical cost contract (Codex review round 2 — reservations
#: must be the fee-inclusive worst-case executable debit, computed through
#: renquant-common's ONE cost model, never a local formula).
#:
#: Coordinated version requirement: ``renquant-common>=0.12.0`` — the
#: version-addressable release fixed in common#28 r2 (its own words:
#: "Consumers requiring cost_model pin >=0.12.0 and fail closed below it";
#: 0.11.0 stays claimed by open common#27). ENFORCEMENT here is structural
#: — module presence + ``COST_MODEL_FINGERPRINT_SCHEMA_VERSION == 1`` + the
#: frozen callable surface — because this fleet consumes renquant-common as
#: a source checkout on PYTHONPATH, where ``importlib.metadata`` reports the
#: (stale) pip install, not the checkout; ``cost_model`` first ships in
#: 0.12.0, so a verified import IS >=0.12.0 content, while a metadata floor
#: would fail closed spuriously on every correctly-deployed machine.
#: Absent/unverifiable contract = FAIL CLOSED for new entries (never a
#: notional-only fallback).
REQUIRED_COST_MODEL_MODULE = "renquant_common.cost_model"
REQUIRED_COST_MODEL_PACKAGE_FLOOR = "0.12.0"
REQUIRED_COST_MODEL_FINGERPRINT_SCHEMA_VERSION = 1
_REQUIRED_COST_MODEL_SURFACE = (
    "CostModelSpec",
    "cost_model_content_sha256",
    "cost_model_spec_from_dict",
    "per_side_cost_bps",
)

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

    SAME-HOST, LOCAL FILESYSTEM ONLY: whatever this resolves to must be a
    local disk path on the one machine both sleeves run on — never a
    network-mounted volume (NFS/SMB/cloud-mount), whose POSIX advisory
    locking support SQLite's WAL-mode coordination depends on is often
    unreliable or absent. See the module docstring's "DEPLOYMENT
    CONSTRAINT" section.
    """
    source = os.environ if env is None else env
    raw = source.get(ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".renquant" / "account_cash_ledger"


def _import_cost_model() -> Any:
    """Import seam for the canonical cost contract (monkeypatchable in
    tests to simulate presence/absence deterministically)."""
    from renquant_common import cost_model  # noqa: PLC0415 (lazy: fail closed at use)

    return cost_model


def load_cost_contract() -> Any:
    """Load + verify the REQUIRED canonical cost contract, fail closed.

    Returns the verified ``renquant_common.cost_model`` module. Raises
    :class:`CostContractUnavailableError` when the module is missing (the
    installed renquant-common predates D-C8a) or when its fingerprint
    schema version / callable surface does not match
    :data:`REQUIRED_COST_MODEL_FINGERPRINT_SCHEMA_VERSION` — a contract
    that LOOKS different is treated as absent, never partially trusted.
    """
    try:
        contract = _import_cost_model()
    except ImportError as exc:
        raise CostContractUnavailableError(
            f"{REQUIRED_COST_MODEL_MODULE} is not importable (installed "
            "renquant-common predates the D-C8a cost contract): the account "
            "cash ledger REQUIRES the canonical cost model for worst-case "
            f"reservation debits — new entries fail closed. ({exc})"
        ) from exc
    schema_version = getattr(
        contract, "COST_MODEL_FINGERPRINT_SCHEMA_VERSION", None
    )
    if schema_version != REQUIRED_COST_MODEL_FINGERPRINT_SCHEMA_VERSION:
        raise CostContractUnavailableError(
            f"{REQUIRED_COST_MODEL_MODULE} fingerprint schema version "
            f"{schema_version!r} != required "
            f"{REQUIRED_COST_MODEL_FINGERPRINT_SCHEMA_VERSION!r} — treating "
            "the contract as absent (fail closed), never partially trusted"
        )
    missing = [
        name
        for name in _REQUIRED_COST_MODEL_SURFACE
        if not callable(getattr(contract, name, None))
    ]
    if missing:
        raise CostContractUnavailableError(
            f"{REQUIRED_COST_MODEL_MODULE} is missing required surface "
            f"{missing} — treating the contract as absent (fail closed)"
        )
    return contract


def worst_case_entry_debit(
    notional: float, cost_spec: Any
) -> "tuple[float, str, str]":
    """(debit, cost_model_sha256, canonical params json) for one BUY entry.

    ``debit = notional * (1 + per_side_cost_bps(spec) / 1e4)`` — the
    worst-case executable cash outflow for one side per the canonical cost
    contract (fee + half-spread + slippage + increment rounding). The sha is
    ``cost_model_content_sha256`` over the SAME spec the debit used, so the
    reservation/run evidence pins exactly which numbers sized the entry.
    ``cost_spec`` may be a ``CostModelSpec`` or its canonical dict form.
    Raises :class:`CostContractUnavailableError` when the contract is
    absent — there is NO notional-only fallback.
    """
    contract = load_cost_contract()
    notional_f = float(notional)
    if not math.isfinite(notional_f) or notional_f <= 0:
        raise AccountCashLedgerError(
            f"entry notional must be finite and positive: {notional!r}"
        )
    if isinstance(cost_spec, Mapping):
        spec = contract.cost_model_spec_from_dict(cost_spec)
    elif isinstance(cost_spec, contract.CostModelSpec):
        spec = cost_spec
    else:
        raise AccountCashLedgerError(
            "cost_spec must be a renquant_common.cost_model.CostModelSpec "
            f"or its canonical dict form, got {type(cost_spec).__name__}"
        )
    debit = notional_f * (1.0 + contract.per_side_cost_bps(spec) / 1e4)
    sha = contract.cost_model_content_sha256(spec)
    params_json = json.dumps(
        spec.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return debit, sha, params_json


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
    #: Cost-contract evidence stamp: content sha + canonical params of the
    #: CostModelSpec whose worst-case debit this reservation is (None only
    #: for raw storage-primitive writes, e.g. tests — every order-path
    #: reservation carries both).
    cost_model_sha256: Optional[str] = None
    cost_model_params: Optional[str] = None

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
                       parent_intent_id  TEXT PRIMARY KEY,
                       account_id        TEXT NOT NULL,
                       sleeve_tag        TEXT NOT NULL,
                       amount            REAL NOT NULL CHECK (amount > 0),
                       status            TEXT NOT NULL
                           CHECK (status IN ('active', 'released')),
                       reserved_at       REAL NOT NULL,
                       expires_at        REAL NOT NULL,
                       released_at       REAL,
                       release_reason    TEXT,
                       cost_model_sha256 TEXT,
                       cost_model_params TEXT
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
                ("hostname", socket.gethostname()),
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
                    hint = (
                        " — this is the exact signature of a same-host/"
                        "local-filesystem deployment violation (see the "
                        "module docstring's DEPLOYMENT CONSTRAINT section): "
                        "a second host is opening a db a different host "
                        "created, almost certainly over a network mount"
                        if key == "hostname" else ""
                    )
                    raise AccountCashLedgerError(
                        f"ledger db {self.db_path} has {key}={row['value']!r}, "
                        f"expected {expected!r} — refusing to mix ledgers{hint}"
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
            cost_model_sha256=(
                str(row["cost_model_sha256"])
                if row["cost_model_sha256"] is not None
                else None
            ),
            cost_model_params=(
                str(row["cost_model_params"])
                if row["cost_model_params"] is not None
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
    def reserve_entry(
        self,
        *,
        sleeve_tag: str,
        parent_intent_id: str,
        notional: float,
        cost_spec: Any,
        now: Optional[dt.datetime] = None,
    ) -> bool:
        """THE order-path reservation (CashLedgerPort seam): reserve the
        WORST-CASE EXECUTABLE DEBIT for one BUY entry.

        Computes ``notional * (1 + per_side_cost_bps(cost_spec)/1e4)``
        through the REQUIRED canonical cost contract
        (``renquant_common.cost_model`` — raises
        :class:`CostContractUnavailableError` when absent/unverifiable;
        there is NO notional-only fallback) and stamps the reservation row
        with ``cost_model_content_sha256(cost_spec)`` + the canonical params
        JSON, so the run evidence pins exactly which cost numbers sized the
        entry. Delegates to :meth:`reserve` for the atomic
        check-and-insert.
        """
        debit, sha, params_json = worst_case_entry_debit(notional, cost_spec)
        return self.reserve(
            sleeve_tag=sleeve_tag,
            parent_intent_id=parent_intent_id,
            amount=debit,
            now=now,
            cost_model_sha256=sha,
            cost_model_params=params_json,
        )

    def reserve(
        self,
        *,
        sleeve_tag: str,
        parent_intent_id: str,
        amount: float,
        now: Optional[dt.datetime] = None,
        cost_model_sha256: Optional[str] = None,
        cost_model_params: Optional[str] = None,
    ) -> bool:
        """Atomically reserve ``amount`` against the shared account headroom.

        STORAGE PRIMITIVE: order paths must go through :meth:`reserve_entry`
        (which computes the fee-inclusive worst-case debit through the
        canonical cost contract and stamps its sha here) — the
        ``CashLedgerPort`` seam exposes only ``reserve_entry``, never this.

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
                            release_reason, cost_model_sha256,
                            cost_model_params)
                       VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, NULL, ?, ?)
                       ON CONFLICT(parent_intent_id) DO UPDATE SET
                           sleeve_tag = excluded.sleeve_tag,
                           amount = excluded.amount,
                           status = 'active',
                           reserved_at = excluded.reserved_at,
                           expires_at = excluded.expires_at,
                           released_at = NULL,
                           release_reason = NULL,
                           cost_model_sha256 = excluded.cost_model_sha256,
                           cost_model_params = excluded.cost_model_params""",
                    (
                        pid,
                        self.account_id,
                        tag,
                        amount_f,
                        now_epoch,
                        now_epoch + self.ttl_seconds,
                        cost_model_sha256,
                        cost_model_params,
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


def build_shared_account_cash_ledger_for_broker(
    broker: BaseBroker,
    *,
    ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[AccountCashLedger]:
    """THE execution-owned wiring contract every launch path (the 104 batch
    process, the crypto 24/7 loop) MUST build its ledger handle through:

    - ``account_id`` is DERIVED from ``broker.get_account_id()`` — the
      broker's own verified real account identity — never accepted as a
      caller-supplied string (Codex round 1), so a per-sleeve tag can never
      leak into the ledger-identity slot;
    - the db LOCATION is resolved by :func:`account_cash_ledger_data_dir` —
      a fixed, machine-scoped canonical root with exactly one (tests/ops)
      override hook. There is NO path parameter (Codex round 2: round-1
      still accepted an arbitrary ``data_dir``, which let two sleeves
      silently create independent per-account databases); a divergent-path
      attempt is a ``TypeError`` at the call site, before any ledger, book,
      or order exists.

    Two sleeves that connect to the SAME brokerage account through their own
    :class:`BaseBroker` instances therefore always resolve to the SAME
    ``account_cash_ledger_db_path``, regardless of which sleeve's process
    constructs it — by construction, not by convention, PROVIDED both
    processes are co-resident on the same host/filesystem (see the module
    docstring's DEPLOYMENT CONSTRAINT). This guarantee is a same-filesystem
    path-resolution property only; it says nothing about whether two
    processes on DIFFERENT hosts/filesystems were even supposed to share
    one ledger in the first place — that is a control-plane preflight
    concern this function does not address. Verified end-to-end with two
    real OS processes on one filesystem (including deliberately divergent
    unrelated env vars) by ``tests/test_account_cash_ledger_shared_process.py``.

    Flag-gated: returns ``None`` (byte-identical legacy behavior) unless
    :data:`ACCOUNT_CASH_LEDGER_FLAG` is explicitly ON. ``broker.get_cash``
    supplies the fresh-every-transaction balance read.

    SAME-HOST DEPLOYMENT ACKNOWLEDGMENT (Codex round 3): the ledger's
    cross-process guarantee depends on POSIX advisory locking, which only
    holds reliably for processes on ONE machine sharing a LOCAL disk (see
    the module docstring's "DEPLOYMENT CONSTRAINT" section) — a caller
    running this on a network-mounted volume, or across two hosts, would
    silently defeat the whole reservation-serialization guarantee. Enabling
    the flag WITHOUT also setting
    :data:`ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST` is therefore a
    configuration error, not a legitimate "off" state: raises
    :class:`AccountCashLedgerError` rather than silently falling back to
    ``None`` (which would look identical to the flag being off at all,
    masking a half-configured deployment).
    """
    if not account_cash_ledger_enabled(env):
        return None
    source = os.environ if env is None else env
    if str(source.get(ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST, "")).strip().lower() not in _TRUTHY:
        raise AccountCashLedgerError(
            f"{ACCOUNT_CASH_LEDGER_FLAG} is enabled but "
            f"{ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST} is not — the "
            "account cash ledger's cross-process locking guarantee only "
            "holds for a same-host, local-filesystem deployment (see the "
            "module docstring); set the acknowledgment env var explicitly "
            "once that has been confirmed for this deployment, never assume it"
        )
    account_id = broker.get_account_id()
    return AccountCashLedger(
        account_cash_ledger_db_path(account_cash_ledger_data_dir(env=env), account_id),
        account_id=account_id,
        broker_cash_fn=broker.get_cash,
        ttl_seconds=ttl_seconds,
    )


def open_session_order_book(
    broker: BaseBroker,
    *,
    sleeve_tag: str,
    trading_day: str,
    cost_model_spec: Any | None = None,
    ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
    env: Optional[Mapping[str, str]] = None,
) -> OrderStateBook:
    """THE execution-owned session-book constructor BOTH real launch paths
    (the 104 batch process and the 105-style/crypto 24/7 loop — the two
    stacks that drive ``submit_remainder`` through a ``BrokerPort``) MUST
    route through if/when they adopt the shared account cash ledger.

    Scope note (Codex D-C4 round-4): this is a library contract, not a
    completed integration — no in-repo production code and no launch
    script in this repo calls this function today; renquant-execution does
    not own either launch path's entry point. Wiring the real 104 and 24/7
    processes onto this constructor (and proving, via an orchestrator-owned
    control-plane preflight, that both resolve to the identical ledger
    file before either submits an order) is separate, out-of-scope work
    tracked against renquant-orchestrator. Do not read this docstring as a
    claim that both launch paths are wired today.

    Launch paths that DO adopt it construct their per-sleeve
    ``OrderStateBook`` HERE instead of calling ``OrderStateBook(...)``
    directly, so the §5.3 shared-ledger wiring cannot be skipped or
    diverged per sleeve:

    - flag OFF -> a plain book (``cash_ledger=None``), byte-identical
      legacy behavior;
    - flag ON -> the shared account ledger is built through
      :func:`build_shared_account_cash_ledger_for_broker` (account id from
      the broker, location from the runtime env — both non-overridable) and
      the canonical cost contract is REQUIRED: ``cost_model_spec`` must be
      supplied and ``renquant_common.cost_model`` must load/verify, both
      checked HERE — a divergent-path or contract-absent misconfiguration
      fails at wiring time, BEFORE any order could be submitted. The
      session book is stamped with the cost-spec content sha
      (``book.cost_model_sha256``) as run evidence; every reservation row
      is stamped again by ``reserve_entry``.
    """
    ledger = build_shared_account_cash_ledger_for_broker(
        broker, ttl_seconds=ttl_seconds, env=env
    )
    if ledger is None:
        return OrderStateBook(account=sleeve_tag, trading_day=trading_day)
    if cost_model_spec is None:
        raise CostContractUnavailableError(
            "the account cash ledger is enabled but no cost_model_spec was "
            "supplied: reservations are the worst-case executable debit "
            "(notional + per-side costs per renquant_common.cost_model) — "
            "failing at wiring time, before any order could be submitted"
        )
    # Verify the contract + spec NOW (not at first BUY): a bad spec or an
    # absent contract must fail before the session opens. The probe debit
    # also yields the run-evidence sha stamped on the book.
    _, cost_sha, _ = worst_case_entry_debit(1.0, cost_model_spec)
    book = OrderStateBook(
        account=sleeve_tag,
        trading_day=trading_day,
        cash_ledger=ledger,
        cost_model_spec=cost_model_spec,
    )
    book.cost_model_sha256 = cost_sha
    return book


__all__ = [
    "ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST",
    "ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE",
    "ACCOUNT_CASH_LEDGER_FLAG",
    "ACCOUNT_CASH_LEDGER_SCHEMA_VERSION",
    "AccountCashLedger",
    "AccountCashLedgerError",
    "CostContractUnavailableError",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "HALT_REASON_RECHECK_MISMATCH",
    "HALT_REASON_RECONCILE_MISMATCH",
    "HALT_REASON_UNKNOWN_OPEN_BUY",
    "LedgerSweepResult",
    "REQUIRED_COST_MODEL_FINGERPRINT_SCHEMA_VERSION",
    "REQUIRED_COST_MODEL_MODULE",
    "REQUIRED_COST_MODEL_PACKAGE_FLOOR",
    "RESERVATION_GRACE_SECONDS",
    "ReservationRow",
    "account_cash_ledger_db_path",
    "account_cash_ledger_enabled",
    "build_shared_account_cash_ledger_for_broker",
    "load_cost_contract",
    "open_session_order_book",
    "parent_intent_id_from_client_order_id",
    "account_cash_ledger_data_dir",
    "worst_case_entry_debit",
]
