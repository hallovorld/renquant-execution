"""Behavior pins for the parameterized read-only broker wrapper (D6-§2a P-1).

Parity contract mirrored from the umbrella's currently-live wrapper
(``RenQuant/live/broker_readonly.py``): reads forward to the underlying
broker, every write is converted into a shadow ack (never submitted), no
code path mutates the underlying broker, and ``broker_name`` is the
state-isolation tag consumed by pipeline ``kernel/state_paths.py``
(``live_state.<tag>.json`` / ``runs.<tag>.db``). New in P-1: the tag is a
validated constructor parameter so two simultaneous shadow arms can tag
disjoint state; the default preserves the legacy hardcoded value.
"""
from __future__ import annotations

import pytest

from renquant_execution import (
    DEFAULT_READONLY_BROKER_NAME,
    ReadOnlyBrokerWrapper,
    get_broker,
    validate_readonly_broker_name,
)
from renquant_execution.alpaca_broker import AlpacaBroker
from renquant_execution.broker import BaseBroker


class RecordingBroker(BaseBroker):
    """Read-serving fake with a tripwire on every mutation path."""

    broker_name = "alpaca"

    def __init__(self) -> None:
        self.writes: list[tuple] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_position(self, symbol: str) -> float:
        return 7.0

    def get_account_value(self) -> float:
        return 10_500.0

    def get_avg_cost(self, symbol: str) -> float:
        return 101.25

    def get_cash(self) -> float:
        return 432.10

    def get_all_positions(self) -> list[dict]:
        return [{"symbol": "AAPL", "qty": 7.0}]

    def get_filled_orders(self, after: str | None = None) -> list[dict]:
        return [{"symbol": "AAPL", "after": after}]

    def get_open_orders(self) -> set[str]:
        return {"MSFT"}

    def supports_broker_side_stops(
        self, symbol: str | None = None, quantity: float | None = None
    ) -> bool:
        return symbol == "AAPL"

    def place_order(self, symbol: str, action: str, quantity: float) -> dict:
        self.writes.append(("place_order", symbol, action, quantity))
        return {"order_id": "real"}

    def place_notional_order(self, symbol: str, action: str, notional: float) -> dict:
        self.writes.append(("place_notional_order", symbol, action, notional))
        return {"order_id": "real"}

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict:
        self.writes.append(("place_stop_order", symbol, quantity, stop_price))
        return {"order_id": "real"}

    def cancel_order(self, order_id: str) -> bool:
        self.writes.append(("cancel_order", order_id))
        return False

    def broker_specific_probe(self) -> str:
        return "underlying-read"


# ── Default-tag backward compatibility ─────────────────────────────────────

def test_default_tag_preserves_legacy_hardcoded_value() -> None:
    wrapper = ReadOnlyBrokerWrapper(RecordingBroker())

    assert wrapper.broker_name == "alpaca_shadow"
    assert wrapper.broker_name == DEFAULT_READONLY_BROKER_NAME
    # Class-level default stays for readers of the class attribute.
    assert ReadOnlyBrokerWrapper.broker_name == "alpaca_shadow"


def test_custom_tag_never_mutates_class_default_or_underlying() -> None:
    underlying = RecordingBroker()
    wrapper = ReadOnlyBrokerWrapper(underlying, broker_name="alpaca_shadow_a")

    assert wrapper.broker_name == "alpaca_shadow_a"
    assert ReadOnlyBrokerWrapper.broker_name == "alpaca_shadow"
    # The wrapper must never mirror or overwrite the underlying's tag —
    # mirroring "alpaca" would key shadow state onto prod state paths.
    assert underlying.broker_name == "alpaca"


# ── Tag validation: fail loud on garbage ───────────────────────────────────

@pytest.mark.parametrize(
    "bad_tag",
    [
        "",
        " ",
        "alpaca shadow",
        "a/b",
        "a\\b",
        "../etc",
        "tag.name",
        "tag\n",
        "宽客",
    ],
)
def test_garbage_tags_are_rejected_loudly(bad_tag: str) -> None:
    with pytest.raises(ValueError, match="path-safe token"):
        ReadOnlyBrokerWrapper(RecordingBroker(), broker_name=bad_tag)


@pytest.mark.parametrize("bad_type", [None, 7, b"alpaca_shadow"])
def test_non_string_tags_are_rejected_loudly(bad_type: object) -> None:
    with pytest.raises(TypeError, match="broker_name must be a str"):
        ReadOnlyBrokerWrapper(RecordingBroker(), broker_name=bad_type)  # type: ignore[arg-type]


def test_validator_returns_valid_tags_unchanged() -> None:
    # No silent normalization: the tag flows verbatim into state filenames
    # (pipeline _safe_broker owns hyphen→underscore mapping + its allowlist).
    for tag in ("alpaca_shadow", "alpaca_shadow_a", "alpaca_shadow_b", "alpaca-paper"):
        assert validate_readonly_broker_name(tag) == tag


# ── Never-submit enforcement (umbrella wrapper parity) ─────────────────────

def test_all_write_paths_are_swallowed_and_shadow_acked() -> None:
    underlying = RecordingBroker()
    wrapper = ReadOnlyBrokerWrapper(underlying, broker_name="alpaca_shadow_a")

    order = wrapper.place_order("AAPL", "buy", 2)
    stop = wrapper.place_stop_order("AAPL", 2.0, 180.5)
    notional = wrapper.place_notional_order("BLK", "buy", 324.17)
    cancelled = wrapper.cancel_order("any-order-id")

    assert underlying.writes == []  # nothing ever reaches the broker
    assert order["status"] == "shadow_ack" and order["shadow"] is True
    assert order["order_id"].startswith("SHADOW-")
    assert order["action"] == "BUY" and order["quantity"] == 2.0
    assert stop["status"] == "shadow_ack" and stop["shadow"] is True
    assert stop["order_id"].startswith("SHADOW-STOP-")
    assert stop["stop_price"] == 180.5
    assert notional["status"] == "shadow_ack" and notional["shadow"] is True
    assert notional["requested_notional"] == 324.17
    assert cancelled is True  # swallowed ack, not the underlying's False


def test_reads_forward_to_the_underlying_broker() -> None:
    underlying = RecordingBroker()
    wrapper = ReadOnlyBrokerWrapper(underlying, broker_name="alpaca_shadow_b")
    wrapper.connect()

    assert underlying.connected is True
    assert wrapper.get_position("AAPL") == pytest.approx(7.0)
    assert wrapper.get_account_value() == pytest.approx(10_500.0)
    assert wrapper.get_avg_cost("AAPL") == pytest.approx(101.25)
    assert wrapper.get_cash() == pytest.approx(432.10)
    assert wrapper.get_all_positions() == [{"symbol": "AAPL", "qty": 7.0}]
    assert wrapper.get_filled_orders(after="2026-07-01") == [
        {"symbol": "AAPL", "after": "2026-07-01"}
    ]
    assert wrapper.get_open_orders() == {"MSFT"}
    assert wrapper.supports_broker_side_stops("AAPL", 3.0) is True
    assert wrapper.supports_broker_side_stops("BLK", 0.4) is False
    # Forward-compat pass-through for broker-specific read accessors.
    assert wrapper.broker_specific_probe() == "underlying-read"
    wrapper.disconnect()
    assert underlying.connected is False


def test_private_and_underlying_attrs_never_forward() -> None:
    wrapper = ReadOnlyBrokerWrapper(RecordingBroker())

    with pytest.raises(AttributeError):
        wrapper._not_a_real_attr  # noqa: B018
    # Guard against __getattr__ recursion on a partially built instance.
    assert wrapper.underlying is wrapper.__dict__["underlying"]


# ── State isolation per tag (two-arm shadow experiment shape) ──────────────

def test_two_arms_with_distinct_tags_key_disjoint_state_paths() -> None:
    """Two wrappers over the SAME real broker must isolate state by tag.

    State filenames are derived by pipeline ``kernel/state_paths.py`` as
    ``live_state.<tag>.json`` / ``runs.<tag>.db`` keyed on ``broker_name``
    (this repo cannot import the pipeline — the naming convention is
    replicated literally here as the cross-repo pin, same pattern as the
    QTY_INTEGRAL_EPS replication in test_order_state_machine.py).
    """
    shared_underlying = RecordingBroker()
    arm_a = ReadOnlyBrokerWrapper(shared_underlying, broker_name="alpaca_shadow_a")
    arm_b = ReadOnlyBrokerWrapper(shared_underlying, broker_name="alpaca_shadow_b")

    assert arm_a.broker_name != arm_b.broker_name

    state_a = f"live_state.{arm_a.broker_name}.json"
    state_b = f"live_state.{arm_b.broker_name}.json"
    runs_a = f"runs.{arm_a.broker_name}.db"
    runs_b = f"runs.{arm_b.broker_name}.db"
    legacy = f"live_state.{DEFAULT_READONLY_BROKER_NAME}.json"
    prod = f"live_state.{shared_underlying.broker_name}.json"

    # Disjoint from each other, from the untouched legacy ops-shadow tag,
    # and from prod state.
    assert len({state_a, state_b, legacy, prod}) == 4
    assert len({runs_a, runs_b}) == 2

    # No cross-talk: writes on either arm never leak to the broker or to
    # the other arm's identity.
    arm_a.place_order("AAPL", "buy", 1)
    arm_b.place_order("AAPL", "sell", 1)
    assert shared_underlying.writes == []
    assert arm_a.broker_name == "alpaca_shadow_a"
    assert arm_b.broker_name == "alpaca_shadow_b"


# ── Factory wiring ─────────────────────────────────────────────────────────

def test_factory_threads_readonly_tag_through_both_readonly_modes() -> None:
    for mode in ("alpaca-shadow", "readonly-alpaca"):
        broker = get_broker(mode, readonly_broker_name="alpaca_shadow_a")
        assert isinstance(broker, ReadOnlyBrokerWrapper)
        assert isinstance(broker.underlying, AlpacaBroker)
        assert broker.broker_name == "alpaca_shadow_a"


def test_factory_default_readonly_tag_is_backward_compatible() -> None:
    broker = get_broker("readonly-alpaca")
    assert broker.broker_name == "alpaca_shadow"


def test_factory_rejects_readonly_tag_on_non_readonly_modes() -> None:
    with pytest.raises(ValueError, match="only valid for read-only broker modes"):
        get_broker("paper", readonly_broker_name="alpaca_shadow_a")
    with pytest.raises(ValueError, match="only valid for read-only broker modes"):
        get_broker("alpaca", readonly_broker_name="alpaca_shadow_a")


def test_factory_propagates_tag_validation_failure() -> None:
    with pytest.raises(ValueError, match="path-safe token"):
        get_broker("alpaca-shadow", readonly_broker_name="../evil")
