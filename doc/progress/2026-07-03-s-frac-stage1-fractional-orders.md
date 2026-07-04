# S-FRAC stage 1 — execution-repo fractional order support + DAY-expiry terminal classification

DATE: 2026-07-03
DESIGN: renquant-orchestrator `doc/design/2026-07-02-s-frac-fractional-v2.md`
(§4 broker rules inventory, §5 reuse inventory, §6 stage-1 row)
CONSUMES: S-FRAC stage 0 (RenQuant#439, merged) — `supports_broker_side_stops(symbol, qty)`
umbrella consumer + the broker probe surface (`is_fractionable` + no-submit classifier)
+ the two items #439 explicitly deferred to stage 1: DAY-expiry and
cancel-with-first-sight-of-fill terminal classification.

## Salvaged from the preserved execution#19 branch (per the §5 inventory, "as-is")

Rebased `feat/fractional-shares` (99ea7a3 + 5ced201, both round-1 hazards fixed and
re-reviewed at close) onto current main, verified green (124 passed) before building:

- fail-closed `_FractionableLookupError` lookup (transient failures never cached);
  `is_fractionable` cached probe
- explicit no-submit statuses (`rejected_non_fractionable`,
  `rejected_fractionable_lookup_failed`) preserving requested-vs-submitted quantity
- first-class `skipped` classification in `classify_broker_result` + audit `n_skipped`
  (no-submit never counted submitted)
- qty-aware `supports_broker_side_stops(symbol, qty)`; fail-closed whole-share
  preflight on the GTC `place_stop_order`
- CI installs the `alpaca` extra (round-1 lesson)

The #19 round-2 blockers are closed externally: the unconsumed capability now has its
real consumer (stage 0's `route_stop_protection`, driven directly in verification), and
the buy→sell→zero-residual lifecycle burden moved to the umbrella suite (#439 audit #1).

## Built new in this stage

1. **Fractional order shapes per the pinned Alpaca rules (§4)**: either fractional
   `qty` OR dollar `notional`, never both; market/limit/stop/stop-limit vocabulary with
   TIF=DAY only; 9dp grid; $1 notional minimum. `validate_fractional_order` +
   `place_notional_order` (market DAY); violations are explicit no-submit statuses
   (`rejected_precision_exceeds_9dp`, `rejected_below_min_notional`,
   `rejected_invalid_fractional_order`) — never silent mutation. Eps-integral submit
   noise (3.0000000001) snaps to the exact whole share.
2. **Terminal classification vocabulary** (the #439-deferred items):
   `TERMINAL_STATUS_MAP` / `classify_terminal_status` /
   `OrderStateBook.apply_terminal_status` — fill-first discipline (cancel-with-fill and
   partial-fill-then-expire book the REAL filled portion; the unfilled remainder feeds
   `cum_expired`/`cum_canceled` and returns to `remaining_unsubmitted`);
   `resolve_day_expiry` end-of-session driver; `reconcile_on_restart` refactored onto
   the shared vocabulary. `classify_broker_result` now surfaces
   `terminal`/`expired`/`canceled` and DAY expiry is terminal (no GTC carryover).
3. **Float comparison hardening**: `QTY_INTEGRAL_EPS = 1e-9` replicated from stage-0's
   `commit_contract.py` (source noted in code; literal constant-equality test as the
   drift tripwire); `is_whole_share` eps-integral; `is_fill_complete` epsilon
   requested-vs-filled everywhere fills are compared.

## Evidence

- Full suite: 149 passed, 1 skipped (baseline 112+1 from slice 1; +11 salvaged, +26 new).
- Cross-repo drive: the REAL stage-0 umbrella functions (`fractional_capability_gate`,
  `route_stop_protection`, `supports_broker_side_stops_for`, fetched from RenQuant main)
  pass against this branch's `AlpacaBroker` surface, including eps literal parity.
- No live broker calls anywhere; fake `TradingClient` with real alpaca-py request shapes.

## What stage 2 (pipeline sizing) consumes

- `place_order` accepting 6dp-floored fractional qty on fractionable assets (the #153
  sizing core emits into this), with the no-submit statuses as its fail-closed fallback
  seam (A-3 remains the fallback per design §7.2).
- `NO_SUBMIT_STATUSES` / `is_no_submit_status` for ledger `sizing_mode` fallback logic.
- `MIN_FRACTIONAL_NOTIONAL_USD` + `validate_fractional_order` as the broker-floor
  contract under the pipeline `min_notional` dust guard.
- The state machine's DAY-expiry accounting for intraday (105) integration.
