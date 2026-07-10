# Account cash ledger: shared-wiring contract (round-1, Codex review of #28)

STATUS:   partially delivered — shared-wiring finding fixed; fee-inclusive
          reservation math NOT yet fixed (sequencing-dependent, see below)
DATE:     2026-07-10
PR:       (this PR)
SPEC:     Codex review of #28 (D-C4).

## What

Codex's review raised two findings. This round addresses the first:

1. **Shared-ledger property not enforced** — the PR exported an optional
   constructor and an optional `OrderStateBook` attachment, but nothing made it
   structurally impossible for two sleeves to end up on DIFFERENT ledgers
   (per-sleeve data dirs, or a caller simply forgetting to attach one). Fixed
   by adding `BaseBroker.get_account_id()` (implemented on `AlpacaBroker` as
   the real `account_number`, the same field `connect()` already verifies
   against `RENQUANT_EXPECTED_LIVE_ACCOUNT` in live mode) and
   `build_shared_account_cash_ledger_for_broker(broker, *, data_dir, ...)` —
   the ONE execution-owned wiring contract every launch path must go through.
   `account_id` is DERIVED from the broker's own verified identity, never
   accepted as a caller-supplied string, so a per-sleeve tag can never leak
   into the ledger-identity slot. Proven end-to-end with TWO REAL OS
   PROCESSES (`tests/test_account_cash_ledger_shared_process.py`) — each with
   its own fake broker instance reporting the same account id — resolving to
   the identical db file and correctly serializing an over-committing
   reservation pair across the process boundary (not just in-process thread
   serialization, already covered by the existing suite).

2. **NOT addressed this round**: "`submit_remainder` reserves `qty * price`
   only... crypto buys consume fee headroom as well... two orders can be
   admitted up to reported cash and then fail at the broker or overcommit
   once fees are applied... The ledger API must reserve worst-case executable
   debit including the exact execution cost specification, with a
   versioned/fingerprinted contract shared with the upstream cost model."
   This is real, but it depends on `renquant-common#28`'s `CostModelSpec` —
   itself not yet merged, and until this round's `cost_model_content_sha256`
   fingerprint helper lands there, there is no "versioned/fingerprinted
   contract" to share yet. Fixing this now would mean either (a) hand-rolling
   a temporary local fee-cost duplicate here — exactly the class of drift the
   RFC's cost-model unification exists to prevent, or (b) hard-depending on
   an unmerged, unversioned package. Correct sequence: merge common#28 first
   (with its fingerprint helpers), THEN reserve
   `qty * price + round_trip_cost_bps(spec)/1e4 * qty * price` (or the
   analogous per-side formula) here, stamping the `cost_model_content_sha256`
   alongside the reservation row so a WF-replay evaluator and this runtime
   path can prove they charged the identical worst-case debit.

## Tests

`tests/test_alpaca_broker_account_id.py` (3 cases): returns the real
`account_number`, fails closed when not connected, fails closed when the
connected account has no `account_number`. `tests/test_account_cash_ledger_
shared_process.py` (3 cases): two independent broker-instance processes
resolve the same ledger file; an over-committing reservation pair correctly
serializes across real processes (exactly one granted); the flag-off path
returns `None` in every process. Verified meaningful via stash-revert: all 5
new-module tests fail to even IMPORT without the source change (the new
names don't exist). Full suite: 317 passed, no regressions.

## Next

Fee-inclusive reservation math tracked above; revisit once common#28 merges
with its cost-model fingerprint helpers.
