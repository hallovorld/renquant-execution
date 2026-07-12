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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .alpaca_broker import AlpacaBroker
from .crypto import CryptoAssetSpec, is_crypto_pair

logger = logging.getLogger(__name__)

# ── canary configuration ────────────────────────────────────────────────────

#: Default canary pairs for the battery.
DEFAULT_CANARY_PAIRS: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")

#: Test notional for battery canary orders ($1.10 — above the $1 minimum,
#: small enough to be immaterial on paper).
DEFAULT_TEST_NOTIONAL_USD: float = 1.10

#: Limit price floor for canary limit-buy orders — set far below any
#: conceivable market price so the order never fills (it will be cancelled
#: immediately).
_CANARY_LIMIT_BUY_PRICE: float = 0.01

#: Stop/limit prices for canary BUY stop-limit orders — set far above any
#: conceivable market price so the stop never triggers (cancelled immediately).
#: NOTE: these prices are deliberately implausible and outside broker price
#: bands.  The stop-limit acceptance check is a METADATA/CAPABILITY check —
#: it validates the broker accepts the GTC stop-limit ORDER TYPE for each
#: canary pair, NOT that any particular stop/limit price will trigger or
#: fill.  Empirical stop-limit execution quality is tested elsewhere (the
#: production protective-stop path and its liveness checker).
_CANARY_STOP_LIMIT_STOP_PRICE: float = 999_999_999.00
_CANARY_STOP_LIMIT_LIMIT_PRICE: float = 999_999_999.00

#: Timeout/poll interval for confirming a canary order's cancellation
#: actually reached a terminal ``canceled`` state (same "confirm, don't
#: assume" discipline Codex required on PR #31's
#: ``AlpacaBroker.replace_crypto_stop_limit`` -- a cancel *request* that
#: didn't raise is not proof the order is gone; it can still be resting or
#: have filled/rejected before the cancel took effect). Applied proactively
#: here, ahead of review, to the two order-placing battery steps below.
DEFAULT_CANCEL_CONFIRM_TIMEOUT_SECONDS: float = 5.0
DEFAULT_CANCEL_CONFIRM_POLL_INTERVAL_SECONDS: float = 0.25

#: Broker order statuses that indicate the order was genuinely accepted and
#: is resting (eligible for subsequent cancel).  Any status outside this set
#: after a place call means the order was not cleanly accepted.
_ACCEPTED_ORDER_STATUSES: frozenset[str] = frozenset({
    "accepted", "new", "held", "partially_filled",
})

#: Statuses that prove the order was rejected or already terminal at place
#: time — the probe should FAIL, not silently pass.
_REJECTED_OR_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "rejected", "expired", "filled", "canceled", "replaced",
    "done_for_day", "stopped", "suspended", "calculated",
})


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

    Steps marked ``required=True`` (the default) must PASS for the battery
    to report ``all_passed``.  Steps marked ``required=False`` are
    *optional* — they contribute to the report but a SKIP or FAIL on an
    optional step does not block the overall battery verdict.
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
        """True when every *required* step is PASS.

        Optional steps (``required=False``) are excluded from the gate —
        a SKIP or FAIL on an optional step does not block the overall
        battery verdict.  This prevents a structural SKIP (e.g.
        ``check_data_parity``, which has no data source wired yet) from
        making a full battery PASS permanently impossible.
        """
        return all(
            s.status == StepStatus.PASS
            for s in self.steps
            if s.required
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


def _validate_order_acceptance(
    result: dict[str, Any],
    *,
    expected_side: str,
    expected_tif: str,
    pair: str,
) -> str | None:
    """Validate that a placed order's returned fields match the probe request.

    Returns ``None`` if the order looks correctly accepted, or a human-readable
    failure reason if any field is wrong.  This prevents treating a nonempty
    ``order_id`` as proof of acceptance when the order was actually rejected,
    filled, expired, or has mismatched side/TIF.
    """
    order_id = result.get("order_id", "")
    if not order_id:
        return f"{pair}: order accepted but no order_id returned"

    status = str(result.get("status", "")).strip().lower()
    if status in _REJECTED_OR_TERMINAL_STATUSES:
        return (
            f"{pair}: order {order_id} returned with terminal/rejected "
            f"status={status!r} (expected an accepted/resting status)"
        )
    if status and status not in _ACCEPTED_ORDER_STATUSES:
        # Unknown/pending status -- not necessarily wrong, but flag it.
        logger.warning(
            "battery: %s order %s has unexpected status %r (not in "
            "accepted set %r); proceeding cautiously",
            pair, order_id, status, _ACCEPTED_ORDER_STATUSES,
        )

    side = str(result.get("side", "") or result.get("action", "")).strip().upper()
    if side and side != expected_side.upper():
        return (
            f"{pair}: order {order_id} side={side!r} does not match "
            f"requested side={expected_side!r}"
        )

    tif = str(result.get("time_in_force", "")).strip().lower()
    if tif and tif != expected_tif.lower():
        return (
            f"{pair}: order {order_id} time_in_force={tif!r} does not match "
            f"requested tif={expected_tif!r}"
        )

    return None


def _confirm_cancel_with_evidence(
    broker: AlpacaBroker,
    order_id: str,
    pair: str,
    *,
    cancel_confirm_timeout_seconds: float,
    cancel_confirm_poll_interval_seconds: float,
    order_details: dict[str, Any],
) -> tuple[bool, str | None]:
    """Request cancellation and poll until terminal state, returning evidence.

    Always polls for terminal state even if ``cancel_order`` raises — a
    cancel request that raises might still have been processed by the broker.
    An order that remains resting or fills is a Tier-1 cleanup failure.

    Returns (cancel_confirmed, failure_reason).
    """
    cancel_raised: str | None = None
    try:
        broker.cancel_order(order_id)
    except Exception as cancel_exc:
        cancel_raised = str(cancel_exc)
        logger.warning(
            "battery: cancel of %s order %s raised: %s",
            pair, order_id, cancel_exc,
        )

    # Always poll for terminal state — even if cancel_order raised, the
    # cancellation may have been processed asynchronously.
    cancel_confirmed = broker.wait_for_order_terminal_cancel(
        order_id,
        timeout_seconds=cancel_confirm_timeout_seconds,
        poll_interval_seconds=cancel_confirm_poll_interval_seconds,
    )

    # Record durable evidence in step result data.
    order_details["cancel_confirmed"] = cancel_confirmed
    if cancel_raised is not None:
        order_details["cancel_exception"] = cancel_raised

    if not cancel_confirmed:
        reason = (
            f"cancel_order raised: {cancel_raised}"
            if cancel_raised is not None
            else (
                f"cancellation of order {order_id} not confirmed "
                f"terminally canceled within "
                f"{cancel_confirm_timeout_seconds}s"
            )
        )
        failure = (
            f"{pair}: order {order_id} cancellation NOT confirmed "
            f"({reason}) -- order may still be resting/filled"
        )
        return False, failure

    return True, None


def check_gtc_order_acceptance(
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

    For each canary pair, places a GTC limit-buy order at a price far below
    market ($0.01) so it will never fill, verifies the broker accepts it
    (validating status, side, and TIF match the request), and then cancels
    it -- CONFIRMING the cancellation actually reached a terminal
    ``canceled`` state via
    :meth:`AlpacaBroker.wait_for_order_terminal_cancel` (same "confirm, don't
    assume" discipline Codex required on PR #31's
    ``replace_crypto_stop_limit``). A cancel request that doesn't raise is
    NOT proof the order is gone -- it can still be resting, or have
    filled/rejected before the cancel took effect -- so an unconfirmed
    cancellation is a Tier-1 FAIL for the affected pair/order, never a
    silent PASS.
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

        # Compute a qty from the test notional at the canary price — must
        # meet the pair's min_order_size.
        raw_qty = test_notional_usd / _CANARY_LIMIT_BUY_PRICE
        # The qty will be huge at $0.01 but that's fine — the order won't fill.
        # Just ensure it meets min_order_size.
        qty = max(raw_qty, spec.min_order_size)

        order_id = None
        placed = False
        try:
            result = broker.place_crypto_limit_order(
                symbol=pair,
                action="BUY",
                qty=qty,
                limit_price=_CANARY_LIMIT_BUY_PRICE,
                time_in_force="gtc",
            )
            order_id = result.get("order_id", "")
            order_details[pair] = {
                "order_id": order_id,
                "status": result.get("status", ""),
                "side": result.get("side", ""),
                "time_in_force": result.get("time_in_force", ""),
                "qty": result.get("quantity", 0.0),
                "limit_price": result.get("limit_price", 0.0),
            }

            # Validate the returned order matches our request — a nonempty
            # order_id alone is NOT proof of acceptance (fix: review item #2).
            validation_failure = _validate_order_acceptance(
                result,
                expected_side="BUY",
                expected_tif="gtc",
                pair=pair,
            )
            if validation_failure is not None:
                failures.append(validation_failure)
                # Still need to cancel if we got an order_id.
                if not order_id:
                    continue
                # Fall through to the finally block for cleanup.
            else:
                placed = True
        except Exception as exc:
            failures.append(f"{pair}: place failed ({exc})")
            continue
        finally:
            # Always try to cancel if we got an order_id, and CONFIRM the
            # cancellation reached a terminal state before treating this
            # pair as a clean PASS.
            if order_id:
                cancel_ok, cancel_failure = _confirm_cancel_with_evidence(
                    broker,
                    order_id,
                    pair,
                    cancel_confirm_timeout_seconds=cancel_confirm_timeout_seconds,
                    cancel_confirm_poll_interval_seconds=cancel_confirm_poll_interval_seconds,
                    order_details=order_details.setdefault(pair, {}),
                )
                if placed and not cancel_ok:
                    failures.append(cancel_failure)  # type: ignore[arg-type]
                elif placed and cancel_ok:
                    placed_and_cancelled.append(pair)

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


def check_stop_limit_acceptance(
    broker: AlpacaBroker,
    pairs: tuple[str, ...] = DEFAULT_CANARY_PAIRS,
    *,
    cancel_confirm_timeout_seconds: float = DEFAULT_CANCEL_CONFIRM_TIMEOUT_SECONDS,
    cancel_confirm_poll_interval_seconds: float = (
        DEFAULT_CANCEL_CONFIRM_POLL_INTERVAL_SECONDS
    ),
) -> StepResult:
    """Place and immediately cancel small GTC stop-limit BUY orders.

    **METADATA/CAPABILITY CHECK** — this step validates that the broker
    accepts the GTC stop-limit ORDER TYPE for each canary pair.  The probe
    prices ($999,999,999 stop/limit) are deliberately implausible and
    outside broker price bands; they prove the broker recognises the order
    type, NOT that any particular stop/limit price will trigger or fill.
    Empirical stop-limit execution quality is tested elsewhere (the
    production protective-stop path and its liveness checker).

    For each canary pair, places a GTC stop-limit BUY order at unreachable
    prices (stop far above market, so the stop never triggers), validates
    the returned order status/side/TIF match the request, and then cancels
    it -- CONFIRMING the cancellation actually reached a terminal
    ``canceled`` state via
    :meth:`AlpacaBroker.wait_for_order_terminal_cancel` (same "confirm, don't
    assume" discipline Codex required on PR #31's
    ``replace_crypto_stop_limit``). BUY-side stop-limits avoid the need for a
    held position (which SELL-side would require due to E11 no-short). An
    unconfirmed cancellation is a Tier-1 FAIL for the affected pair/order,
    never a silent PASS.
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

        qty = spec.min_order_size
        order_id = None
        placed = False
        try:
            result = broker.place_crypto_stop_limit_order(
                symbol=pair,
                action="BUY",
                qty=qty,
                stop_price=_CANARY_STOP_LIMIT_STOP_PRICE,
                limit_price=_CANARY_STOP_LIMIT_LIMIT_PRICE,
                time_in_force="gtc",
            )
            order_id = result.get("order_id", "")
            order_details[pair] = {
                "order_id": order_id,
                "status": result.get("status", ""),
                "side": result.get("side", ""),
                "time_in_force": result.get("time_in_force", ""),
                "qty": result.get("quantity", 0.0),
                "stop_price": result.get("stop_price", 0.0),
                "limit_price": result.get("limit_price", 0.0),
                "check_type": "metadata_capability",
            }

            # Validate the returned order matches our request — a nonempty
            # order_id alone is NOT proof of acceptance (fix: review item #2).
            validation_failure = _validate_order_acceptance(
                result,
                expected_side="BUY",
                expected_tif="gtc",
                pair=pair,
            )
            if validation_failure is not None:
                failures.append(validation_failure)
                if not order_id:
                    continue
            else:
                placed = True
        except Exception as exc:
            failures.append(f"{pair}: place failed ({exc})")
            continue
        finally:
            if order_id:
                cancel_ok, cancel_failure = _confirm_cancel_with_evidence(
                    broker,
                    order_id,
                    pair,
                    cancel_confirm_timeout_seconds=cancel_confirm_timeout_seconds,
                    cancel_confirm_poll_interval_seconds=cancel_confirm_poll_interval_seconds,
                    order_details=order_details.setdefault(pair, {}),
                )
                if placed and not cancel_ok:
                    failures.append(cancel_failure)  # type: ignore[arg-type]
                elif placed and cancel_ok:
                    placed_and_cancelled.append(pair)

    if failures:
        return StepResult(
            name=name,
            status=StepStatus.FAIL,
            detail=(
                f"{len(failures)}/{len(pairs)} stop-limit orders failed: "
                f"{'; '.join(failures)}"
            ),
            data={"orders": order_details, "check_type": "metadata_capability"},
        )
    return StepResult(
        name=name,
        status=StepStatus.PASS,
        detail=(
            f"{len(placed_and_cancelled)} GTC stop-limit BUY orders "
            f"placed+cancelled (cancellation confirmed) -- "
            f"METADATA/CAPABILITY check (implausible probe prices, "
            f"not empirical fill proof): "
            f"{', '.join(placed_and_cancelled)}"
        ),
        data={"orders": order_details, "check_type": "metadata_capability"},
    )


def check_buying_power_behavior(broker: AlpacaBroker) -> StepResult:
    """Report crypto buying power fields (OBSERVATIONAL, not a gate).

    Crypto on Alpaca is NOT marginable — buying power for crypto should equal
    the non-marginable buying power (cash, not leveraged). This step reports
    the account's buying_power and non_marginable_buying_power for operator
    inspection.

    **OBSERVATIONAL ONLY**: this step does not validate the crypto
    non-marginable invariant (NMBP == cash) because the relationship between
    these fields is account-type-dependent and cannot be reliably asserted
    from a single snapshot.  The operator should review the reported values
    for consistency.  A future version may add a dedicated non-marginable
    invariant check once the expected relationship is confirmed across all
    Alpaca account types.
    """
    name = "buying_power_behavior"
    try:
        info = broker.get_account_info()
    except Exception as exc:
        return StepResult(
            name=name,
            status=StepStatus.ERROR,
            detail=f"get_account_info() failed: {exc}",
        )
    bp = info.get("buying_power", 0.0)
    nmbp = info.get("non_marginable_buying_power", 0.0)
    cash = info.get("cash", 0.0)

    data = {
        "buying_power": bp,
        "non_marginable_buying_power": nmbp,
        "cash": cash,
        "check_type": "observational",
    }

    # Report the relationship for the operator to verify.
    # OBSERVATIONAL: NMBP >= 0 alone does not prove the crypto non-marginable
    # invariant holds — it only shows the field is populated.
    return StepResult(
        name=name,
        status=StepStatus.PASS,
        detail=(
            f"OBSERVATIONAL (not a gate): "
            f"non_marginable_buying_power={nmbp}, "
            f"buying_power={bp}, cash={cash}"
        ),
        data=data,
    )


def check_data_parity(
    pairs: tuple[str, ...] = DEFAULT_CANARY_PAIRS,
) -> StepResult:
    """Two-source price comparison for canary pairs (OPTIONAL).

    This step is a PLACEHOLDER: the AlpacaBroker adapter wraps the Trading
    API, not the Market Data API. A proper two-source price comparison
    requires either a CryptoHistoricalDataClient or an external data source
    -- both of which are outside the execution repo's boundary (execution owns
    broker mutation, not data feeds). The orchestrator or data repo should
    wire this step once data-feed infrastructure is in place.

    Marked ``required=False`` so its structural SKIP does not make a full
    battery PASS permanently impossible.  Once a data source is wired and
    the check actually runs, it should be promoted to ``required=True``.
    """
    name = "data_parity"
    return StepResult(
        name=name,
        status=StepStatus.SKIP,
        detail=(
            f"OPTIONAL: data parity check requires a market-data source "
            f"outside the execution repo boundary "
            f"(pairs: {', '.join(pairs)}); "
            "placeholder -- wire when data-feed infrastructure is available"
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

    # Get account info for the report header.
    # SAFETY: a failed account lookup or unverified environment is a FAIL,
    # NEVER a silent default to "paper".  The battery places orders and must
    # be certain it is running on paper before proceeding.
    try:
        info = broker.get_account_info()
        account_id = info.get("account_id", "unknown")
        paper_flag = info.get("paper")
        if paper_flag is True:
            environment = "paper"
        elif paper_flag is False:
            environment = "live"
        else:
            environment = "unknown"
    except Exception as exc:
        # Cannot verify environment — report FAIL immediately.
        return BatteryReport(
            timestamp=timestamp,
            account_id="unknown",
            environment="unknown",
            dry_run=dry_run,
            steps=[
                StepResult(
                    name="environment_verification",
                    status=StepStatus.FAIL,
                    detail=(
                        f"account lookup failed, cannot verify paper "
                        f"environment: {exc}"
                    ),
                )
            ],
        )

    if environment != "paper":
        return BatteryReport(
            timestamp=timestamp,
            account_id=account_id,
            environment=environment,
            dry_run=dry_run,
            steps=[
                StepResult(
                    name="environment_verification",
                    status=StepStatus.FAIL,
                    detail=(
                        f"environment={environment!r} (paper flag={paper_flag!r}); "
                        f"battery requires verified paper environment, "
                        f"refusing to proceed"
                    ),
                )
            ],
        )

    steps: list[StepResult] = []

    # Step 1: account status
    logger.info("battery: checking crypto account status")
    steps.append(check_crypto_account_status(broker))

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
        steps.append(check_gtc_order_acceptance(broker, pairs, test_notional_usd))

    # Step 4: stop-limit acceptance (skip in dry_run)
    if dry_run:
        steps.append(StepResult(
            name="stop_limit_acceptance",
            status=StepStatus.SKIP,
            detail="dry_run=True, order placement skipped",
        ))
    else:
        logger.info("battery: testing stop-limit order acceptance")
        steps.append(check_stop_limit_acceptance(broker, pairs))

    # Step 5: buying power behavior
    logger.info("battery: checking buying power behavior")
    steps.append(check_buying_power_behavior(broker))

    # Step 6: data parity (placeholder)
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
    "DEFAULT_CANARY_PAIRS",
    "DEFAULT_CANCEL_CONFIRM_POLL_INTERVAL_SECONDS",
    "DEFAULT_CANCEL_CONFIRM_TIMEOUT_SECONDS",
    "DEFAULT_TEST_NOTIONAL_USD",
    "BatteryReport",
    "StepResult",
    "StepStatus",
    "check_buying_power_behavior",
    "check_crypto_account_status",
    "check_data_parity",
    "check_gtc_order_acceptance",
    "check_pair_snapshot",
    "check_stop_limit_acceptance",
    "run_full_battery",
]
