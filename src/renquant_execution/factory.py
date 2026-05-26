"""Broker factory for explicit execution modes."""
from __future__ import annotations

from .alpaca_broker import AlpacaBroker
from .broker import BaseBroker
from .paper_broker import PaperBroker
from .readonly_broker import ReadOnlyBrokerWrapper


def get_broker(broker_type: str, *, initial_cash: float = 100_000.0) -> BaseBroker:
    """Create a broker adapter from an audited broker mode string."""
    broker = broker_type.strip().lower()
    if broker == "paper":
        return PaperBroker(initial_cash=initial_cash)
    if broker == "alpaca-paper":
        return AlpacaBroker(paper=True)
    if broker == "alpaca":
        return AlpacaBroker(paper=False)
    if broker == "alpaca-shorts":
        return AlpacaBroker(paper=True, env_prefix="ALPACA_SHORTS", label="alpaca-shorts")
    if broker in {"alpaca-shadow", "readonly-alpaca"}:
        return ReadOnlyBrokerWrapper(AlpacaBroker(paper=False))
    raise ValueError(f"unsupported broker_type: {broker_type!r}")
