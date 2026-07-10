# ReadOnlyBrokerWrapper — parameterized broker-state tag (P-1)

STATUS:   delivered
DATE:     2026-07-10
PR:       (this PR)
CONTEXT:  `renquant_execution.readonly_broker.ReadOnlyBrokerWrapper` was an
          unwired port of the umbrella's live `live/broker_readonly.py`
          wrapper, and it inherited the umbrella copy's defect: the
          state-isolation tag was a hardcoded class constant
          (`broker_name = "alpaca_shadow"`, readonly_broker.py:14; never
          set in `__init__`). One hardcoded tag means at most ONE isolated
          shadow book per machine — every consumer keys state paths
          (`live_state.<tag>.json` / `runs.<tag>.db`, pipeline
          `kernel/state_paths.py`) on `broker_name`.

WHAT:     The tag is now a validated constructor/factory parameter:
          - `ReadOnlyBrokerWrapper(underlying, broker_name="alpaca_shadow")`
            — default preserves the pre-parameterization value exactly
            (class attribute kept too, for class-level readers).
          - `validate_readonly_broker_name()` — shape-only guard: non-empty
            path-safe token (`[A-Za-z0-9_-]+`), TypeError on non-str,
            ValueError on garbage; NO silent normalization. Membership
            stays owned by the pipeline's fail-closed `ALLOWED_BROKERS`
            allowlist — not duplicated here (check-existing-contract rule).
          - `get_broker(mode, readonly_broker_name=...)` — factory
            threading for the two read-only modes; passing the tag with a
            non-readonly mode fails loud.
          - Swallowed writes now log at DEBUG with the arm's tag (parity
            with the umbrella wrapper's audit logging).
          - `__getattr__` hardened: `underlying` guarded so a partially
            constructed instance raises AttributeError, never recurses.

WHY/DIR:  Prerequisite P-1 of the D6-§2a two-arm breadth-lever shadow A/B
          (renquant-orchestrator#443,
          `doc/design/2026-07-09-governor-prereg-replay-protocol.md` §2a):
          arms S-0.5/S-1.0 need tags `alpaca_shadow_a`/`alpaca_shadow_b`
          disjoint from the legacy `alpaca_shadow` ops shadow. The change
          stands on its own merit (wiring + parameterizing an existing dead
          port, execution-repo boundary only) and does NOT depend on that
          RFC's approval; no experiment logic is wired here.

PARITY:   vs the umbrella `live/broker_readonly.py` contract — reads
          forwarded, writes swallowed to shadow acks, no network mutation,
          `broker_name` = state-isolation key. Intentional deltas (all
          pre-existing port choices, now documented + pinned):
          1. ack `status` is `"shadow_ack"` (umbrella: `"filled"` /
             `"accepted"`); both carry `shadow: True`.
          2. `place_notional_order` is shadow-acked here; the umbrella
             wrapper lacks the override (its `__getattr__` would forward a
             notional order to the REAL broker — a defect, not a contract).
          3. `supports_broker_side_stops` forwards `(symbol, quantity)`
             directly; no TypeError back-compat shim (this repo's
             `BaseBroker` already defines the qty-aware signature).
          4. shadow order-id format `SHADOW-<uuid12>` (umbrella:
             `shadow-<ts>-<seq>-<uuid6>`).

EVIDENCE: 23 new tests in `tests/test_readonly_broker_port.py` (never-
          submit on all four write paths, per-tag state-path disjointness
          incl. legacy + prod tags, loud validation, default-tag backward
          compat, factory threading/rejection). Full suite: 200 passed,
          1 skipped (pre-existing yfinance-optional skip; baseline before
          this PR: 177 passed, 1 skipped — 178 collected, per PR #25).
          `[VERIFIED — pytest, CI-equivalent py3.10 + alpaca extra env]`

NEXT:     P-2 (renquant-orchestrator two-arm shadow runner) constructs
          `ReadOnlyBrokerWrapper(AlpacaBroker(paper=False), broker_name=<tag>)`
          per arm; pipeline `ALLOWED_BROKERS` gains the two experiment tags
          in its own repo (separate PR, per D6-§2a ownership map).
