"""Tests for renquant_execution.preopen_cancel_gate."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from renquant_execution import preopen_cancel_gate as gate


def _mock_order(
    symbol: str = "X",
    order_type: str = "OrderType.MARKET",
    side: str = "buy",
    qty: str = "1",
    id: str = "o-1",
):
    return SimpleNamespace(
        symbol=symbol,
        order_type=order_type,
        side=side,
        qty=qty,
        id=id,
        position_intent="buy_to_open",
    )


def _client_factory(client):
    return lambda **_kwargs: client


def _orders_request(**kwargs):
    return kwargs


def test_severity_below_threshold_no_cancel():
    metrics = {
        "source": "ES=F",
        "prior_close": 5000.0,
        "latest": 5005.0,
        "current_pct": 0.001,
        "sigma_60d": 0.005,
        "severity": 0.2,
        "n_obs": 100,
        "stale_minutes": 1.0,
    }
    client = MagicMock()
    with patch.object(gate, "compute_overnight_severity", return_value=metrics):
        result = gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=False,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request,
            open_status="open",
        )
    assert result["action"] == "pass"
    assert result["cancelled"] == []
    client.get_orders.assert_not_called()


def test_data_unavailable_no_cancel():
    client = MagicMock()
    with patch.object(gate, "compute_overnight_severity", side_effect=ValueError("stale ES=F data")), \
            patch.object(gate, "post_ntfy_alert") as ntfy:
        result = gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=False,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request,
            open_status="open",
        )
    assert result["action"] == "data-unavailable"
    assert result["cancelled"] == []
    client.get_orders.assert_not_called()
    ntfy.assert_called_once()
    event = ntfy.call_args.args[1]
    assert event.taxonomy == "PREOPEN_GATE_DEGRADED"
    assert event.cooldown_seconds == 6 * 60 * 60
    assert "gate ran blind" in event.body


@pytest.mark.parametrize(
    ("order_type", "expected"),
    [
        ("OrderType.MARKET", True),
        ("market", True),
        ("OrderType.MARKET_ON_OPEN", True),
        ("market_on_open", True),
        ("moo", True),
        ("OrderType.LIMIT", False),
        ("stop", False),
    ],
)
def test_market_order_matcher_includes_market_on_open(order_type, expected):
    assert gate._is_market_order(_mock_order(order_type=order_type)) is expected


def test_missing_alpaca_credentials_raise_clear_error(monkeypatch):
    metrics = {
        "source": "ES=F",
        "prior_close": 5000.0,
        "latest": 4700.0,
        "current_pct": -0.06,
        "sigma_60d": 0.005,
        "severity": -12.0,
        "n_obs": 100,
        "stale_minutes": 1.0,
    }
    client = MagicMock()
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with patch.object(gate, "compute_overnight_severity", return_value=metrics), \
            pytest.raises(RuntimeError, match="ALPACA_API_KEY.*ALPACA_SECRET_KEY"):
        gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=False,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request,
            open_status="open",
        )
    client.get_orders.assert_not_called()


def test_preopen_cancel_ledger_defaults_to_home_when_repo_root_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("RENQUANT_PREOPEN_CANCEL_LEDGER", raising=False)
    monkeypatch.delenv("RENQUANT_REPO_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert gate._preopen_cancel_ledger() == tmp_path / ".renquant" / "preopen_cancel_ledger.jsonl"


def test_severity_above_threshold_cancels_market_orders(tmp_path, monkeypatch):
    metrics = {
        "source": "ES=F",
        "prior_close": 5000.0,
        "latest": 4700.0,
        "current_pct": -0.06,
        "sigma_60d": 0.005,
        "severity": -12.0,
        "n_obs": 100,
        "stale_minutes": 1.0,
    }
    client = MagicMock()
    # severity is a gap-DOWN (-12 sigma) -> the adverse side is SELLs.
    client.get_orders.return_value = [
        _mock_order(symbol="META", side="sell", id="o-1"),
        _mock_order(symbol="TXN", side="sell", id="o-2"),
        _mock_order(symbol="AAPL", order_type="OrderType.LIMIT", side="sell", id="o-3"),
    ]
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("RENQUANT_PREOPEN_CANCEL_LEDGER", str(tmp_path / "ledger.jsonl"))

    with patch.object(gate, "compute_overnight_severity", return_value=metrics), \
            patch.object(gate, "post_ntfy_alert") as ntfy:
        result = gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=False,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request,
            open_status="open",
        )

    assert result["action"] == "cancelled"
    assert sorted(result["cancelled"]) == ["META", "TXN"]
    assert sorted(call.args[0] for call in client.cancel_order_by_id.call_args_list) == ["o-1", "o-2"]
    ntfy.assert_called_once()
    event = ntfy.call_args.args[1]
    assert event.taxonomy == "PREOPEN_CANCEL"


def _run_gate(client, metrics, monkeypatch, tmp_path, **kw):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("RENQUANT_PREOPEN_CANCEL_LEDGER", str(tmp_path / "ledger.jsonl"))
    with patch.object(gate, "compute_overnight_severity", return_value=metrics), \
            patch.object(gate, "post_ntfy_alert"):
        return gate.cancel_stale_market_orders(
            threshold_sigma=2.0, dry_run=False,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request, open_status="open", **kw)


def _metrics(severity):
    return {"source": "ES=F", "prior_close": 5000.0, "latest": 4900.0, "current_pct": severity * 0.005,
            "sigma_60d": 0.005, "severity": severity, "n_obs": 100, "stale_minutes": 1.0}


def test_gap_down_keeps_buys_cancels_sells(tmp_path, monkeypatch):
    """The 2026-06-23 incident fix: a severe gap-DOWN must NOT cancel buy orders
    (a cheaper entry); it cancels only the sells."""
    client = MagicMock()
    client.get_orders.return_value = [
        _mock_order(symbol="CRWD", side="buy", id="b-1"),
        _mock_order(symbol="AVGO", side="buy", id="b-2"),
        _mock_order(symbol="MU", side="sell", id="s-1"),
    ]
    result = _run_gate(client, _metrics(-2.52), monkeypatch, tmp_path)
    assert result["cancelled"] == ["MU"]
    assert result["kept"] == 2
    assert [c.args[0] for c in client.cancel_order_by_id.call_args_list] == ["s-1"]


def test_gap_up_cancels_buys_keeps_sells(tmp_path, monkeypatch):
    client = MagicMock()
    client.get_orders.return_value = [
        _mock_order(symbol="CRWD", side="buy", id="b-1"),
        _mock_order(symbol="MU", side="sell", id="s-1"),
    ]
    result = _run_gate(client, _metrics(+2.52), monkeypatch, tmp_path)
    assert result["cancelled"] == ["CRWD"]
    assert result["kept"] == 1
    assert [c.args[0] for c in client.cancel_order_by_id.call_args_list] == ["b-1"]


def test_gap_down_all_buys_keeps_all(tmp_path, monkeypatch):
    client = MagicMock()
    client.get_orders.return_value = [_mock_order(symbol="CRWD", side="buy", id="b-1")]
    result = _run_gate(client, _metrics(-2.52), monkeypatch, tmp_path)
    assert result["cancelled"] == []
    assert result["action"] == "triggered_all_favorable_kept"
    client.cancel_order_by_id.assert_not_called()


def test_cancel_both_sides_flag_cancels_everything(tmp_path, monkeypatch):
    client = MagicMock()
    client.get_orders.return_value = [
        _mock_order(symbol="CRWD", side="buy", id="b-1"),
        _mock_order(symbol="MU", side="sell", id="s-1"),
    ]
    result = _run_gate(client, _metrics(-2.52), monkeypatch, tmp_path, cancel_both_sides=True)
    assert sorted(result["cancelled"]) == ["CRWD", "MU"]


def test_dry_run_does_not_actually_cancel(monkeypatch):
    metrics = {
        "source": "ES=F",
        "prior_close": 5000.0,
        "latest": 4700.0,
        "current_pct": -0.06,
        "sigma_60d": 0.005,
        "severity": -12.0,
        "n_obs": 100,
        "stale_minutes": 1.0,
    }
    client = MagicMock()
    client.get_orders.return_value = [_mock_order(symbol="META", id="o-1")]
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    with patch.object(gate, "compute_overnight_severity", return_value=metrics), \
            patch.object(gate, "post_ntfy_alert") as ntfy:
        result = gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=True,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request,
            open_status="open",
        )

    assert result["action"] == "dry-run"
    assert result["cancelled"] == []
    assert result["considered"] == 1
    client.cancel_order_by_id.assert_not_called()
    ntfy.assert_not_called()


def test_cancel_failure_does_not_abort_batch(tmp_path, monkeypatch):
    metrics = {
        "source": "ES=F",
        "prior_close": 5000.0,
        "latest": 4700.0,
        "current_pct": -0.06,
        "sigma_60d": 0.005,
        "severity": -12.0,
        "n_obs": 100,
        "stale_minutes": 1.0,
    }
    client = MagicMock()
    client.get_orders.return_value = [
        _mock_order(symbol="META", side="sell", id="o-1"),
        _mock_order(symbol="TXN", side="sell", id="o-2"),
    ]
    client.cancel_order_by_id.side_effect = [Exception("alpaca timeout"), None]
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("RENQUANT_PREOPEN_CANCEL_LEDGER", str(tmp_path / "ledger.jsonl"))

    with patch.object(gate, "compute_overnight_severity", return_value=metrics), \
            patch.object(gate, "post_ntfy_alert") as ntfy:
        result = gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=False,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request,
            open_status="open",
        )

    assert result["action"] == "partial_cancelled"
    assert result["cancelled"] == ["TXN"]
    assert result["failed"][0]["symbol"] == "META"
    event = ntfy.call_args.args[1]
    assert event.taxonomy == "PREOPEN_CANCEL_PARTIAL"
    assert "FAILED 1" in event.body


def test_all_cancel_failures_send_failure_alert(monkeypatch):
    metrics = {
        "source": "ES=F",
        "prior_close": 5000.0,
        "latest": 4700.0,
        "current_pct": -0.06,
        "sigma_60d": 0.005,
        "severity": -12.0,
        "n_obs": 100,
        "stale_minutes": 1.0,
    }
    client = MagicMock()
    client.get_orders.return_value = [
        _mock_order(symbol="META", side="sell", id="o-1"),
        _mock_order(symbol="TXN", side="sell", id="o-2"),
    ]
    client.cancel_order_by_id.side_effect = Exception("alpaca timeout")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    with patch.object(gate, "compute_overnight_severity", return_value=metrics), \
            patch.object(gate, "post_ntfy_alert") as ntfy:
        result = gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=False,
            trading_client_factory=_client_factory(client),
            orders_request_factory=_orders_request,
            open_status="open",
        )

    assert result["action"] == "cancel_failed"
    assert result["cancelled"] == []
    assert [failure["symbol"] for failure in result["failed"]] == ["META", "TXN"]
    event = ntfy.call_args.args[1]
    assert event.taxonomy == "PREOPEN_CANCEL_FAILED"
    assert "FAILED to cancel 2" in event.body


def test_severity_uses_intraday_current_price_not_daily_open():
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pandas_market_calendars")
    pytest.importorskip("yfinance")

    now = pd.Timestamp("2026-05-22 13:15:00Z")
    intraday_idx = pd.DatetimeIndex([
        pd.Timestamp("2026-05-21 20:00:00Z"),
        pd.Timestamp("2026-05-22 13:10:00Z"),
        now,
    ])
    intraday = pd.DataFrame({"Close": [5000.0, 4955.0, 4950.0]}, index=intraday_idx)

    daily_idx = pd.date_range("2026-01-01", periods=100, freq="B")
    closes = pd.Series([100.0] * 100, index=daily_idx)
    returns = pd.Series([0.01 if i % 2 == 0 else -0.01 for i in range(100)], index=daily_idx)
    opens = closes.shift(1).fillna(100.0) * (1.0 + returns)
    opens.iloc[-1] = 150.0
    daily = pd.DataFrame({"Open": opens, "Close": closes}, index=daily_idx)
    expected_sigma = float(
        ((daily["Open"] - daily["Close"].shift(1)) / daily["Close"].shift(1))
        .dropna()
        .tail(60)
        .std()
    )

    def fake_download(_symbol, *args, **kwargs):
        return intraday if kwargs.get("interval") == "5m" else daily

    with patch("yfinance.download", side_effect=fake_download):
        metrics = gate.compute_overnight_severity(
            symbol="ES=F",
            lookback_days=120,
            sigma_window=60,
            now=now,
        )

    expected_move = (4950.0 - 5000.0) / 5000.0
    assert metrics["source"] == "ES=F"
    assert metrics["sigma_source"] == "SPY"
    assert abs(metrics["current_pct"] - expected_move) < 1e-12
    assert abs(metrics["sigma_60d"] - expected_sigma) < 1e-12
    assert abs(metrics["severity"] - expected_move / expected_sigma) < 1e-12
    assert metrics["n_obs"] == 99


def test_main_skips_closed_market_day_before_fetching_or_cancelling():
    with patch.object(gate, "_is_nyse_session_date", return_value=False), \
            patch.object(gate, "cancel_stale_market_orders") as cancel:
        assert gate.main([]) == 0
        cancel.assert_not_called()
