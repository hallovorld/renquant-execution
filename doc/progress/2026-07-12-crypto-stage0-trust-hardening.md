# Crypto Stage-0 battery trust hardening (PR #35 round 2)

Date: 2026-07-12

## What changed

Addresses 5 remaining Codex review items on the Stage-0 crypto battery
(PR #35, branch `fix/crypto-battery-review-r2`).

### 1. `_validate_order_acceptance()` strictness

- Removed `partially_filled` and `held` from `_ACCEPTED_ORDER_STATUSES`
  (only `accepted`, `new` are truly resting).
- Unknown/pending/absent status is now rejected (was: logged as warning
  and accepted).
- Missing side or time_in_force fields are now rejected (was: absent
  fields silently accepted).
- Added `expected_order_type` and `expected_asset_class` validation
  parameters.  Both callers now pass `limit`/`stop_limit` and `crypto`.
- AlpacaBroker thin wrappers now include `order_type` in result dicts.

### 2. Residual position/order audit after fill

- Added `_check_residual_exposure()`: queries order final state
  (`get_order_state`) and position (`get_position`) after every probe.
- Nonzero `filled_qty` or nonzero position is a Tier-1 failure with
  durable evidence in the step data.
- Added `AlpacaBroker.get_order_state()` thin wrapper for order state
  queries.
- Called after every probe in both GTC limit and stop-limit steps.

### 3. Stop-limit probe as non-gating diagnostic

- `check_stop_limit_acceptance` now returns `required=False`.
- Relabelled from `metadata_capability` to `diagnostic_capability`.
- Price-band rejections are classified as diagnostic failures, not false
  capability proofs.
- A FAIL on this step does not block the battery's `all_passed`.

### 4. Environment verification independence

- `get_account_info()` now exposes `base_url` from the trading client.
- `BatteryReport` carries `base_url` as an immutable report field.
- `run_full_battery` cross-checks: if `paper=True` but base_url does
  not contain "paper", the report fails with `environment=inconsistent`.

### 5. Nonempty required-gate set + report schema/hash

- `all_passed` now rejects vacuous truth: returns `False` when no
  required steps exist.
- Added `REPORT_SCHEMA_VERSION = "1.0.0"` and
  `report_schema_version` field on `BatteryReport`.
- Added `BatteryReport.content_hash()`: SHA-256 of canonical report
  content for tamper-evident audit trails.

## Test coverage

82 tests in `test_crypto_stage0_checks.py` (was 56), 471 total (2 skipped).

New test classes:
- `TestUnknownMissingOrderFields` (6 tests)
- `TestWrongOrderTypeAssetClass` (5 tests)
- `TestResidualExposureAudit` (4 tests)
- `TestPriceBandRejectionDiagnostic` (1 test)
- `TestEnvironmentBaseUrlVerification` (4 tests)
- `TestNonemptyRequiredGateSet` (2 tests)
- `TestReportSchemaVersionAndHash` (4 tests)

## Files changed

- `src/renquant_execution/alpaca_broker.py` (thin wrappers: order_type,
  base_url, get_order_state)
- `src/renquant_execution/crypto_stage0_checks.py` (all 5 fixes)
- `tests/test_crypto_stage0_checks.py` (26 new tests)
