"""Read-only broker wrapper for shadow/live decision rehearsal.

Owning implementation of the umbrella repo's ``live/broker_readonly.py``
port (renquant-execution owns broker wrappers). The wrapper:

- Forwards every read-side method (account value, cash, positions,
  filled/open orders) to the underlying real broker, so a shadow run sees
  the same live market and account state as the primary run.
- Converts every write-side method (``place_order``,
  ``place_notional_order``, ``place_stop_order``, ``cancel_order``) into a
  synthesized shadow ack. No broker round-trip ever happens on a write
  path; the underlying broker is never mutated.

Broker-tag parameterization (D6-§2a prerequisite P-1, renquant-orchestrator
``doc/design/2026-07-09-governor-prereg-replay-protocol.md``): the state
isolation tag ``broker_name`` is a constructor parameter instead of a
hardcoded class constant, so multiple simultaneous shadow arms can each tag
their own state (e.g. ``alpaca_shadow_a`` / ``alpaca_shadow_b``). Consumers
key state paths on ``broker_name`` — renquant-pipeline
``kernel/state_paths.py`` derives ``live_state.<tag>.json`` and
``runs.<tag>.db`` from it, guarded by its own fail-closed ``ALLOWED_BROKERS``
allowlist. This module validates the tag SHAPE (non-empty path-safe token,
fail loud on garbage); membership in the pipeline allowlist stays owned by
the pipeline repo (do not duplicate that contract here).

Default ``broker_name="alpaca_shadow"`` preserves the pre-parameterization
behavior exactly (backward compatible for every existing caller, including
the ``get_broker`` factory modes ``alpaca-shadow`` / ``readonly-alpaca``).

Safety property: no method on this class makes a network call or mutates
the underlying broker. Pinned in ``tests/test_readonly_broker_port.py`` and
``tests/test_execution_pipeline.py``.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from .broker import BaseBroker

log = logging.getLogger("renquant_execution.readonly_broker")

# Default state-isolation tag — the pre-parameterization hardcoded value.
# Kept as the constructor default so legacy single-arm shadow callers are
# unchanged; the legacy daily Step-4 ops shadow keeps owning this tag.
DEFAULT_READONLY_BROKER_NAME = "alpaca_shadow"

# Path-safe token: letters, digits, underscore, hyphen. No path separators,
# no dots (rules out ``..`` traversal), no whitespace. The tag is embedded
# verbatim in state filenames (``live_state.<tag>.json`` / ``runs.<tag>.db``)
# by pipeline ``kernel/state_paths.py``, so anything outside this charset is
# rejected loudly instead of being silently corrected.
_BROKER_TAG_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


def validate_readonly_broker_name(broker_name: str) -> str:
    """Validate a shadow broker-state tag; fail loud on garbage.

    Shape-only validation (non-empty path-safe token). The pipeline's
    ``ALLOWED_BROKERS`` allowlist remains the fail-closed membership gate at
    the state-path boundary; this guard exists so a bad tag dies at
    construction time with a clear error instead of surfacing later as a
    mangled state filename.
    """
    if not isinstance(broker_name, str):
        raise TypeError(
            f"broker_name must be a str, got {type(broker_name).__name__}"
        )
    if not _BROKER_TAG_RE.fullmatch(broker_name):
        raise ValueError(
            "broker_name must be a non-empty path-safe token "
            f"([A-Za-z0-9_-]+), got {broker_name!r}"
        )
    return broker_name


class ReadOnlyBrokerWrapper(BaseBroker):
    """Forward account reads while converting all mutations into shadow events."""

    # Class-level default kept for backward compatibility with readers of
    # ``ReadOnlyBrokerWrapper.broker_name``; every instance sets its own
    # (validated) ``broker_name`` in ``__init__``.
    broker_name = DEFAULT_READONLY_BROKER_NAME

    def __init__(
        self,
        underlying: BaseBroker,
        broker_name: str = DEFAULT_READONLY_BROKER_NAME,
    ) -> None:
        self.underlying = underlying
        # Deliberately NOT mirroring ``underlying.broker_name``: the wrapper's
        # own tag is the state-isolation key (live_state.<tag>.json /
        # runs.<tag>.db via pipeline kernel/state_paths.py). Mirroring the
        # underlying real broker's tag would write shadow state over prod
        # state — the exact contamination this wrapper exists to prevent.
        self.broker_name = validate_readonly_broker_name(broker_name)

    def connect(self) -> None:
        self.underlying.connect()

    def disconnect(self) -> None:
        self.underlying.disconnect()

    def get_position(self, symbol: str) -> float:
        return self.underlying.get_position(symbol)

    def get_account_value(self) -> float:
        return self.underlying.get_account_value()

    def get_avg_cost(self, symbol: str) -> float:
        return self.underlying.get_avg_cost(symbol)

    def get_cash(self) -> float:
        return self.underlying.get_cash()

    def get_all_positions(self) -> list[dict[str, Any]]:
        return self.underlying.get_all_positions()

    def get_filled_orders(
        self,
        after: str | None = None,
        asset_class: str | None = "us_equity",
    ) -> list[dict[str, Any]]:
        # E3 asset-class forwarding: only pass the parameter through when a
        # caller opts OUT of the equity default, so underlying brokers (and
        # test fakes) with the legacy signature keep working unchanged.
        if asset_class == "us_equity":
            return self.underlying.get_filled_orders(after=after)
        return self.underlying.get_filled_orders(after=after, asset_class=asset_class)

    def get_open_orders(self, asset_class: str | None = "us_equity") -> set[str]:
        if asset_class == "us_equity":
            return self.underlying.get_open_orders()
        return self.underlying.get_open_orders(asset_class=asset_class)

    def supports_broker_side_stops(
        self, symbol: str | None = None, quantity: float | None = None
    ) -> bool:
        return self.underlying.supports_broker_side_stops(symbol, quantity)

    def place_order(self, symbol: str, action: str, quantity: float) -> dict[str, Any]:
        log.debug(
            "[%s] ReadOnlyBrokerWrapper.place_order swallowed: %s %s x%s",
            self.broker_name, action, symbol, quantity,
        )
        return {
            "order_id": f"SHADOW-{uuid.uuid4().hex[:12].upper()}",
            "status": "shadow_ack",
            "symbol": symbol,
            "action": action.upper(),
            "quantity": float(quantity),
            "shadow": True,
            "timestamp": time.time(),
        }

    def place_notional_order(self, symbol: str, action: str, notional: float) -> dict[str, Any]:
        log.debug(
            "[%s] ReadOnlyBrokerWrapper.place_notional_order swallowed: %s %s $%s",
            self.broker_name, action, symbol, notional,
        )
        return {
            "order_id": f"SHADOW-{uuid.uuid4().hex[:12].upper()}",
            "status": "shadow_ack",
            "symbol": symbol,
            "action": action.upper(),
            "quantity": 0.0,
            "requested_notional": float(notional),
            "notional": float(notional),
            "shadow": True,
            "timestamp": time.time(),
        }

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict[str, Any]:
        log.debug(
            "[%s] ReadOnlyBrokerWrapper.place_stop_order swallowed: STOP %s x%s @ %s",
            self.broker_name, symbol, quantity, stop_price,
        )
        return {
            "order_id": f"SHADOW-STOP-{uuid.uuid4().hex[:12].upper()}",
            "status": "shadow_ack",
            "symbol": symbol,
            "action": "SELL",
            "quantity": float(quantity),
            "stop_price": float(stop_price),
            "shadow": True,
            "timestamp": time.time(),
        }

    def cancel_order(self, order_id: str) -> bool:
        log.debug(
            "[%s] ReadOnlyBrokerWrapper.cancel_order swallowed: %s",
            self.broker_name, order_id,
        )
        return True

    def __getattr__(self, name: str) -> Any:
        # Forward-compat pass-through for broker-specific read accessors.
        # Only runs for attributes NOT found on the wrapper — every explicit
        # override above (including the write-swallowers and the instance
        # broker_name) always wins. ``underlying`` is guarded so a partially
        # constructed instance raises AttributeError instead of recursing.
        if name.startswith("_") or name == "underlying":
            raise AttributeError(name)
        return getattr(self.underlying, name)
