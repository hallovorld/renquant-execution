"""Bounded read/connect timeout on the Alpaca broker's account-read calls.

Root cause (verified 2026-08-11 07:00): the alpaca-py ``RESTClient`` issues
every HTTP call with ``timeout=None`` (``alpaca/common/rest.py::_one_request``),
so a stalled Alpaca socket hangs until the OS-level TCP timeout (~82s observed)
before the P-BROKER-CONNECT preflight can fail -- aborting the whole intraday
cycle. Fix A gives the two account-read calls the preflight makes
(``connect()`` / ``get_account_value()``) a bounded ``(connect, read)`` timeout,
armed via a substituted session, WITHOUT changing order-submission semantics.

No network / no alpaca SDK needed: these exercise the timeout-injection and
arming mechanism directly with fakes, the same no-network style as
``test_alpaca_broker_account_id.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types

import requests

from renquant_execution.alpaca_broker import (
    AlpacaBroker,
    _bounded_timeout_session_class,
)

# The bounded-timeout session class is built lazily (so importing
# renquant_execution needs neither requests nor the alpaca extra); resolve it
# once here for the tests, which run with the alpaca extra installed.
_BoundedTimeoutSession = _bounded_timeout_session_class()


class _FakeAccount:
    status = "active"
    account_number = "PA3REAL0001"
    portfolio_value = 12345.67


def test_importing_the_module_does_not_pull_requests():
    """Boundary guard: ``requests`` only ships with the ``alpaca`` extra, and
    this module is designed to import without the broker SDK (paper/shadow
    orchestration). The bounded-timeout session is built lazily, so importing
    ``renquant_execution.alpaca_broker`` must NOT pull ``requests`` at import
    time. Run in a fresh interpreter so an already-loaded ``requests`` from
    another test can't mask a regression."""
    code = (
        "import sys; import renquant_execution.alpaca_broker as m; "
        "assert 'requests' not in sys.modules, 'requests imported at module load'; "
        "assert 'alpaca' not in sys.modules, 'alpaca imported at module load'; "
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_bounded_timeout_session_injects_only_when_armed(monkeypatch):
    """The session injects the default timeout ONLY when armed and only when
    the caller did not pass one -- an explicit timeout always wins."""
    captured: list = []

    def fake_super_request(self, *args, **kwargs):  # patches requests.Session.request
        captured.append(kwargs.get("timeout"))
        return "resp"

    monkeypatch.setattr(requests.Session, "request", fake_super_request)

    session = _BoundedTimeoutSession(default_timeout=None)

    # Unarmed (default_timeout=None): behaves exactly like a stock Session ->
    # no timeout injected (this is the order-submission path's behaviour).
    session.request("GET", "https://api.alpaca.markets/v2/account")
    assert captured[-1] is None

    # Armed: the SDK-shaped call (no timeout kwarg) gets the bounded default.
    session.default_timeout = (5.0, 10.0)
    session.request("GET", "https://api.alpaca.markets/v2/account")
    assert captured[-1] == (5.0, 10.0)

    # An explicit caller timeout is never overridden.
    session.request("GET", "https://api.alpaca.markets/v2/account", timeout=1.0)
    assert captured[-1] == 1.0


def test_get_account_value_arms_then_restores_bounded_timeout():
    """get_account_value() arms the configured (connect, read) timeout DURING
    the account read, then restores it to None -- proving order-submission
    calls (which never arm) stay unbounded."""
    broker = AlpacaBroker(connect_timeout_seconds=5.0, read_timeout_seconds=10.0)
    session = _BoundedTimeoutSession(default_timeout=None)
    armed_during_call: list = []

    class _FakeClient:
        def __init__(self):
            self._session = session

        def get_account(self):
            # Observe what the session's default_timeout is at the moment the
            # SDK would issue the request.
            armed_during_call.append(session.default_timeout)
            return _FakeAccount()

    broker._trading_client = _FakeClient()

    assert broker.get_account_value() == 12345.67
    assert armed_during_call == [(5.0, 10.0)]  # armed during the read
    assert session.default_timeout is None  # disarmed after -> order path unbounded


def test_get_account_value_restores_timeout_even_on_error():
    """A raised account read still restores the disarmed state (no leak that
    could silently bound a later order-submission call)."""
    broker = AlpacaBroker()
    session = _BoundedTimeoutSession(default_timeout=None)

    class _BoomClient:
        def __init__(self):
            self._session = session

        def get_account(self):
            raise RuntimeError("Read timed out.")

    broker._trading_client = _BoomClient()

    try:
        broker.get_account_value()
    except RuntimeError:
        pass
    assert session.default_timeout is None


def test_connect_installs_bounded_session_and_arms_get_account(monkeypatch):
    """connect() swaps in the bounded session AND arms it around get_account()
    (the exact call that hung ~82s on 2026-08-11 07:00), then leaves it
    disarmed so subsequent order calls stay unbounded."""
    seen: dict = {}

    class _FakeTradingClient:
        def __init__(self, *args, **kwargs):
            # SDK-style: a plain requests.Session assigned at construction.
            self._session = requests.Session()

        def get_account(self):
            sess = self._session
            seen["is_bounded"] = isinstance(sess, _BoundedTimeoutSession)
            seen["armed_during"] = getattr(sess, "default_timeout", "MISSING")
            return _FakeAccount()

    fake_client_mod = types.ModuleType("alpaca.trading.client")
    fake_client_mod.TradingClient = _FakeTradingClient
    monkeypatch.setitem(sys.modules, "alpaca", types.ModuleType("alpaca"))
    monkeypatch.setitem(sys.modules, "alpaca.trading", types.ModuleType("alpaca.trading"))
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake_client_mod)

    broker = AlpacaBroker(
        api_key="k", secret_key="s", paper=True,
        connect_timeout_seconds=5.0, read_timeout_seconds=10.0,
    )
    broker.connect()

    assert seen["is_bounded"] is True
    assert seen["armed_during"] == (5.0, 10.0)
    # After connect returns the session is disarmed: order submission is never
    # bounded by this fix.
    assert broker._trading_client._session.default_timeout is None
