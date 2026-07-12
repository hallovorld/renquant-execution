# Crypto Stage-0 paper battery rehomed to execution (D-C12)

STATUS: delivered
DATE:   2026-07-12
PR:     (this PR)

## Context

Orchestrator PR #498 added the Stage-0 paper battery for crypto trading
capability (RFC D-C12) as `scripts/crypto_stage0_battery.py`. Codex
correctly rejected it: the battery uses Alpaca SDK calls (TradingClient,
order requests, asset queries) which are broker-facing checks belonging in
renquant-execution, not orchestrator.

Orchestrator CLAUDE.md hard boundary: "Do not implement broker adapters here."

## What this PR does

Rehomes the full standalone battery script to
`scripts/crypto_stage0_battery.py` in renquant-execution:

- 7 verification steps: crypto_status, pair_snapshot, order_acceptance
  (GTC limit), stop_limit_acceptance, fee_from_fill, buying_power,
  data_parity (Alpaca vs yfinance)
- CLI entrypoint with --paper (required), --dry-run, --output flags
- JSON report with PASS/FAIL/SKIP/ERROR per step
- BatteryReport dataclass with summary statistics

Tests rehomed to `tests/test_crypto_stage0_battery.py` (12 test cases,
all mocked -- no network calls).

`pyproject.toml` updated to add `scripts` to pytest pythonpath.

## Architecture note

This follows the same pattern as software_stops_liveness.py (#29/#30):
broker-facing checkers live in renquant-execution; orchestrator can
consume them as a thin CLI wrapper if needed.
