# Crypto Stage-0 battery — step checks moved out of the orchestrator, into execution

STATUS: delivered (new module + tests; nothing scheduled — no runtime change)
DATE:   2026-07-12
PR:     (this PR)
CONTEXT: this is the `renquant-execution` half of D-C12 (the Stage-0 paper
battery). The 7 broker-facing step checks were originally added directly to
`renquant-orchestrator` (PR #498, `scripts/crypto_stage0_battery.py`) ahead
of Codex's review. Two problems with that were found and fixed proactively:

1. **CI was genuinely red.** Orchestrator's CI job
   (`.github/workflows/ci.yml`) never installs `alpaca-py` — the pip
   install line lists `pytest numpy pandas scipy xgboost pyarrow pydantic
   cvxpy scikit-learn pandas_market_calendars` only. The step functions'
   deferred (in-function) `from alpaca...` imports raised
   `ModuleNotFoundError` in that environment even with a `MagicMock()`
   client, because the SDK enum/request TYPES themselves (not just the
   client) were unavailable — surfacing in CI as `ERROR` status on
   `test_no_tradable`, `test_all_accepted`, `test_partial_failure`, etc.
   instead of the expected `PASS`/`FAIL`.
2. **Architecture violation.** `renquant-orchestrator`'s own `CLAUDE.md`
   states a hard boundary: "Do not implement broker adapters here."
   Constructing Alpaca order requests / enums / asset-class filters and
   driving the trading/data clients directly is broker-adapter work — it
   belongs in `renquant-execution`, which already owns all Alpaca SDK
   interaction in this codebase (`alpaca_broker.py`, `alpaca_broker_port.py`)
   and already declares `alpaca-py` as a real (optional-extra) dependency,
   installed in this repo's own CI job. This is the same anti-pattern
   Codex flagged repeatedly this cycle (e.g. orchestrator#481's umbrella
   dependency; the architecture-violation-registry audit).

This exactly mirrors the `software_stops_liveness.py` precedent
(renquant-execution#29/#30, 2026-07-11/12): a broker/runtime-facing
checker moved out of orchestrator into this repo, with orchestrator kept
as a thin CLI/reporting consumer.

## What this PR does

Moves the 7 step-check functions **verbatim** (logic/PASS-FAIL-ERROR
classification unchanged — a repo-boundary fix, not a behavior rewrite)
from orchestrator's `scripts/crypto_stage0_battery.py` into:

- `src/renquant_execution/crypto_stage0_checks.py` — `StepResult`
  dataclass, the two Alpaca client factories (`get_trading_client`,
  `get_crypto_data_client` — de-underscored since they are now consumed
  cross-repo), and the 7 step functions: `step_crypto_status`,
  `step_pair_snapshot`, `step_order_acceptance` (GTC limit),
  `step_stop_limit_acceptance` (GTC stop-limit), `step_fee_from_fill`,
  `step_buying_power`, `step_data_parity` (Alpaca vs. yfinance daily
  close). `CANARY_PAIRS` / `TEST_NOTIONAL_USD` constants moved alongside.
- `tests/test_crypto_stage0_checks.py` — the 12 step-level tests moved
  verbatim from orchestrator's test file (imports adjusted only; same
  mocking/assertions). Two of these (`TestOrderAcceptance::test_all_accepted`
  and `TestStopLimitAcceptance::test_all_accepted`) genuinely construct
  real `alpaca.trading.requests.LimitOrderRequest` /
  `StopLimitOrderRequest` objects against a `MagicMock()` client — the SDK
  import path is exercised for real, not skipped or mocked away, because
  this repo's CI installs the `alpaca` extra.

`BatteryReport`, `run_battery`, CLI arg parsing, JSON report
writing/exit-code handling, and the `run_battery`-level tests stay in
`renquant-orchestrator` (orchestration/reporting, not broker-adapter
logic) — see orchestrator's own
`doc/progress/2026-07-12-crypto-stage0-battery.md` for that half.

## Public surface — judgment call, flag for review

`crypto_stage0_checks` is **not** re-exported from
`renquant_execution/__init__.py`. Orchestrator imports directly:

```python
from renquant_execution.crypto_stage0_checks import (
    StepResult, step_crypto_status, step_pair_snapshot,
    step_order_acceptance, step_stop_limit_acceptance,
    step_fee_from_fill, step_buying_power, step_data_parity,
    get_trading_client, get_crypto_data_client,
)
```

Two conventions coexist in this package: (a) stable, semantically-specific
names go through `__init__.py`'s `__all__` (e.g. `execution_payload`,
`normalize_order_intent`); (b) an "operational checker" module with its
own generic-sounding vocabulary is imported directly by submodule path —
the precedent being `software_stops_liveness` itself, never listed in
`__all__`. `StepResult`, `CANARY_PAIRS`, `get_trading_client`, etc. are
generic enough that a bare re-export risks a future namespace collision,
so direct-import was chosen. This is reversible at low cost if a
reviewer prefers the `__init__.py`-export convention instead.

## Verification

`python -m pytest tests/ -q` (with `renquant-common/src` on `PYTHONPATH`,
this repo's standard invocation): **383 passed, 2 skipped** (skips
pre-exist, unrelated to this change). The 12 moved step-check tests pass
with `alpaca-py` genuinely imported (not stubbed at the module level).

## Constraints honored

- `src/renquant_execution/alpaca_broker.py` was **not** touched (a
  concurrent, unrelated fix is in flight on a different branch).
- `--paper` required / live-hard-blocked safety property is unaffected —
  that logic lives entirely in orchestrator's `run_battery`/`main`, not
  in these step functions.
- No behavior change: every step function's PASS/FAIL/SKIP/ERROR
  classification logic is byte-identical to orchestrator PR #498's
  original.
