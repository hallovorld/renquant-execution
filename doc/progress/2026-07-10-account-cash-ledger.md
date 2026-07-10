# Account-scoped cash reservation ledger (crypto RFC §5.3 CORRECTED, D-C4)

STATUS:   delivered (flag-gated, default OFF)
DATE:     2026-07-10
PR:       (this PR)
SPEC:     merged crypto RFC, renquant-orchestrator
          `doc/design/2026-07-10-crypto-trading-rfc.md` §5.3 — the
          "Cash (CORRECTED — Codex review, 2026-07-10)" block, including the
          round-2 pre-submit broker-cash recheck.

CONTEXT:  `OrderStateBook` is per broker TAG (`alpaca` equity,
          `alpaca_crypto` sleeve) and `reserved_cash()` sums only its OWN
          open buy children — two books sizing `broker_cash - reserved_cash`
          from two LOCAL views of the ONE real brokerage account can each
          believe headroom exists that the other has already spent (a
          genuine concurrent double-reservation; cadence separation does not
          bound it). §5.3's fix is a single execution-owned ledger keyed by
          the REAL account, shared by every sleeve.

## What shipped

- `src/renquant_execution/account_cash_ledger.py` — `AccountCashLedger`:
  - One SQLite db per account (`data/account_cash_ledger.<account>.db`),
    WAL mode, `BEGIN IMMEDIATE` single-writer transactions, per-operation
    short-lived connections (safe across threads AND across the two real
    processes: 104 batch + crypto 24/7 loop).
  - `reserve(sleeve_tag, parent_intent_id, amount) -> bool`:
    `parent_intent_id` IS the idempotency key (UPSERT-then-check). Inside
    ONE write transaction: fresh `broker_cash` fetch (never cached), check
    `broker_cash - SUM(active, non-expired reservations across ALL tags) -
    amount >= 0`, insert row with `reserved_at`/`expires_at`. A retried
    call on an ACTIVE row is a no-op returning the original `True` — never
    a second reservation. `False` = the order placement must not proceed.
  - TTL = `MAX_PENDING_AGE_SECONDS + grace` (900s) — reuses the SS10
    order-timeout convention per the RFC, not a fresh number. Expired rows
    stop debiting headroom (the RFC's literal "active, non-expired" check
    formula) but are NEVER silently auto-released.
  - `release(parent_intent_id, reason)` — idempotent; wired into the SAME
    `OrderStateBook` lifecycle transitions that observe fill/cancel/reject/
    expire (plus the broker-submit-failure REJECTED path).
  - `recheck_before_submit()` — §5.3 round 2: immediately before the
    order-submit API call, re-fetch broker cash and re-verify
    `broker_cash - SUM(active, non-expired) >= 0`; a failure refuses the
    entry AND fail-closes new entries across EVERY sleeve (sticky
    account-wide halt in the shared db).
  - `sweep(broker_open_buy_intents, local_inflight_intents)` — orphan
    sweep: orphans (no broker order, no local in-flight state) are
    released + counted + returned for alerting; expired-unreleased rows are
    surfaced (never auto-released for expiry alone); a broker open BUY with
    no ACTIVE reservation (external/manual order, headroom leak) halts
    every sleeve. `clear_halt()` is an explicit operator/reconciliation
    action; nothing automated clears a halt.
  - `maybe_build_account_cash_ledger()` — flag-gated constructor
    (`RENQUANT_ACCOUNT_CASH_LEDGER`, default OFF -> `None`).

- `src/renquant_execution/order_state_machine.py` — hook wiring (all behind
  `cash_ledger=None` defaults; `None` = byte-identical legacy behavior):
  - `CashLedgerPort` Protocol (this module never imports the ledger; the
    ledger imports the TTL convention from here).
  - `OrderStateBook(..., cash_ledger=None)` + `attach_cash_ledger()`
    (snapshots never carry the ledger — runtime wiring, not state).
  - Release fires on the SAME transitions the book already owns: full fill
    (`on_fill` -> reason `filled`) and `_close_open_child` (reasons
    `canceled`/`rejected`/`expired`), covering `apply_terminal_status`,
    watchdog cancels, and the broker-submit-failure reject.
  - `submit_remainder` (BUY only; SELL/exits NEVER touch the ledger): 1)
    `reserve()` before any child exists — refusal maps to the EXISTING A2
    reason `insufficient_buying_power_headroom` (reused, not duplicated);
    when the account is fail-closed the halt reason propagates and the
    session book halts too; 2) `recheck_before_submit()` immediately before
    `port.submit_order` — a mismatch rejects the child (which releases the
    reservation on that same transition), halts this book, and the sticky
    ledger halt fail-closes every other sleeve.
  - `reconcile_on_restart`: when a ledger is attached, the same
    reconcile-before-emit pass runs the ledger sweep (result stored on
    `book.last_ledger_sweep` for alerting); unknown broker orders are
    conservatively treated as buys (external/manual ids can never match a
    reservation -> account-wide halt); a book reconcile mismatch ESCALATES
    from the session halt to the account-wide halt.
  - `parent_intent_id_from_client_order_id()` — inverts the SS7 two-level
    id for the sweep; non-conforming (manual) ids return verbatim and fail
    closed.

## Tests

- `tests/test_account_cash_ledger.py`: 45 tests — atomicity under
  concurrent reserves (two racing threads: exactly one of 60+60 wins 100;
  4-way racing idempotent retries reserve once; a second PROCESS via
  subprocess sees and honors the first process's reservation through the
  shared WAL db), idempotent retry no-op, refused-retry re-evaluation,
  released-row re-activation, fresh broker-cash fetch per attempt, TTL
  convention pin + expired-stops-debiting-but-stays-active, orphan sweep
  (released+counted, expired surfaced not released, unknown open buy ->
  every-sleeve fail-closed, released-row-doesn't-cover-open-buy), pre-submit
  recheck mismatch -> entry refused + account-wide halt, release wired to
  fill/cancel/reject/expire + partial-fill-keeps-reservation + re-emit
  re-reserves, cross-sleeve double-reservation prevented (the RFC §5.3
  scenario verbatim, two books one ledger), exits-never-blocked under a
  halted ledger (ledger not even consulted), reconcile_on_restart sweep
  paths (clean / crashed-orphan / manual order / missing-at-broker
  escalation / known-SELL no-op), flag-off identity (no ledger, no files,
  unchanged snapshot schema) and flag-on canonical path.
- Full suite: **311 passed** (baseline 266 post-#27, +45). `make doctor` ok.

## §5.3 ambiguities resolved (explicit list)

1. **Reserve retry after release re-checks headroom** instead of blind
   no-op `True`: the RFC's no-op clause ("same result as the original
   call") exists so retries "never double-reserve"; its named scenarios
   (timeout retry, crash-and-resubmit) are ACTIVE-row cases and stay
   no-ops. A RELEASED row holds no cash — a blind `True` would let a
   re-emitted remainder (cancel -> re-emit is a legal SS7 path) submit a
   buy with NO active reservation behind it, exactly the leak the ledger
   exists to prevent. So: ACTIVE -> no-op `True`; RELEASED -> fresh atomic
   headroom check + re-activation with new timestamps.
2. **A refused reserve inserts no row**, so a later retry re-evaluates
   fresh headroom and may succeed. Refusal reserved nothing (cannot
   double-reserve); recording refusals as permanent rows would wedge the
   intent for the whole session even after headroom frees.
3. **Expired reservations and the headroom SUM**: the RFC's check formula
   is literally "SUM(all active, non-expired ...)" while also mandating
   expired rows are "NOT auto-released silently". Implemented both
   literally: expired rows stop debiting the SUM (the TTL "bounds how long
   a crash can hold phantom headroom") but the row stays ACTIVE until the
   sweep surfaces it (`expired_unreleased`) — release happens only through
   the lifecycle hooks or the counted+alerted orphan path.
4. **"No ledger reservation" for the unknown-open-buy halt means no ACTIVE
   reservation** — a broker open buy whose reservation was already
   released is the same headroom leak (committed cash the SUM no longer
   covers) and halts every sleeve.
5. **TTL grace margin**: RFC says "order timeout budget + a fixed grace
   margin" without a number. Grace = `MAX_PENDING_AGE_SECONDS / 2` (300s),
   i.e. TTL 900s — derived from the existing SS10 constant rather than
   inventing an unrelated number; a crashed reservation outlives the
   stale-pending watchdog budget by half a cycle before it stops debiting.
6. **Halt reasons are sticky, first-reason-wins**, and `clear_halt()` is
   deliberately manual — the RFC's fail-closed states are cleared by
   reconciliation, never by an automated retry path.
7. **Sweep result surfacing**: `reconcile_on_restart` keeps its
   `ReconcileResult` return type (byte-compatible API) and exposes the
   ledger sweep on `book.last_ledger_sweep` for the caller's
   alerting/ntfy hook ("released, COUNTED, and ALERTED" — counting is the
   ledger's job here, alert transport is the caller's existing seam).
8. **Reservation amount = entry notional (`qty x price`)**, matching the
   A2 `evaluate_entry_headroom` input on the same path. Fee-aware amounts
   (crypto taker bps) arrive when the crypto sleeve wiring (D-C11) sizes
   its own `reserve()` calls; `submit_remainder` today submits equity-style
   children with `fee_bps=0` (pre-existing behavior, unchanged).

## Path-to-live (NOT this PR)

Default OFF; nothing consults the ledger until a caller attaches one. The
104 sizing path gains its single `reserve()` call when the umbrella/
orchestrator wiring passes a constructed ledger (flag ON + real
`broker_cash_fn` + real account id) — that flip is a separate,
operator-visible deployment step per the deployed-but-dark lesson, and the
crypto sleeve (D-C11) must NOT go live before it.
