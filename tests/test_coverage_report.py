"""Tests for renquant_execution.coverage_report — versioned coverage report API.

Coverage:
- build + verify roundtrip (happy path)
- tampered field -> verify fails (each mutable-equivalent field)
- freshness: fresh, stale, future timestamp
- validation: every __post_init__ guard
- invalid environment rejected
- positions_covered > positions_total rejected
- order_ids immutability (tuple)
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from renquant_execution.coverage_report import (
    CoverageReport,
    build_coverage_report,
    verify_coverage_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 12, 14, 0, 0, tzinfo=timezone.utc)


def _sample_report(**overrides) -> CoverageReport:
    """Build a valid report, merging *overrides* into defaults."""
    defaults = dict(
        timestamp_utc=_NOW,
        account_id="ACCT-001",
        environment="live",
        positions_covered=8,
        positions_total=10,
        violations=1,
        order_ids=("ord-a", "ord-b"),
        source_version="1.2.3",
    )
    defaults.update(overrides)
    return build_coverage_report(**defaults)


# ---------------------------------------------------------------------------
# Build + verify roundtrip
# ---------------------------------------------------------------------------


class TestBuildVerifyRoundtrip:
    def test_happy_path(self):
        r = _sample_report()
        assert verify_coverage_report(r)
        assert r.account_id == "ACCT-001"
        assert r.environment == "live"
        assert r.positions_covered == 8
        assert r.positions_total == 10
        assert r.violations == 1
        assert r.order_ids == ("ord-a", "ord-b")
        assert r.source_version == "1.2.3"
        assert len(r.report_id) == 36  # UUID format
        assert len(r.integrity_hash) == 64

    def test_paper_environment(self):
        r = _sample_report(environment="paper")
        assert verify_coverage_report(r)
        assert r.environment == "paper"

    def test_zero_positions(self):
        r = _sample_report(positions_covered=0, positions_total=0, violations=0)
        assert verify_coverage_report(r)

    def test_empty_order_ids(self):
        r = _sample_report(order_ids=())
        assert verify_coverage_report(r)


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection:
    """Replacing any field with object.__setattr__ should break verification."""

    @pytest.fixture()
    def report(self):
        return _sample_report()

    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("account_id", "TAMPERED"),
            ("environment", "paper"),
            ("positions_covered", 999),
            ("positions_total", 999),
            ("violations", 42),
            ("source_version", "0.0.0"),
            ("report_id", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        ],
    )
    def test_tampered_field_fails_verify(self, report, field, bad_value):
        # Bypass frozen to simulate a tampered wire-format reconstruction.
        object.__setattr__(report, field, bad_value)
        assert not verify_coverage_report(report)

    def test_tampered_order_ids_fails_verify(self, report):
        object.__setattr__(report, "order_ids", ("injected",))
        assert not verify_coverage_report(report)

    def test_tampered_timestamp_fails_verify(self, report):
        object.__setattr__(
            report,
            "timestamp_utc",
            _NOW + timedelta(hours=1),
        )
        assert not verify_coverage_report(report)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_fresh_within_default(self):
        r = _sample_report(timestamp_utc=_NOW)
        assert r.is_fresh(_NOW + timedelta(seconds=60))

    def test_fresh_at_boundary(self):
        r = _sample_report(timestamp_utc=_NOW)
        assert r.is_fresh(_NOW + timedelta(seconds=300))

    def test_stale_beyond_default(self):
        r = _sample_report(timestamp_utc=_NOW)
        assert not r.is_fresh(_NOW + timedelta(seconds=301))

    def test_stale_custom_max_age(self):
        r = _sample_report(timestamp_utc=_NOW)
        assert r.is_fresh(_NOW + timedelta(seconds=59), max_age_seconds=60)
        assert not r.is_fresh(_NOW + timedelta(seconds=61), max_age_seconds=60)

    def test_future_timestamp_not_fresh(self):
        """A report from the future (clock skew) is NOT considered fresh."""
        r = _sample_report(timestamp_utc=_NOW + timedelta(seconds=10))
        assert not r.is_fresh(_NOW)


# ---------------------------------------------------------------------------
# Validation — __post_init__
# ---------------------------------------------------------------------------


class TestValidation:
    """Each __post_init__ guard must raise ValueError with a useful message."""

    def test_empty_account_id(self):
        with pytest.raises(ValueError, match="account_id"):
            _sample_report(account_id="")

    def test_whitespace_account_id(self):
        with pytest.raises(ValueError, match="account_id"):
            _sample_report(account_id="   ")

    def test_invalid_environment(self):
        with pytest.raises(ValueError, match="environment"):
            _sample_report(environment="staging")

    def test_negative_positions_covered(self):
        with pytest.raises(ValueError, match="positions_covered"):
            _sample_report(positions_covered=-1)

    def test_negative_positions_total(self):
        with pytest.raises(ValueError, match="positions_total"):
            _sample_report(positions_total=-1, positions_covered=0)

    def test_negative_violations(self):
        with pytest.raises(ValueError, match="violations"):
            _sample_report(violations=-1)

    def test_covered_exceeds_total(self):
        with pytest.raises(ValueError, match="positions_covered.*<=.*positions_total"):
            _sample_report(positions_covered=11, positions_total=10)

    def test_empty_source_version(self):
        with pytest.raises(ValueError, match="source_version"):
            _sample_report(source_version="")

    def test_bad_integrity_hash_format(self):
        """Directly constructing with a non-hex hash must fail."""
        with pytest.raises(ValueError, match="integrity_hash"):
            CoverageReport(
                report_id="test-id",
                timestamp_utc=_NOW,
                account_id="ACCT",
                environment="live",
                positions_covered=0,
                positions_total=0,
                violations=0,
                order_ids=(),
                source_version="1.0",
                integrity_hash="not-a-valid-hex",
            )

    def test_empty_report_id(self):
        with pytest.raises(ValueError, match="report_id"):
            CoverageReport(
                report_id="",
                timestamp_utc=_NOW,
                account_id="ACCT",
                environment="live",
                positions_covered=0,
                positions_total=0,
                violations=0,
                order_ids=(),
                source_version="1.0",
                integrity_hash="a" * 64,
            )

    def test_empty_integrity_hash(self):
        with pytest.raises(ValueError, match="integrity_hash"):
            CoverageReport(
                report_id="test-id",
                timestamp_utc=_NOW,
                account_id="ACCT",
                environment="live",
                positions_covered=0,
                positions_total=0,
                violations=0,
                order_ids=(),
                source_version="1.0",
                integrity_hash="",
            )


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_frozen(self):
        r = _sample_report()
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.account_id = "hacked"  # type: ignore[misc]

    def test_order_ids_is_tuple(self):
        r = _sample_report()
        assert isinstance(r.order_ids, tuple)


# ---------------------------------------------------------------------------
# Deterministic hashing
# ---------------------------------------------------------------------------


class TestDeterministicHash:
    def test_same_inputs_same_hash(self):
        """Two reports built from identical inputs (except report_id/hash)
        should produce different hashes — because report_id differs (UUID).
        """
        r1 = _sample_report()
        r2 = _sample_report()
        # report_id is a fresh UUID each time, so hashes differ.
        assert r1.integrity_hash != r2.integrity_hash

    def test_hash_is_lowercase_hex(self):
        r = _sample_report()
        assert r.integrity_hash == r.integrity_hash.lower()
        assert len(r.integrity_hash) == 64
        assert all(c in "0123456789abcdef" for c in r.integrity_hash)
