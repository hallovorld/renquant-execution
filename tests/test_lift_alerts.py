"""Parity test for the alerts lift (live/alerts.py → renquant-execution).

alerts.py is a pure-stdlib operational-notification leaf (ntfy publish + retry +
persisted duplicate suppression). Lifted verbatim (no internal-kernel imports to
rewrite). These tests exercise the module's value-add — the dedupe/suppression
logic — without any network call.
"""
from __future__ import annotations

import importlib

alerts = importlib.import_module("renquant_execution.alerts")


def test_alerts_imports_and_exposes_api() -> None:
    for name in ("AlertEvent", "post_ntfy_alert", "stable_alert_key"):
        assert hasattr(alerts, name), f"missing {name}"


def test_stable_alert_key_deterministic_and_compact() -> None:
    k1 = alerts.stable_alert_key("RENQUANT-104", "no_trade", "BULL_CALM")
    k2 = alerts.stable_alert_key("RENQUANT-104", "no_trade", "BULL_CALM")
    k3 = alerts.stable_alert_key("RENQUANT-104", "no_trade", "BEAR")
    assert k1 == k2 and k1 != k3
    assert len(k1) == 24 and all(c in "0123456789abcdef" for c in k1)


def test_env_suppression_returns_false_without_network(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RENQUANT_NO_NOTIFY", "1")
    ev = alerts.AlertEvent(taxonomy="INFO", title="t", body="b", key="k")
    # Must NOT attempt any network publish when env-suppressed.
    assert alerts.post_ntfy_alert("http://unused", ev,
                                  state_path=tmp_path / "s.json") is False


def test_should_suppress_cooldown_and_force_and_no_key() -> None:
    now = 1_000_000.0
    ev = alerts.AlertEvent(taxonomy="INFO", title="t", body="b",
                           key="abc", cooldown_seconds=3600)
    eid = alerts._event_id(ev)
    state = {"events": {eid: {"sent_at": now - 60}}}  # sent 60s ago
    assert alerts._should_suppress(ev, eid, state, now) is True          # within cooldown
    assert alerts._should_suppress(ev, eid, state, now + 7200) is False  # cooldown elapsed
    # force bypasses dedupe
    forced = alerts.AlertEvent(taxonomy="INFO", title="t", body="b",
                               key="abc", cooldown_seconds=3600, force=True)
    assert alerts._should_suppress(forced, eid, state, now) is False
    # no key → never suppress (actionable/unkeyed alerts always fire)
    nokey = alerts.AlertEvent(taxonomy="INFO", title="t", body="b")
    assert alerts._should_suppress(nokey, None, state, now) is False
