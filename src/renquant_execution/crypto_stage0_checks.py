"""Crypto Stage-0 paper battery step checks (RFC D-C12).

Ownership: broker-adapter checks that construct Alpaca SDK request/enum
objects and drive the trading/data clients directly.

Safety invariants (codex review round 2):
  * ``run_battery()`` hard-rejects ``paper=False`` -- the battery NEVER
    touches a live account.
  * ``transactional=False`` (the default) runs only passive/read-only
    checks (account status, pair snapshot, buying power, data parity).
    Transactional paper probes (order acceptance, stop-limit acceptance,
    fee-from-fill round-trip) require ``transactional=True``.
  * Every order submission (limit, stop-limit, market) polls to terminal
    state before returning -- no fire-and-forget.
  * ``step_fee_from_fill`` executes a bounded-notional BUY, polls to
    fill, submits a compensating SELL for the filled qty, polls that to
    fill, and audits residual position.  Cleanup failure is surfaced as
    a distinct Tier-1 result.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

CANARY_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD"]
TEST_NOTIONAL_USD = 1.10

_TERMINAL_STATUS_FRAGMENTS = ("fill", "cancel", "expire", "reject")
_POLL_MAX_ATTEMPTS = 10
_POLL_SLEEP_SEC = 0.5


@dataclass
class StepResult:
    name: str
    status: str  # PASS, FAIL, SKIP, ERROR
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _poll_order_terminal(
    client,
    order_id,
    *,
    max_attempts: int = _POLL_MAX_ATTEMPTS,
    sleep_sec: float = _POLL_SLEEP_SEC,
) -> tuple[bool, Any]:
    """Poll until an order reaches a terminal state.

    Returns ``(reached_terminal, order_object)``.  Terminal means the
    stringified status contains one of: fill, cancel, expire, reject.
    """
    order = None
    for _ in range(max_attempts):
        order = client.get_order_by_id(order_id)
        status_str = str(order.status).lower()
        if any(frag in status_str for frag in _TERMINAL_STATUS_FRAGMENTS):
            return True, order
        time.sleep(sleep_sec)
    return False, order


def get_trading_client(*, paper: bool = True):
    """Create Alpaca TradingClient from env vars."""
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        raise SystemExit("alpaca-py not installed; pip install alpaca-py")

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise SystemExit("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")

    return TradingClient(key, secret, paper=paper)


def get_crypto_data_client():
    """Create Alpaca CryptoHistoricalDataClient."""
    try:
        from alpaca.data.historical.crypto import CryptoHistoricalDataClient
    except ImportError:
        return None

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    return CryptoHistoricalDataClient(key, secret)


def step_crypto_status(client) -> StepResult:
    """Verify crypto_status == ACTIVE on the account."""
    try:
        account = client.get_account()
        status = getattr(account, "crypto_status", None)
        if status is None:
            return StepResult(
                "crypto_status",
                "FAIL",
                "Account object has no crypto_status attribute",
                {"account_id": account.id},
            )
        status_str = str(status).upper()
        if status_str == "ACTIVE" or status_str == "ACCOUNTSTATUS.ACTIVE":
            return StepResult(
                "crypto_status",
                "PASS",
                f"crypto_status={status_str}",
                {"account_id": account.id, "crypto_status": status_str},
            )
        return StepResult(
            "crypto_status",
            "FAIL",
            f"crypto_status={status_str} (expected ACTIVE)",
            {"account_id": account.id, "crypto_status": status_str},
        )
    except Exception as e:
        return StepResult("crypto_status", "ERROR", str(e))


def step_pair_snapshot(client) -> StepResult:
    """Snapshot all tradable crypto pairs and their increments."""
    try:
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus

        assets = client.get_all_assets(
            GetAssetsRequest(
                asset_class=AssetClass.CRYPTO,
                status=AssetStatus.ACTIVE,
            )
        )
        tradable = [a for a in assets if a.tradable]
        pairs = {}
        for a in tradable:
            pairs[a.symbol] = {
                "name": a.name,
                "min_order_size": str(getattr(a, "min_order_size", "N/A")),
                "min_trade_increment": str(getattr(a, "min_trade_increment", "N/A")),
                "price_increment": str(getattr(a, "price_increment", "N/A")),
                "fractionable": getattr(a, "fractionable", None),
                "marginable": getattr(a, "marginable", None),
                "shortable": getattr(a, "shortable", None),
            }
        if not pairs:
            return StepResult(
                "pair_snapshot", "FAIL", "No tradable crypto pairs found"
            )
        return StepResult(
            "pair_snapshot",
            "PASS",
            f"{len(pairs)} tradable crypto pairs",
            {"pair_count": len(pairs), "pairs": pairs},
        )
    except Exception as e:
        return StepResult("pair_snapshot", "ERROR", str(e))


def step_order_acceptance(client, *, dry_run: bool) -> StepResult:
    """Test GTC limit order acceptance on canary pairs.

    Submits a far-from-market limit BUY, cancels it, and polls until the
    cancel reaches a terminal state before reporting success.
    """
    if dry_run:
        return StepResult(
            "order_acceptance",
            "SKIP",
            "Skipped in dry-run mode (no orders placed)",
        )
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        results_per_pair = {}
        for pair in CANARY_PAIRS:
            symbol = pair.replace("/", "")
            try:
                order = client.submit_order(
                    LimitOrderRequest(
                        symbol=symbol,
                        notional=TEST_NOTIONAL_USD,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.GTC,
                        limit_price=0.01,
                    )
                )
                client.cancel_order_by_id(order.id)
                terminal, final = _poll_order_terminal(client, order.id)
                results_per_pair[pair] = {
                    "accepted": True,
                    "cancel_confirmed": terminal,
                    "order_id": str(order.id),
                    "final_status": str(final.status),
                    "tif": "GTC",
                }
            except Exception as e:
                results_per_pair[pair] = {"accepted": False, "error": str(e)}

        all_accepted = all(r.get("accepted") for r in results_per_pair.values())
        all_cancelled = all(
            r.get("cancel_confirmed", True) for r in results_per_pair.values()
        )
        accepted_count = sum(
            r.get("accepted", False) for r in results_per_pair.values()
        )
        detail = (
            f"{accepted_count}/{len(results_per_pair)} pairs accepted GTC limit"
        )
        if not all_cancelled:
            detail += "; some cancels not confirmed terminal"
        return StepResult(
            "order_acceptance",
            "PASS" if (all_accepted and all_cancelled) else "FAIL",
            detail,
            {"results": results_per_pair},
        )
    except Exception as e:
        return StepResult("order_acceptance", "ERROR", str(e))


def step_stop_limit_acceptance(client, *, dry_run: bool) -> StepResult:
    """Test GTC stop-limit order acceptance on canary pairs.

    Submits a far-from-market stop-limit SELL, cancels it, and polls
    until the cancel reaches a terminal state before reporting success.
    """
    if dry_run:
        return StepResult(
            "stop_limit_acceptance",
            "SKIP",
            "Skipped in dry-run mode",
        )
    try:
        from alpaca.trading.requests import StopLimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        results_per_pair = {}
        for pair in CANARY_PAIRS:
            symbol = pair.replace("/", "")
            try:
                order = client.submit_order(
                    StopLimitOrderRequest(
                        symbol=symbol,
                        notional=TEST_NOTIONAL_USD,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        stop_price=0.01,
                        limit_price=0.01,
                    )
                )
                client.cancel_order_by_id(order.id)
                terminal, final = _poll_order_terminal(client, order.id)
                results_per_pair[pair] = {
                    "accepted": True,
                    "cancel_confirmed": terminal,
                    "order_id": str(order.id),
                    "final_status": str(final.status),
                }
            except Exception as e:
                results_per_pair[pair] = {"accepted": False, "error": str(e)}

        all_accepted = all(r.get("accepted") for r in results_per_pair.values())
        all_cancelled = all(
            r.get("cancel_confirmed", True) for r in results_per_pair.values()
        )
        accepted_count = sum(
            r.get("accepted", False) for r in results_per_pair.values()
        )
        detail = (
            f"{accepted_count}/{len(results_per_pair)} pairs accepted "
            "GTC stop-limit"
        )
        if not all_cancelled:
            detail += "; some cancels not confirmed terminal"
        return StepResult(
            "stop_limit_acceptance",
            "PASS" if (all_accepted and all_cancelled) else "FAIL",
            detail,
            {"results": results_per_pair},
        )
    except Exception as e:
        return StepResult("stop_limit_acceptance", "ERROR", str(e))


def step_fee_from_fill(client, *, dry_run: bool) -> StepResult:
    """Place a bounded-notional market BUY, poll to fill, submit a
    compensating SELL for the filled qty, poll that to fill, and audit
    the residual position.

    A cleanup failure (compensating sell does not fill or residual
    position remains) is surfaced as a Tier-1 FAIL with
    ``cleanup_failure=True`` in the result data.
    """
    if dry_run:
        return StepResult(
            "fee_from_fill",
            "SKIP",
            "Skipped in dry-run mode",
        )
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        symbol = CANARY_PAIRS[0].replace("/", "")

        # -- 1. Submit bounded-notional BUY ------------------------------------
        buy_order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                notional=TEST_NOTIONAL_USD,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
            )
        )

        # -- 2. Poll until BUY fills ------------------------------------------
        buy_terminal, buy_filled = _poll_order_terminal(client, buy_order.id)
        buy_status_str = str(buy_filled.status).lower()

        if not buy_terminal or "fill" not in buy_status_str:
            return StepResult(
                "fee_from_fill",
                "FAIL",
                f"BUY did not fill; status={buy_filled.status}",
                {
                    "buy_order_id": str(buy_order.id),
                    "buy_status": str(buy_filled.status),
                },
            )

        fee_data: dict[str, Any] = {
            "buy_order_id": str(buy_filled.id),
            "symbol": symbol,
            "buy_status": str(buy_filled.status),
            "filled_avg_price": str(
                getattr(buy_filled, "filled_avg_price", "N/A")
            ),
            "filled_qty": str(getattr(buy_filled, "filled_qty", "N/A")),
            "notional": str(getattr(buy_filled, "notional", "N/A")),
        }

        # -- 3. Submit compensating SELL for the filled qty --------------------
        filled_qty = getattr(buy_filled, "filled_qty", None)
        if not filled_qty:
            fee_data["cleanup_failure"] = True
            return StepResult(
                "fee_from_fill",
                "FAIL",
                "BUY filled but filled_qty unavailable for compensating sell",
                fee_data,
            )

        sell_order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=str(filled_qty),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )
        )

        # -- 4. Poll until SELL fills ------------------------------------------
        sell_terminal, sell_filled = _poll_order_terminal(client, sell_order.id)
        sell_status_str = str(sell_filled.status).lower()
        fee_data["sell_order_id"] = str(sell_filled.id)
        fee_data["sell_status"] = str(sell_filled.status)

        if not sell_terminal or "fill" not in sell_status_str:
            fee_data["cleanup_failure"] = True
            return StepResult(
                "fee_from_fill",
                "FAIL",
                f"Compensating SELL did not fill; status={sell_filled.status}; "
                "residual position may remain",
                fee_data,
            )

        # -- 5. Residual-position audit ----------------------------------------
        try:
            positions = client.get_all_positions()
            residual = [
                p
                for p in positions
                if getattr(p, "symbol", "") == symbol
            ]
            if residual:
                residual_qty = str(getattr(residual[0], "qty", "unknown"))
                fee_data["residual_qty"] = residual_qty
                fee_data["cleanup_failure"] = True
                return StepResult(
                    "fee_from_fill",
                    "FAIL",
                    f"Round-trip complete but residual position remains: "
                    f"qty={residual_qty}",
                    fee_data,
                )
        except Exception as e:
            fee_data["residual_check_error"] = str(e)

        return StepResult(
            "fee_from_fill",
            "PASS",
            f"Round-trip complete; avg_price={fee_data['filled_avg_price']}",
            fee_data,
        )
    except Exception as e:
        return StepResult("fee_from_fill", "ERROR", str(e))


def step_buying_power(client) -> StepResult:
    """Check non-marginable buying power behavior for crypto."""
    try:
        account = client.get_account()
        bp_data = {
            "buying_power": str(account.buying_power),
            "cash": str(account.cash),
            "non_marginable_buying_power": str(
                getattr(account, "non_marginable_buying_power", "N/A")
            ),
            "crypto_buying_power": str(
                getattr(account, "crypto_buying_power", "N/A")
            ),
        }
        return StepResult(
            "buying_power",
            "PASS",
            f"cash={account.cash}, crypto_bp={bp_data['crypto_buying_power']}",
            bp_data,
        )
    except Exception as e:
        return StepResult("buying_power", "ERROR", str(e))


def step_data_parity(*, dry_run: bool) -> StepResult:
    """Two-source daily close parity: Alpaca crypto bars vs yfinance."""
    if dry_run:
        return StepResult(
            "data_parity",
            "SKIP",
            "Skipped in dry-run mode",
        )

    data_client = get_crypto_data_client()
    if data_client is None:
        return StepResult(
            "data_parity",
            "SKIP",
            "CryptoHistoricalDataClient not available",
        )

    try:
        import yfinance as yf
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta

        end = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start = end - timedelta(days=7)

        results = {}
        for pair in CANARY_PAIRS[:2]:
            slug = pair.replace("/", "")
            yf_ticker = pair.split("/")[0] + "-USD"
            try:
                bars = data_client.get_crypto_bars(
                    CryptoBarsRequest(
                        symbol_or_symbols=slug,
                        timeframe=TimeFrame.Day,
                        start=start,
                        end=end,
                    )
                )
                alpaca_closes = {}
                if slug in bars:
                    for bar in bars[slug]:
                        dt = bar.timestamp.strftime("%Y-%m-%d")
                        alpaca_closes[dt] = float(bar.close)

                yf_data = yf.download(yf_ticker, start=start, end=end, progress=False)
                yf_closes = {}
                if not yf_data.empty:
                    for idx, row in yf_data.iterrows():
                        dt = idx.strftime("%Y-%m-%d")
                        close_val = row["Close"]
                        if hasattr(close_val, "item"):
                            close_val = close_val.item()
                        yf_closes[dt] = float(close_val)

                common_dates = sorted(set(alpaca_closes) & set(yf_closes))
                if not common_dates:
                    results[pair] = {"matched": False, "reason": "no common dates"}
                    continue

                max_diff_pct = 0.0
                for d in common_dates:
                    diff = abs(alpaca_closes[d] - yf_closes[d]) / yf_closes[d] * 100
                    max_diff_pct = max(max_diff_pct, diff)

                results[pair] = {
                    "matched": max_diff_pct < 2.0,
                    "common_dates": len(common_dates),
                    "max_diff_pct": round(max_diff_pct, 4),
                }
            except Exception as e:
                results[pair] = {"matched": False, "error": str(e)}

        all_matched = all(r.get("matched") for r in results.values())
        return StepResult(
            "data_parity",
            "PASS" if all_matched else "FAIL",
            f"{'All' if all_matched else 'Some'} pairs within 2% parity",
            {"results": results},
        )
    except ImportError:
        return StepResult("data_parity", "SKIP", "yfinance not installed")
    except Exception as e:
        return StepResult("data_parity", "ERROR", str(e))


# ── High-level battery entry point ──────────────────────────────────────────


def run_battery(
    *,
    paper: bool = True,
    dry_run: bool = False,
    transactional: bool = False,
) -> list[StepResult]:
    """Run the Stage-0 crypto battery.

    Parameters
    ----------
    paper : bool
        Must be ``True``.  Passing ``paper=False`` raises ``ValueError``
        -- the battery NEVER touches a live account.
    dry_run : bool
        When ``True`` the transactional steps return SKIP instead of
        placing orders (same as before).
    transactional : bool
        ``False`` (default) runs only passive/read-only checks.
        ``True`` additionally runs the three paper-order probes
        (order acceptance, stop-limit acceptance, fee-from-fill
        round-trip).

    Returns
    -------
    list[StepResult]
        One result per step, in deterministic order.
    """
    if not paper:
        raise ValueError(
            "run_battery() only supports paper=True; "
            "live trading is not permitted for battery checks"
        )

    client = get_trading_client(paper=True)

    results: list[StepResult] = []

    # -- Passive checks (always run) -------------------------------------------
    results.append(step_crypto_status(client))
    results.append(step_pair_snapshot(client))
    results.append(step_buying_power(client))
    results.append(step_data_parity(dry_run=dry_run))

    # -- Transactional paper probes (opt-in) -----------------------------------
    if transactional:
        results.append(step_order_acceptance(client, dry_run=dry_run))
        results.append(step_stop_limit_acceptance(client, dry_run=dry_run))
        results.append(step_fee_from_fill(client, dry_run=dry_run))
    else:
        results.append(
            StepResult(
                "order_acceptance",
                "SKIP",
                "Skipped (transactional=False)",
            )
        )
        results.append(
            StepResult(
                "stop_limit_acceptance",
                "SKIP",
                "Skipped (transactional=False)",
            )
        )
        results.append(
            StepResult(
                "fee_from_fill",
                "SKIP",
                "Skipped (transactional=False)",
            )
        )

    return results
