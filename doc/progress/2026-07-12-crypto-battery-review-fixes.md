# Crypto Stage-0 Battery Review Fixes (R2)

Date: 2026-07-12
Supersedes: PR #34 (feat/crypto-stage0-battery-api)
Branch: fix/crypto-battery-review-r2

## Summary

Addresses 6 Codex review items on the crypto Stage-0 battery checks.

## Review Items Fixed

1. **Cancel-then-verify unsafe**: Extracted `_confirm_cancel_with_evidence()`
   that always polls for terminal state even if `cancel_order` raises (the
   cancel request may have been processed asynchronously). Includes durable
   order/terminal-state evidence (`cancel_confirmed`, `cancel_exception`) in
   step result data.

2. **Acceptance inferred from nonempty order_id**: Added
   `_validate_order_acceptance()` that validates the returned order's status,
   side, and TIF match the requested probe. Rejected/filled/expired/missing
   statuses are now hard failures, not silent passes.

3. **Fixed implausible probe prices**: Stop-limit acceptance step is now
   explicitly labelled as a METADATA/CAPABILITY check in both the docstring,
   detail string, and `check_type` data field. The `$999,999,999` probe
   prices are documented as deliberately implausible -- they prove the
   broker accepts the order TYPE, not empirical fill quality.

4. **Paper gate**: `run_full_battery()` now treats failed account lookup or
   unverified environment (paper flag is None/missing/False) as a FAIL with
   an `environment_verification` step. Never defaults to `"paper"`.

5. **check_data_parity() always SKIPS but all_passed requires PASS**: Added
   `required` field to `StepResult` (default `True`). `all_passed` now only
   gates on `required=True` steps. `check_data_parity` is marked
   `required=False` so its structural SKIP does not permanently block the
   battery verdict.

6. **Buying-power check**: Relabelled as OBSERVATIONAL (not a gate) with
   `check_type="observational"` in data. Always returns PASS with the
   buying-power values for operator inspection. Does not assert the crypto
   non-marginable invariant.

## Test Coverage Added

24 new tests (32 -> 56 in the battery test file):

- `TestCancelSucceedsButNeverReachesTerminal`: cancel accepted but order
  stays resting (timeout -> FAIL); cancel raises but poll finds canceled
  (-> PASS with evidence)
- `TestImmediateFillOnProbeOrder`: immediate fill on GTC limit and
  stop-limit probes -> FAIL
- `TestReturnedRejectionOrNoOrder`: rejected/expired/no-order-id/wrong-side
  statuses -> FAIL
- `TestValidateOrderAcceptance`: direct unit tests for the validation helper
- `TestUnverifiedEnvironment`: failed account lookup, None paper flag, live
  environment -> FAIL
- `TestStopLimitMetadataCapabilityLabel`: pass/fail results carry the
  metadata_capability label
- `TestOptionalStepGating`: optional SKIP/FAIL does not block all_passed;
  required FAIL does block

## Files Changed

- `src/renquant_execution/crypto_stage0_checks.py` — all 6 fixes
- `tests/test_crypto_stage0_checks.py` — 24 new adversarial tests, 2
  existing tests updated for new behavior
