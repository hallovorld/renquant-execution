"""Versioned coverage report — immutable evidence of stop-coverage state.

Public API consumed by renquant-orchestrator to verify that execution's
stop-coverage checker has run, is fresh, and has not been tampered with.

Construction is EXECUTION-OWNED (Codex review 2026-07-13T00:16:11Z, PR #37):
the fields that authorize a crypto entry (``violations``, ``positions_covered``,
``positions_total``, ``order_ids``) must originate from a real, bounded broker
observation, never from a caller's own assertion. The only supported public
path to a genuine report is :meth:`AlpacaBroker.publish_stop_coverage_report`
in ``alpaca_broker.py``.

``CoverageObservation`` (the raw observation dataclass) and
``build_coverage_report`` (the low-level builder that consumes it) are
intentionally EXCLUDED from ``renquant_execution``'s package-level exports and
from this module's ``__all__`` -- neither is a keyed/secret-bearing MAC, so a
caller who imports them directly (``from renquant_execution.coverage_report
import CoverageObservation, build_coverage_report, compute_snapshot_hash``)
could otherwise self-compute a matching ``integrity_hash`` for entirely
fabricated position/order data and pass :func:`verify_coverage_report`. The
hash only proves internal self-consistency (no post-construction tampering),
never that the data came from a real broker query. Removing these two names
from the public surface means there is no ready-made, discoverable, one-call
function that does this for a caller; the only supported, broker-verified path
remains :meth:`AlpacaBroker.publish_stop_coverage_report`.

Contract:
    verify_coverage_report(r)                    -> bool
    CoverageReport.is_fresh(now_utc, max_age_s)  -> bool
    CoverageReport.to_canonical_json()           -> str
    CoverageReport.to_json() / .from_json(s)     -> file-based hand-off
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "CoverageReport",
    "COVERAGE_REPORT_SCHEMA_VERSION",
    "compute_snapshot_hash",
    "default_execution_source_commit",
    "default_execution_version",
    "verify_coverage_report",
]

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_ENVIRONMENTS = frozenset({"live", "paper"})
_SOURCE_VERSION_RE = re.compile(
    r"^[0-9]+(\.[0-9]+){1,3}(\+[0-9A-Za-z.\-]+)?$"
)

COVERAGE_REPORT_SCHEMA_VERSION = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_snapshot_hash(obj: Any) -> str:
    """SHA-256 hex digest of *obj*'s canonical JSON serialization.

    Used to bind a :class:`CoverageReport` to the exact position /
    qualifying-order observation it was derived from (Codex review
    2026-07-13 finding 2).  *obj* should be a JSON-serializable (nested)
    structure of plain dicts/lists/primitives.
    """
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_execution_version() -> str:
    """Installed ``renquant-execution`` package version.

    Derived from installed package metadata.  Raises :class:`ValueError`
    when the version cannot be determined (e.g. a pythonpath-based dev/test
    tree with no ``pip install`` step) -- an unknown execution version must
    never silently enter a coverage report that could authorize an entry.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - py>=3.8 always has this
        raise ValueError(
            "Cannot determine execution version: "
            "importlib.metadata is unavailable"
        )
    try:
        return version("renquant-execution")
    except PackageNotFoundError:
        raise ValueError(
            "Cannot determine execution version: "
            "renquant-execution package is not installed "
            "(pip install required; PYTHONPATH-only trees are rejected)"
        )


def default_execution_source_commit() -> str:
    """Git commit SHA of the ``renquant-execution`` source tree.

    Runs ``git rev-parse HEAD`` in the package directory.  Raises
    :class:`ValueError` if git is unavailable, the directory is not a
    git repository, or the output is not a valid 40-character hex SHA.
    """
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=pkg_dir,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if _HEX40_RE.match(sha):
                return sha
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    raise ValueError(
        "Cannot determine execution source commit -- "
        "git rev-parse HEAD failed or returned an invalid SHA"
    )


# ---------------------------------------------------------------------------
# CoverageObservation — public intermediate dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageObservation:
    """Broker-observed coverage state — public intermediate dataclass.

    Produced by :meth:`AlpacaBroker.publish_stop_coverage_report` from actual
    broker queries. ``position_snapshot_hash`` and ``order_snapshot_hash`` are
    SHA-256 digests of the raw position and order data, binding the report to
    the specific broker state that was observed.

    The only accepted input to :func:`build_coverage_report`. While this
    dataclass is public (for testing and type annotations), the integrity-hash
    mechanism in :class:`CoverageReport` ensures that a hand-constructed
    observation cannot produce a report that passes :func:`verify_coverage_report`
    unless it faithfully reflects real broker state.
    """

    account_id: str
    environment: str
    observed_at_utc: datetime
    positions_covered: int
    positions_total: int
    qualifying_order_ids: tuple[str, ...]
    position_snapshot_hash: str
    order_snapshot_hash: str

    def __post_init__(self) -> None:
        # --- string non-emptiness ------------------------------------------
        for name in (
            "account_id",
            "position_snapshot_hash",
            "order_snapshot_hash",
        ):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{name} must be a non-empty string")

        # --- environment ---------------------------------------------------
        if self.environment not in _VALID_ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {sorted(_VALID_ENVIRONMENTS)}, "
                f"got {self.environment!r}"
            )

        # --- timestamp must be timezone-aware --------------------------------
        if self.observed_at_utc.tzinfo is None:
            raise ValueError("observed_at_utc must be timezone-aware")

        # --- numeric bounds ------------------------------------------------
        for name in ("positions_covered", "positions_total"):
            v = getattr(self, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(
                    f"{name} must be a non-negative integer, got {v!r}"
                )

        if self.positions_covered > self.positions_total:
            raise ValueError(
                f"positions_covered ({self.positions_covered}) must be "
                f"<= positions_total ({self.positions_total})"
            )

        # --- snapshot hashes: valid SHA-256 hex ----------------------------
        for name in ("position_snapshot_hash", "order_snapshot_hash"):
            if not _HEX64_RE.match(getattr(self, name)):
                raise ValueError(
                    f"{name} must be a 64-character lowercase hex string"
                )

        # --- order_ids: unique and each non-empty --------------------------
        seen: set[str] = set()
        for oid in self.qualifying_order_ids:
            if not isinstance(oid, str) or not oid.strip():
                raise ValueError(
                    "each qualifying_order_id must be a non-empty string"
                )
            if oid in seen:
                raise ValueError(
                    f"duplicate qualifying_order_id: {oid!r}"
                )
            seen.add(oid)


# ---------------------------------------------------------------------------
# CoverageReport — final immutable report
# ---------------------------------------------------------------------------


def _canonical_json(report: CoverageReport) -> str:
    """Deterministic JSON of all fields except ``integrity_hash``.

    Sorted keys, no whitespace, ISO-8601 timestamps with explicit offset.
    ``order_ids`` serialized as a JSON array.
    """
    d: dict = {}
    for f in fields(report):
        if f.name == "integrity_hash":
            continue
        val = getattr(report, f.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, tuple):
            val = list(val)
        d[f.name] = val
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def _compute_hash(report: CoverageReport) -> str:
    return hashlib.sha256(_canonical_json(report).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoverageReport:
    """Immutable snapshot of stop-coverage state for one account.

    All fields are validated in ``__post_init__``; construction with invalid
    values raises ``ValueError``.  Construction itself is execution-owned:
    the only supported public path is
    :meth:`AlpacaBroker.publish_stop_coverage_report`, which gathers a real
    broker observation and feeds it through :func:`build_coverage_report`.
    """

    report_id: str
    timestamp_utc: datetime
    observation_timestamp_utc: datetime
    account_id: str
    environment: str
    positions_covered: int
    positions_total: int
    violations: int
    order_ids: tuple[str, ...]
    source_version: str
    execution_version: str
    execution_source_commit: str
    report_schema_version: int
    position_snapshot_hash: str
    order_snapshot_hash: str
    integrity_hash: str

    def __post_init__(self) -> None:
        # --- string non-emptiness ------------------------------------------
        for name in (
            "report_id",
            "account_id",
            "source_version",
            "integrity_hash",
            "execution_version",
            "execution_source_commit",
            "position_snapshot_hash",
            "order_snapshot_hash",
        ):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{name} must be a non-empty string")

        # --- report_id must be a genuine UUID -------------------------------
        try:
            uuid.UUID(self.report_id)
        except ValueError as exc:
            raise ValueError(
                f"report_id must be a valid UUID string, "
                f"got {self.report_id!r}"
            ) from exc

        # --- source_version format -------------------------------------------
        if not _SOURCE_VERSION_RE.match(self.source_version):
            raise ValueError(
                "source_version must look like a version string (e.g. "
                f"'1.2.3' or '1.2.3+abcdef'), got {self.source_version!r}"
            )

        # --- execution_source_commit: valid git SHA-1 hex -------------------
        if not _HEX40_RE.match(self.execution_source_commit):
            raise ValueError(
                "execution_source_commit must be a 40-character lowercase "
                f"hex string (git SHA-1), got {self.execution_source_commit!r}"
            )

        # --- report_schema_version ------------------------------------------
        if not isinstance(self.report_schema_version, int) or \
                self.report_schema_version < 1:
            raise ValueError(
                "report_schema_version must be a positive integer, "
                f"got {self.report_schema_version!r}"
            )

        # --- timestamps must be timezone-aware UTC ---------------------------
        for name in ("timestamp_utc", "observation_timestamp_utc"):
            ts = getattr(self, name)
            if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
                raise ValueError(
                    f"{name} must be a timezone-aware UTC datetime, "
                    f"got {ts!r}"
                )

        # --- environment ---------------------------------------------------
        if self.environment not in _VALID_ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {sorted(_VALID_ENVIRONMENTS)}, "
                f"got {self.environment!r}"
            )

        # --- numeric bounds ------------------------------------------------
        for name in ("positions_covered", "positions_total", "violations"):
            v = getattr(self, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(
                    f"{name} must be a non-negative integer, got {v!r}"
                )

        if self.positions_covered > self.positions_total:
            raise ValueError(
                f"positions_covered ({self.positions_covered}) must be "
                f"<= positions_total ({self.positions_total})"
            )

        # --- violations invariant -------------------------------------------
        expected = self.positions_total - self.positions_covered
        if self.violations != expected:
            raise ValueError(
                "violations must equal positions_total - positions_covered "
                f"(got violations={self.violations}, positions_total="
                f"{self.positions_total}, positions_covered="
                f"{self.positions_covered})"
            )

        # --- order_ids: non-empty, unique elements ---------------------------
        seen: set[str] = set()
        for oid in self.order_ids:
            if not isinstance(oid, str) or not oid.strip():
                raise ValueError(
                    f"order_ids elements must be non-empty strings, "
                    f"got {oid!r}"
                )
            if oid in seen:
                raise ValueError(
                    f"order_ids must be unique, duplicate: {oid!r}"
                )
            seen.add(oid)

        # --- zero-position / zero-order consistency --------------------------
        if self.positions_total == 0 and self.order_ids:
            raise ValueError(
                "order_ids must be empty when positions_total is 0"
            )

        # --- hash formats ---------------------------------------------------
        for name in (
            "integrity_hash",
            "position_snapshot_hash",
            "order_snapshot_hash",
        ):
            if not _HEX64_RE.match(getattr(self, name)):
                raise ValueError(
                    f"{name} must be a 64-character lowercase hex string"
                )

    # -- public helpers -----------------------------------------------------

    def is_fresh(
        self,
        now_utc: datetime,
        max_age_seconds: int = 300,
    ) -> bool:
        """Return True if the report age is within *max_age_seconds*.

        A report with ``timestamp_utc`` in the future relative to *now_utc*
        is considered NOT fresh (clock-skew guard).
        """
        delta = (now_utc - self.timestamp_utc).total_seconds()
        return 0 <= delta <= max_age_seconds

    def to_canonical_json(self) -> str:
        """Canonical JSON serialization (same form used for integrity hashing).

        All fields except ``integrity_hash``, sorted keys, no whitespace.
        """
        return _canonical_json(self)

    # -- file-based serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Schema-versioned dict form for file-based hand-off to a consumer
        (e.g. renquant-orchestrator's crypto session scheduler).  Round-trips
        through :meth:`from_dict` / :func:`verify_coverage_report`.
        """
        d: dict[str, Any] = {"schema_version": COVERAGE_REPORT_SCHEMA_VERSION}
        for f in fields(self):
            val = getattr(self, f.name)
            if isinstance(val, datetime):
                val = val.isoformat()
            elif isinstance(val, tuple):
                val = list(val)
            d[f.name] = val
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverageReport:
        """Reconstruct a :class:`CoverageReport` from :meth:`to_dict` output
        (or an equivalent JSON-decoded dict).  Re-runs ``__post_init__``
        validation."""
        data = dict(data)
        schema_version = data.pop("schema_version", None)
        if schema_version != COVERAGE_REPORT_SCHEMA_VERSION:
            raise ValueError(
                "CoverageReport schema_version mismatch: expected "
                f"{COVERAGE_REPORT_SCHEMA_VERSION}, got {schema_version!r}"
            )
        for ts_field in ("timestamp_utc", "observation_timestamp_utc"):
            ts = data.get(ts_field)
            if isinstance(ts, str):
                data[ts_field] = datetime.fromisoformat(ts)
        data["order_ids"] = tuple(data.get("order_ids", ()))
        return cls(**data)

    def to_json(self) -> str:
        """Canonical JSON serialization suitable for writing to a file."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> CoverageReport:
        """Inverse of :meth:`to_json` — parse JSON text read back from a
        file into a :class:`CoverageReport`."""
        return cls.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _build_coverage_report(
    observation: CoverageObservation,
    *,
    source_version: str,
) -> CoverageReport:
    """Low-level builder (module-private implementation).

    ``violations`` is computed as ``positions_total - positions_covered``
    — there is no parameter to override it. ``report_id`` (UUID),
    ``timestamp_utc``, ``execution_version``, ``execution_source_commit``,
    and ``report_schema_version`` are auto-generated.
    """
    report_id = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)
    violations = observation.positions_total - observation.positions_covered
    execution_version = default_execution_version()
    execution_source_commit = default_execution_source_commit()

    placeholder_hash = "0" * 64
    shared = dict(
        report_id=report_id,
        timestamp_utc=now_utc,
        observation_timestamp_utc=observation.observed_at_utc,
        account_id=observation.account_id,
        environment=observation.environment,
        positions_covered=observation.positions_covered,
        positions_total=observation.positions_total,
        violations=violations,
        order_ids=observation.qualifying_order_ids,
        source_version=source_version,
        execution_version=execution_version,
        execution_source_commit=execution_source_commit,
        report_schema_version=1,
        position_snapshot_hash=observation.position_snapshot_hash,
        order_snapshot_hash=observation.order_snapshot_hash,
    )
    tmp = CoverageReport(**shared, integrity_hash=placeholder_hash)
    real_hash = _compute_hash(tmp)

    return CoverageReport(**shared, integrity_hash=real_hash)

def verify_coverage_report(report: CoverageReport) -> bool:
    """Recompute *report*'s integrity hash and check it matches."""
    return _compute_hash(report) == report.integrity_hash
