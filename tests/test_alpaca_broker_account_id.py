"""Tests for AlpacaBroker.get_account_id() (Codex review, D-C4 round-1):
the shared cash ledger's identity is derived from the broker's OWN verified
account_number, never a caller-supplied string. No network: _trading_client
and _account are set directly, the same way connect() would populate them.
"""
from __future__ import annotations

import pytest

from renquant_execution.alpaca_broker import AlpacaBroker


class _FakeAccount:
    def __init__(self, account_number: str):
        self.account_number = account_number


def test_get_account_id_returns_real_account_number():
    broker = AlpacaBroker()
    broker._trading_client = object()  # only presence is checked
    broker._account = _FakeAccount("PA3REAL0001")
    assert broker.get_account_id() == "PA3REAL0001"


def test_get_account_id_fails_closed_when_not_connected():
    broker = AlpacaBroker()
    with pytest.raises(RuntimeError, match="not connected"):
        broker.get_account_id()


def test_get_account_id_fails_closed_when_account_missing_account_number():
    broker = AlpacaBroker()
    broker._trading_client = object()
    broker._account = _FakeAccount("")
    with pytest.raises(RuntimeError, match="no account_number"):
        broker.get_account_id()
