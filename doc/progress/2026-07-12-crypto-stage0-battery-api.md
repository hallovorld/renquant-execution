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
