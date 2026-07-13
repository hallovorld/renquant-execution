"""Crypto Stage-0 battery checks — validate crypto trading prerequisites.

This module validates that an Alpaca PAPER account is correctly set up for
crypto trading by running a structured battery of checks against the live
broker API. Every check uses the AlpacaBroker adapter (never direct alpaca-py
imports) so that the battery's findings reflect the exact same code path
production orders will take.

The battery REFUSES to run on a non-paper broker — it places (and immediately
cancels) small orders as part of its validation, and must never produce side
effects on a live account.

Usage::

    from renquant_execution.alpaca_broker import AlpacaBroker
    from renquant_execution.crypto_stage0_checks import run_full_battery

    broker = AlpacaBroker(paper=True)
    broker.connect()
    report = run_full_battery(broker, dry_run=False)
    for step in report.steps:
        print(f"{step.status.value:5s}  {step.name}: {step.detail}")

Restructured from the orchestrator repo's battery script (code review:
broker-facing checks belong in the execution repo that owns AlpacaBroker).
This exactly mirrors the ``software_stops_liveness.py`` precedent
(renquant-execution#29/#30): a broker/runtime-facing checker moved out of
orchestrator into this repo, with orchestrator kept as a thin CLI/reporting
consumer.

Required/optional step policy (Codex review 2026-07-12 finding 5): every
:class:`StepResult` carries a ``required: bool`` field. :attr:`BatteryReport
.all_passed` only requires every ``required=True`` step to PASS — a SKIP on a
``required=False`` step (currently only :func:`check_data_parity`, a
data-domain placeholder outside this repo's execution-capability boundary)
must not block an otherwise-clean battery run from reporting overall success.

Single public entry point for transactional probes (Codex review 2026-07-12
finding 4): the two steps that place (and cancel) real paper orders --
GTC-limit and stop-limit acceptance -- are private
(``_check_gtc_order_acceptance`` / ``_check_stop_limit_acceptance``), not
exported in ``__all__``. :func:`run_full_battery` is the only sanctioned path
that can create a probe order; an orchestrator caller must go through it, not
call the transactional checks piecemeal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from .alpaca_broker import AlpacaBroker, _is_resting_order_status
from .crypto import (
    CryptoAssetSpec,
    is_crypto_pair,
    round_price_to_increment,
    snap_qty_to_increment,
)

logger = logging.getLogger(__name__)

# ── canary configuration ────────────────────────────────────────────────────

#: Default canary pairs for the battery.
DEFAULT_CANARY_PAIRS: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")

#: Test notional for battery canary orders ($11 — above the $10 minimum
#: cost-basis requirement, small enough to be immaterial on paper).
DEFAULT_TEST_NOTIONAL_USD: float = 11.0

#: GTC limit-BUY canary probe price, as a fraction of the pair's REAL current
#: reference price (Codex review 2026-07-12 finding 3 on #34: a universal
#: fixed price like $0.01 says nothing about whether a given pair's actual
#: price band/tick grid would even accept the order -- a rejection there
#: proves nothing about genuine GTC support). Half of the current price is
#: comfortably below market (won't fill) while staying within the pair's
#: real, currently-valid price grid.
DEFAULT_CANARY_LIMIT_BUY_FRACTION_OF_REFERENCE: float = 0.5

#: Stop-BUY canary probe stop price, as a multiple of the pair's REAL current
#: reference price -- far enough above market that the stop cannot trigger,
#: while still being derived from (and therefore validated against) the
#: pair's real price grid rather than a universal implausible constant.
DEFAULT_CANARY_STOP_MULTIPLE_OF_REFERENCE: float = 3.0

#: BUY stop-limit requires limit_price >= stop_price (the limit caps how high
#: you pay once triggered) -- this is the buffer above the derived stop price.
DEFAULT_CANARY_STOP_LIMIT_BUFFER: float = 1.01

#: Timeout/poll interval for confirming a canary order's cancellation
#: actually reached a terminal ``canceled`` state (same "confirm, don't
#: assume" discipline Codex required on PR #31's
#: ``AlpacaBroker.replace_crypto_stop_limit`` -- a cancel *request* that
#: didn't raise is not proof the order is gone; it can still be resting or
#: have filled/rejected before the cancel took effect). Applied proactively
#: here, ahead of review, to the two order-placing battery steps below.
DEFAULT_CANCEL_CONFIRM_TIMEOUT_SECONDS: float = 5.0
DEFAULT_CANCEL_CONFIRM_POLL_INTERVAL_SECONDS: float = 0.25

#: Order statuses meaning the probe order actually FILLED (or partially
#: filled) instead of resting -- real paper inventory was acquired. Reported
#: distinctly from a generic "not resting" rejection because it is the more
#: severe Tier-1 condition (Codex review 2026-07-12 finding 1/2).
_FILLED_ORDER_STATUSES = frozenset({"filled", "partially_filled"})


# ── result types ────────────────────────────────────────────────────────────


class StepStatus(str, Enum):
    """Battery step outcome."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass(frozen=True)
class StepResult:
    """Result of a single battery step.

    ``required`` (Codex review 2026-07-12 finding 5): whether this step must
    PASS for :attr:`BatteryReport.all_passed` to be True. Defaults to
    ``True`` (a genuine execution-capability check); the one current
    ``required=False`` step is :func:`check_data_parity` (an always-SKIP
    placeholder for a data-domain concern outside this repo's boundary --
    see its docstring).
    """

    name: str
    status: StepStatus
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class BatteryReport:
    """Aggregate result of a full Stage-0 battery run."""

    timestamp: str
    account_id: str
    environment: str
    dry_run: bool
    steps: list[StepResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        """Whether every ``required=True`` step PASSed.

        A SKIP/FAIL/ERROR on a ``required=False`` step (currently only
        ``data_parity``) does not block overall success (Codex review
        2026-07-12 finding 5) -- required steps still must all PASS.
        """
        return all(
            s.status == StepStatus.PASS for s in self.steps if s.required
        )

    @property
    def summary(self) -> str:
        counts = {s.value: 0 for s in StepStatus}
        for step in self.steps:
            counts[step.status.value] += 1
        parts = [f"{k}={v}" for k, v in sorted(counts.items()) if v > 0]
        return f"{len(self.steps)} steps: {', '.join(parts)}"


# ── individual battery steps ────────────────────────────────────────────────


def check_crypto_account_status(broker: AlpacaBroker) -> StepResult:
    """Verify the account is active and has crypto trading enabled."""
    name = "crypto_account_status"
    try:
        info = broker.get_account_info()
    except Exception as exc:
        return StepResult(
            name=name,
            status=StepStatus.ERROR,
            detail=f"get_account_info() failed: {exc}",
        )
    status = info.get("status", "")
    crypto_status = info.get("crypto_status", "")
    account_id = info.get("account_id", "")

    if status != "ACTIVE":
        return StepResult(
            name=name,
            status=StepStatus.FAIL,
            detail=f"account {account_id} status is {status!r}, expected ACTIVE",
            data=info,
        )
    # Alpaca paper accounts may report crypto_status as ACTIVE or APPROVED.
    # An empty string means the field is absent on the SDK model (older
    # versions / certain account types) — we treat that as PASS with a note,
    # not a hard failure, since the real proof is whether orders are accepted.
    if crypto_status == "INACTIVE":
        return StepResult(
            name=name,
            status=StepStatus.FAIL,
            detail=f"account {account_id} ACTIVE but crypto_status=INACTIVE",
            data=info,
        )
    detail = f"account {account_id} ACTIVE"
    if crypto_status:
        detail += f", crypto_status={crypto_status}"
    return StepResult(name=name, status=StepStatus.PASS, detail=detail, data=info)


def check_pair_snapshot(
    broker: AlpacaBroker,
    pairs: tuple[str, ...] = DEFAULT_CANARY_PAIRS,
) -> StepResult:
    """List tradable pairs with their order-grid increments.

    Attempts to resolve the CryptoAssetSpec for each canary pair. A pair that
    cannot be resolved is recorded as a failure.
    """
    name = "pair_snapshot"
    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for pair in pairs:
        if not is_crypto_pair(pair):
            failures.append(f"{pair}: not a valid pair-form symbol")
            continue
        try:
            spec: CryptoAssetSpec = broker.get_crypto_asset_spec(pair)
            results[pair] = {
                "symbol": spec.symbol,
                "min_order_size": spec.min_order_size,
                "min_trade_increment": spec.min_trade_increment,
                "price_increment": spec.price_increment,
                "tradable": True,
            }
        except Exception as exc:
            failures.append(f"{pair}: {exc}")
            results[pair] = {"symbol": pair, "tradable": False, "error": str(exc)}

    if failures:
        return StepResult(
            name=name,
            status=StepStatus.FAIL,
            detail=f"{len(failures)}/{len(pairs)} pairs failed: {'; '.join(failures)}",
            data={"pairs": results},
        )
    pair_summaries = [
        f"{p}(inc={r['min_trade_increment']}, price_inc={r['price_increment']})"
        for p, r in results.items()
    ]
    return StepResult(
        name=name,
        status=StepStatus.PASS,
        detail=f"{len(pairs)} pairs resolved: {', '.join(pair_summaries)}",
        data={"pairs": results},
    )


def _check_residual_exposure(
    broker: AlpacaBroker,
    order_id: str,
    symbol: str,
    *,
    detail: dict[str, Any],
) -> tuple[bool, str | None]:
    """Query order final state and position to detect residual exposure.

    Cancellation confirmation alone does not undo a fill -- if the probe
    order partially or fully filled before the cancel took effect, the
    account has unintended exposure.  This method queries:

    1. The order's final state (``filled_qty``, ``status``) via
       :meth:`AlpacaBroker.get_order_state`.
    2. The relevant position for ``symbol`` via
       :meth:`AlpacaBroker.get_position`.

    Returns ``(True, None)`` if no residual exposure is detected, or
    ``(False, failure_reason)`` with durable evidence in ``detail``
    if any nonzero fill or position exists.  A nonzero residual is a
    Tier-1 failure -- the probe created unintended market exposure.

    **Fails closed on API errors** (rescued from PR #36): position lookup
    errors (timeout/auth/500) are treated as Tier-1 unknown failures, not
    silently passed.  ``get_position()`` already handles "position not
    found" internally -- any propagating exception is a real API error.
    """
    # ── query order final state ────────────────────────────────────────
    try:
        order_state = broker.get_order_state(order_id)
    except Exception as exc:
        detail["residual_check_error"] = str(exc)
        return False, (
            f"cannot query final state of order {order_id}: {exc}"
        )

    filled_qty = float(order_state.get("filled_qty", 0.0) or 0.0)
    final_status = order_state.get("status", "")
    detail["final_order_state"] = {
        "status": final_status,
        "filled_qty": filled_qty,
        "filled_avg_price": order_state.get("filled_avg_price", 0.0),
    }

    if filled_qty > 0:
        return False, (
            f"order {order_id} has residual fill "
            f"(filled_qty={filled_qty}, status={final_status!r}) -- "
            f"Tier-1 failure: cancellation did not undo the fill"
        )

    # ── query position ─────────────────────────────────────────────────
    try:
        position_qty = broker.get_position(symbol)
    except Exception as exc:
        # get_position() already returns 0.0 for a recognized "position
        # not found" response without raising (AlpacaBroker._is_not_found_error).
        # Any exception that propagates is a real API error (timeout,
        # auth failure, 500, etc.) -- cannot confirm zero position, fail
        # closed as Tier-1 unknown failure.
        detail["position_check_error"] = str(exc)
        return False, (
            f"position lookup failed during residual-exposure "
            f"audit ({exc}) -- cannot confirm zero position; treating "
            f"as unknown failure (Tier-1)"
        )

    detail["residual_position_qty"] = position_qty

    if abs(position_qty) > 0:
        return False, (
            f"nonzero residual position after probe "
            f"(qty={position_qty}) -- Tier-1 failure: "
            f"probe order may have created unintended exposure"
        )

    return True, None


def _place_probe_and_confirm_cleanup(
    broker: AlpacaBroker,
    *,
    symbol: str,
    place_fn: Callable[[], dict[str, Any]],
    expected_order_type: str,
    expected_side: str,
    expected_time_in_force: str,
    expected_qty: float,
    expected_price_fields: dict[str, float],
    cancel_confirm_timeout_seconds: float,
    cancel_confirm_poll_interval_seconds: float,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Place one transactional probe order, validate genuine acceptance, then
    cancel and CONFIRM the cancellation reached a terminal state.

    Shared by :func:`_check_gtc_order_acceptance` and
    :func:`_check_stop_limit_acceptance` -- both probes follow the exact same
    place -> validate -> cancel -> confirm lifecycle, so the safety-critical
    logic lives in exactly one place.

    Returns ``(ok, failure_reason, detail)``. ``ok`` is True only when the
    order was genuinely accepted in a resting state, EVERY broker-confirmed
    field (type/side/TIF/price(s)/quantity) matches what was requested, AND
    the subsequent cancellation was confirmed terminal -- never merely "the
    place/cancel calls didn't raise" (Codex review 2026-07-12 findings 1 and
    2, and round-2 findings 1-3 on execution#34).
    """
    try:
        result = place_fn()
    except Exception as exc:
        return False, f"place failed ({exc})", {}

    order_id = result.get("order_id", "")
    status = result.get("status", "")
    detail: dict[str, Any] = {
        "order_id": order_id,
        "status": status,
        "confirmed_order_type": result.get("confirmed_order_type", ""),
        "confirmed_asset_class": result.get("confirmed_asset_class", ""),
        "side": result.get("side", ""),
        "confirmed_time_in_force": result.get("confirmed_time_in_force", ""),
        "qty": result.get("quantity", 0.0),
        "confirmed_qty": result.get("confirmed_qty"),
    }
    for field_name in expected_price_fields:
        detail[field_name] = result.get(field_name)

    if not order_id:
        return False, "order accepted but no order_id returned", detail

    # A FILLED probe order is a distinct, MORE severe Tier-1 condition than a
    # merely-rejected one: real (paper) inventory was acquired, not just "no
    # resting order to clean up".
    if status in _FILLED_ORDER_STATUSES:
        # Fail-closed residual check (rescued from PR #36): query order
        # final state AND position; API errors = Tier-1 unknown failure.
        _check_residual_exposure(broker, order_id, symbol, detail=detail)
        return (
            False,
            f"order {order_id} reports status={status!r} -- probe order "
            "FILLED instead of resting; real paper inventory acquired "
            f"(Tier-1 condition)",
            detail,
        )

    # Alpaca crypto orders often start as pending_new and transition to
    # new/accepted within ~1s. Poll briefly before rejecting.
    if not _is_resting_order_status(status) and "pending" in status.lower():
        import time as _time

        deadline = _time.monotonic() + cancel_confirm_timeout_seconds
        while _time.monotonic() < deadline:
            _time.sleep(cancel_confirm_poll_interval_seconds)
            try:
                refreshed = broker.get_order_state(order_id)
                status = refreshed.get("status", status)
                detail["status"] = status
            except Exception:
                break
            if _is_resting_order_status(status):
                break
            if status in _FILLED_ORDER_STATUSES:
                break

    if not _is_resting_order_status(status):
        if status in _FILLED_ORDER_STATUSES:
            _check_residual_exposure(broker, order_id, symbol, detail=detail)
            return (
                False,
                f"order {order_id} reports status={status!r} -- probe order "
                "FILLED instead of resting; real paper inventory acquired "
                f"(Tier-1 condition)",
                detail,
            )
        return (
            False,
            f"order {order_id} status {status!r} rejected the probe "
            "(not a genuinely resting/accepted order)",
            detail,
        )

    # The order IS resting -- attempt cleanup and CONFIRM terminal
    # cancellation before doing anything else (Codex review 2026-07-12
    # finding 1: a cancel_order() call that doesn't raise is not proof the
    # order is gone). Codex round-2 finding 5: a cancel_order() call that
    # DOES raise is likewise not proof the order is STILL there -- the
    # request may have reached the broker despite a client-side/transport
    # exception. Always poll for the actual terminal state regardless of
    # whether cancel_order raised; record both the exception (if any) and
    # the observed terminal state as durable evidence.
    cancel_exception: str | None = None
    try:
        broker.cancel_order(order_id)
    except Exception as cancel_exc:
        cancel_exception = str(cancel_exc)

    cancel_confirmed = broker.wait_for_order_terminal_cancel(
        order_id,
        timeout_seconds=cancel_confirm_timeout_seconds,
        poll_interval_seconds=cancel_confirm_poll_interval_seconds,
    )
    detail["cancel_confirmed"] = cancel_confirmed
    detail["cancel_exception"] = cancel_exception
    if not cancel_confirmed:
        # An unconfirmed cancellation is ambiguous: the order may still be
        # resting, or it may have filled during the cancel-confirm window.
        # Fail-closed residual check (rescued from PR #36).
        _check_residual_exposure(broker, order_id, symbol, detail=detail)
        reason = (
            f"cancel_order raised ({cancel_exception}) and cancellation of "
            f"order {order_id} was not subsequently confirmed terminally "
            f"canceled within {cancel_confirm_timeout_seconds}s -- order may "
            f"still be resting/uncancelled or may have filled"
            if cancel_exception is not None
            else (
                f"cancellation of order {order_id} not confirmed terminally "
                f"canceled within {cancel_confirm_timeout_seconds}s -- order "
                f"may still be resting/uncancelled or may have filled"
            )
        )
        return False, reason, detail

    # Residual exposure audit AFTER confirmed cancellation (rescued from
    # PR #36): even a confirmed cancel does not undo a fill that happened
    # before the cancel took effect.  A resting-at-acceptance-time order
    # can still fill during the cancel-confirm window.  Fail closed on
    # API errors -- cannot confirm zero position = Tier-1 unknown failure.
    clean, residual_reason = _check_residual_exposure(
        broker, order_id, symbol, detail=detail,
    )
    if not clean:
        return False, residual_reason, detail

    # Field validation (Codex review 2026-07-12 finding 2, strengthened per
    # round-2 finding 2), checked AFTER cleanup so a mismatched-but-resting
    # order is cleaned up regardless of whether the mismatch itself is
    # reported as a failure. A MISSING/empty broker-confirmed field must
    # FAIL, never be silently skipped -- the original `if field and field
    # != expected` pattern let an empty/unset field slip past validation
    # entirely, which is exactly backwards: we cannot confirm a field
    # matches if the broker didn't report it at all.
    #
    # Validates broker-confirmed fields (confirmed_*) extracted from the
    # SDK Order object, NOT the wrapper's request-echo fields (rescued
    # from PR #36).
    field_failures: list[str] = []
    confirmed_order_type = result.get("confirmed_order_type", "")
    if not confirmed_order_type or confirmed_order_type != expected_order_type:
        field_failures.append(
            f"confirmed_order_type {confirmed_order_type!r} != expected {expected_order_type!r}"
        )
    side = result.get("side", "")
    if not side or side != expected_side:
        field_failures.append(f"side {side!r} != expected {expected_side!r}")
    tif = result.get("confirmed_time_in_force", "")
    if not tif or tif != expected_time_in_force:
        field_failures.append(
            f"confirmed_time_in_force {tif!r} != expected {expected_time_in_force!r}"
        )
    confirmed_asset_class = result.get("confirmed_asset_class", "")
    if confirmed_asset_class and confirmed_asset_class != "crypto":
        field_failures.append(
            f"confirmed_asset_class {confirmed_asset_class!r} != expected 'crypto'"
        )
    # confirmed_qty reads the broker's own order.qty, NOT our
    # submitted/snapped qty echoed back (Codex round-2 review finding 1/2:
    # validating a value against itself proves nothing).
    confirmed_qty = result.get("confirmed_qty")
    if confirmed_qty is None or confirmed_qty <= 0:
        field_failures.append(f"confirmed_qty {confirmed_qty!r} is missing or non-positive")
    else:
        qty_tolerance = max(1e-9, abs(expected_qty) * 1e-6)
        if abs(float(confirmed_qty) - float(expected_qty)) > qty_tolerance:
            field_failures.append(
                f"confirmed_qty {confirmed_qty!r} != requested {expected_qty!r}"
            )
    for field_name, expected_value in expected_price_fields.items():
        actual = result.get(field_name)
        if actual is None or expected_value is None:
            field_failures.append(f"{field_name} missing from broker response")
            continue
        tolerance = max(1e-6, abs(float(expected_value)) * 1e-6)
        if abs(float(actual) - float(expected_value)) > tolerance:
            field_failures.append(
                f"{field_name} {actual!r} != requested {expected_value!r}"
            )
    if field_failures:
        return False, "; ".join(field_failures), detail

    return True, None, detail


def _check_gtc_order_acceptance(
    broker: AlpacaBroker,
    pairs: tuple[str, ...] = DEFAULT_CANARY_PAIRS,
    test_notional_usd: float = DEFAULT_TEST_NOTIONAL_USD,
    *,
    cancel_confirm_timeout_seconds: float = DEFAULT_CANCEL_CONFIRM_TIMEOUT_SECONDS,
    cancel_confirm_poll_interval_seconds: float = (
        DEFAULT_CANCEL_CONFIRM_POLL_INTERVAL_SECONDS
    ),
) -> StepResult:
    """Place and immediately cancel small GTC limit-buy orders.

    For each canary pair, places a GTC limit-buy order at roughly half the
    pair's REAL current reference price (derived via
    :meth:`AlpacaBroker.get_crypto_reference_price` -- Codex review
    2026-07-12 finding 3: a universal fixed price like $0.01 says nothing
    about whether a given pair's actual price band/tick grid would even
    accept the order) so it will never fill, verifies the broker accepts it
    in a genuinely resting state with matching order fields (finding 2), and
    then cancels it -- CONFIRMING the cancellation actually reached a
    terminal ``canceled`` state (finding 1 / PR #31 precedent). Private:
    this is a transactional probe -- the only sanctioned public entry point
    that may place battery probe orders is :func:`run_full_battery`
    (finding 4).
    """
    name = "gtc_order_acceptance"
    placed_and_cancelled: list[str] = []
    failures: list[str] = []
    order_details: dict[str, Any] = {}

    for pair in pairs:
        try:
            spec = broker.get_crypto_asset_spec(pair)
        except Exception as exc:
            failures.append(f"{pair}: spec lookup failed ({exc})")
            continue
        try:
            quote = broker.get_crypto_reference_quote(pair)
        except Exception as exc:
            failures.append(f"{pair}: reference quote lookup failed ({exc})")
            continue

        raw_limit_price = (
            quote.mid_price * DEFAULT_CANARY_LIMIT_BUY_FRACTION_OF_REFERENCE
        )
        limit_price = round_price_to_increment(raw_limit_price, spec.price_increment)
        raw_qty = test_notional_usd / limit_price
        # Snap onto the pair's min_trade_increment grid BEFORE handing off
        # to place_crypto_limit_order: a raw float division (test_notional /
        # a quote-derived, non-round limit_price) can carry more significant
        # decimal digits than the broker's 9dp precision grid allows, and
        # place_crypto_limit_order's own preflight (validate_crypto_order)
        # checks precision on the qty it's GIVEN, before its own internal
        # snapping runs -- so an unsnapped qty here can be spuriously
        # rejected even though the properly-snapped quantity would be fine.
        qty = snap_qty_to_increment(
            max(raw_qty, spec.min_order_size), spec.min_trade_increment
        )

        ok, reason, detail = _place_probe_and_confirm_cleanup(
            broker,
            symbol=pair,
            place_fn=lambda pair=pair, qty=qty, limit_price=limit_price: (
                broker.place_crypto_limit_order(
                    symbol=pair,
                    action="BUY",
                    qty=qty,
                    limit_price=limit_price,
                    time_in_force="gtc",
                )
            ),
            expected_order_type="limit",
            expected_side="BUY",
            expected_time_in_force="gtc",
            expected_qty=qty,
            expected_price_fields={"confirmed_limit_price": limit_price},
            cancel_confirm_timeout_seconds=cancel_confirm_timeout_seconds,
            cancel_confirm_poll_interval_seconds=cancel_confirm_poll_interval_seconds,
        )
        detail["quote_timestamp"] = quote.timestamp.isoformat()
        detail["quote_age_seconds"] = quote.age_seconds
        order_details[pair] = detail
        if ok:
            placed_and_cancelled.append(pair)
        else:
            failures.append(f"{pair}: {reason}")

    if failures:
        return StepResult(
            name=name,
            status=StepStatus.FAIL,
            detail=(
                f"{len(failures)}/{len(pairs)} GTC limit orders failed: "
                f"{'; '.join(failures)}"
            ),
            data={"orders": order_details},
        )
    return StepResult(
        name=name,
        status=StepStatus.PASS,
        detail=(
            f"{len(placed_and_cancelled)} GTC limit-buy orders placed+cancelled "
            f"(cancellation confirmed): {', '.join(placed_and_cancelled)}"
        ),
        data={"orders": order_details},
    )


def _check_stop_limit_acceptance(
    broker: AlpacaBroker,
    pairs: tuple[str, ...] = DEFAULT_CANARY_PAIRS,
    *,
    cancel_confirm_timeout_seconds: float = DEFAULT_CANCEL_CONFIRM_TIMEOUT_SECONDS,
    cancel_confirm_poll_interval_seconds: float = (
        DEFAULT_CANCEL_CONFIRM_POLL_INTERVAL_SECONDS
    ),
) -> StepResult:
    """Place and immediately cancel small GTC stop-limit BUY orders.

    For each canary pair, places a GTC stop-limit BUY order derived from the
    pair's REAL current reference price (stop at ~3x current price, limit a
    small buffer above the stop -- Codex review 2026-07-12 finding 3; a fixed
    $999,999,999 constant is not a valid cross-pair capability test), verifies
    the broker accepts it in a genuinely resting state with matching order
    fields (finding 2), and then cancels it -- CONFIRMING the cancellation
    actually reached a terminal ``canceled`` state (finding 1 / PR #31
    precedent). BUY-side stop-limits avoid the need for a held position
    (which SELL-side would require due to E11 no-short). Private: this is a
    transactional probe -- the only sanctioned public entry point that may
    place battery probe orders is :func:`run_full_battery` (finding 4).
    """
    name = "stop_limit_acceptance"
    placed_and_cancelled: list[str] = []
    failures: list[str] = []
    order_details: dict[str, Any] = {}

    for pair in pairs:
        try:
            spec = broker.get_crypto_asset_spec(pair)
        except Exception as exc:
            failures.append(f"{pair}: spec lookup failed ({exc})")
            continue
        try:
            quote = broker.get_crypto_reference_quote(pair)
        except Exception as exc:
            failures.append(f"{pair}: reference quote lookup failed ({exc})")
            continue

        raw_stop_price = quote.mid_price * DEFAULT_CANARY_STOP_MULTIPLE_OF_REFERENCE
        stop_price = round_price_to_increment(raw_stop_price, spec.price_increment)
        raw_limit_price = stop_price * DEFAULT_CANARY_STOP_LIMIT_BUFFER
        limit_price = round_price_to_increment(raw_limit_price, spec.price_increment)
        raw_qty = DEFAULT_TEST_NOTIONAL_USD / limit_price
        qty = snap_qty_to_increment(
            max(raw_qty, spec.min_order_size), spec.min_trade_increment
        )

        ok, reason, detail = _place_probe_and_confirm_cleanup(
            broker,
            symbol=pair,
            place_fn=lambda pair=pair, qty=qty, stop_price=stop_price, limit_price=limit_price: (
                broker.place_crypto_stop_limit_order(
                    symbol=pair,
                    action="BUY",
                    qty=qty,
                    stop_price=stop_price,
                    limit_price=limit_price,
                    time_in_force="gtc",
                )
            ),
            expected_order_type="stop_limit",
            expected_side="BUY",
            expected_time_in_force="gtc",
            expected_qty=qty,
            expected_price_fields={
                "confirmed_stop_price": stop_price,
                "confirmed_limit_price": limit_price,
            },
            cancel_confirm_timeout_seconds=cancel_confirm_timeout_seconds,
            cancel_confirm_poll_interval_seconds=cancel_confirm_poll_interval_seconds,
        )
        detail["quote_timestamp"] = quote.timestamp.isoformat()
        detail["quote_age_seconds"] = quote.age_seconds
        order_details[pair] = detail
        if ok:
            placed_and_cancelled.append(pair)
        else:
            failures.append(f"{pair}: {reason}")

    if failures:
        return StepResult(
            name=name,
            status=StepStatus.FAIL,
            detail=(
                f"{len(failures)}/{len(pairs)} stop-limit orders failed: "
                f"{'; '.join(failures)}"
            ),
            data={"orders": order_details},
        )
    return StepResult(
        name=name,
        status=StepStatus.PASS,
        detail=(
            f"{len(placed_and_cancelled)} GTC stop-limit BUY orders "
            f"placed+cancelled (cancellation confirmed): "
            f"{', '.join(placed_and_cancelled)}"
        ),
        data={"orders": order_details},
    )


def check_buying_power_behavior(broker: AlpacaBroker) -> StepResult:
    """Observational check of crypto buying-power fields -- NOT a verified
    invariant (Codex review 2026-07-12 finding 6).

    Crypto on Alpaca is understood to be non-marginable (buying power for
    crypto should track cash, not leverage), but this step does not have --
    and does not claim to have -- a specific, documented Alpaca account-field
    invariant it has verified. It only flags an internally-inconsistent
    reading (``non_marginable_buying_power<=0`` while ``cash>0``) as a
    misconfiguration signal, and reports the raw fields for a human to judge.
    Marked ``required=False``: this step's PASS is not a proven capability
    guarantee and must not gate overall battery eligibility the way a real
    execution-capability check does.
    """
    name = "buying_power_behavior"
    try:
        info = broker.get_account_info()
    except Exception as exc:
        return StepResult(
            name=name,
            status=StepStatus.ERROR,
            detail=f"get_account_info() failed: {exc}",
            required=False,
        )
    bp = info.get("buying_power", 0.0)
    nmbp = info.get("non_marginable_buying_power", 0.0)
    cash = info.get("cash", 0.0)

    data = {
        "buying_power": bp,
        "non_marginable_buying_power": nmbp,
        "cash": cash,
    }

    # For a paper account, non_marginable_buying_power should be positive and
    # represent the crypto-available capital. If NMBP is zero or negative
    # while cash is positive, something is misconfigured.
    if nmbp <= 0.0 and cash > 0.0:
        return StepResult(
            name=name,
            status=StepStatus.FAIL,
            detail=(
                f"non_marginable_buying_power={nmbp} but cash={cash} -- "
                "crypto buying power appears misconfigured (observational "
                "check, not a verified invariant)"
            ),
            data=data,
            required=False,
        )
    # Observational only -- this is NOT a verified Alpaca account-field
    # invariant for non-marginable crypto behavior, just a consistency
    # reading for a human to judge (Codex review 2026-07-12 finding 6).
    return StepResult(
        name=name,
        status=StepStatus.PASS,
        detail=(
            f"non_marginable_buying_power={nmbp}, buying_power={bp}, "
            f"cash={cash} -- observational only, not a verified invariant"
        ),
        data=data,
        required=False,
    )


def check_data_parity(
    pairs: tuple[str, ...] = DEFAULT_CANARY_PAIRS,
) -> StepResult:
    """Two-source price comparison for canary pairs.

    This step is a PLACEHOLDER: the AlpacaBroker adapter wraps the Trading
    API, not the Market Data API. A proper two-source price comparison
    requires either a CryptoHistoricalDataClient or an external data source
    -- both of which are outside the execution repo's boundary (execution owns
    broker mutation, not data feeds). The orchestrator or data repo should
    wire this step once data-feed infrastructure is in place.

    ``required=False`` (Codex review 2026-07-12 finding 5): this is a
    data-domain concern, not an execution-capability one -- an always-SKIP
    placeholder must not make a clean battery run structurally impossible to
    report as passing.
    """
    name = "data_parity"
    return StepResult(
        name=name,
        status=StepStatus.SKIP,
        detail=(
            f"data parity check requires a market-data source outside the "
            f"execution repo boundary (pairs: {', '.join(pairs)}); "
            "placeholder -- wire when data-feed infrastructure is available "
            "(required=False: data-domain concern, not execution-capability)"
        ),
        data={"pairs": list(pairs), "reason": "no_data_source"},
        required=False,
    )


# ── battery runner ──────────────────────────────────────────────────────────


def _assert_paper_mode(broker: AlpacaBroker) -> None:
    """Refuse to run the battery on a non-paper broker.

    The battery places (and cancels) orders -- it MUST NOT run on a live
    account. This is a hard safety gate, not a configuration option.
    """
    if not getattr(broker, "paper", False):
        raise RuntimeError(
            "crypto Stage-0 battery REFUSES to run on a non-paper broker -- "
            "the battery places and cancels orders for validation, which must "
            "never happen on a live account"
        )


def run_full_battery(
    broker: AlpacaBroker,
    *,
    dry_run: bool = False,
    pairs: tuple[str, ...] = DEFAULT_CANARY_PAIRS,
    test_notional_usd: float = DEFAULT_TEST_NOTIONAL_USD,
) -> BatteryReport:
    """Run the full Stage-0 crypto battery and return a structured report.

    The ONLY sanctioned public entry point that may place transactional
    probe orders (Codex review 2026-07-12 finding 4) -- an orchestrator
    caller must go through this function, never
    ``_check_gtc_order_acceptance``/``_check_stop_limit_acceptance``
    directly (both private, not exported).

    Parameters
    ----------
    broker : AlpacaBroker
        A connected, paper-mode AlpacaBroker instance.
    dry_run : bool
        If True, only run account/asset checks -- skip order placement steps.
    pairs : tuple[str, ...]
        Canary pairs to validate (default: BTC/USD, ETH/USD, SOL/USD).
    test_notional_usd : float
        Notional for canary orders (default: $1.10).

    Returns
    -------
    BatteryReport
        Structured report with step-by-step results.

    Raises
    ------
    RuntimeError
        If the broker is not in paper mode.
    """
    _assert_paper_mode(broker)

    timestamp = datetime.now(timezone.utc).isoformat()

    # Account/environment identity verification runs FIRST and is fail-closed
    # (Codex review 2026-07-12 finding 4): broker.paper=True alone is
    # necessary but not sufficient -- the account-identity lookup itself must
    # also succeed and independently confirm a paper account. A failed or
    # ambiguous lookup must refuse to run the battery, never silently default
    # environment="paper" and continue (the old behavior this replaces).
    account_step = check_crypto_account_status(broker)
    account_id = account_step.data.get("account_id", "") or "unknown"
    reported_paper = bool(account_step.data.get("paper", False))

    if account_step.status == StepStatus.ERROR or not reported_paper:
        reason = (
            "account/environment identity could not be verified "
            f"({account_step.detail})"
            if account_step.status == StepStatus.ERROR
            else (
                "get_account_info() reports paper=False despite "
                "broker.paper=True -- refusing to run the battery (never "
                "default an unverified/failed environment lookup to "
                "'paper', Codex review 2026-07-12 finding 4)"
            )
        )
        return BatteryReport(
            timestamp=timestamp,
            account_id=account_id,
            environment="unverified",
            dry_run=dry_run,
            steps=[
                account_step,
                StepResult(
                    name="environment_verification",
                    status=StepStatus.ERROR,
                    detail=reason,
                    required=True,
                ),
            ],
        )

    environment = "paper"
    steps: list[StepResult] = [account_step]

    # Step 2: pair snapshots
    logger.info("battery: checking pair snapshots for %s", pairs)
    steps.append(check_pair_snapshot(broker, pairs))

    # Step 3: GTC order acceptance (skip in dry_run)
    if dry_run:
        steps.append(StepResult(
            name="gtc_order_acceptance",
            status=StepStatus.SKIP,
            detail="dry_run=True, order placement skipped",
        ))
    else:
        logger.info("battery: testing GTC order acceptance")
        steps.append(
            _check_gtc_order_acceptance(broker, pairs, test_notional_usd)
        )

    # Step 4: stop-limit acceptance (skip in dry_run)
    if dry_run:
        steps.append(StepResult(
            name="stop_limit_acceptance",
            status=StepStatus.SKIP,
            detail="dry_run=True, order placement skipped",
        ))
    else:
        logger.info("battery: testing stop-limit order acceptance")
        steps.append(_check_stop_limit_acceptance(broker, pairs))

    # Step 5: buying power behavior (observational only, required=False)
    logger.info("battery: checking buying power behavior")
    steps.append(check_buying_power_behavior(broker))

    # Step 6: data parity (placeholder, required=False)
    steps.append(check_data_parity(pairs))

    report = BatteryReport(
        timestamp=timestamp,
        account_id=account_id,
        environment=environment,
        dry_run=dry_run,
        steps=steps,
    )
    logger.info("battery complete: %s", report.summary)
    return report


__all__ = [
    "DEFAULT_CANARY_LIMIT_BUY_FRACTION_OF_REFERENCE",
    "DEFAULT_CANARY_PAIRS",
    "DEFAULT_CANARY_STOP_LIMIT_BUFFER",
    "DEFAULT_CANARY_STOP_MULTIPLE_OF_REFERENCE",
    "DEFAULT_CANCEL_CONFIRM_POLL_INTERVAL_SECONDS",
    "DEFAULT_CANCEL_CONFIRM_TIMEOUT_SECONDS",
    "DEFAULT_TEST_NOTIONAL_USD",
    "BatteryReport",
    "StepResult",
    "StepStatus",
    "check_buying_power_behavior",
    "check_crypto_account_status",
    "check_data_parity",
    "check_pair_snapshot",
    "run_full_battery",
]
