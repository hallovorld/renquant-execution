# Crypto order semantics — execution slice of the merged crypto RFC

- Date: 2026-07-10
- Branch: `feat/crypto-order-semantics`
- Spec: merged crypto RFC (renquant-orchestrator
  `doc/design/2026-07-10-crypto-trading-rfc.md`) — §2.1 gap table E1-E12,
  §3.2 execution additions, §5.1 stop-limit semantics.
- Scope note: the dispatching task labeled this slice "D-C1"; in the RFC's
  own §7 deliverable table it corresponds to the order-semantics portion of
  **D-C4 + D-C5** (D-C1 there is the renquant-common calendar/slug helper).
  The §5.3 `AccountCashLedger` (the other half of D-C4) is explicitly NOT in
  this PR — it lands separately before the crypto sleeve goes live.

## What shipped

Every change is asset-class-gated: crypto rules bind only on pair-form
symbols (`BTC/USD`, RFC §3.0) or an explicit `asset_class="crypto"`; equity
paths are pinned byte-identical by regression tests.

| Gap | Change |
|---|---|
| E1/E2 | `crypto.py` validation seam next to `broker.py`'s fractional validators: `validate_crypto_order` accepts market/limit/stop_limit × GTC/IOC, REJECTS DAY (inverse of the equity fractional DAY pin, same enforcement style). `AlpacaBroker.place_order` gains keyword-only `time_in_force`/`asset_class`; crypto market orders default IOC (RFC maps IOC = immediate entry), GTC opt-in; an equity order with non-DAY TIF fails loud. |
| E3 | `get_filled_orders`/`get_open_orders` gain `asset_class` (default `us_equity` — existing callers unchanged; `"crypto"` explicit; `None` = all). **[VERIFIED alpaca-py 0.43.4]: `GetOrdersRequest` has NO `asset_class` field (`trading/requests.py:198-219`) — pydantic silently dropped the kwarg the legacy code passed, so the historical "US_EQUITY filter" never reached the API.** The filter is now client-side on the returned `Order.asset_class` (`trading/models.py:188`), with a pair-form-symbol fallback when the SDK omits it. `ReadOnlyBrokerWrapper` forwards the parameter only when non-default (legacy underlying fakes untouched). |
| E4 | `CryptoFeeSchedule` (taker 25 / maker 15 bps defaults, marked `[GUESS: Stage-0 verifies]`). `cap_affordable_qty` gains `fee_bps=0.0` (0.0 = IEEE-identical legacy math); new `cap_affordable_qty_crypto` sizes on the increment grid net of fees; `PaperBroker` nets taker fees on crypto fills only (buy affordability includes the fee); `OrderStateBook` children carry `fee_bps` (snapshot back-compat: missing key → 0.0) and `reserved_cash()` reserves `notional × (1 + fee_bps/1e4)` for fee-bearing children only. |
| E5/E6 | No fractionable lookup for crypto (natively fractional — proven by a `fractionable=False` asset still submitting); qty floored onto per-pair `min_trade_increment`, rejected below `min_order_size` (no-submit, never rounded up); NO whole-share snap. Spec source: pinned `crypto_asset_specs` constructor snapshot (RFC §3.1) first, else fail-closed `get_asset` lookup (Asset fields per SDK), cached only on confirmed success. |
| E7 | `round_price_to_increment` — per-asset `price_increment` grid (Decimal), replacing the equity 2/4-dp rule for crypto stop/limit prices. |
| E8 | `AlpacaBroker.place_crypto_stop_limit(symbol, qty, stop, limit)` — `StopLimitOrderRequest` + GTC + native fractional qty; docstring carries the RFC §5.1 gap-through/non-fill honesty language verbatim (pinned by test). Fail-LOUD (not no-submit): a protective stop that can't be placed is a Tier-1 condition. `place_stop_order` refuses crypto (no plain stop in the SDK crypto matrix) and directs to the stop-limit path; `supports_broker_side_stops` answers True for crypto fractional. |
| E9 | `resolve_day_expiry` skips crypto-pair parents entirely (no status fetch): a resting GTC crypto order is a legitimate overnight state, never terminated at an equity close. The crypto `max_resting_age` watchdog is D-C4-followup scope (sleeve wiring), not this sweep. |
| E10 | `is_market_open(symbol)` returns True for crypto with NO broker round-trip (proven by a disconnected-broker test); the NYSE pre-open cancel gate excludes crypto orders even in `--cancel-both-sides` mode. `AlpacaBrokerPort` (105 equity DAY-pinned port) fail-closes on crypto symbols — crypto port wiring is D-C11 scope. |
| E11 | `crypto_no_short_violation`/`assert_crypto_no_short`: crypto sell qty ≤ held qty, asserted before submit on both the market path (no-submit `rejected_crypto_no_short`) and the stop-limit path (raise). `place_notional_order` refuses crypto pairs (MARKET+DAY shape by construction). |

New no-submit vocabulary (all in `NO_SUBMIT_STATUSES`, only producible by
crypto paths): `rejected_invalid_crypto_order`, `rejected_crypto_no_short`,
`rejected_below_min_order_size`, `rejected_crypto_spec_lookup_failed`.

## Tests

- `tests/test_crypto_order_semantics.py`: 65 tests — per-gap regression,
  the order-shape matrix (market/limit/stop_limit × GTC/IOC accepted, DAY
  rejected), and equity byte-identity pins (fractional validator DAY pin,
  equity request shape + whole-share snap, PaperBroker equity result-dict
  equality with/without a fee schedule, `reserved_cash` equity identity,
  snapshot back-compat, `supports_broker_side_stops` equity pins).
- Full suite: **266 passed** (baseline 201 post-#26, +65).
- End-to-end drive (not just unit tests): crypto + equity intents through
  `execute_live_commit` → `BrokerExecutionPipeline` → PaperBroker with fees;
  fake-client AlpacaBroker market/stop-limit/no-short/DAY-reject/24-7 flow.

## RFC ambiguities resolved (explicit list)

1. **Deliverable ID**: task said "D-C1"; RFC §7 calls this slice D-C4/D-C5
   order semantics. Implemented the task's item list; `AccountCashLedger`
   (§5.3) deliberately excluded.
2. **Crypto market-order default TIF = IOC** (RFC §3.2 maps IOC to
   "immediate entry", which a market order is; GTC reserved for resting
   limit/protective stops and available explicitly).
3. **E3 filter mechanics**: RFC prescribed a request-level asset-class
   filter; the SDK has none on `GetOrdersRequest` (silently dropped kwarg —
   pre-existing latent bug on main). Implemented client-side on
   `Order.asset_class` with symbol-form fallback.
4. **Method name** `place_crypto_stop_limit` per RFC §3.2's named signature
   (task text said "place_stop_limit_order path").
5. **Stop-limit failures raise** (fail-loud) instead of returning no-submit
   rows: RFC §5.1 treats a missing protective stop as Tier-1.
6. **Sell-side increment snapping** floors to the grid like buys (residual
   below one increment is untradeable dust; requested vs submitted qty both
   recorded in the result for audit).
7. **`max_resting_age` watchdog** (RFC §3.2 E9 second half) deferred to the
   sleeve-wiring PR; this PR ships the DAY-sweep exemption the task named.
