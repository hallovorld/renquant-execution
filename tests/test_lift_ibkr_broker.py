"""Parity test for the IBKR broker lift (live/ibkr_broker.py → renquant-execution).

IBKRBroker is a stub (raises NotImplementedError until ib_insync is wired). The
lift is verbatim (only `from .broker import BaseBroker`, already present). Test:
it imports, subclasses the canonical BaseBroker, the factory wires `ibkr`, and
the stub fails loud rather than silently no-op'ing.
"""
from __future__ import annotations

import importlib

import pytest

ibkr = importlib.import_module("renquant_execution.ibkr_broker")
factory = importlib.import_module("renquant_execution.factory")
broker_mod = importlib.import_module("renquant_execution.broker")


def test_ibkr_imports_and_subclasses_base_broker() -> None:
    assert issubclass(ibkr.IBKRBroker, broker_mod.BaseBroker)


def test_factory_wires_ibkr() -> None:
    b = factory.get_broker("ibkr")
    assert isinstance(b, ibkr.IBKRBroker)


def test_ibkr_stub_fails_loud_not_silent() -> None:
    b = ibkr.IBKRBroker()
    # A stub broker must raise (never silently pretend to connect/trade).
    with pytest.raises(NotImplementedError):
        b.connect()
    with pytest.raises(NotImplementedError):
        b.place_order("AAPL", "buy", 1)
