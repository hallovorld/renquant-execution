"""Bounded read/connect timeout on the Alpaca broker's account-read calls.

Root cause (verified 2026-08-11 07:00): the alpaca-py ``RESTClient`` issues
every HTTP call with ``timeout=None`` (``alpaca/common/rest.py::_one_request``),
so a stalled Alpaca socket hangs until the OS-level TCP timeout (~82s observed)
before the P-BROKER-CONNECT preflight can fail -- aborting the whole intraday
cycle. Fix A gives the two account-read calls the preflight makes
(``connect()`` / ``get_account_value()``) a bounded ``(connect, read)`` timeout
by TEMPORARILY WRAPPING the SDK session's ``request`` method (Codex
execution#41: wrap, don't replace -- so proxies/verify/cert/cookies/hooks/
params/auth/adapters survive), WITHOUT changing order-submission semantics.

No network / no alpaca SDK needed: these exercise the timeout-injection and
wrap/restore mechanism directly with fakes, the same no-network style as
``test_alpaca_broker_account_id.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest
import requests

from renquant_execution.alpaca_broker import AlpacaBroker


class _FakeAccount:
    status = "active"
    account_number = "PA3REAL0001"
    portfolio_value = 12345.67


def _a_response_hook(response, *args, **kwargs):  # representative session hook
    return response


def test_importing_the_module_does_not_pull_requests():
    """Boundary guard: ``requests`` only ships with the ``alpaca`` extra, and
    this module is designed to import without the broker SDK (paper/shadow
    orchestration). The wrap mechanism never imports ``requests`` at all, so
    importing ``renquant_execution.alpaca_broker`` must NOT pull ``requests``
    (or ``alpaca``) at import time. Run in a fresh interpreter so an
    already-loaded ``requests`` from another test can't mask a regression."""
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


def test_bounded_account_timeout_injects_only_in_window(monkeypatch):
    """The bounded ``(connect, read)`` timeout is injected ONLY inside the
    context and only when the caller passed none -- an explicit timeout always
    wins, and OUT of the window the session's own ``request`` runs untouched
    (this is the order-submission path's behaviour: no injected timeout)."""
    captured: list = []

    def fake_super_request(self, *args, **kwargs):  # patches requests.Session.request
        captured.append(kwargs.get("timeout"))
        return "resp"

    monkeypatch.setattr(requests.Session, "request", fake_super_request)

    session = requests.Session()

    class _FakeClient:
        def __init__(self):
            self._session = session

    broker = AlpacaBroker(connect_timeout_seconds=5.0, read_timeout_seconds=10.0)
    broker._trading_client = _FakeClient()

    # Out of window: an order-path-style call carries NO injected timeout.
    session.request("GET", "https://api.alpaca.markets/v2/orders")
    assert captured[-1] is None

    with broker._bounded_account_timeout():
        # In window, caller omits timeout -> the bounded default is injected.
        session.request("GET", "https://api.alpaca.markets/v2/account")
        assert captured[-1] == (5.0, 10.0)
        # An explicit caller timeout is never overridden.
        session.request("GET", "https://api.alpaca.markets/v2/account", timeout=1.0)
        assert captured[-1] == 1.0

    # After the window: original request restored, order path unbounded again.
    session.request("GET", "https://api.alpaca.markets/v2/orders")
    assert captured[-1] is None
    # Pristine reversibility: no leftover instance attribute.
    assert "request" not in session.__dict__


def test_get_account_value_wraps_then_restores_request():
    """get_account_value() wraps the session's request DURING the account read
    (so the read is bounded), then restores it -- proving order-submission
    calls (which never enter the context) keep the original ``request``."""
    session = requests.Session()
    original_request = session.request  # the class-bound method
    wrapped_during_call: list = []

    class _FakeClient:
        def __init__(self):
            self._session = session

        def get_account(self):
            # request is temporarily the wrapper (an instance attribute) here.
            wrapped_during_call.append("request" in session.__dict__)
            return _FakeAccount()

    broker = AlpacaBroker(connect_timeout_seconds=5.0, read_timeout_seconds=10.0)
    broker._trading_client = _FakeClient()

    assert broker.get_account_value() == 12345.67
    assert wrapped_during_call == [True]  # wrapped during the read
    assert "request" not in session.__dict__  # restored after
    assert session.request == original_request  # same original method -> order path unchanged


def test_get_account_value_restores_request_even_on_error():
    """A raised account read still restores the original ``request`` (no leak
    that could silently bound a later order-submission call)."""
    session = requests.Session()
    original_request = session.request

    class _BoomClient:
        def __init__(self):
            self._session = session

        def get_account(self):
            raise RuntimeError("Read timed out.")

    broker = AlpacaBroker()
    broker._trading_client = _BoomClient()

    with pytest.raises(RuntimeError):
        broker.get_account_value()
    assert "request" not in session.__dict__
    assert session.request == original_request


def test_connect_preserves_non_header_session_state_same_object(monkeypatch):
    """HIGH regression (Codex execution#41 finding 2): wrap-not-replace must
    leave the SDK's OWN session object -- and ALL of its non-header transport
    state (proxies/verify/cert/cookies/params/auth/hooks/mounted adapters) --
    untouched by connect()'s bounded account read, while still bounding that
    read. The previous replace-the-session design silently reset every one of
    these back to defaults."""
    seen: dict = {}
    captured_timeouts: list = []

    def fake_super_request(self, *args, **kwargs):  # patches requests.Session.request
        captured_timeouts.append(kwargs.get("timeout"))
        return "resp"

    monkeypatch.setattr(requests.Session, "request", fake_super_request)

    class _FakeTradingClient:
        def __init__(self, *args, **kwargs):
            s = requests.Session()
            # Representative NON-header session state the SDK could have seeded.
            s.proxies = {"https": "http://proxy.internal:8080"}
            s.verify = False
            s.cert = "/etc/ssl/client-cert.pem"
            s.cookies.set("sessionid", "abc123")
            s.params = {"account_scope": "trading"}
            s.auth = ("api-user", "api-secret")
            s.hooks["response"].append(_a_response_hook)
            self._session = s
            seen["session"] = s
            seen["adapters"] = dict(s.adapters)

        def get_account(self):
            # SDK-shaped account read: issues through the session, must be bounded.
            self._session.request("GET", "https://api.alpaca.markets/v2/account")
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

    sess = broker._trading_client._session
    # SAME object: connect() never swapped the session.
    assert sess is seen["session"]
    # Every piece of non-header transport state survives, equal to what was set.
    assert sess.proxies == {"https": "http://proxy.internal:8080"}
    assert sess.verify is False
    assert sess.cert == "/etc/ssl/client-cert.pem"
    assert sess.cookies.get("sessionid") == "abc123"
    assert sess.params == {"account_scope": "trading"}
    assert sess.auth == ("api-user", "api-secret")
    assert _a_response_hook in sess.hooks["response"]
    assert dict(sess.adapters) == seen["adapters"]
    # The account read WAS bounded (the exact call that hung ~82s on 08-11 07:00).
    assert captured_timeouts == [(5.0, 10.0)]
    # And restored afterward: a later order-path call carries no injected timeout.
    sess.request("GET", "https://api.alpaca.markets/v2/orders")
    assert captured_timeouts[-1] is None
    assert "request" not in sess.__dict__


def test_bounded_account_timeout_raises_when_session_unusable():
    """No silent degrade (Codex execution#41 finding 3): if the trading client
    has no usable session -- ``_session`` is None, or its ``request`` is not
    callable -- the context manager RAISES a diagnosable RuntimeError instead
    of yielding an UNBOUNDED read. A silent unbounded fallback would defeat the
    fast-fail bound the paired retry path depends on."""
    broker = AlpacaBroker()

    class _NoSessionClient:
        _session = None

    broker._trading_client = _NoSessionClient()
    with pytest.raises(RuntimeError, match="missing or has no callable"):
        with broker._bounded_account_timeout():
            pass  # pragma: no cover -- must not be reached

    class _BadSession:
        request = "not-callable"

    class _BadSessionClient:
        _session = _BadSession()

    broker._trading_client = _BadSessionClient()
    with pytest.raises(RuntimeError, match="missing or has no callable"):
        with broker._bounded_account_timeout():
            pass  # pragma: no cover -- must not be reached
