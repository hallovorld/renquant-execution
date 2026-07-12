# 2026-07-12 -- Crypto Stage-0 battery check API

## Bottom line

Restructures the crypto Stage-0 battery from the orchestrator repo into
renquant-execution. The battery validates crypto trading prerequisites on
Alpaca's PAPER account using the AlpacaBroker adapter exclusively (no
direct alpaca-py imports). Code review correctly identified that
broker-facing checks belong in the execution repo.

## What this PR contains

- `alpaca_broker.py`: 4 new thin wrapper methods on AlpacaBroker:
  - `get_account_info()` -- account metadata (status, crypto_status, buying power)
  - `get_crypto_asset_spec(symbol)` -- public wrapper for per-pair order-grid spec
  - `place_crypto_limit_order(symbol, action, qty, limit_price)` -- crypto GTC/IOC limit order
  - `place_crypto_stop_limit_order(symbol, action, qty, stop_price, limit_price)` -- general-purpose crypto stop-limit (BUY + SELL sides)

- `crypto_stage0_checks.py`: complete rewrite (was a direct-alpaca-py
  importer, now uses AlpacaBroker adapter exclusively):
  - `StepResult` / `StepStatus` / `BatteryReport` data types
  - 6 battery steps: account status, pair snapshot, GTC order acceptance,
    stop-limit acceptance, buying power behavior, data parity (placeholder)
  - `run_full_battery(broker, dry_run=False)` -- orchestrates all steps
  - Hard safety gate: refuses to run on non-paper broker

- `tests/test_crypto_stage0_checks.py`: 30 tests covering all battery steps,
  broker thin wrappers, dry-run mode, live-run mode, error handling, and
  the paper-only safety gate. All mock the broker (no alpaca-py needed in CI).

## Key design choices

1. All checks route through the AlpacaBroker adapter, never direct alpaca-py.
2. GTC acceptance tested via limit BUY at $0.01 (never fills); stop-limit
   acceptance tested via BUY stop-limit at unreachable prices (never triggers).
   Both cancelled immediately.
3. Data parity is a SKIP placeholder -- the Trading API has no market-data
   endpoint; the orchestrator/data repo wires this when infrastructure exists.
4. Not re-exported from `__init__.py` (follows software_stops_liveness precedent).

## Verification

- 30 new tests pass, 417 total (2 skipped) `[VERIFIED]`

## Reconciliation note (2026-07-12, post-review)

A concurrent PR (#32, `feat/crypto-stage0-battery-checks`) built the same
move (broker-facing Stage-0 checks out of orchestrator) with standalone
alpaca-py client factories instead of routing through `AlpacaBroker`. The
two PRs conflicted (`mergeable: CONFLICTING`, both created
`crypto_stage0_checks.py` / `tests/test_crypto_stage0_checks.py`). #32 was
closed in favor of this PR: this design is meaningfully safer on every axis
Codex's #32 review (2026-07-12T22:06:29Z, CHANGES_REQUESTED) flagged --

- **Hard paper-mode enforcement.** `_assert_paper_mode(broker)` refuses to
  run if `not getattr(broker, "paper", False)`. #32 only defaulted
  `paper=True` on a parameter, which Codex correctly flagged as
  insufficient ("a thin orchestrator consumer can therefore invoke real
  order submission").
- **No risky fee-observation step.** #32 had a `step_fee_from_fill` that
  placed a real market BUY and waited for a fill with no compensating
  sell/cleanup -- Codex flagged this as leaving orphaned paper inventory.
  This PR simply never had that step (the safer of Codex's two suggested
  remediations: remove it, vs. building full round-trip-with-compensation
  machinery).
- **Unreachable canary prices.** `check_gtc_order_acceptance` places a BUY
  limit at $0.01 (can't fill); `check_stop_limit_acceptance` places a
  **BUY-side** stop-limit with the stop far ABOVE market (can't trigger).
  #32's SELL-side stop-limit probe at $0.01 was flagged by Codex as an
  inventory/no-short/immediate-trigger risk; BUY-side has no such issue
  under the existing `crypto_no_short_violation` logic (only constrains
  SELL quantity against held position).

The rebase branch was cut from pre-#31 `main` (`git merge-base` = the #30
commit, one commit behind `main`'s tip at rebase time), so
`git diff origin/main origin/feat/crypto-stage0-battery-api --
alpaca_broker.py` looked like it *removed* `_wait_for_order_terminal_cancel`
/ the confirmed-cancel `replace_crypto_stop_limit` / `check_crypto_stop_coverage`
-- it didn't; that content simply didn't exist yet on this branch. Rebasing
onto current `main` (one commit ahead: #31) surfaced the only real
conflict, entirely inside `alpaca_broker.py`, and it was a textbook
non-overlapping add/add: `main` (via #31) added
`_wait_for_order_terminal_cancel` / `replace_crypto_stop_limit` /
`check_crypto_stop_coverage` right where this branch's four new thin
wrapper methods (`get_account_info`, `get_crypto_asset_spec`,
`place_crypto_limit_order`, `place_crypto_stop_limit_order`) were also
inserted. Resolved by keeping both blocks, `main`'s content first
(unmodified) followed by this branch's four wrappers (unmodified) --
nothing from either side was discarded. `git rebase --continue` completed
cleanly on the first attempt after that; full suite re-run green
(421 passed, 2 skipped) immediately after.

**Ground-truth correction:** Codex has, in fact, already reviewed this PR
(2026-07-12T22:16:36Z, CHANGES_REQUESTED) -- 6 findings, not zero. Finding 1
is exactly the terminal-cancellation-confirmation gap fixed below (proactively
scoped ahead of seeing that review). Findings 2-6 (acceptance inferred from a
nonempty order id rather than a genuinely-resting status; fixed canary prices
not derived from a versioned quote/price-band contract; the paper gate
trusting only `broker.paper` while the report can still default
environment=paper on a failed account lookup; `check_data_parity` always
SKIPping while `BatteryReport.all_passed` requires every step PASS, making a
clean full-battery run structurally impossible; the buying-power check's
NMBP-nonnegative-only assertion not actually establishing non-marginable
crypto behavior) are **not addressed in this revision** -- they were outside
this fix's explicit scope and involve design judgment calls the coordinator
asked to make personally. Flagging here so the next round of work (or the
coordinator's own pass before merge) has the full, current review state,
not a stale "Codex hasn't looked yet" assumption.

## Revision note (2026-07-12): proactive terminal-cancellation confirmation

Both `check_gtc_order_acceptance` and `check_stop_limit_acceptance` used to
call `broker.cancel_order(order_id)` inside a `finally` block and only check
whether it *raised* -- not whether the order actually reached a confirmed
terminal `canceled` state. That's the same "confirm, don't assume" gap
Codex required closed on PR #31's `replace_crypto_stop_limit`
(`AlpacaBroker._wait_for_order_terminal_cancel`, merged into `main` via #31).
Applied the same discipline here, proactively:

- Added a new **public** wrapper, `AlpacaBroker.wait_for_order_terminal_cancel`,
  that delegates to the existing private `_wait_for_order_terminal_cancel`.
  Chose the public-wrapper route (option (b)) over calling the
  underscore-prefixed method directly from `crypto_stage0_checks.py`, for
  consistency: this PR's whole design principle for the four existing thin
  wrappers (`get_account_info`, `get_crypto_asset_spec`,
  `place_crypto_limit_order`, `place_crypto_stop_limit_order`) is that the
  battery module never reaches into `AlpacaBroker` private state -- adding
  one direct private-method call would have been the only exception to that
  rule in the whole file. The wrapper is a pure pass-through (same
  signature, same defaults, same docstring pointer back to the private
  method) and does **not** modify `replace_crypto_stop_limit`'s own call
  site, which still calls the private method directly (PR #31's own logic
  is untouched, per scope).
- Both battery steps now poll `wait_for_order_terminal_cancel(order_id)`
  after a successful `cancel_order()` call. If the terminal `canceled`
  state is not confirmed within the timeout (or `cancel_order` itself
  raised), the affected pair/order is added to the step's `failures` list
  instead of `placed_and_cancelled` -- the step reports **FAIL**, naming
  the pair and order id, never a silent PASS. `order_details[pair]` now
  also carries a `cancel_confirmed: bool` field for the report/log trail.
- New tests (`test_gtc_acceptance_fails_when_cancellation_not_confirmed`,
  `test_stop_limit_acceptance_fails_when_cancellation_not_confirmed`) drive
  a fake client whose `get_order_by_id` reports a resting, non-terminal
  status (`"accepted"`) forever after cancellation is requested, with a
  deliberately tiny timeout/poll interval (0.05s / 0.01s) passed as new
  keyword-only parameters (`cancel_confirm_timeout_seconds`,
  `cancel_confirm_poll_interval_seconds`, both default to the same 5.0s /
  0.25s production defaults as `_wait_for_order_terminal_cancel`) so the new
  tests run in well under a second. Existing happy-path tests were
  unaffected: the fake client's default behavior (`cancel_order_by_id`
  marks the order `"canceled"` immediately, mirroring the convention
  already used in `tests/test_crypto_order_semantics.py` for the #31
  tests) makes `wait_for_order_terminal_cancel` return `True` on the very
  first poll, with zero added test runtime.
- Full suite: 421 passed, 2 skipped (was 419 passed, 2 skipped before this
  revision; +2 new tests) `[VERIFIED]`.
