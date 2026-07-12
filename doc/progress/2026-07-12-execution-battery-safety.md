# Execution battery safety hardening (codex review round 2)

STATUS: delivered
DATE:   2026-07-12
PR:     #32 (feat/crypto-stage0-battery-checks)
CONTEXT: Codex review of PR #32 flagged 5 safety/correctness issues in
the Stage-0 battery step checks.  All 5 are addressed in this commit.

## Changes

### 1. Paper-only hard enforcement (review item 1)

Added `run_battery(*, paper=True, dry_run=False, transactional=False)`
as the high-level entry point.  `paper=False` raises `ValueError` --
the battery NEVER touches a live account.  The low-level
`get_trading_client(paper=...)` factory retains its parameter for
direct use, but `run_battery` hardcodes `paper=True`.

### 2. step_fee_from_fill round-trip (review item 2)

Previously: placed a market BUY, slept 3s, checked status, returned.
No compensating sell, no residual-position audit.

Now: bounded-notional BUY -> poll to fill (max 10 attempts, 0.5s
sleep) -> compensating SELL for `filled_qty` -> poll SELL to fill ->
residual-position audit via `get_all_positions`.  Cleanup failure
(sell doesn't fill, or residual position remains) surfaces as a
distinct Tier-1 FAIL with `cleanup_failure=True` in result data.

### 3. step_stop_limit_acceptance cancel confirmation (review item 3)

After `cancel_order_by_id`, polls with `_poll_order_terminal` until
the order reaches a terminal state (canceled/expired/rejected).
Reports FAIL if cancel is not confirmed terminal.

### 4. step_order_acceptance cancel confirmation (review item 4)

Same fix as item 3 -- polls to terminal state after cancel.  PASS
requires both acceptance AND confirmed cancellation.

### 5. Passive/transactional separation (review item 5)

`run_battery(transactional=False)` (default) runs only passive
read-only checks: crypto_status, pair_snapshot, buying_power,
data_parity.  The three paper-order probes (order_acceptance,
stop_limit_acceptance, fee_from_fill) are returned as SKIP.
`transactional=True` runs all 7 steps.  Default battery is
passive-only and live-impossible.

## Implementation details

- `_poll_order_terminal(client, order_id, *, max_attempts=10,
  sleep_sec=0.5)` -- shared polling helper; returns
  `(reached_terminal, order_object)`.  Terminal = status string
  contains "fill", "cancel", "expire", or "reject".
- All `time.sleep` calls are patchable in tests (no real sleeps in
  the test suite).

## Verification

`make test`: 398 passed, 2 skipped (pre-existing).  27 tests in
`test_crypto_stage0_checks.py` cover all 5 review items explicitly.
