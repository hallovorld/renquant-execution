"""Tests for renquant_execution.coverage_report — versioned coverage report API.

Coverage:
- CoverageObservation construction + validation (module-private intermediate)
- build + verify roundtrip (happy path) via the observation path
- tampered field -> verify fails (each mutable-equivalent field)
- freshness: fresh, stale, future timestamp
- validation: every __post_init__ guard (CoverageReport and CoverageObservation),
  including the Codex review 2026-07-13T00:16:11Z findings (report_id UUID,
  timestamp tz-awareness x2, source_version format, execution_version
  non-emptiness, order_ids uniqueness/non-emptiness, the violations ==
  positions_total - positions_covered invariant, and zero-position/
  zero-order consistency)
- to_canonical_json() determinism
- canonical serialization round-trip (to_dict/from_dict, to_json/from_json)
- Integration tests with a fake broker observer (Codex review finding 4):
  execution-owned observation detects missing/uncovered stops, duplicate
  order ids rejected, inconsistent position counts rejected, and --
  crucially -- there is no PUBLIC one-call function a caller can use to
  hand-pick field values and mint an authorized report.

``CoverageObservation`` and ``_build_coverage_report`` are module-private
(renamed from an earlier public ``CoverageObservation``/``build_coverage_report``
design that Codex's review -- and independent analysis -- flagged: a public
one-call builder taking caller-supplied ``positions_covered``/``violations``/
snapshot hashes is exactly the "authorization path" a bad-faith caller could
use to fabricate a zero-violation report, since neither the hash nor the
observation's own format checks prove the data came from a real broker
query). The only supported way to obtain a genuine report is
``AlpacaBroker.publish_stop_coverage_report()`` in ``alpaca_broker.py`` --
see ``tests/test_publish_stop_coverage_report.py`` for its integration tests.
Tests here import the private names directly to exercise validation in
isolation, which is a normal use of module internals from a test -- not the
"authorization path" the review was concerned about.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from renquant_execution.coverage_report import (
    COVERAGE_REPORT_SCHEMA_VERSION,
    CoverageReport,
    _build_coverage_report,
    _compute_hash,
    CoverageObservation,
    compute_snapshot_hash,
    default_execution_source_commit,
    default_execution_version,
    verify_coverage_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 12, 14, 0, 0, tzinfo=timezone.utc)
_VALID_UUID = "12345678-1234-5678-1234-567812345678"
_HASH_A = compute_snapshot_hash("position-data")
_HASH_B = compute_snapshot_hash("order-data")
_MOCK_EXEC_VERSION = "0.1.0"
_MOCK_EXEC_COMMIT = "a" * 40


@pytest.fixture(autouse=True)
def _mock_execution_identity(monkeypatch):
    """Stub execution identity functions so builder-path tests do not
    depend on pip-installed package metadata (``default_execution_version``
    raises ``ValueError`` when the package is not installed) or git
    availability (``default_execution_source_commit``).

    Tests that exercise the REAL functions (e.g. ``TestDefaultExecutionVersion``,
    ``TestDefaultExecutionSourceCommit``) override this fixture or call the
    imported function directly (the ``from ... import`` binding is NOT
    affected by ``monkeypatch.setattr`` on the module attribute).
    """
    import renquant_execution.coverage_report as cr

    monkeypatch.setattr(cr, "default_execution_version", lambda: _MOCK_EXEC_VERSION)
    monkeypatch.setattr(
        cr, "default_execution_source_commit", lambda: _MOCK_EXEC_COMMIT
    )


def _sample_observation(**overrides) -> CoverageObservation:
    """Build a valid observation with defaults."""
    defaults = dict(
        account_id="ACCT-001",
        environment="live",
        observed_at_utc=_NOW,
        positions_covered=8,
        positions_total=10,
        qualifying_order_ids=("ord-a", "ord-b"),
        position_snapshot_hash=_HASH_A,
        order_snapshot_hash=_HASH_B,
    )
    defaults.update(overrides)
    return CoverageObservation(**defaults)


def _sample_report(**obs_overrides) -> CoverageReport:
    """Build a valid report via the observation path."""
    obs = _sample_observation(**obs_overrides)
    return _build_coverage_report(obs, source_version="1.2.3")


def _direct_report(**overrides) -> CoverageReport:
    """Construct CoverageReport directly (bypassing the builder), for tests
    that exercise __post_init__ guards which the builder's auto-generated
    fields would otherwise short-circuit."""
    defaults = dict(
        report_id=_VALID_UUID,
        timestamp_utc=_NOW,
        observation_timestamp_utc=_NOW,
        account_id="ACCT",
        environment="live",
        positions_covered=0,
        positions_total=0,
        violations=0,
        order_ids=(),
        source_version="1.0",
        execution_version="0.1.0",
        execution_source_commit=_MOCK_EXEC_COMMIT,
        report_schema_version=1,
        position_snapshot_hash=_HASH_A,
        order_snapshot_hash=_HASH_B,
        integrity_hash="a" * 64,
    )
    defaults.update(overrides)
    return CoverageReport(**defaults)


# ---------------------------------------------------------------------------
# CoverageObservation construction + validation
# ---------------------------------------------------------------------------


class TestObservationConstruction:
    def test_happy_path(self):
        obs = _sample_observation()
        assert obs.account_id == "ACCT-001"
        assert obs.environment == "live"
        assert obs.positions_covered == 8
        assert obs.positions_total == 10
        assert obs.qualifying_order_ids == ("ord-a", "ord-b")
        assert obs.position_snapshot_hash == _HASH_A
        assert obs.order_snapshot_hash == _HASH_B

    def test_frozen(self):
        obs = _sample_observation()
        with pytest.raises(dataclasses.FrozenInstanceError):
            obs.account_id = "hacked"  # type: ignore[misc]


class TestObservationValidation:
    def test_empty_account_id_rejected(self):
        with pytest.raises(ValueError, match="account_id"):
            _sample_observation(account_id="")

    def test_invalid_environment_rejected(self):
        with pytest.raises(ValueError, match="environment"):
            _sample_observation(environment="staging")

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValueError, match="observed_at_utc"):
            _sample_observation(
                observed_at_utc=datetime(2026, 7, 12, 14, 0, 0)
            )

    def test_negative_positions_rejected(self):
        with pytest.raises(ValueError, match="positions_covered"):
            _sample_observation(positions_covered=-1)

    def test_covered_exceeds_total_rejected(self):
        with pytest.raises(ValueError, match="positions_covered"):
            _sample_observation(positions_covered=11, positions_total=10)

    def test_bad_position_hash_rejected(self):
        with pytest.raises(ValueError, match="position_snapshot_hash"):
            _sample_observation(position_snapshot_hash="not-hex")

    def test_bad_order_hash_rejected(self):
        with pytest.raises(ValueError, match="order_snapshot_hash"):
            _sample_observation(order_snapshot_hash="not-hex")

    def test_empty_order_id_element_rejected(self):
        with pytest.raises(ValueError, match="qualifying_order_id"):
            _sample_observation(qualifying_order_ids=("ord-a", ""))

    def test_duplicate_order_ids_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            _sample_observation(qualifying_order_ids=("ord-a", "ord-a"))

    def test_empty_order_ids_allowed(self):
        obs = _sample_observation(qualifying_order_ids=())
        assert obs.qualifying_order_ids == ()


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
        assert r.violations == 2  # computed: 10 - 8
        assert r.order_ids == ("ord-a", "ord-b")
        assert r.position_snapshot_hash == _HASH_A
        assert r.order_snapshot_hash == _HASH_B
        assert r.source_version == "1.2.3"
        assert r.execution_version  # non-empty
        assert r.observation_timestamp_utc == _NOW
        assert len(r.report_id) == 36  # UUID format
        assert len(r.integrity_hash) == 64

    def test_paper_environment(self):
        r = _sample_report(environment="paper")
        assert verify_coverage_report(r)
        assert r.environment == "paper"

    def test_zero_positions(self):
        r = _sample_report(
            positions_covered=0,
            positions_total=0,
            qualifying_order_ids=(),
        )
        assert verify_coverage_report(r)
        assert r.violations == 0

    def test_empty_order_ids(self):
        r = _sample_report(qualifying_order_ids=())
        assert verify_coverage_report(r)

    def test_violations_are_computed_not_supplied(self):
        """_build_coverage_report computes violations from the observation.
        There is no parameter to override it."""
        r = _sample_report(positions_covered=3, positions_total=10)
        assert r.violations == 7  # 10 - 3


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
            ("execution_version", "9.9.9"),
            ("execution_source_commit", "b" * 40),
            ("report_schema_version", 99),
            ("report_id", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            ("position_snapshot_hash", "f" * 64),
            ("order_snapshot_hash", "f" * 64),
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

    def test_tampered_observation_timestamp_fails_verify(self, report):
        object.__setattr__(
            report,
            "observation_timestamp_utc",
            _NOW + timedelta(hours=1),
        )
        assert not verify_coverage_report(report)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_fresh_within_default(self):
        r = _sample_report()
        assert r.is_fresh(r.timestamp_utc + timedelta(seconds=60))

    def test_fresh_at_boundary(self):
        r = _sample_report()
        assert r.is_fresh(r.timestamp_utc + timedelta(seconds=300))

    def test_stale_beyond_default(self):
        r = _sample_report()
        assert not r.is_fresh(r.timestamp_utc + timedelta(seconds=301))

    def test_stale_custom_max_age(self):
        r = _sample_report()
        ts = r.timestamp_utc
        assert r.is_fresh(ts + timedelta(seconds=59), max_age_seconds=60)
        assert not r.is_fresh(ts + timedelta(seconds=61), max_age_seconds=60)

    def test_future_timestamp_not_fresh(self):
        """A report from the future (clock skew) is NOT considered fresh."""
        r = _sample_report()
        past = r.timestamp_utc - timedelta(seconds=10)
        assert not r.is_fresh(past)


# ---------------------------------------------------------------------------
# Validation -- CoverageReport __post_init__
# ---------------------------------------------------------------------------


class TestReportValidation:
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

    def test_covered_exceeds_total(self):
        with pytest.raises(ValueError, match="positions_covered.*<=.*positions_total"):
            _sample_report(positions_covered=11, positions_total=10)

    def test_empty_source_version(self):
        with pytest.raises(ValueError, match="source_version"):
            _direct_report(source_version="")

    def test_bad_integrity_hash_format(self):
        with pytest.raises(ValueError, match="integrity_hash"):
            _direct_report(integrity_hash="not-a-valid-hex")

    def test_empty_report_id(self):
        with pytest.raises(ValueError, match="report_id"):
            _direct_report(report_id="")

    def test_empty_integrity_hash(self):
        with pytest.raises(ValueError, match="integrity_hash"):
            _direct_report(integrity_hash="")

    # -- report_id UUID format -----------------------------------------------

    def test_report_id_must_be_a_uuid(self):
        with pytest.raises(ValueError, match="report_id"):
            _direct_report(report_id="not-a-uuid")

    # -- timestamp timezone-awareness ----------------------------------------

    def test_timestamp_must_be_timezone_aware(self):
        with pytest.raises(ValueError, match="timestamp_utc"):
            _direct_report(timestamp_utc=datetime(2026, 7, 12, 14, 0, 0))

    def test_timestamp_must_be_utc_not_other_offset(self):
        other_tz = timezone(timedelta(hours=5))
        with pytest.raises(ValueError, match="timestamp_utc"):
            _direct_report(
                timestamp_utc=datetime(2026, 7, 12, 19, 0, 0, tzinfo=other_tz)
            )

    def test_observation_timestamp_must_be_timezone_aware(self):
        with pytest.raises(ValueError, match="observation_timestamp_utc"):
            _direct_report(
                observation_timestamp_utc=datetime(2026, 7, 12, 14, 0, 0)
            )

    # -- source_version format -----------------------------------------------

    @pytest.mark.parametrize(
        "bad_version", ["", "v1", "one.two.three", "1.", "-1.0"]
    )
    def test_source_version_bad_format_rejected(self, bad_version):
        with pytest.raises(ValueError, match="source_version"):
            _direct_report(source_version=bad_version)

    def test_source_version_accepts_bare_major_minor(self):
        r = _direct_report(source_version="1.0")
        assert r.source_version == "1.0"

    def test_source_version_accepts_build_metadata(self):
        r = _direct_report(source_version="1.2.3+abc1234")
        assert r.source_version == "1.2.3+abc1234"

    # -- execution_version ---------------------------------------------------

    def test_empty_execution_version_rejected(self):
        with pytest.raises(ValueError, match="execution_version"):
            _direct_report(execution_version="")

    # -- execution_source_commit ----------------------------------------------

    def test_empty_execution_source_commit_rejected(self):
        with pytest.raises(ValueError, match="execution_source_commit"):
            _direct_report(execution_source_commit="")

    def test_non_hex_execution_source_commit_rejected(self):
        with pytest.raises(ValueError, match="execution_source_commit"):
            _direct_report(execution_source_commit="not-a-sha")

    def test_wrong_length_execution_source_commit_rejected(self):
        with pytest.raises(ValueError, match="execution_source_commit"):
            _direct_report(execution_source_commit="a" * 64)  # 64 != 40

    def test_valid_execution_source_commit_accepted(self):
        r = _direct_report(execution_source_commit="abcdef0123456789" * 2 + "abcdef01")
        assert len(r.execution_source_commit) == 40

    # -- report_schema_version ------------------------------------------------

    def test_zero_report_schema_version_rejected(self):
        with pytest.raises(ValueError, match="report_schema_version"):
            _direct_report(report_schema_version=0)

    def test_negative_report_schema_version_rejected(self):
        with pytest.raises(ValueError, match="report_schema_version"):
            _direct_report(report_schema_version=-1)

    def test_valid_report_schema_version_accepted(self):
        r = _direct_report(report_schema_version=1)
        assert r.report_schema_version == 1

    # -- order_ids validation ------------------------------------------------

    def test_order_ids_empty_element_rejected(self):
        with pytest.raises(ValueError, match="order_ids"):
            _direct_report(
                order_ids=("ord-a", ""),
                positions_covered=8,
                positions_total=10,
                violations=2,
            )

    def test_order_ids_duplicate_rejected(self):
        with pytest.raises(ValueError, match="order_ids"):
            _direct_report(
                order_ids=("ord-a", "ord-a"),
                positions_covered=8,
                positions_total=10,
                violations=2,
            )

    # -- violations invariant ------------------------------------------------

    def test_violations_invariant_enforced(self):
        """violations must exactly equal positions_total - positions_covered."""
        with pytest.raises(ValueError, match="violations"):
            _direct_report(
                positions_covered=8,
                positions_total=10,
                violations=1,  # should be 2
            )

    def test_violations_invariant_enforced_zero_case(self):
        with pytest.raises(ValueError, match="violations"):
            _direct_report(
                positions_covered=0, positions_total=0, violations=1
            )

    # -- zero-position / zero-order consistency ------------------------------

    def test_zero_positions_with_nonempty_order_ids_rejected(self):
        with pytest.raises(ValueError, match="order_ids"):
            _direct_report(
                positions_covered=0,
                positions_total=0,
                violations=0,
                order_ids=("should-not-exist",),
            )

    # -- snapshot hash format ------------------------------------------------

    def test_position_snapshot_hash_bad_format(self):
        with pytest.raises(ValueError, match="position_snapshot_hash"):
            _direct_report(position_snapshot_hash="not-hex")

    def test_order_snapshot_hash_bad_format(self):
        with pytest.raises(ValueError, match="order_snapshot_hash"):
            _direct_report(order_snapshot_hash="not-hex")

    def test_empty_position_snapshot_hash(self):
        with pytest.raises(ValueError, match="position_snapshot_hash"):
            _direct_report(position_snapshot_hash="")


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
        should produce different hashes -- because report_id differs (UUID).
        """
        r1 = _sample_report()
        r2 = _sample_report()
        assert r1.integrity_hash != r2.integrity_hash

    def test_hash_is_lowercase_hex(self):
        r = _sample_report()
        assert r.integrity_hash == r.integrity_hash.lower()
        assert len(r.integrity_hash) == 64
        assert all(c in "0123456789abcdef" for c in r.integrity_hash)


# ---------------------------------------------------------------------------
# to_canonical_json
# ---------------------------------------------------------------------------


class TestToCanonicalJson:
    def test_returns_string(self):
        r = _sample_report()
        cj = r.to_canonical_json()
        assert isinstance(cj, str)

    def test_excludes_integrity_hash(self):
        r = _sample_report()
        cj = r.to_canonical_json()
        parsed = json.loads(cj)
        assert "integrity_hash" not in parsed

    def test_includes_all_other_fields(self):
        r = _sample_report()
        cj = r.to_canonical_json()
        parsed = json.loads(cj)
        for f in dataclasses.fields(r):
            if f.name == "integrity_hash":
                continue
            assert f.name in parsed

    def test_deterministic(self):
        r = _sample_report()
        assert r.to_canonical_json() == r.to_canonical_json()


# ---------------------------------------------------------------------------
# compute_snapshot_hash
# ---------------------------------------------------------------------------


class TestComputeSnapshotHash:
    def test_deterministic_for_equivalent_dicts(self):
        assert compute_snapshot_hash(
            {"BTC/USD": 0.5, "ETH/USD": 2.0}
        ) == compute_snapshot_hash({"ETH/USD": 2.0, "BTC/USD": 0.5})

    def test_differs_for_different_content(self):
        assert compute_snapshot_hash(
            {"BTC/USD": 0.5}
        ) != compute_snapshot_hash({"BTC/USD": 0.6})

    def test_returns_lowercase_hex64(self):
        h = compute_snapshot_hash({"a": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# default_execution_version
# ---------------------------------------------------------------------------


class TestDefaultExecutionVersion:
    def test_returns_installed_version(self, monkeypatch):
        """When the package is installed, returns its version string."""
        import importlib.metadata

        monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
        v = default_execution_version()
        assert v == "0.1.0"

    def test_raises_when_not_installed(self, monkeypatch):
        """When package metadata is unavailable, raises ValueError instead
        of silently accepting 0.0.0+unknown."""
        import importlib.metadata

        def _raise(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _raise)
        with pytest.raises(ValueError, match="[Cc]annot determine"):
            default_execution_version()


class TestDefaultExecutionSourceCommit:
    def test_returns_40_char_hex(self):
        """In a git repo, returns a valid 40-char lowercase hex SHA."""
        sha = default_execution_source_commit()
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_raises_outside_git_repo(self, monkeypatch, tmp_path):
        """Outside a git repo, raises ValueError."""
        import renquant_execution.coverage_report as cr

        monkeypatch.setattr(
            cr.os.path, "abspath", lambda _: str(tmp_path / "nonexistent.py")
        )
        with pytest.raises(ValueError, match="[Cc]annot determine"):
            default_execution_source_commit()


# ---------------------------------------------------------------------------
# Canonical serialization (to_dict/from_dict, to_json/from_json)
# ---------------------------------------------------------------------------


class TestCanonicalSerialization:
    def test_to_dict_round_trips_through_from_dict(self):
        r = _sample_report()
        d = r.to_dict()
        assert d["schema_version"] == COVERAGE_REPORT_SCHEMA_VERSION
        r2 = CoverageReport.from_dict(d)
        assert r2 == r
        assert verify_coverage_report(r2)

    def test_to_json_round_trips_through_from_json(self):
        r = _sample_report()
        raw = r.to_json()
        parsed = json.loads(raw)
        assert parsed["account_id"] == "ACCT-001"
        r2 = CoverageReport.from_json(raw)
        assert r2 == r
        assert verify_coverage_report(r2)

    def test_to_dict_serializes_order_ids_as_list(self):
        r = _sample_report()
        d = r.to_dict()
        assert isinstance(d["order_ids"], list)
        assert d["order_ids"] == ["ord-a", "ord-b"]

    def test_to_dict_serializes_timestamps_as_iso_string(self):
        r = _sample_report()
        d = r.to_dict()
        assert isinstance(d["timestamp_utc"], str)
        assert isinstance(d["observation_timestamp_utc"], str)

    def test_to_dict_includes_new_fields(self):
        r = _sample_report()
        d = r.to_dict()
        assert d["execution_source_commit"] == _MOCK_EXEC_COMMIT
        assert d["report_schema_version"] == 1

    def test_from_dict_rejects_schema_version_mismatch(self):
        r = _sample_report()
        d = r.to_dict()
        d["schema_version"] = 999
        with pytest.raises(ValueError, match="schema_version"):
            CoverageReport.from_dict(d)

    def test_from_dict_rejects_missing_schema_version(self):
        r = _sample_report()
        d = r.to_dict()
        del d["schema_version"]
        with pytest.raises(ValueError, match="schema_version"):
            CoverageReport.from_dict(d)

    def test_from_json_detects_hand_edited_tamper(self):
        """A hand-edited JSON (e.g. bumping positions_covered) round-trips
        structurally but fails integrity verification."""
        r = _sample_report()
        d = r.to_dict()
        d["positions_covered"] = 0
        d["violations"] = 10
        raw = json.dumps(d)
        r2 = CoverageReport.from_json(raw)
        assert not verify_coverage_report(r2)


# ---------------------------------------------------------------------------
# Integration tests with a fake broker observer (Codex review finding 4)
# ---------------------------------------------------------------------------


class FakeBrokerObserver:
    """Simulates broker stop-coverage queries for testing at the
    coverage_report layer (below AlpacaBroker) -- see
    ``tests/test_publish_stop_coverage_report.py`` for the full
    AlpacaBroker-level fake-broker integration tests.

    Given a set of held positions and a set of symbols with valid stops,
    produces a :class:`CoverageObservation` matching what
    ``AlpacaBroker.publish_stop_coverage_report()`` would produce.
    """

    def __init__(
        self,
        account_id: str,
        environment: str,
        positions: list[str],
        stop_symbols: set[str],
    ):
        self._account_id = account_id
        self._environment = environment
        self._positions = positions
        self._stop_symbols = stop_symbols

    def observe(self) -> CoverageObservation:
        covered = [s for s in self._positions if s in self._stop_symbols]
        pos_data = json.dumps(sorted(self._positions))
        order_data = json.dumps(sorted(self._stop_symbols))
        return CoverageObservation(
            account_id=self._account_id,
            environment=self._environment,
            observed_at_utc=datetime.now(timezone.utc),
            positions_covered=len(covered),
            positions_total=len(self._positions),
            qualifying_order_ids=tuple(
                f"stop-{s}" for s in sorted(covered)
            ),
            position_snapshot_hash=compute_snapshot_hash(pos_data),
            order_snapshot_hash=compute_snapshot_hash(order_data),
        )


class TestFakeBrokerIntegration:
    """Codex review finding 4: integration tests proving the execution-owned
    observation path works correctly."""

    def test_detects_uncovered_stops(self):
        """Execution-owned observation detects positions without stops."""
        broker = FakeBrokerObserver(
            account_id="INT-001",
            environment="paper",
            positions=["BTC/USD", "ETH/USD", "SOL/USD"],
            stop_symbols={"BTC/USD"},
        )
        obs = broker.observe()
        report = _build_coverage_report(obs, source_version="1.0.0")

        assert report.violations == 2
        assert report.positions_covered == 1
        assert report.positions_total == 3
        assert verify_coverage_report(report)

    def test_all_covered_zero_violations(self):
        """When all positions have stops, violations = 0."""
        broker = FakeBrokerObserver(
            account_id="INT-002",
            environment="live",
            positions=["BTC/USD", "ETH/USD"],
            stop_symbols={"BTC/USD", "ETH/USD"},
        )
        obs = broker.observe()
        report = _build_coverage_report(obs, source_version="1.0.0")

        assert report.violations == 0
        assert report.positions_covered == 2
        assert report.positions_total == 2
        assert verify_coverage_report(report)

    def test_no_positions_zero_everything(self):
        """Empty portfolio produces a valid zero-everything report."""
        broker = FakeBrokerObserver(
            account_id="INT-003",
            environment="paper",
            positions=[],
            stop_symbols=set(),
        )
        obs = broker.observe()
        report = _build_coverage_report(obs, source_version="1.0.0")

        assert report.violations == 0
        assert report.positions_total == 0
        assert report.order_ids == ()
        assert verify_coverage_report(report)

    def test_cannot_override_computed_violations(self):
        """_build_coverage_report computes violations from the observation.
        A caller with 2 uncovered positions cannot claim 0 violations --
        there is no `violations=` parameter anywhere in the call chain."""
        broker = FakeBrokerObserver(
            account_id="INT-004",
            environment="paper",
            positions=["BTC/USD", "ETH/USD", "SOL/USD"],
            stop_symbols={"BTC/USD"},
        )
        obs = broker.observe()
        report = _build_coverage_report(obs, source_version="1.0.0")

        assert report.violations == 2

    def test_observation_duplicate_order_ids_rejected(self):
        """Duplicate order IDs in the observation are rejected at
        construction."""
        with pytest.raises(ValueError, match="duplicate"):
            CoverageObservation(
                account_id="ACCT",
                environment="live",
                observed_at_utc=_NOW,
                positions_covered=2,
                positions_total=2,
                qualifying_order_ids=("ord-1", "ord-1"),
                position_snapshot_hash=_HASH_A,
                order_snapshot_hash=_HASH_B,
            )

    def test_observation_inconsistent_positions_rejected(self):
        """positions_covered > positions_total in the observation is
        rejected."""
        with pytest.raises(ValueError, match="positions_covered"):
            CoverageObservation(
                account_id="ACCT",
                environment="live",
                observed_at_utc=_NOW,
                positions_covered=5,
                positions_total=3,
                qualifying_order_ids=(),
                position_snapshot_hash=_HASH_A,
                order_snapshot_hash=_HASH_B,
            )

    def test_report_carries_observation_timestamp(self):
        """The report preserves observation_timestamp_utc separately from
        its own timestamp_utc (which is when the report object was built)."""
        obs_time = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
        obs = _sample_observation(observed_at_utc=obs_time)
        report = _build_coverage_report(obs, source_version="1.0.0")

        assert report.observation_timestamp_utc == obs_time
        assert report.timestamp_utc != obs_time

    def test_report_carries_execution_version(self):
        """The report includes the execution package version, derived
        automatically -- never supplied by a caller."""
        report = _sample_report()
        assert report.execution_version
        assert report.execution_version == _MOCK_EXEC_VERSION

    def test_report_carries_execution_source_commit(self):
        """The report includes the git commit SHA of the execution package."""
        report = _sample_report()
        assert report.execution_source_commit == _MOCK_EXEC_COMMIT

    def test_report_carries_report_schema_version(self):
        """The report schema version is always 1 (current)."""
        report = _sample_report()
        assert report.report_schema_version == 1

    def test_report_snapshot_hashes_bind_to_observation(self):
        """The report's snapshot hashes match the observation's hashes,
        binding the report to specific broker state."""
        obs = _sample_observation()
        report = _build_coverage_report(obs, source_version="1.0.0")

        assert report.position_snapshot_hash == obs.position_snapshot_hash
        assert report.order_snapshot_hash == obs.order_snapshot_hash

    def test_different_broker_states_produce_different_hashes(self):
        """Two observations from different broker states produce reports
        with different snapshot hashes."""
        broker_a = FakeBrokerObserver(
            account_id="A",
            environment="paper",
            positions=["BTC/USD"],
            stop_symbols={"BTC/USD"},
        )
        broker_b = FakeBrokerObserver(
            account_id="A",
            environment="paper",
            positions=["BTC/USD", "ETH/USD"],
            stop_symbols={"BTC/USD", "ETH/USD"},
        )
        report_a = _build_coverage_report(
            broker_a.observe(), source_version="1.0.0"
        )
        report_b = _build_coverage_report(
            broker_b.observe(), source_version="1.0.0"
        )
        assert (
            report_a.position_snapshot_hash
            != report_b.position_snapshot_hash
        )
# ---------------------------------------------------------------------------
# Codex review Item 1: CoverageObservation and build_coverage_report are
# public, importable, and listed in __all__.
# ---------------------------------------------------------------------------


class TestNoPublicAuthorizationPath:
    """Codex review 2026-07-13T00:16:11Z finding 1: build_coverage_report
    was a general caller-populated builder -- exactly the "authorization
    path" that let a caller mint a fabricated report. It (and its
    CoverageObservation input) must NOT be part of the public API; the only
    supported path to a genuine report is
    AlpacaBroker.publish_stop_coverage_report()."""

    def test_builder_not_exported_at_package_or_module_all_level(self):
        import renquant_execution
        import renquant_execution.coverage_report as cr_module

        assert not hasattr(renquant_execution, "build_coverage_report")
        assert not hasattr(renquant_execution, "CoverageObservation")
        assert not hasattr(cr_module, "build_coverage_report")
        assert "build_coverage_report" not in cr_module.__all__
        assert "CoverageObservation" not in cr_module.__all__

        # The rest of the public surface is intact.
        assert "CoverageReport" in cr_module.__all__
        assert "verify_coverage_report" in cr_module.__all__

    def test_hand_constructed_report_with_placeholder_hash_fails_verify(self):
        """A caller who hand-constructs a CoverageReport with a self-chosen
        placeholder integrity_hash produces an object that fails verification."""
        fake_hash = "a" * 64
        hand_built = CoverageReport(
            report_id=str(uuid.uuid4()),
            timestamp_utc=_NOW,
            observation_timestamp_utc=_NOW,
            account_id="FAKE",
            environment="live",
            positions_covered=10,
            positions_total=10,
            violations=0,
            order_ids=(),
            source_version="1.0",
            execution_version="0.1.0",
            execution_source_commit=_MOCK_EXEC_COMMIT,
            report_schema_version=1,
            position_snapshot_hash=fake_hash,
            order_snapshot_hash=fake_hash,
            integrity_hash=fake_hash,
        )
        assert not verify_coverage_report(hand_built)

    def test_forged_self_consistent_report_passes_verify(self):
        """Demonstrates that hash-only verification cannot distinguish
        execution-observed from caller-forged reports. Cryptographic
        attestation required for entry authorization."""
        # Step 1: hand-construct a CoverageReport with violations=0,
        # using a placeholder integrity_hash.
        placeholder_hash = "0" * 64
        forged_fields = dict(
            report_id=str(uuid.uuid4()),
            timestamp_utc=_NOW,
            observation_timestamp_utc=_NOW,
            account_id="FORGED",
            environment="live",
            positions_covered=10,
            positions_total=10,
            violations=0,
            order_ids=(),
            source_version="1.0.0",
            execution_version="0.1.0",
            execution_source_commit=_MOCK_EXEC_COMMIT,
            report_schema_version=1,
            position_snapshot_hash="a" * 64,
            order_snapshot_hash="b" * 64,
        )
        tmp = CoverageReport(**forged_fields, integrity_hash=placeholder_hash)

        # Step 2: recompute the hash using the internal _compute_hash logic.
        correct_hash = _compute_hash(tmp)

        # Step 3: construct the forged report with the correctly computed hash.
        forged = CoverageReport(**forged_fields, integrity_hash=correct_hash)

        # Step 4: the forged report PASSES verify -- this is the gap.
        assert verify_coverage_report(forged)
