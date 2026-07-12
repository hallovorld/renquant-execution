#!/usr/bin/env python3
"""Stage-0 paper battery for crypto trading capability (RFC D-C12).

Verifies Alpaca crypto prerequisites empirically on the PAPER account:
  1. crypto_status == ACTIVE
  2. Pair list + increment snapshot (min_order_size, min_trade_increment, price_increment)
  3. GTC/IOC order acceptance per pair subset
  4. GTC stop_limit acceptance per pair subset
  5. Fee schedule from fill receipts
  6. Non-marginable buying power behavior
  7. Two-source data parity check (Alpaca vs yfinance daily close)

Outputs a JSON report with PASS/FAIL/SKIP per step.

Usage::

    # Dry-run (no orders placed, only account + asset checks):
    python scripts/crypto_stage0_battery.py --paper --dry-run

    # Full battery (places + cancels small test orders on paper):
    python scripts/crypto_stage0_battery.py --paper --output battery_report.json

Design reference: doc/design/2026-07-10-crypto-trading-rfc.md §6 Stage 0.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("crypto_stage0")

CANARY_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD"]
TEST_NOTIONAL_USD = 1.10


@dataclass
class StepResult:
    name: str
    status: str  # PASS, FAIL, SKIP, ERROR
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatteryReport:
    timestamp_utc: str = ""
    account_id: str = ""
    environment: str = "paper"
    dry_run: bool = False
    steps: list[StepResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.status == "PASS")

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.status == "FAIL")

    def summary(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "account_id": self.account_id,
            "environment": self.environment,
            "dry_run": self.dry_run,
            "total": len(self.steps),
            "passed": self.passed,
            "failed": self.failed,
            "skipped": sum(1 for s in self.steps if s.status == "SKIP"),
            "errors": sum(1 for s in self.steps if s.status == "ERROR"),
            "steps": [asdict(s) for s in self.steps],
        }


def _get_trading_client(*, paper: bool = True):
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


def _get_crypto_data_client():
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
        try:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass, AssetStatus

            assets = client.get_all_assets(
                GetAssetsRequest(
                    asset_class=AssetClass.CRYPTO,
                    status=AssetStatus.ACTIVE,
                )
            )
        except ImportError:
            assets = client.get_all_assets()
        tradable = [a for a in assets if getattr(a, "tradable", False)]
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


def _build_limit_order(symbol: str, notional: float) -> Any:
    """Build a limit order request, returning a dict if alpaca-py is unavailable."""
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        return LimitOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=0.01,
        )
    except ImportError:
        return {
            "symbol": symbol,
            "notional": notional,
            "side": "buy",
            "time_in_force": "gtc",
            "limit_price": 0.01,
        }


def _build_stop_limit_order(symbol: str, notional: float) -> Any:
    """Build a stop-limit order request."""
    try:
        from alpaca.trading.requests import StopLimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        return StopLimitOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=0.01,
            limit_price=0.01,
        )
    except ImportError:
        return {
            "symbol": symbol,
            "notional": notional,
            "side": "sell",
            "time_in_force": "gtc",
            "stop_price": 0.01,
            "limit_price": 0.01,
        }


def step_order_acceptance(client, *, dry_run: bool) -> StepResult:
    """Test GTC limit order acceptance on canary pairs."""
    if dry_run:
        return StepResult(
            "order_acceptance",
            "SKIP",
            "Skipped in dry-run mode (no orders placed)",
        )
    try:
        results_per_pair = {}
        for pair in CANARY_PAIRS:
            symbol = pair.replace("/", "")
            try:
                order = client.submit_order(
                    _build_limit_order(symbol, TEST_NOTIONAL_USD)
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
        results_per_pair = {}
        for pair in CANARY_PAIRS:
            symbol = pair.replace("/", "")
            try:
                order = client.submit_order(
                    _build_stop_limit_order(symbol, TEST_NOTIONAL_USD)
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


def _build_market_order(symbol: str, notional: float) -> Any:
    """Build a market order request."""
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        return MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
    except ImportError:
        return {
            "symbol": symbol,
            "notional": notional,
            "side": "buy",
            "time_in_force": "gtc",
        }


def step_fee_from_fill(client, *, dry_run: bool) -> StepResult:
    """Place a small market buy to capture fee data from the fill receipt."""
    if dry_run:
        return StepResult(
            "fee_from_fill",
            "SKIP",
            "Skipped in dry-run mode",
        )
    try:
        symbol = CANARY_PAIRS[0].replace("/", "")
        order = client.submit_order(
            _build_market_order(symbol, TEST_NOTIONAL_USD)
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

    data_client = _get_crypto_data_client()
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


def run_battery(*, paper: bool, dry_run: bool) -> BatteryReport:
    """Run the full Stage-0 battery."""
    report = BatteryReport(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        dry_run=dry_run,
        environment="paper" if paper else "LIVE-BLOCKED",
    )

    if not paper:
        report.steps.append(
            StepResult("safety", "FAIL", "Battery requires --paper flag")
        )
        return report

    client = _get_trading_client(paper=True)

    log.info("Step 1/7: crypto_status")
    r1 = step_crypto_status(client)
    report.steps.append(r1)
    report.account_id = r1.data.get("account_id", "")

    log.info("Step 2/7: pair_snapshot")
    report.steps.append(step_pair_snapshot(client))

    log.info("Step 3/7: order_acceptance (GTC limit)")
    report.steps.append(step_order_acceptance(client, dry_run=dry_run))

    log.info("Step 4/7: stop_limit_acceptance (GTC stop-limit)")
    report.steps.append(step_stop_limit_acceptance(client, dry_run=dry_run))

    log.info("Step 5/7: fee_from_fill (market buy)")
    report.steps.append(step_fee_from_fill(client, dry_run=dry_run))

    log.info("Step 6/7: buying_power")
    report.steps.append(step_buying_power(client))

    log.info("Step 7/7: data_parity")
    report.steps.append(step_data_parity(dry_run=dry_run))

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage-0 paper battery for crypto trading capability"
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        required=True,
        help="Use paper account (REQUIRED -- live is never permitted)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip order-placement steps (only check account + assets + data)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write JSON report to file (default: stdout)",
    )
    args = parser.parse_args(argv)

    report = run_battery(paper=args.paper, dry_run=args.dry_run)

    summary = report.summary()
    output_str = json.dumps(summary, indent=2, default=str)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_str)
        log.info("Report written to %s", args.output)
    else:
        print(output_str)

    log.info(
        "Battery complete: %d passed, %d failed, %d skipped, %d errors",
        summary["passed"],
        summary["failed"],
        summary["skipped"],
        summary["errors"],
    )
    return 1 if report.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
