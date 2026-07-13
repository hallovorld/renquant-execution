"""Versioned coverage report — immutable evidence of stop-coverage state.

Public API consumed by renquant-orchestrator to verify that execution's
stop-coverage checker has run, is fresh, and has not been tampered with.

Contract:
    build_coverage_report(...)  -> CoverageReport   # construct + hash
    verify_coverage_report(r)   -> bool              # integrity check
    CoverageReport.is_fresh(now_utc, max_age_seconds)  # staleness gate

The integrity_hash is SHA-256 of the canonical JSON serialization of all
fields except itself.  Any mutation after construction invalidates the hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone

__all__ = [
    "CoverageReport",
    "build_coverage_report",
    "verify_coverage_report",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_ENVIRONMENTS = frozenset({"live", "paper"})


def _canonical_json(report: CoverageReport) -> str:
    """Deterministic JSON of all fields except integrity_hash.

    Sorted keys, no whitespace, ISO-8601 timestamp with explicit +00:00.
    ``order_ids`` is serialized as a JSON array (it is a tuple at runtime).
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
    values raises ``ValueError``.
    """

    report_id: str
    timestamp_utc: datetime
    account_id: str
    environment: str
    positions_covered: int
    positions_total: int
    violations: int
    order_ids: tuple[str, ...]
    source_version: str
    integrity_hash: str

    def __post_init__(self) -> None:
        # --- string non-emptiness ------------------------------------------
        for name in ("report_id", "account_id", "source_version", "integrity_hash"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")

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
                raise ValueError(f"{name} must be a non-negative integer, got {v!r}")

        if self.positions_covered > self.positions_total:
            raise ValueError(
                f"positions_covered ({self.positions_covered}) must be "
                f"<= positions_total ({self.positions_total})"
            )

        # --- integrity_hash format -----------------------------------------
        if not _HEX64_RE.match(self.integrity_hash):
            raise ValueError(
                "integrity_hash must be a 64-character lowercase hex string"
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


def build_coverage_report(
    *,
    timestamp_utc: datetime,
    account_id: str,
    environment: str,
    positions_covered: int,
    positions_total: int,
    violations: int,
    order_ids: tuple[str, ...],
    source_version: str,
) -> CoverageReport:
    """Construct a :class:`CoverageReport` with generated id and hash.

    Parameters mirror :class:`CoverageReport` minus ``report_id`` (auto-UUID)
    and ``integrity_hash`` (auto-computed).
    """
    report_id = str(uuid.uuid4())

    # Build a temporary object without integrity_hash to compute the hash.
    # We use a placeholder that satisfies validation, then replace.
    placeholder_hash = "0" * 64
    tmp = CoverageReport(
        report_id=report_id,
        timestamp_utc=timestamp_utc,
        account_id=account_id,
        environment=environment,
        positions_covered=positions_covered,
        positions_total=positions_total,
        violations=violations,
        order_ids=order_ids,
        source_version=source_version,
        integrity_hash=placeholder_hash,
    )
    real_hash = _compute_hash(tmp)

    # Reconstruct with the real hash (frozen dataclass — can't mutate).
    return CoverageReport(
        report_id=report_id,
        timestamp_utc=timestamp_utc,
        account_id=account_id,
        environment=environment,
        positions_covered=positions_covered,
        positions_total=positions_total,
        violations=violations,
        order_ids=order_ids,
        source_version=source_version,
        integrity_hash=real_hash,
    )


def verify_coverage_report(report: CoverageReport) -> bool:
    """Recompute *report*'s integrity hash and check it matches."""
    return _compute_hash(report) == report.integrity_hash
