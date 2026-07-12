# 2026-07-12 -- Crypto Stage-0 battery check API

## Bottom line

Restructures the crypto Stage-0 battery from the orchestrator repo into
renquant-execution. The battery validates crypto trading prerequisites on
Alpaca's PAPER account using the AlpacaBroker adapter exclusively (no
direct alpaca-py imports). Code review correctly identified that
broker-facing checks belong in the execution repo.

## What this PR contains

- `alpaca_broker.py`: originally 4 thin wrapper methods on AlpacaBroker, now
  6 (see the Revision note for the 2 added post-Codex-review):
  - `get_account_info()` -- account metadata (status, crypto_status, buying power)
  - `get_crypto_asset_spec(symbol)` -- public wrapper for per-pair order-grid spec
  - `place_crypto_limit_order(symbol, action, qty, limit_price)` -- crypto GTC/IOC limit order
  - `place_crypto_stop_limit_order(symbol, action, qty, stop_price, limit_price)` -- general-purpose crypto stop-limit (BUY + SELL sides)
  - `wait_for_order_terminal_cancel(order_id)` -- public wrapper over the
    existing private `_wait_for_order_terminal_cancel` (PR #31)
  - `get_crypto_reference_price(symbol)` -- latest-quote-derived reference
    price via `CryptoHistoricalDataClient` (finding 3)

- `crypto_stage0_checks.py`: complete rewrite (was a direct-alpaca-py
  importer, now uses AlpacaBroker adapter exclusively):
  - `StepResult` (now with a `required: bool` field) / `StepStatus` /
    `BatteryReport` data types
  - 6 battery steps: account status, pair snapshot, GTC order acceptance,
    stop-limit acceptance (both now private -- see Revision note finding 4),
    buying power behavior (observational, finding 6), data parity
    (placeholder, `required=False`, finding 5)
  - `run_full_battery(broker, dry_run=False)` -- orchestrates all steps; the
    only public entry point that can place a probe order (finding 4)
  - Hard safety gate: refuses to run on non-paper broker, PLUS fail-closed
    account/environment identity verification (finding 4)

- `tests/test_crypto_stage0_checks.py`: 45 tests (30 original + 15 from the
  Revision note) covering all battery steps, broker thin wrappers,
  dry-run mode, live-run mode, error handling, the paper-only safety gate,
  and the 6 Codex findings below. All mock the broker (no alpaca-py needed
  for the battery-logic tests; the broker thin-wrapper tests construct real
  alpaca-py request/response shapes, same as before).

## Key design choices

1. All checks route through the AlpacaBroker adapter, never direct alpaca-py.
2. GTC acceptance tested via a limit BUY at ~half the pair's real current
   reference price (never fills); stop-limit acceptance tested via a BUY
   stop-limit at ~3x the reference price (never triggers). Both cancelled
   immediately, with cancellation CONFIRMED (not just requested) before a
   PASS is reported. See the Revision note below -- this replaced an
   earlier fixed-constant ($0.01 / $999,999,999) design per Codex review.
3. Data parity is a SKIP placeholder -- the Trading API has no market-data
   endpoint; the orchestrator/data repo wires this when infrastructure
   exists. `required=False`: it does not block `BatteryReport.all_passed`.
4. Not re-exported from `__init__.py` (follows software_stops_liveness precedent).
5. The two transactional (order-placing) checks are private
   (`_check_gtc_order_acceptance` / `_check_stop_limit_acceptance`) --
   `run_full_battery` is the only public entry point that can place a
   probe order (see Revision note, finding 4).

## Verification

- 45 tests pass, 434 total (2 skipped) `[VERIFIED]` (current, post-revision;
  see Verification note at the end of the Revision note section below for
  the step-by-step count)

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
(2026-07-12T22:16:36Z, CHANGES_REQUESTED) -- 6 findings, not zero. This
landed mid-work, while the terminal-cancellation-confirmation fix (finding
1, scoped proactively ahead of seeing the review) was already being pushed.
All 6 findings are addressed below, in the SAME push -- not a reactive
follow-up discovering them later, but a proactive fix landing
concurrently/ahead of a second Codex pass on this revision.

## Revision note (2026-07-12): all 6 Codex findings addressed proactively

Codex's second review (2026-07-12T22:16:36Z, CHANGES_REQUESTED) raised 6
findings on top of the ones #32 already got right. All 6 are addressed in
this revision, in the same push as the originally-scoped terminal-
cancellation fix:

### Finding 1 -- cleanup must be a Tier-1 failure, not a silent PASS

`_check_gtc_order_acceptance`/`_check_stop_limit_acceptance` (renamed
private, see finding 4) used to call `broker.cancel_order(order_id)` inside
a `finally` block and only check whether it *raised* -- not whether the
order actually reached a confirmed terminal `canceled` state, and they
appended the pair to the success list *before* that check even ran. Fixed
via a new shared helper, `_place_probe_and_confirm_cleanup`:

- Added a new **public** wrapper, `AlpacaBroker.wait_for_order_terminal_cancel`,
  delegating to the existing private `_wait_for_order_terminal_cancel`
  (PR #31). Chose the public-wrapper route (option (b)) over calling the
  underscore-prefixed method directly from `crypto_stage0_checks.py`, for
  consistency: this PR's whole design principle for its thin wrappers
  (`get_account_info`, `get_crypto_asset_spec`, `place_crypto_limit_order`,
  `place_crypto_stop_limit_order`, and now `get_crypto_reference_price`,
  finding 3) is that the battery module never reaches into `AlpacaBroker`
  private *methods* (it does directly reuse two private *module-level pure
  functions*, `_enum_value`/`_is_resting_order_status` -- see finding 2's
  note on why that's a different, lower-risk judgment call). The wrapper is
  a pure pass-through and does **not** modify `replace_crypto_stop_limit`'s
  own call site (PR #31's own logic is untouched, per scope).
- A probe order that fills (or partially fills) is now reported as a
  DISTINCT, more severe Tier-1 condition ("real paper inventory acquired")
  from a merely-rejected one, and no cancel is attempted against it (there
  is nothing resting to cancel).
- A resting order is cancelled and the cancellation is polled via
  `wait_for_order_terminal_cancel`; if not confirmed within the timeout (or
  `cancel_order` itself raised), the step reports **FAIL** naming the
  pair/order id and `order_details[pair]["cancel_confirmed"] = False` --
  never a silent PASS.

### Finding 2 -- acceptance must be a genuinely resting, field-matching order

A nonempty `order_id` used to be treated as full proof of acceptance.
`place_crypto_limit_order`/`place_crypto_stop_limit_order` (this PR's own
two new wrapper methods -- not PR #31's) now re-derive `status`/`order_type`/
`side`/`confirmed_time_in_force`/`confirmed_limit_price`/
`confirmed_stop_price` via `_enum_value(getattr(order, ...))` instead of
trusting `_order_to_dict`'s naive `str()` cast -- the exact normalization
`get_open_orders_detailed` already applies elsewhere in this file for the
same reason (a real alpaca-py `(str, Enum)` field stringifies to
`"ClassName.MEMBER"`, not the lowercase wire value). `crypto_stage0_checks.py`
directly imports `_is_resting_order_status` from `alpaca_broker.py` (a
same-package private *function*, not a private *method* -- Codex's own
review explicitly suggested reusing it, and as a stateless pure function it
carries materially less encapsulation risk than reaching into instance
private methods, so direct import was the right call here even though
finding 1's wrapper decision went the other way for a stateful method).
`_place_probe_and_confirm_cleanup` now:

- rejects `filled`/`partially_filled` as the distinct Tier-1 case above;
- rejects any other non-genuinely-resting status (reusing
  `_is_resting_order_status`, the single canonical helper PR #31
  established -- notably this does NOT reject `"new"`, since that helper's
  own established contract already treats `new` as genuinely resting;
  duplicating a second, subtly different "resting" definition here would
  contradict the existing single source of truth, so the review's literal
  phrasing was interpreted as "don't infer acceptance without checking
  status", not as a request to redefine what counts as resting);
- after a confirmed-clean cancellation, validates `order_type`/`side`/
  `confirmed_time_in_force`/price fields against what was actually
  requested, and reports FAIL (naming the mismatch) if any disagree -- the
  order is still cleaned up either way.

### Finding 3 -- canary prices must be derived from the pair's real price, not magic constants

Replaced the fixed `$0.01` limit-BUY / `$999,999,999` stop-BUY constants
with prices derived from a new `AlpacaBroker.get_crypto_reference_price(symbol)`
method (a `CryptoHistoricalDataClient.get_crypto_latest_quote` lookup,
mid-of-bid-ask with single-sided fallback) -- deliberately scoped: one
latest-quote lookup, not a versioned price-band/quote-schema system. GTC
limit-BUY probe = ~50% of the reference price
(`DEFAULT_CANARY_LIMIT_BUY_FRACTION_OF_REFERENCE`); stop-BUY probe = ~3x the
reference price with a 1% buffer on the limit
(`DEFAULT_CANARY_STOP_MULTIPLE_OF_REFERENCE` /
`DEFAULT_CANARY_STOP_LIMIT_BUFFER`), both rounded to the pair's real
`price_increment` via the existing `round_price_to_increment` helper. A
reference-price lookup failure is a step FAIL for that pair (never silently
falls back to a fixed constant). Note: this surfaced a pre-existing
precision-ordering quirk in `place_crypto_limit_order` -- its preflight
`validate_crypto_order` checks the qty it's GIVEN for excess decimal
precision (9dp grid) BEFORE its own internal `snap_qty_to_increment` call,
so a raw `test_notional_usd / quote_derived_price` division (which can
carry many more significant digits than the old fixed `$0.01` ever did) was
occasionally rejected even though the properly-snapped quantity would have
been fine. Fixed by snapping the qty in `_check_gtc_order_acceptance`
itself before calling `place_crypto_limit_order` (a caller-side fix, not a
change to `place_crypto_limit_order`'s own preflight order).

### Finding 4 -- single public entry point for transactional probes

`_check_gtc_order_acceptance` and `_check_stop_limit_acceptance` (formerly
`check_gtc_order_acceptance`/`check_stop_limit_acceptance`) are now private
(underscore-prefixed) and removed from `__all__` -- `run_full_battery` is
the only sanctioned public entry point that can place a probe order; an
orchestrator caller can no longer call the transactional checks piecemeal.
Separately, `run_full_battery`'s environment/account-identity resolution no
longer defaults to `environment="paper"` on a failed lookup: it now runs
`check_crypto_account_status` FIRST, and if that step ERRORs, OR if it
succeeds but reports `paper=False` (despite `broker.paper=True` already
passing the hard `_assert_paper_mode` gate), the battery returns immediately
with `environment="unverified"` and a single `environment_verification`
ERROR step -- no transactional (or even further passive) steps run. Both
`broker.paper is True` AND a successfully-verified, paper-reporting account
lookup are now required before any probe order can be placed.

### Finding 5 -- required/optional step policy

`StepResult` gained a `required: bool = True` field. `check_data_parity`
(an always-SKIP data-domain placeholder, outside this repo's
execution-capability boundary) is `required=False`.
`BatteryReport.all_passed` now only requires every `required=True` step to
PASS, so a SKIP on `data_parity` no longer makes a clean, fully-passing
battery run structurally impossible to report as passing (the previous
behavior Codex flagged).

### Finding 6 -- buying-power check relabeled observational

`check_buying_power_behavior` doesn't establish (and never claimed to
establish, on inspection) a specific documented Alpaca account-field
invariant for non-marginable crypto behavior -- it only flags an internally
inconsistent reading as a misconfiguration signal. Kept the same heuristic
(a real, useful sanity check) but relabeled it explicitly: `required=False`,
and both the PASS and FAIL `detail` strings now say "observational only,
not a verified invariant" rather than implying a proven guarantee.

### Tests and verification

`tests/test_crypto_stage0_checks.py` gained coverage for: order-fill
Tier-1 rejection, order-field-mismatch rejection (side), reference-price
lookup failure, quote-derived pricing (asserting prices are in the
expected reference-relative range, not the old fixed constants),
`AlpacaBroker.get_crypto_reference_price` itself (mid-of-bid-ask, single-
sided fallback, lookup failure, no-usable-quote), the two
environment-verification fail-closed paths (account lookup ERROR; account
lookup succeeds but reports `paper=False`), and the required/optional
`all_passed` policy (a required-step failure still fails the battery; the
two `required=False` steps no longer block an otherwise-clean pass). All
existing happy-path tests were updated for the renamed private functions
and the enhanced fake `_FakeTradingClient` (which now echoes
`order_type`/`time_in_force`/`limit_price`/`stop_price` from the real
alpaca-py request object onto the returned fake order, so default
happy-path tests satisfy the new field-validation naturally, and a
`reference_prices=` override lets a test simulate a lookup failure).

Full suite: 434 passed, 2 skipped (was 419 passed, 2 skipped before any of
this revision's changes; +15 new tests net) `[VERIFIED]`.

## Revision note (round 2, 2026-07-12T22:51:25Z, post-Codex live re-review of 9dd8964)

Codex re-reviewed the round-1 fix (9dd8964) and found 5 further issues.
Each was independently verified against the actual code before acting --
one claim's literal premise didn't match the code, though the underlying
concern it was pointing at turned out to be real elsewhere.

### Finding 1 (as literally stated) -- NOT reproduced; pushed back

Codex's review said: "the probe passes `expected_price_fields` keyed as
`limit_price`" -- implying price validation compares the requested value to
itself. Direct inspection of `_check_gtc_order_acceptance` /
`_check_stop_limit_acceptance` shows both already pass
`expected_price_fields={"confirmed_limit_price": limit_price}` (and
`confirmed_stop_price` for the stop-limit probe) -- keyed on the
`confirmed_*` field, not the plain one -- and `confirmed_limit_price` /
`confirmed_stop_price` are populated in `alpaca_broker.py` by reading
`order.limit_price` / `order.stop_price` directly off the SDK's
`submit_order()` response object, genuinely independent of what
`_check_gtc_order_acceptance` asked for. This specific claim does not match
the code at 9dd8964; not changed.

### Finding 1's underlying concern -- REAL, found for QUANTITY instead

Re-examining the exact same class of bug Codex was describing (a
"confirmed" field that's actually just an echo of our own request, not
independently sourced) surfaced a genuine instance of it: `quantity` in the
`place_crypto_limit_order`/`place_crypto_stop_limit_order` result dicts is
set by `result.update({..., "quantity": float(submit_qty), ...})` -- our
OWN submitted/snapped value, not `order.qty` from the broker's response.
`_place_probe_and_confirm_cleanup` (round 1) never validated quantity at
all (see Finding 2), so this wasn't yet exploitable, but adding quantity
validation against the self-referential `quantity` field would have been
exactly the vacuous check Codex is worried about. Fixed by adding a
genuinely independent `confirmed_quantity` field (reads `order.qty`
directly, mirroring `confirmed_limit_price`/`confirmed_stop_price`) to both
order-placement methods, and validating THAT in
`_place_probe_and_confirm_cleanup`, not the echoed `quantity` field.

### Finding 2 -- field matching was fail-open; quantity unvalidated -- CONFIRMED, fixed

`if order_type and order_type != expected_order_type` (and the same
pattern for `side`/`confirmed_time_in_force`) skipped the check entirely
when the field was empty/missing -- exactly backwards, since a missing
broker-confirmed field means we CAN'T confirm a match, which must FAIL, not
pass silently. Changed to `if not order_type or order_type != expected`.
Quantity (via the new `confirmed_quantity` field, see above) is now
validated with the same tolerance-based comparison already used for price
fields.

### Finding 3 -- filled/partially-filled recorded no residual-position evidence -- CONFIRMED, fixed

A bare `status="filled"` is not durable evidence of how much paper
inventory now exists. Added `_query_residual_position()` (best-effort,
never raises -- the fill-failure condition must still be reported even if
this diagnostic extra can't be obtained) calling `broker.get_position()`,
recorded as `residual_position_qty` in the step detail whenever a probe
order fills instead of resting.

### Finding 4 -- quote-derived prices had no provenance/freshness check -- CONFIRMED, fixed

`get_crypto_reference_price()` returned a bare `float` with no timestamp,
source, or symbol identity, and could silently derive canary prices from
stale market data. Added `CryptoQuoteSnapshot` (symbol, bid/ask/mid,
timestamp, age_seconds) and `get_crypto_reference_quote()`, which rejects a
missing timestamp, a stale quote (`age_seconds > max_staleness_seconds`,
default 60s), or an implausible future timestamp (>5s ahead of now) before
returning. `get_crypto_reference_price()` is now a thin wrapper over it for
callers that only want the number. Both battery probes switched to the
typed quote and record `quote_timestamp`/`quote_age_seconds` in their
per-pair detail.

### Finding 5 -- cancel_order() exception short-circuited without polling -- CONFIRMED, fixed

Round 1 returned immediately on a `cancel_order()` exception without
polling for the actual terminal state. A raised exception (e.g. a
transport-level timeout) is not proof the cancel request never reached the
broker -- exactly the inverse of the "no exception raised is not proof of
success" discipline this same function already applies elsewhere. Now
`cancel_order()`'s exception (if any) is caught and recorded, but
`wait_for_order_terminal_cancel()` is ALWAYS called afterward regardless;
the result is a PASS with the exception recorded as evidence if the poll
independently confirms terminal cancellation, or a FAIL naming both the
exception and the unconfirmed poll result if it doesn't. (Checked whether
this was actually a regression from an earlier version, as Codex's review
phrased it ("rather than polling as earlier code did") -- neither the
immediately-prior commit (ed1fbe66) nor `replace_crypto_stop_limit`'s
established PR #31 pattern actually polled after a raised cancel exception
either, so there wasn't a prior behavior to regress from. The suggested
improvement is correct and valuable on its own merits regardless.)

### Tests and verification (round 2)

Added 10 new tests: residual-position-query-on-fill, missing/blank
`order_type` field forced-fail, quantity-mismatch forced-fail,
`get_crypto_reference_quote`'s typed-snapshot return and its 3 fail-closed
paths (missing timestamp, stale, implausible-future-timestamp) plus one
accepts-within-bound case, and both halves of the cancel-exception-then-poll
behavior (poll confirms cancellation despite the exception -> PASS with
evidence; poll also fails to confirm -> FAIL naming both). Full suite: 444
passed, 2 skipped (was 434/2 before this round; +10 net) `[VERIFIED]`.

## Revision note (round 3, 2026-07-12) -- residual-exposure race closed

A concurrent session opened execution#35 then #36 in parallel with round 2
above, independently re-implementing overlapping fixes on the same
lineage. Compared both against this PR's round-2 head in detail rather than
picking one blind: #35/#36's order-acceptance-strictness, environment
(`base_url`) cross-check, and report-integrity (nonempty required-gate set,
schema version, content hash) ideas were good, but its stop-limit fix
relabeled the check `required=False`/non-gating instead of fixing the
actual price-band false-negative risk -- the weaker of the two remediation
paths Codex explicitly offered, whereas this PR's quote-derived canary
pricing keeps the check meaningful. Closed #35 and #36 crediting their
genuinely good ideas (`gh pr view 35`/`36 --repo hallovorld/renquant-execution`
for the full comparison).

One idea from #36 was worth adopting on its own merits: `_check_residual_exposure()`
queried order state + position after EVERY probe, not just when the
*initial* synchronous response already reported filled/partially_filled.
This closes a real race this PR's round-2 fix didn't cover -- a probe order
resting at acceptance time can still fill asynchronously during the
cancel-confirm window, and the round-2 fix's residual-position query only
fired on the initial-status branch. Added the same residual-position query
to the `cancel_confirmed is False` branch (an unconfirmed cancellation is
ambiguous: still resting, or filled during the window --
`wait_for_order_terminal_cancel` returns `False` for either case per its
own docstring, so this is exactly where the ambiguity needs resolving with
evidence).

1 new test (`test_gtc_acceptance_queries_residual_position_when_cancel_unconfirmed`).
Full suite: 445 passed, 2 skipped (was 444/2; +1 net) `[VERIFIED]`.
