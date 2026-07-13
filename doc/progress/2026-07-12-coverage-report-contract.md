# Versioned coverage report public API

STATUS: delivered (new module + tests + Codex R2 fixes; no runtime change)
DATE:   2026-07-12
PR:     #37

CONTEXT: renquant-orchestrator#501 -- Codex requires execution to publish a
versioned coverage report with account/order/position identity and integrity
hash so the orchestrator can verify stop-coverage state at commit time.

## What this PR does

Adds `src/renquant_execution/coverage_report.py` -- a frozen-dataclass
contract for immutable, hash-verified coverage reports:

- **`CoverageReport`** (frozen dataclass): `report_id`, `timestamp_utc`,
  `observation_timestamp_utc`, `account_id`, `environment` (live/paper),
  `positions_covered`, `positions_total`, `violations`, `order_ids` (tuple),
  `source_version`, `execution_version`, `execution_source_commit` (git SHA),
  `report_schema_version` (int), `position_snapshot_hash`,
  `order_snapshot_hash`, `integrity_hash` (SHA-256 of canonical JSON).

- **`verify_coverage_report(report)`**: recomputes hash from fields and
  returns True iff it matches `integrity_hash`.

- **`CoverageReport.is_fresh(now_utc, max_age_seconds=300)`**: staleness
  gate; future timestamps (clock skew) return False.

- **`default_execution_version()`**: raises `ValueError` if the package
  version cannot be determined (rejects `0.0.0+unknown`).

- **`default_execution_source_commit()`**: returns the git commit SHA of
  the execution package; raises `ValueError` outside a git repo.

## Codex R2 review fixes

1. **Negative forge test**: `test_forged_self_consistent_report_passes_verify`
   demonstrates that hash-only verification cannot distinguish
   execution-observed from caller-forged reports (gap acknowledgment).

2. **Immutable producer identity**: `execution_source_commit` (git SHA) and
   `report_schema_version` fields added; `default_execution_version()` now
   rejects unknown versions with `ValueError`.

3. **Rebased onto merged exec #34** (d8e3fb1).

## Tests

`tests/test_coverage_report.py` -- 112 tests.
`tests/test_publish_stop_coverage_report.py` -- 13 tests.

Full suite: 570 passed, 2 skipped, 0 failed.

## Not in scope

- Wiring the report into any live checker or orchestrator consumer -- that
  is orchestrator-side work (PR #501).
- Cryptographic attestation to close the forge gap exposed by the negative
  test -- requires a keyed MAC or signature, future work.
