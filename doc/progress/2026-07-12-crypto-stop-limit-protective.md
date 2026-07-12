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
