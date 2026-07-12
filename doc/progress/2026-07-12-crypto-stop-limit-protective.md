# 2026-07-12 — D-C5 crypto GTC stop-limit protective path completion

## Bottom line

Completes D-C5 from the crypto RFC: cancel-then-replace for stop price/qty
changes, detailed open-order query for auditing, and Tier-1 stop-coverage
check that verifies every crypto position has a resting protective stop.

## What this PR contains

- `alpaca_broker.py`: `replace_crypto_stop_limit` (cancel old + place new),
  `get_open_orders_detailed` (full order dicts incl. type/stop/limit prices),
  `check_crypto_stop_coverage` (Tier-1 audit: violations for unprotected
  crypto positions)
- `test_crypto_order_semantics.py`: 7 new tests covering replace, detailed
  query, full/partial/zero/equity-excluded coverage checks
- `_FakeCryptoClient`: `get_all_positions` and `cancel_order_by_id` additions

## Key design choices

1. Cancel-then-replace (not atomic) because Alpaca has no atomic stop-limit
   replace. Caller MUST treat cancel-success + place-failure as Tier-1.
2. Coverage check compares total resting stop-limit SELL qty against held qty
   per symbol, with QTY_INTEGRAL_EPS tolerance.
3. Equity positions are excluded from coverage check (they use the existing
   whole-share GTC stop path).

## Verification

- 72 tests pass (7 new) `[VERIFIED]`
- 379 total execution tests pass, 1 skipped `[VERIFIED]`

## Revision note (2026-07-12, post-Codex CHANGES_REQUESTED)

Codex (haorensjtu-dev) left a blocking review on this PR
(2026-07-12T21:33:31Z) with 5 findings against the original
`check_crypto_stop_coverage` / `replace_crypto_stop_limit`. Quoting/closely
paraphrasing each, and the fix applied:

1. **"lines 906-911 check only order_type and side. An IOC/DAY/non-GTC
   stop-limit is counted as coverage. Require time_in_force == gtc, valid
   positive stop and limit prices, and a broker status that is genuinely
   resting."** — `check_crypto_stop_coverage` now requires a "qualifying"
   stop to pass ALL of: `order_type == "stop_limit"`, `side == "SELL"`,
   `time_in_force == "gtc"` (case-insensitive), `stop_price > 0`,
   `limit_price > 0`, AND a genuinely resting broker status (new
   `_is_resting_order_status`: excludes anything containing `"pending"`,
   requires `"new"`/`"accepted"`/`"held"` — `QueryOrderStatus.OPEN` still
   reports `pending_cancel`/`pending_replace`/`pending_new` as "open", none
   of which is a live, triggerable stop). New `violation_kind` of
   `"non_resting_ignored"` distinguishes "a stop order exists but isn't
   resting right now" from a true `"uncovered"`.

2. **"Summing all stop SELL quantities can declare a position covered by
   multiple independently executable stops... Coverage must model
   reservations explicitly: require one authoritative protective order per
   position... fail closed on duplicate competing stops."** —
   `check_crypto_stop_coverage` now COUNTS qualifying stops per symbol
   instead of summing quantities: 0 → `"uncovered"`/`"non_resting_ignored"`,
   exactly 1 with sufficient qty → covered, exactly 1 short → `"partial"`,
   **2 or more → `"duplicate"`** (fail-closed; the summed quantity is never
   treated as safe, even if it numerically exceeds the held quantity).

3. **"replace_crypto_stop_limit cancels then immediately submits a
   replacement without confirming cancellation... Poll/verify the terminal
   cancellation state before replacement and return a structured, durable
   Tier-1 unprotected result if cancellation or replacement cannot be
   confirmed."** — new private `_wait_for_order_terminal_cancel(order_id, *,
   timeout_seconds=5.0, poll_interval_seconds=0.25)` polls
   `client.get_order_by_id` until a confirmed terminal `canceled` status or
   timeout. `replace_crypto_stop_limit` now: cancels → confirms via the
   poller → only then places the replacement. Judgment call: 5.0s
   timeout / 0.25s poll interval, chosen as "ample margin for a sub-second
   paper/live cancel ack without stalling the caller noticeably" — both are
   overridable keyword args on `replace_crypto_stop_limit` itself, not
   hardcoded.

4. **"The cancel-then-replace gap is only described in a docstring. The
   adapter must emit an auditable state/incident that upstream orchestration
   can consume... A plain no-submit/exception is insufficient because
   callers can forget to interpret it."** — `replace_crypto_stop_limit` now
   returns a discriminated dict: `{"protected": bool, "status": "replaced" |
   "cancel_unconfirmed" | "unprotected_after_cancel", "old_order_id",
   "new_order_id", "unprotected_reason", "reason", ...}` (plus
   `place_crypto_stop_limit`'s own fields on success). On both failure
   statuses it ALSO emits a `RuntimeWarning` (this file's existing
   fail-closed convention, matching `_no_submit_result`), so a caller
   watching only warnings/logs still notices. Docstring states explicitly:
   the real "upstream orchestration blocks new entries + pages the owner"
   wiring is `check_crypto_stop_coverage()` — a failed replace leaves that
   symbol uncovered, so the orchestrator scheduler's next
   `check_crypto_stop_coverage()` call (PR #497, fixed in parallel)
   independently re-discovers it — a second, independent layer beyond this
   function's own return value.

5. **"Use the pair's min_trade_increment, rather than a generic
   equity-oriented epsilon, when comparing held and covered crypto
   quantities."** — the covered-vs-held comparison now resolves
   `self._resolve_crypto_spec(symbol)` and uses `spec.min_trade_increment`
   as the tolerance instead of `QTY_INTEGRAL_EPS`. If the spec lookup fails
   for a symbol, that symbol is a violation (new `violation_kind`
   `"spec_lookup_failed"`) — never a silent fallback to the equity epsilon
   or a skip.

**Also found and fixed in the same pass (not one of the 5 numbered
findings, but directly undermines them):** `get_open_orders_detailed`'s
`order_type`/`time_in_force` fields (and `_order_to_dict`'s `side`/`status`
fields it inherits) were extracted via a naive `str(getattr(order, ...))`
cast. `[VERIFIED alpaca-py 0.43.4]`: a real SDK `Order`'s enum fields
stringify via plain `str()` to `"ClassName.MEMBER"` (`Enum.__str__`), NOT the
lowercase wire value (e.g. `str(OrderStatus.ACCEPTED) ==
"OrderStatus.ACCEPTED"`, but `.value == "accepted"`) — only the test-double
`SimpleNamespace` fixtures (plain strings) masked this. Left as-is, EVERY
qualifying-stop check in this PR would have silently never matched a real
broker response (permanent 100% false "uncovered", not a safety hazard in
direction but a total functional no-op in production). Fixed by having
`get_open_orders_detailed` re-derive `status`/`side`/`order_type`/
`time_in_force` via the same safe `getattr(x, "value", x)` idiom
`_order_matches_asset_class` already used for `asset_class` (new
`_enum_value` helper). Covered by
`test_get_open_orders_detailed_normalizes_sdk_enum_like_fields`.

Tests added (`tests/test_crypto_order_semantics.py`): non-GTC rejection,
duplicate-stops fail-closed, pending-status exclusion, increment-boundary
just-inside/just-outside, spec-lookup-failure fail-closed, SDK-enum-like
field normalization, cancel-unconfirmed (no replacement placed), and
placement-fails-after-confirmed-cancel. 15 tests in the affected area, 387
passed + 2 skipped (389 total) in the full suite `[VERIFIED]`.

## Revision note round 2 (2026-07-12T21:52:07Z, post-Codex CHANGES_REQUESTED)

Codex found one remaining fail-open in the round-1 fix:

> One remaining blocking fail-open in the revised replace path:
> `place_crypto_stop_limit` can return a no-submit result (for example
> invalid grid, below minimum, or spec lookup failure) without raising.
> `replace_crypto_stop_limit` catches exceptions only, then unconditionally
> sets `protected=True` and `status=replaced`. After a confirmed cancel this
> can falsely report protection even though no replacement order exists.
>
> Treat a returned skipped/no-submit result, missing `order_id`, or
> non-resting returned status exactly like
> `replacement_failed_after_confirmed_cancel`: `protected=False`,
> `unprotected_after_cancel`, durable Tier-1 signal. Add a regression test
> using a non-throwing no-submit replacement path.

Verified directly against the current code before fixing: as written today,
`place_crypto_stop_limit` never actually returns a no-submit-shaped dict —
every rejection path (invalid grid, below-minimum size, spec-lookup failure,
no-short violation, bad prices) raises `ValueError`, by design, per its own
docstring ("Fail-loud (not no-submit) on violations"). So the specific
triggering scenario Codex names does not occur with today's code. That does
not make the underlying concern wrong, though — `replace_crypto_stop_limit`
should not depend on that invariant holding forever (a future change to
`place_crypto_stop_limit`, or an Alpaca API edge case where a submission
call returns normally with an order the exchange itself immediately
rejected in the response body rather than via an exception, would silently
defeat the round-1 fix). Applied the fix as a strictly-beneficial
defensive improvement regardless of whether the current code can trigger
it: after `place_crypto_stop_limit` returns without raising,
`replace_crypto_stop_limit` now additionally validates the result has a
non-empty `order_id` AND a genuinely resting status (reusing
`_is_resting_order_status`/`_enum_value`) before declaring `protected=True`
— either check failing routes through the same
`unprotected_after_cancel`/Tier-1 path as an exception.

Added `test_replace_crypto_stop_limit_treats_missing_order_id_as_unprotected`
and `test_replace_crypto_stop_limit_treats_non_resting_returned_status_as_unprotected`
(both inject a non-throwing no-submit-shaped return via a monkeypatched
`place_crypto_stop_limit`, since the real method cannot currently produce
one). Full suite: 389 passed, 2 skipped (391 total) `[VERIFIED]`.

Not merged — left open for Codex re-review per the operating agreement.
