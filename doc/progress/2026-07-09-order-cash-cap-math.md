# Order cash-cap sizing math — ownership move from the umbrella (S-FRAC v2, D7 #1)

DATE: 2026-07-09
DESIGN: renquant-orchestrator `doc/design/2026-07-02-s-frac-fractional-v2.md`
(D7 gap inventory #1 — the last buy-path int truncation)
CONSUMES: nothing new; reuses `broker.py::MIN_FRACTIONAL_NOTIONAL_USD`
CONSUMED BY: RenQuant#454 — the umbrella `cap_buy_order_to_cash` becomes a
time-bounded compatibility call-site that delegates here

## Why here

RenQuant#454 first implemented the fractional-aware cash cap directly in the
umbrella's `adapters/runner_execmath.py`. Its review (codex) ruled the umbrella
must not gain new execution capability — the umbrella is being deprecated in
favor of the multi-repo model, and order-sizing math is execution-repo
territory. This PR makes renquant-execution the owner; the umbrella keeps only
a fail-closed delegate call-site slated for deletion when RunnerAdapter order
math migrates here (adapter-migration program).

## What was built

`src/renquant_execution/order_math.py::cap_affordable_qty(price, cash, *,
fractional=False, min_fractional_notional=MIN_FRACTIONAL_NOTIONAL_USD)` —
pure sizing function, no I/O:

- **Whole-share mode (default)**: exact legacy umbrella semantics —
  `int(cash // price)` shares, `0` (reject) below one whole share. Returns
  `int`; flag-off consumers are byte-identity-pinned on value AND type.
- **Fractional mode**: floors onto the 6dp sizing grid
  (`floor(cash / price * 1e6) / 1e6`, the renquant-pipeline
  `compute_position_size` convention); a floored notional below the ~$1
  broker fractional minimum returns `0.0` (reject — never a doomed dust
  submission). Flooring, never round-to-nearest: realized notional <= cash.
- Reject semantics are a `<= 0` return, folded into the function; garbage
  inputs (non-finite / non-positive price, non-finite cash) raise
  `ValueError` so misuse is loud. Negative cash (exhausted budget) is a
  valid reject input.
- The $1 threshold is `broker.py::MIN_FRACTIONAL_NOTIONAL_USD` imported —
  not a duplicated literal.

## Tests (ported from the umbrella pins, RenQuant#454)

`tests/test_order_math.py` — worked examples (the exact D7 #1 cases: 0.5-share
slice admitted, $0.50 dust rejected, 33.333333 floor) plus seeded-grid
invariants (4000 cases per property, no hypothesis, replayable from printed
inputs): **flag-off byte-identity vs a frozen verbatim legacy copy**,
never-overspend in both modes, 6dp-grid + min-notional landing, reject
justification, monotonicity in cash, ValueError on garbage.

Full suite: 178 passed (was 160 on main; +18 new pins).
