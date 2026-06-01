"""Pre-open cancel gate for queued live market orders.

The gate runs shortly before the NYSE open. It measures the current ES futures
move from the prior NYSE cash close, normalizes it by recent SPY cash
close-to-open overnight volatility, and cancels pending live Alpaca market
orders when the move exceeds the configured sigma threshold.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

from .alerts import AlertEvent, post_ntfy_alert, stable_alert_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("renquant_execution.preopen_cancel_gate")


def _repo_root() -> Path:
    raw = os.environ.get("RENQUANT_REPO_ROOT")
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def _preopen_cancel_ledger() -> Path:
    raw = os.environ.get("RENQUANT_PREOPEN_CANCEL_LEDGER")
    if raw:
        return Path(raw).expanduser().resolve()
    root = os.environ.get("RENQUANT_REPO_ROOT")
    if root:
        return Path(root).expanduser().resolve() / "logs" / "alerts" / "preopen_cancel_ledger.jsonl"
    return Path.home() / ".renquant" / "preopen_cancel_ledger.jsonl"


def _load_env(path: Path | None = None) -> None:
    env_path = path or Path(os.environ.get("RENQUANT_ENV_FILE", _repo_root() / ".env"))
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _to_utc_timestamp(ts: Any):
    import pandas as pd

    out = pd.Timestamp(ts)
    if out.tzinfo is None:
        return out.tz_localize("UTC")
    return out.tz_convert("UTC")


def _series(df: Any, name: str):
    series = df[name]
    if hasattr(series, "squeeze"):
        series = series.squeeze()
    if getattr(series, "ndim", 1) > 1:
        series = series.iloc[:, 0]
    return series.astype(float).dropna()


def _previous_nyse_close(now_utc: Any):
    import pandas as pd
    import pandas_market_calendars as mcal

    now_utc = _to_utc_timestamp(now_utc)
    cal = mcal.get_calendar("NYSE")
    start = (now_utc - pd.Timedelta(days=14)).date()
    end = now_utc.date()
    sched = cal.schedule(start, end)
    if sched.empty:
        raise ValueError("NYSE calendar returned no recent sessions")
    closes = sched["market_close"].map(_to_utc_timestamp)
    prior = closes[closes < now_utc]
    if prior.empty:
        raise ValueError("no prior NYSE cash close before current time")
    return prior.iloc[-1]


def _is_nyse_session_date(day: Any = None) -> bool:
    import pandas as pd
    import pandas_market_calendars as mcal

    target = pd.Timestamp.now(tz="America/New_York").date() if day is None else day
    cal = mcal.get_calendar("NYSE")
    return not cal.schedule(target, target).empty


def _cash_overnight_sigma(
    yf: Any,
    *,
    sigma_symbol: str,
    fallback_symbol: str,
    lookback_days: int,
    sigma_window: int,
    now_utc: Any,
) -> tuple[float, int, str]:
    import pandas as pd

    end = _to_utc_timestamp(now_utc)
    start = end - pd.Timedelta(days=int(lookback_days * 1.7))

    def fetch(symbol: str):
        try:
            return yf.download(
                symbol,
                start=start.date(),
                end=(end + pd.Timedelta(days=1)).date(),
                progress=False,
                auto_adjust=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("yf.download(%s daily) failed: %s", symbol, exc)
            return None

    used = sigma_symbol
    df = fetch(sigma_symbol)
    if df is None or df.empty:
        log.warning("sigma symbol %s unavailable; falling back to %s", sigma_symbol, fallback_symbol)
        used = fallback_symbol
        df = fetch(fallback_symbol)
    if df is None or df.empty:
        raise ValueError(f"both {sigma_symbol} and {fallback_symbol} unavailable from yfinance")

    df = df.sort_index()
    opens = _series(df, "Open")
    closes = _series(df, "Close")
    prior_close = closes.shift(1)
    overnight = ((opens - prior_close) / prior_close).replace(
        [float("inf"), -float("inf")],
        pd.NA,
    )
    overnight = overnight.dropna()
    if len(overnight) < 10:
        raise ValueError(f"insufficient overnight sigma history ({len(overnight)} obs)")
    return float(overnight.tail(sigma_window).std()), int(len(overnight)), used


def _current_futures_move(
    yf: Any,
    *,
    symbol: str,
    now_utc: Any,
    max_stale_minutes: float,
) -> dict[str, Any]:
    import pandas as pd

    now_utc = _to_utc_timestamp(now_utc)
    prior_cash_close = _previous_nyse_close(now_utc)
    try:
        df = yf.download(
            symbol,
            period="10d",
            interval="5m",
            prepost=True,
            progress=False,
            auto_adjust=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"yf.download({symbol} 5m) failed: {exc}") from exc
    if df is None or df.empty:
        raise ValueError(f"{symbol} 5m history unavailable from yfinance")

    close = _series(df.sort_index(), "Close")
    if close.empty:
        raise ValueError(f"{symbol} 5m close series unavailable")
    close.index = pd.DatetimeIndex([_to_utc_timestamp(ts) for ts in close.index])
    close = close[close.index <= now_utc]
    if close.empty:
        raise ValueError(f"{symbol} 5m history has no bars before now")

    latest_ts = close.index[-1]
    stale_minutes = (now_utc - latest_ts).total_seconds() / 60.0
    if stale_minutes > max_stale_minutes:
        raise ValueError(f"{symbol} latest 5m bar is stale ({stale_minutes:.1f} min old)")

    ref = close[close.index <= prior_cash_close]
    if ref.empty:
        raise ValueError(f"{symbol} has no 5m bar before prior NYSE close")
    ref_ts = ref.index[-1]
    prior_price = float(ref.iloc[-1])
    latest = float(close.iloc[-1])
    if prior_price <= 0:
        raise ValueError(f"{symbol} invalid prior close proxy {prior_price}")
    return {
        "prior_close": prior_price,
        "latest": latest,
        "current_pct": (latest - prior_price) / prior_price,
        "prior_close_time": ref_ts.isoformat(),
        "latest_time": latest_ts.isoformat(),
        "stale_minutes": float(stale_minutes),
    }


def compute_overnight_severity(
    *,
    symbol: str = "ES=F",
    sigma_symbol: str = "SPY",
    fallback_sigma_symbol: str = "^GSPC",
    lookback_days: int = 90,
    sigma_window: int = 60,
    max_stale_minutes: float = 120.0,
    now: Any = None,
) -> dict[str, Any]:
    """Return current futures move, recent overnight sigma, and severity."""
    import pandas as pd
    import yfinance as yf

    now_utc = _to_utc_timestamp(now or pd.Timestamp.now(tz="UTC"))
    move = _current_futures_move(
        yf,
        symbol=symbol,
        now_utc=now_utc,
        max_stale_minutes=max_stale_minutes,
    )
    sigma_60d, n_obs, sigma_used = _cash_overnight_sigma(
        yf,
        sigma_symbol=sigma_symbol,
        fallback_symbol=fallback_sigma_symbol,
        lookback_days=lookback_days,
        sigma_window=sigma_window,
        now_utc=now_utc,
    )

    severity = 0.0
    current_pct = 0.0
    if sigma_60d > 1e-8:
        current_pct = float(move["current_pct"])
        severity = current_pct / sigma_60d
    return {
        "source": symbol,
        "sigma_source": sigma_used,
        "prior_close": float(move["prior_close"]),
        "latest": float(move["latest"]),
        "current_pct": float(current_pct),
        "sigma_60d": sigma_60d,
        "severity": float(severity),
        "n_obs": int(n_obs),
        "prior_close_time": move["prior_close_time"],
        "latest_time": move["latest_time"],
        "stale_minutes": move["stale_minutes"],
    }


def _load_order_api() -> tuple[Callable[..., Any], Callable[..., Any], Any]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    return TradingClient, GetOrdersRequest, QueryOrderStatus.OPEN


def _alpaca_credentials() -> tuple[str, str]:
    missing = [name for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY") if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY are required before pre-open cancels; "
            f"missing: {', '.join(missing)}"
        )
    return os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]


def _is_market_order(order: Any) -> bool:
    order_type = str(getattr(order, "order_type", "")).lower()
    normalized = order_type.rsplit(".", 1)[-1]
    return normalized in {"market", "market_on_open", "moo"}


def cancel_stale_market_orders(
    *,
    threshold_sigma: float,
    dry_run: bool,
    trading_client_factory: Callable[..., Any] | None = None,
    orders_request_factory: Callable[..., Any] | None = None,
    open_status: Any = None,
) -> dict[str, Any]:
    """Cancel pending market orders if overnight severity exceeds threshold."""
    try:
        metrics = compute_overnight_severity()
    except ValueError as exc:
        log.warning("PREOPEN-GATE: DATA-UNAVAILABLE - %s. No cancel.", exc)
        _post_data_unavailable_alert(str(exc))
        return {
            "metrics": {"error": str(exc)},
            "cancelled": [],
            "considered": 0,
            "action": "data-unavailable",
        }

    sev = metrics["severity"]
    pct = metrics["current_pct"]
    sigma = metrics["sigma_60d"]
    log.info(
        "PREOPEN-GATE: %s current-vs-cash-close %+.3f%% "
        "(%s sigma_60d=%.3f%%, severity=%+.2f sigma, threshold=+/-%.1f sigma, "
        "n_obs=%d, stale=%.1f min)",
        metrics["source"],
        pct * 100,
        metrics.get("sigma_source", "?"),
        sigma * 100,
        sev,
        threshold_sigma,
        metrics["n_obs"],
        metrics.get("stale_minutes", -1.0),
    )

    if abs(sev) < threshold_sigma:
        log.info("PREOPEN-GATE: PASS - severity within +/-%.1f sigma. No action.", threshold_sigma)
        return {"metrics": metrics, "cancelled": [], "considered": 0, "action": "pass"}

    if trading_client_factory is None or orders_request_factory is None or open_status is None:
        trading_client_factory, orders_request_factory, open_status = _load_order_api()
    api_key, secret_key = _alpaca_credentials()

    client = trading_client_factory(
        api_key=api_key,
        secret_key=secret_key,
        paper=False,
    )
    req = orders_request_factory(status=open_status, limit=200)
    orders = client.get_orders(filter=req)
    pending_market = [order for order in orders if _is_market_order(order)]
    log.warning(
        "PREOPEN-GATE: TRIGGERED - severity=%+.2f sigma >= +/-%.1f sigma; "
        "evaluating %d pending market order(s) for cancel.",
        sev,
        threshold_sigma,
        len(pending_market),
    )

    cancelled: list[str] = []
    cancelled_rows: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for order in pending_market:
        log.warning(
            "  -> CANCEL %s %s qty=%s (id=%s, intent=%s)",
            getattr(order, "side", ""),
            getattr(order, "symbol", ""),
            getattr(order, "qty", ""),
            getattr(order, "id", ""),
            getattr(order, "position_intent", "n/a"),
        )
        if dry_run:
            continue
        try:
            client.cancel_order_by_id(order.id)
            cancelled.append(str(order.symbol))
            cancelled_rows.append({
                "date": time.strftime("%Y-%m-%d"),
                "broker": "alpaca",
                "order_id": str(order.id),
                "symbol": str(order.symbol),
                "side": str(getattr(order, "side", "")),
                "qty": str(getattr(order, "qty", "")),
            })
        except Exception as exc:  # noqa: BLE001
            log.error("  ! cancel failed for %s: %s", getattr(order, "symbol", ""), exc)
            failed.append({
                "symbol": str(getattr(order, "symbol", "")),
                "order_id": str(getattr(order, "id", "")),
                "error": str(exc),
            })

    if cancelled:
        _append_preopen_cancel_ledger(cancelled_rows)
        _post_cancel_alert(
            metrics=metrics,
            pct=pct,
            severity=sev,
            threshold_sigma=threshold_sigma,
            taxonomy="PREOPEN_CANCEL_PARTIAL" if failed else "PREOPEN_CANCEL",
            title="RenQuant 104 PREOPEN CANCEL PARTIAL" if failed else "RenQuant 104 PREOPEN CANCEL",
            body_suffix=(
                f"cancelled {len(cancelled)} pending order(s): {','.join(cancelled)}"
                + (
                    f"; FAILED {len(failed)}: {','.join(f['symbol'] for f in failed)}"
                    if failed else ""
                )
            ),
            key_parts=[
                sorted(str(row["order_id"]) for row in cancelled_rows),
                sorted(cancelled),
                sorted(str(item["order_id"]) for item in failed),
            ],
        )
    elif failed:
        _post_cancel_alert(
            metrics=metrics,
            pct=pct,
            severity=sev,
            threshold_sigma=threshold_sigma,
            taxonomy="PREOPEN_CANCEL_FAILED",
            title="RenQuant 104 PREOPEN CANCEL FAILED",
            body_suffix=(
                f"FAILED to cancel {len(failed)} pending order(s): "
                f"{','.join(item['symbol'] for item in failed)}"
            ),
            key_parts=[
                sorted(str(item["order_id"]) for item in failed),
                sorted(str(item["symbol"]) for item in failed),
            ],
        )

    if dry_run:
        action = "dry-run"
    elif failed and not cancelled:
        action = "cancel_failed"
    elif failed:
        action = "partial_cancelled"
    elif cancelled:
        action = "cancelled"
    else:
        action = "triggered_no_market_orders"
    return {
        "metrics": metrics,
        "cancelled": cancelled,
        "failed": failed,
        "considered": len(pending_market),
        "action": action,
    }


def _post_cancel_alert(
    *,
    metrics: dict[str, Any],
    pct: float,
    severity: float,
    threshold_sigma: float,
    taxonomy: str,
    title: str,
    body_suffix: str,
    key_parts: list[object],
) -> None:
    msg = (
        f"{taxonomy}: {metrics['source']} current-vs-cash-close "
        f"{pct * 100:+.2f}% ({severity:+.1f} sigma >= +/-{threshold_sigma:.1f} sigma) "
        f"{body_suffix}"
    )
    topic = os.environ.get("RENQUANT_NTFY_TOPIC", "renquant")
    post_ntfy_alert(
        f"https://ntfy.sh/{topic}",
        AlertEvent(
            taxonomy=taxonomy,
            title=title,
            body=msg,
            key=stable_alert_key(taxonomy.lower(), time.strftime("%Y-%m-%d"), *key_parts),
            priority="high",
            cooldown_seconds=24 * 60 * 60,
        ),
        logger=log,
    )


def _post_data_unavailable_alert(error: str) -> None:
    taxonomy = "PREOPEN_GATE_DEGRADED"
    topic = os.environ.get("RENQUANT_NTFY_TOPIC", "renquant")
    post_ntfy_alert(
        f"https://ntfy.sh/{topic}",
        AlertEvent(
            taxonomy=taxonomy,
            title="RenQuant 104 PREOPEN GATE DEGRADED",
            body=(
                f"{taxonomy}: data unavailable; gate ran blind and did not cancel orders. "
                f"error={error}"
            ),
            key=stable_alert_key(taxonomy.lower(), time.strftime("%Y-%m-%d")),
            priority="default",
            cooldown_seconds=6 * 60 * 60,
        ),
        logger=log,
    )


def _append_preopen_cancel_ledger(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path = _preopen_cancel_ledger()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pre-open cancel gate for queued post-close market orders",
    )
    parser.add_argument(
        "--severity-threshold-sigma",
        type=float,
        default=2.0,
        help=(
            "Cancel pending orders if absolute overnight sigma-normalized return "
            "is at least this value. Default: 2.0."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute severity and list cancelables, but do not submit cancels.",
    )
    parser.add_argument(
        "--ignore-calendar",
        action="store_true",
        help="Run even when today is not an NYSE trading session.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_parser().parse_args(argv)
    if not args.ignore_calendar and not _is_nyse_session_date():
        log.info("NYSE closed today - skipping pre-open cancel gate.")
        return 0
    result = cancel_stale_market_orders(
        threshold_sigma=args.severity_threshold_sigma,
        dry_run=args.dry_run,
    )
    log.info(
        "Done. Action=%s, cancelled=%s, considered=%d.",
        result["action"],
        result["cancelled"],
        result["considered"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
