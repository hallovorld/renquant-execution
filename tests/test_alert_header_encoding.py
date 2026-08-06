"""A non-ASCII alert title must not disable the alert it is attached to.

`renquant_common.notify` fixed this on 2026-07-29 after a live alert was lost:
HTTP header values go on the wire as latin-1, so `urllib` raises
`UnicodeEncodeError` while BUILDING the request — the whole notification is
dropped, body included, not merely its title.

`renquant_execution.alerts` has a second, independent sender that never got the
fix. Observed live 2026-08-06:

    ntfy publish attempt 1/3 failed ('latin-1' codec can't encode character
    '\\u2014' in position 25); retrying
    ntfy publish attempt 2/3 failed (same); retrying
    ntfy sent via curl fallback: PROTECTIVE CENSUS FAILED — broker unreachable

Every urllib attempt failed on a single em dash. The alert survived only because
`curl` does not latin-1-encode headers — so the retry loop and the fallback
together were masking a total failure of the primary path.

These tests use a fake transport; none of them reach the network.
"""

from __future__ import annotations

import urllib.request

import pytest

from renquant_execution import alerts as A


NON_ASCII_TITLES = [
    pytest.param("PROTECTIVE CENSUS FAILED — broker unreachable", id="em-dash"),
    pytest.param("rq104 blend 假想前10 — 2026-07-28", id="chinese-plus-em-dash"),
    pytest.param("stops “armed” for 3 names", id="curly-quotes"),
]


def _event(title: str) -> A.AlertEvent:
    return A.AlertEvent(taxonomy="test", title=title, body="body", priority="default")


@pytest.mark.parametrize("title", NON_ASCII_TITLES)
def test_a_non_ascii_title_does_not_kill_the_primary_send(title, monkeypatch):
    """The regression itself: building the request must not raise."""
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        seen["data"] = req.data

        class _R:
            def read(self):
                return b""
        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    ok = A._send_ntfy("http://example.invalid/topic", _event(title),
                      A.logger if hasattr(A, "logger") else __import__("logging").getLogger("t"))

    assert ok is True, "the primary path must succeed, not fall through to curl"
    # urllib title-cases header keys
    header = seen["headers"].get("Title")
    assert header is not None
    header.encode("latin-1")            # would raise before the fix
    assert seen["data"] == b"body"      # the body still ships


def test_an_ascii_title_is_left_completely_alone(monkeypatch):
    """Anti-vacuity: the fix must not rewrite the ordinary case, or every alert
    the operator reads would arrive base64-wrapped."""
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)

        class _R:
            def read(self):
                return b""
        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    A._send_ntfy("http://example.invalid/topic", _event("rq104 DEGRADED: 2 issues"),
                 __import__("logging").getLogger("t"))

    assert seen["headers"].get("Title") == "rq104 DEGRADED: 2 issues"


@pytest.mark.parametrize("title", NON_ASCII_TITLES)
def test_the_curl_fallback_header_is_encoded_the_same_way(title, monkeypatch):
    """The two paths must not disagree. curl tolerates a raw UTF-8 header, so
    before this change the fallback delivered a differently-encoded title than
    the primary path would have — the reader could not tell which sender ran."""
    calls: dict = {}

    def _boom(req, timeout=None):
        raise OSError("primary down")

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd

        class _P:
            returncode = 0
        return _P()

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(A.subprocess, "run", _fake_run)
    monkeypatch.setenv("RENQUANT_NTFY_RETRIES", "1")
    monkeypatch.setenv("RENQUANT_NTFY_BACKOFF_SECONDS", "0")

    A._send_ntfy("http://example.invalid/topic", _event(title),
                 __import__("logging").getLogger("t"))

    cmd = calls["cmd"]
    title_arg = cmd[cmd.index("-H") + 1]
    assert title_arg.startswith("Title: ")
    title_arg.encode("latin-1")          # same guarantee as the primary path


def test_the_encoder_is_the_shared_one_not_a_local_copy():
    """Two copies of this rule is how one of them stops being fixed — which is
    exactly what happened here: renquant-common was repaired 2026-07-29 and this
    sender was not."""
    from renquant_common.notify import encode_header as canonical

    assert A.encode_header is canonical
