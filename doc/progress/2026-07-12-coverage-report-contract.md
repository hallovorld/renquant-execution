# Versioned coverage report public API

STATUS: delivered (new module + tests; no runtime change)
DATE:   2026-07-12
PR:     (this PR)
CONTEXT: renquant-orchestrator#501 — Codex requires execution to publish a
versioned coverage report with account/order/position identity and integrity
hash so the orchestrator can verify stop-coverage state at commit time.

## What this PR does

Adds `src/renquant_execution/coverage_report.py` — a frozen-dataclass
contract for immutable, hash-verified coverage reports:

- **`CoverageReport`** (frozen dataclass): `report_id`, `timestamp_utc`,
  `account_id`, `environment` (live/paper), `positions_covered`,
  `positions_total`, `violations`, `order_ids` (tuple), `source_version`,
  `integrity_hash` (SHA-256 of canonical JSON of all other fields).
  `__post_init__` validates every field: non-empty strings, environment
  enum, non-negative counts, covered <= total, 64-char hex hash.

- **`build_coverage_report(...)`**: constructs a report with auto-generated
  UUID `report_id` and computed `integrity_hash`.

- **`verify_coverage_report(report)`**: recomputes hash from fields and
  returns True iff it matches `integrity_hash`.

- **`CoverageReport.is_fresh(now_utc, max_age_seconds=300)`**: staleness
  gate; future timestamps (clock skew) return False.

All three symbols are re-exported from `renquant_execution.__init__`.

## Tests

`tests/test_coverage_report.py` — 33 tests covering:
- Build + verify roundtrip (live, paper, zero positions, empty order_ids)
- Tamper detection (every field, including order_ids and timestamp)
- Freshness (within bounds, at boundary, stale, custom max_age, future)
- Validation (every `__post_init__` guard — 11 error paths)
- Immutability (frozen, tuple order_ids)
- Deterministic hashing properties

Full suite: 478 passed, 2 skipped, 0 failed.

## Not in scope

- Wiring the report into any live checker or orchestrator consumer — that
  is orchestrator-side work (PR #501).
- Persistence or serialization to disk/wire — the consumer decides format.
- Any change to software-stops or liveness checking modules.
