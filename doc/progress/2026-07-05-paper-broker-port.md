# PaperBrokerPort — the BrokerPort's paper-simulation adapter

DATE:     2026-07-05
PR:       (this PR)
CONTEXT:  renquant-orchestrator#365 (S9.4 paper-mode authorization gate) added
          a fail-closed `isinstance(port, PaperBroker)` check before live
          submission, but its own regression test could not exercise the
          real `PaperBroker` end-to-end: it had to subclass with
          `_ReconcilableFakePaperBroker(PaperBroker)` to add `open_orders()`,
          since `PaperBroker` never implemented slice-1's `BrokerPort`
          protocol (submit_order/cancel_order/open_orders/order_status,
          all client_order_id-keyed). Codex round 3 flagged this as a
          real-vs-claimed gap: the PR proved fail-closed coupling but not a
          runnable paper-canary execution path.

WHAT:     `PaperBrokerPort` — a real `BrokerPort` adapter over `PaperBroker`,
          following the same pattern as `AlpacaBrokerPort` (execution#21).
          `PaperBroker.place_order()` resolves synchronously to a terminal
          fill/reject before returning — no live/pending order state, no
          client_order_id concept (it mints its own internal `order_id`).
          The adapter stores a `client_order_id -> {status, filled_qty}`
          map, rejects duplicate client_order_ids
          (`BrokerPortContractError`, reused from `alpaca_broker_port` —
          not redefined), and:
          - `open_orders()` always returns `{}` — correct, not a stub: no
            PaperBroker order is ever left unfilled.
          - `cancel_order()` returns the already-terminal status without a
            live cancel attempt, for the same reason.
          - `order_status()` returns the stored terminal result.

WHY/DIR:  closes the real protocol gap rather than a test-only shim. Any
          caller (orchestrator's Stage-2 session runner, tests, future
          paper-canary tooling) can now construct a genuine `PaperBrokerPort`
          and run the real `reconcile_on_restart` / `begin_session` /
          `submit_remainder` path end-to-end, not just a subclassed fake
          with one method patched in.

EVIDENCE: 9 new tests in `test_paper_broker_port.py`, including
          `test_reconcile_on_restart_succeeds_against_fresh_book_and_paper_port`
          — the exact call `begin_session()` makes, run against the real
          adapter. Full suite: 160 passed.
          `[VERIFIED — pytest, this session]`

NEXT:     renquant-orchestrator#365 should replace
          `_ReconcilableFakePaperBroker` with this adapter directly once
          this PR merges (tracked there, not here).
