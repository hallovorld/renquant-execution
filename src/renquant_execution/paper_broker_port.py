"""``PaperBrokerPort`` — the paper-simulation adapter for the slice-1
``BrokerPort`` Protocol (:mod:`renquant_execution.order_state_machine`).

``PaperBroker`` (:mod:`renquant_execution.paper_broker`) is a synchronous,
fill-or-reject simulator: ``place_order`` resolves to a terminal status
(``filled`` or ``rejected``) before it returns — there is no live/pending
order state, and no ``client_order_id`` concept (it mints its own internal
``order_id``). ``BrokerPort`` requires a ``client_order_id``-keyed
submit/cancel/status/open-orders contract with idempotent duplicate
rejection. This adapter bridges the two: it is the ONLY place that maps
child_order_id to a stored terminal outcome.

Because every order PaperBroker accepts is already terminal by the time
``submit_order`` returns, ``open_orders()`` correctly returns ``{}``
always and ``cancel_order()`` returns the already-terminal status without
attempting a live cancel — that is honest paper-broker behavior, not a
missing capability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .alpaca_broker_port import BrokerPortContractError
from .order_state_machine import SIDE_BUY
from .paper_broker import PaperBroker


@dataclass
class PaperBrokerPort:
    """Slice-1 ``BrokerPort`` over a ``PaperBroker`` instance.

    ``broker`` defaults to a fresh ``PaperBroker`` if not supplied. Callers
    that want a specific starting cash balance or a pre-seeded price book
    should construct and pass their own ``PaperBroker``.
    """

    broker: PaperBroker = field(default_factory=PaperBroker)
    _orders: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.broker.connected:
            self.broker.connect()

    def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float | None = None,
    ) -> Mapping[str, Any]:
        del limit_price  # PaperBroker fills at set_price(); no limit concept.
        if client_order_id in self._orders:
            raise BrokerPortContractError(
                f"duplicate client_order_id: {client_order_id!r}"
            )
        side_u = str(side).upper()
        action = "BUY" if side_u == SIDE_BUY else "SELL"
        result = self.broker.place_order(symbol=symbol, action=action, quantity=qty)
        record = {
            "status": result["status"],
            "broker_order_id": str(result["order_id"]),
            "client_order_id": client_order_id,
            "filled_qty": float(result["quantity"]),
        }
        self._orders[client_order_id] = record
        return record

    def cancel_order(self, client_order_id: str) -> Mapping[str, Any]:
        record = self._orders.get(client_order_id)
        if record is None:
            raise KeyError(f"unknown client_order_id: {client_order_id!r}")
        # Already terminal (filled/rejected) by the time submit_order
        # returned — there is nothing live to cancel.
        return {"status": record["status"], "filled_qty": record["filled_qty"]}

    def open_orders(self) -> Mapping[str, float]:
        # PaperBroker resolves every order synchronously in place_order();
        # nothing is ever left open/unfilled.
        return {}

    def order_status(self, client_order_id: str) -> Mapping[str, Any]:
        record = self._orders.get(client_order_id)
        if record is None:
            raise KeyError(f"unknown client_order_id: {client_order_id!r}")
        return {"status": record["status"], "filled_qty": record["filled_qty"]}


__all__ = ["PaperBrokerPort"]
