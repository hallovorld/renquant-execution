"""Broker factory for explicit execution modes."""
from __future__ import annotations

from .alpaca_broker import AlpacaBroker
from .broker import BaseBroker
from .ibkr_broker import IBKRBroker
from .paper_broker import PaperBroker
from .readonly_broker import DEFAULT_READONLY_BROKER_NAME, ReadOnlyBrokerWrapper

_READONLY_MODES = frozenset({"alpaca-shadow", "readonly-alpaca"})


def get_broker(
    broker_type: str,
    *,
    initial_cash: float = 100_000.0,
    readonly_broker_name: str | None = None,
) -> BaseBroker:
    """Create a broker adapter from an audited broker mode string.

    ``readonly_broker_name`` parameterizes the read-only wrapper's
    state-isolation tag (D6-§2a P-1: one tag per shadow arm, e.g.
    ``alpaca_shadow_a`` / ``alpaca_shadow_b``). It is only meaningful for the
    read-only modes (``alpaca-shadow`` / ``readonly-alpaca``); passing it for
    any other mode is a wiring error and fails loud. ``None`` keeps the
    backward-compatible default tag (``alpaca_shadow``).
    """
    broker = broker_type.strip().lower()
    if readonly_broker_name is not None and broker not in _READONLY_MODES:
        raise ValueError(
            "readonly_broker_name is only valid for read-only broker modes "
            f"{sorted(_READONLY_MODES)}, got broker_type={broker_type!r}"
        )
    if broker == "paper":
        return PaperBroker(initial_cash=initial_cash)
    if broker == "alpaca-paper":
        return AlpacaBroker(paper=True)
    if broker == "alpaca":
        return AlpacaBroker(paper=False)
    if broker == "alpaca-paper":
        return AlpacaBroker(paper=True, env_prefix="ALPACA_PAPER", label="alpaca-paper")
    if broker in _READONLY_MODES:
        return ReadOnlyBrokerWrapper(
            AlpacaBroker(paper=False),
            broker_name=(
                DEFAULT_READONLY_BROKER_NAME
                if readonly_broker_name is None
                else readonly_broker_name
            ),
        )
    if broker == "ibkr":
        return IBKRBroker()
    raise ValueError(f"unsupported broker_type: {broker_type!r}")
