# Versioned coverage report -- diagnostic contract

STATUS: delivered (new module + tests + Codex R2/R3 fixes; no runtime change)
DATE:   2026-07-12
PR:     #37

CONTEXT: renquant-orchestrator#501 -- Codex requires execution to publish a
versioned coverage report with account/order/position identity and integrity
hash so the orchestrator can verify stop-coverage state for monitoring.

## Scope

This module produces **diagnostic / shadow** reports only.  Every report
carries `trust_level = "unattested_diagnostic"` (frozen at construction,
not overridable).  A diagnostic report is useful for monitoring and
alerting but is NOT entry-authorization evidence -- that would require
cryptographic attestation (keyed MAC or signature), which is future work.

## What this PR does

Adds `src/renquant_execution/coverage_report.py` -- a frozen-dataclass
contract for immutable, hash-verified diagnostic coverage reports:

- **`CoverageReport`** (frozen dataclass): `report_id`, `timestamp_utc`,
  `observation_timestamp_utc`, `account_id`, `environment` (live/paper),
  `positions_covered`, `positions_total`, `violations`, `order_ids` (tuple),
  `source_version`, `execution_version`, `execution_source_commit` (git SHA),
  `report_schema_version` (int), `position_snapshot_hash`,
  `order_snapshot_hash`, `trust_level` (always `"unattested_diagnostic"`),
  `integrity_hash` (SHA-256 of canonical JSON including trust_level).

- **`verify_coverage_report_integrity(report)`**: recomputes hash from
  fields and returns True iff it matches `integrity_hash`.  Verifies
  self-consistency only -- NOT authenticity or authorization.

- **`CoverageReport.is_fresh(now_utc, max_age_seconds=300)`**: staleness
  gate; future timestamps (clock skew) return False.

- **`default_execution_version()`**: raises `ValueError` if the package
  version cannot be determined (rejects `0.0.0+unknown`).

- **`default_execution_source_commit()`**: returns the git commit SHA of
  the execution package; raises `ValueError` outside a git repo.

## Codex R2/R3 review fixes

1. **Negative forge test**: `test_forged_self_consistent_report_passes_verify`
   demonstrates that hash-only verification cannot distinguish
   execution-observed from caller-forged reports (gap acknowledgment) --
   but `trust_level` prevents any authorization gate from accepting.

2. **Immutable producer identity**: `execution_source_commit` (git SHA) and
   `report_schema_version` fields added; `default_execution_version()` now
   rejects unknown versions with `ValueError`.

3. **Rebased onto merged exec #34** (d8e3fb1).

4. **Diagnostic-only scope enforced in code** (R3):
   - `verify_coverage_report()` renamed to `verify_coverage_report_integrity()`
   - `trust_level = "unattested_diagnostic"` field added (frozen, in hash preimage)
   - Authorization-claim language removed from all docstrings and docs
   - `test_report_rejected_by_authorization_gate` added

## Tests

`tests/test_coverage_report.py` -- 120+ tests.
`tests/test_publish_stop_coverage_report.py` -- 13 tests.

## Not in scope

- Wiring the report into any live checker or orchestrator consumer -- that
  is orchestrator-side work (PR #501).
- Cryptographic attestation to close the forge gap exposed by the negative
  test -- requires a keyed MAC or signature, future work.
