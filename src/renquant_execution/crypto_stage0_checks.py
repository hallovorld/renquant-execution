"""Crypto Stage-0 paper battery step checks (RFC D-C12).

Ownership: these are broker-adapter checks — they construct Alpaca SDK
request/enum objects (``LimitOrderRequest``, ``StopLimitOrderRequest``,
``OrderSide``, ``TimeInForce``, ``GetAssetsRequest``, ``CryptoBarsRequest``,
etc.) and drive the trading/data clients directly. That is broker-adapter
work, which ``renquant-orchestrator``'s own ``CLAUDE.md`` hard-boundaries
away from that repo ("Do not implement broker adapters here."). This
module is a straight MOVE (not a rewrite) of the 7 step-check functions
that originally lived in ``renquant-orchestrator``'s
``scripts/crypto_stage0_battery.py`` (PR #498) — logic and PASS/FAIL/ERROR
classification are unchanged; only the import boundary moved.

Two independent reasons this moved here, found and fixed proactively
(before Codex's review of orchestrator#498):

1. **CI was genuinely red.** Orchestrator's CI job
   (``renquant-orchestrator/.github/workflows/ci.yml``) never installs
   ``alpaca-py`` — its pip install line lists
   ``pytest numpy pandas scipy xgboost pyarrow pydantic cvxpy
   scikit-learn pandas_market_calendars`` only. The step functions'
   deferred (in-function) ``from alpaca...`` imports raised
   ``ModuleNotFoundError`` in that environment even with a
   ``MagicMock()`` client passed in, because the SDK enum/request TYPES
   themselves (not just the client) were unavailable — surfacing as
   ``ERROR`` status instead of the expected ``PASS``/``FAIL`` in CI's
   test run.
2. **Architecture boundary.** Same anti-pattern Codex flagged repeatedly
   this cycle (e.g. orchestrator#481's umbrella-script issue; the
   architecture-violation-registry audit) — orchestrator directly
   touching a broker SDK it should only consume through execution's
   public API. This repo (``renquant-execution``) already owns all
   Alpaca SDK interaction elsewhere (``alpaca_broker.py``,
   ``alpaca_broker_port.py``) and already declares ``alpaca-py`` as a
   real (optional-extra) dependency — see ``pyproject.toml``'s
   ``[project.optional-dependencies] alpaca`` group, installed in this
   repo's own CI job.

This exactly mirrors the ``software_stops_liveness.py`` precedent
(renquant-execution#29/#30, 2026-07-11/12): a broker/runtime-facing
checker moved out of orchestrator into this repo, with orchestrator kept
as a thin CLI/reporting consumer.

Ownership split (this module vs. the orchestrator script):
  * renquant-execution (HERE) — the 7 broker-facing STEP CHECKS
    themselves (account/asset/order/data queries against the Alpaca SDK)
    plus the client factories they need.
  * renquant-orchestrator — CLI argument parsing (``--paper``,
    ``--dry-run``), aggregating the 7 ``StepResult``s into a
    ``BatteryReport``, JSON report writing, and exit-code handling. Does
    not reimplement any broker-facing logic; imports the step functions
    from here.

Public surface (judgment call — flag for review): NOT re-exported from
``renquant_execution/__init__.py``. Two conventions coexist in this
package: (a) stable, semantically-specific names go through
``__init__.py``'s ``__all__`` (e.g. ``execution_payload``,
``normalize_order_intent``); (b) an "operational checker" module that
owns its own generic-sounding vocabulary is imported directly by its
submodule path instead — the precedent being ``software_stops_liveness``
itself, which orchestrator consumes without ever appearing in
``__all__``. This module's names (``StepResult``, ``CANARY_PAIRS``,
``get_trading_client``, ...) are generic enough that re-exporting them
bare from the flat package namespace risks a future collision with an
unrelated checker; that plus following the closer structural precedent
(another "battery of operational checks" module) is why direct-import
was chosen here:

    from renquant_execution.crypto_stage0_checks import (
        StepResult, step_crypto_status, step_pair_snapshot, ...,
        get_trading_client, get_crypto_data_client,
    )

If a reviewer prefers the ``__init__.py``-export convention instead
(matching ``execution_payload`` et al.), that is an easy, low-risk
follow-up — nothing about the module's internals depends on which
surface orchestrator uses.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

CANARY_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD"]
TEST_NOTIONAL_USD = 1.10


@dataclass
class StepResult:
    name: str
    status: str  # PASS, FAIL, SKIP, ERROR
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


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
    """Test GTC limit order acceptance on canary pairs."""
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
                results_per_pair[pair] = {
                    "accepted": True,
                    "order_id": str(order.id),
                    "tif": "GTC",
                }
            except Exception as e:
                results_per_pair[pair] = {"accepted": False, "error": str(e)}

        all_ok = all(r.get("accepted") for r in results_per_pair.values())
        return StepResult(
            "order_acceptance",
            "PASS" if all_ok else "FAIL",
            f"{sum(r.get('accepted', False) for r in results_per_pair.values())}/{len(results_per_pair)} pairs accepted GTC limit",
            {"results": results_per_pair},
        )
    except Exception as e:
        return StepResult("order_acceptance", "ERROR", str(e))


def step_stop_limit_acceptance(client, *, dry_run: bool) -> StepResult:
    """Test GTC stop-limit order acceptance on canary pairs."""
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
                results_per_pair[pair] = {
                    "accepted": True,
                    "order_id": str(order.id),
                }
            except Exception as e:
                results_per_pair[pair] = {"accepted": False, "error": str(e)}

        all_ok = all(r.get("accepted") for r in results_per_pair.values())
        return StepResult(
            "stop_limit_acceptance",
            "PASS" if all_ok else "FAIL",
            f"{sum(r.get('accepted', False) for r in results_per_pair.values())}/{len(results_per_pair)} pairs accepted GTC stop-limit",
            {"results": results_per_pair},
        )
    except Exception as e:
        return StepResult("stop_limit_acceptance", "ERROR", str(e))


def step_fee_from_fill(client, *, dry_run: bool) -> StepResult:
    """Place a small market buy to capture fee data from the fill receipt."""
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
        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                notional=TEST_NOTIONAL_USD,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
            )
        )
        time.sleep(3)
        filled = client.get_order_by_id(order.id)
        fee_data = {
            "order_id": str(filled.id),
            "symbol": symbol,
            "status": str(filled.status),
            "filled_avg_price": str(getattr(filled, "filled_avg_price", "N/A")),
            "filled_qty": str(getattr(filled, "filled_qty", "N/A")),
            "notional": str(getattr(filled, "notional", "N/A")),
        }
        status_str = str(filled.status).lower()
        if "fill" in status_str:
            return StepResult(
                "fee_from_fill",
                "PASS",
                f"Market buy filled; avg_price={fee_data['filled_avg_price']}",
                fee_data,
            )
        return StepResult(
            "fee_from_fill",
            "FAIL",
            f"Order status={filled.status}, expected filled",
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
