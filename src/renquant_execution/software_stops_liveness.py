#!/usr/bin/env python
"""Software-stop registry liveness watchdog (S-FRAC stage 3 ops).

Ownership: this is a broker/order-management RUNTIME-MONITORING concern —
"is the layer that protects fractional positions still alive" — not a
pipeline decision-time concern (arming/sizing a stop) and not a training
concern. It lives here, in ``renquant-execution``, per the
renquant-orchestrator#481 Codex review (2026-07-11): the prior umbrella
script (``RenQuant/scripts/check_software_stops_liveness.py``) created a new
production scheduling/runtime dependency on the deprecated umbrella and its
venv, which is the exact anti-pattern the multi-repo split exists to avoid.

This module is a faithful port of that umbrella script's watchdog logic —
market-session gating, staleness computation, nagios-style exit codes. It is
NOT a re-derivation: the staleness *arithmetic* still lives in
``renquant_pipeline.software_stops`` (moved there 2026-07-04, RenQuant#440 —
settled, not revisited here) so the checker and the sell-only loop's
heartbeat-stamper can never disagree. This module only owns: resolving the
registry path, the market-session gate, and turning the pipeline's staleness
verdict into an operator-facing message + exit code.

Ownership split (unchanged by this port):
  * renquant-pipeline  — the registry DATA MODEL + staleness arithmetic
    (``software_stops.py``) and the decision-time arming task
    (``kernel/pipeline/task_software_stops.py``). Untouched.
  * renquant-execution (HERE) — the liveness CHECKER: is the loop that
    evaluates armed stops still alive during a market session.
  * renquant-orchestrator — the pinned DEPLOYMENT/SCHEDULE (launchd plist)
    and the notification-consumer wrapper that invokes this module's CLI
    through the pinned sibling-checkout PYTHONPATH convention and pages ntfy
    on a non-OK exit. Does not reimplement any of this module's logic.

Cross-repo dependency direction: this module depends on
``renquant_pipeline.software_stops`` for registry parsing/staleness math.
Schema validation specifically goes through pipeline's PUBLIC, versioned
``validate_software_stop_snapshot()`` contract (software-stops-v1) — not
the module's private ``_validate_snapshot`` — per the Codex review on
this exact chain (renquant-execution#30, 2026-07-12T11:57:53Z): "The
schema is pipeline-owned... orchestrator -> execution public CLI ->
pipeline public schema API; no consumer reaches through a private
boundary." See renquant-pipeline#192 (round 8) for the contract this
module now consumes. That import is DEFERRED (module-level import would
drag in ``renquant_pipeline``'s full package ``__init__`` — cvxpy,
renquant-base-data, renquant-artifacts — the same lazy-import discipline
this repo already applies to optional heavy deps like ``alpaca-py``, see
``igv_short_monitor.get_market()``). See ``_pipeline_stops_api()`` below.
Hermetic tests inject a fake adapter instead of requiring the real
(dependency-heavy) pipeline package to be installed — see
``tests/test_software_stops_liveness.py`` for what that trades off.

Exit codes (nagios-ish, consumable by any wrapper — unchanged contract from
the ported umbrella script). This is the DEFAULT ("check") mode:
    0  OK        — no registry / no armed stops / heartbeat fresh /
                   market closed (nothing can be evaluated off-session)
    1  STALE     — armed stops exist and the heartbeat is missing or
                   older than max_staleness_minutes during a session
    2  CORRUPT   — the registry file exists but cannot be read/validated:
                   registered stops are UNKNOWABLE and new fractional
                   entries are already fail-closed by the stage-0 gate

``--validate-registry`` mode is a SEPARATE verdict space — structural
registry validity only, never staleness or market session (see
``validate_registry()`` below for why this exists as a public, versioned
boundary):
    0  REGISTRY_VALID    — file exists and is a well-formed, schema-valid
                            software-stop registry
    1  REGISTRY_MISSING  — no registry file at the resolved path
    2  REGISTRY_CORRUPT  — file exists but is unreadable or fails schema
                            validation

RUNTIME CONTRACT (same discipline as orchestrator's shadow_ab_daily.sh,
Codex r2 on orchestrator#460): no default here points at the deprecated
umbrella or at any sibling directory. The registry location is an EXPLICIT
caller-supplied value (``--registry`` or ``--data-root``); there is no
built-in fallback path. Today the live sell-only loop still writes the
registry under the (still-umbrella-anchored) runtime data root — migrating
that anchor is R-PIN territory, out of scope here. The caller (the
orchestrator wrapper script today) supplies whatever root is currently
correct as an explicit argument/environment value.

Run standalone (with PYTHONPATH pointed at the pinned renquant-pipeline and
renquant-common checkouts' ``src/`` — see renquant-orchestrator
``scripts/stops_liveness_pager.sh``):

    python -m renquant_execution.software_stops_liveness \\
        --data-root /path/to/runtime/root --broker alpaca

Or, to only answer "does a real, schema-valid registry exist here" without
evaluating staleness/market session (the public boundary a caller in
another repo — e.g. renquant-orchestrator's install-time arming guard —
should use instead of reaching into this module's private
``_pipeline_stops_api()``; see renquant-orchestrator#481, Codex,
2026-07-12T11:33:56Z):

    python -m renquant_execution.software_stops_liveness \\
        --validate-registry --data-root /path/to/runtime/root --broker alpaca

Optional ``--ntfy-topic`` posts the alarm to ntfy.sh directly (best-effort,
kept for CLI parity with the ported script). The orchestrator wrapper does
NOT use this flag: it owns paging itself (via ``curl -f``) so a delivery
failure is detectable and a checker *crash* also pages instead of dying
dark — see the orchestrator progress doc for that design decision.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple

OK, STALE, CORRUPT = 0, 1, 2

# Separate verdict space for ``--validate-registry`` mode (structural
# validity only — never staleness or market session). Deliberately NOT
# aliased to OK/STALE/CORRUPT above even though the integers coincide: the
# two modes answer different questions and must be able to diverge in
# meaning without becoming a silent renumbering hazard. See
# ``validate_registry()`` for the rationale.
REGISTRY_VALID, REGISTRY_MISSING, REGISTRY_CORRUPT = 0, 1, 2


class _PipelineStopsAPI(NamedTuple):
    """The exact subset of ``renquant_pipeline.software_stops`` this checker
    depends on. Bundled as a NamedTuple so tests can inject a fake instance
    without requiring the real (dependency-heavy) pipeline package."""

    default_registry_path: str
    validate_snapshot: Callable[[Any], dict]
    compute_staleness: Callable[..., dict]
    registry_path_for: Callable[[Any, "str | None"], Path]


def _pipeline_stops_api() -> _PipelineStopsAPI:
    """Deferred import of the pipeline's registry module (see module
    docstring for why this is lazy, not top-level).

    Schema validation is bound to pipeline's PUBLIC
    ``validate_software_stop_snapshot`` contract, not the private
    ``_validate_snapshot`` — see the module docstring's cross-repo
    dependency paragraph and renquant-pipeline#192 (round 8, Codex review
    on renquant-execution#30, 2026-07-12T11:57:53Z). This deferred import
    only succeeds once that pipeline PR has merged to the pinned
    ``renquant_pipeline`` checkout; until then it raises ImportError,
    which is the correct, expected state for an as-yet-unmerged public
    contract dependency (not a bug to work around with a fallback)."""
    from renquant_pipeline.software_stops import (  # noqa: PLC0415
        DEFAULT_REGISTRY_PATH,
        compute_staleness,
        registry_path_for,
        validate_software_stop_snapshot,
    )

    return _PipelineStopsAPI(
        default_registry_path=DEFAULT_REGISTRY_PATH,
        validate_snapshot=validate_software_stop_snapshot,
        compute_staleness=compute_staleness,
        registry_path_for=registry_path_for,
    )


def market_session_open(now: datetime.datetime) -> bool:
    """True when the NYSE regular session is plausibly open.

    Uses the canonical ``renquant_common.market_calendar`` (an existing,
    already-declared dependency of this repo — unlike ``renquant_pipeline``
    this import is cheap and not deferred); falls back to weekday
    09:30-16:00 America/New_York if that module is unavailable (fail-open
    toward CHECKING: a holiday false-positive produces a spurious page,
    never a missed one). Ported verbatim from the umbrella checker's
    ``market_session_open`` (campaign B5 equivalence-proven logic).
    """
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        now_et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        now_et = now
    try:
        from renquant_common.market_calendar import session_bounds  # noqa: PLC0415

        bounds = session_bounds(now_et.date())
        if bounds is None:
            return False
        now_ts = now.astimezone(datetime.timezone.utc)
        return bool(bounds.open <= now_ts <= bounds.close)
    except Exception:
        if now_et.weekday() >= 5:
            return False
        minutes = now_et.hour * 60 + now_et.minute
        return (9 * 60 + 30) <= minutes <= (16 * 60)


def check(
    registry_path: Path,
    *,
    now: "datetime.datetime | None" = None,
    force_session: bool = False,
    _api: "_PipelineStopsAPI | None" = None,
) -> "tuple[int, str]":
    """Pure check body (unit-tested): returns (exit_code, message).

    ``_api`` is test-only dependency injection for the pipeline registry
    module; production callers omit it and get the real lazy import.
    """
    api = _api or _pipeline_stops_api()
    now_dt = now or datetime.datetime.now().astimezone()
    if not registry_path.exists():
        return OK, (
            f"OK: no software-stop registry at {registry_path} — the layer "
            "has never armed a stop (flag off or no fractional positions)."
        )
    try:
        snapshot = api.validate_snapshot(json.loads(registry_path.read_text()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return CORRUPT, (
            f"CORRUPT: software-stop registry {registry_path} unreadable "
            f"({type(exc).__name__}: {exc}). Registered stops are "
            "UNKNOWABLE; new fractional entries are fail-closed by the "
            "stage-0 capability gate. OPERATOR ACTION REQUIRED: inspect / "
            "quarantine the file, verify positions are protected or flat."
        )
    state = api.compute_staleness(snapshot, now=now_dt)
    n = state["n_stops"]
    age = state["age_minutes"]
    age_str = f"{age:.1f}m" if age is not None else "never"
    if n == 0:
        return OK, (
            f"OK: registry {registry_path} has 0 armed stops "
            f"(heartbeat age: {age_str}) — nothing unprotected."
        )
    if not force_session and not market_session_open(now_dt):
        return OK, (
            f"OK: market session closed — {n} armed stop(s) cannot be "
            f"evaluated off-session by design (heartbeat age: {age_str}). "
            "Overnight gap risk parity is the design's §3.3 analysis."
        )
    if state["stale"]:
        return STALE, (
            f"STALE: {n} ARMED software stop(s) in {registry_path} but the "
            f"sell-only loop has not evaluated the registry for {age_str} "
            f"(budget: {state['max_staleness_minutes']:.0f}m) during a "
            "market session. Positions are UNPROTECTED until the loop "
            "returns — restart the intraday loop or manually flatten/"
            "hedge (respond promptly per the operator runbook)."
        )
    return OK, (
        f"OK: {n} armed stop(s), heartbeat {age_str} old "
        f"(budget {state['max_staleness_minutes']:.0f}m)."
    )


def validate_registry(
    registry_path: Path, *, _api: "_PipelineStopsAPI | None" = None,
) -> "tuple[int, str]":
    """Public, versioned, narrow validation boundary: "does a real,
    schema-valid software-stop registry exist at this path?" — nothing
    more (no staleness, no market-session evaluation; use ``check()`` for
    that).

    This exists specifically so callers OUTSIDE this repo (notably
    renquant-orchestrator's install-time arming guard,
    ``scripts/install_stops_pager.sh``) have a stable, documented surface
    to depend on instead of importing this module's private
    ``_pipeline_stops_api()`` (or any other private name) across the repo
    boundary. Per the renquant-orchestrator#481 Codex review
    (2026-07-12T11:33:56Z): a leading-underscore name is an
    implementation detail that can be refactored without a compatibility
    guarantee, so a cross-repo fail-closed safety guard must not depend on
    it — it must go through a public CLI/API surface with a stable
    verdict/exit-code contract instead. ``validate_registry()`` (and its
    ``--validate-registry`` CLI mode below) IS that surface.

    ``_api`` is test-only dependency injection for the pipeline registry
    module, same pattern as ``check()``; production callers omit it and
    get the real lazy import.
    """
    api = _api or _pipeline_stops_api()
    if not registry_path.exists():
        return REGISTRY_MISSING, (
            f"MISSING: no software-stop registry file at {registry_path}"
        )
    try:
        api.validate_snapshot(json.loads(registry_path.read_text()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return REGISTRY_CORRUPT, (
            f"CORRUPT: {registry_path} unreadable or fails schema "
            f"validation ({type(exc).__name__}: {exc})"
        )
    return REGISTRY_VALID, (
        f"VALID: {registry_path} is a well-formed software-stop registry"
    )


def _post_ntfy(topic: str, message: str) -> None:
    """Best-effort direct ntfy post — kept for CLI parity with the ported
    umbrella script. NOT used by the orchestrator wrapper, which owns
    paging itself so delivery failure is detectable (exit 70) and a
    checker crash pages instead of dying dark."""
    try:
        from urllib import request  # noqa: PLC0415

        req = request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode(),
            headers={"Title": "RenQuant SOFTWARE-STOP watchdog"},
        )
        request.urlopen(req, timeout=10)
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        print(f"(ntfy post failed: {exc})", file=sys.stderr)


def resolve_registry_path(
    *,
    registry: "str | None",
    data_root: "str | None",
    broker: "str | None",
    _api: "_PipelineStopsAPI | None" = None,
) -> Path:
    """Fail-closed path resolution — no default points at any repo.

    Exactly one of ``registry`` (a full explicit path) or ``data_root``
    (composed with the pipeline's broker-tagged relative default) must be
    supplied by the caller.
    """
    if registry:
        return Path(registry)
    if not data_root:
        raise SystemExit(
            "usage error: supply --registry <path> OR --data-root <path> "
            "(no default registry location — RUNTIME CONTRACT, see module "
            "docstring)."
        )
    api = _api or _pipeline_stops_api()
    return api.registry_path_for(Path(data_root) / api.default_registry_path, broker)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--registry", default=None,
        help="full explicit registry file path (highest precedence)",
    )
    ap.add_argument(
        "--data-root", default=None,
        help="runtime data root the registry lives under; composed with "
             "the pipeline's broker-tagged relative default path. Required "
             "if --registry is not given (no built-in default).",
    )
    ap.add_argument(
        "--broker", default="alpaca",
        help="broker tag for the registry filename (default: alpaca)",
    )
    ap.add_argument(
        "--now", default=None,
        help="ISO timestamp override for the current time (tests)",
    )
    ap.add_argument(
        "--force-session", action="store_true",
        help="skip the market-session check (treat as in-session)",
    )
    ap.add_argument(
        "--ntfy-topic", default=None,
        help="post STALE/CORRUPT alarms directly to this ntfy.sh topic "
             "(best-effort; the orchestrator wrapper does not use this — "
             "it owns paging itself)",
    )
    ap.add_argument(
        "--validate-registry", action="store_true",
        help="run the public structural-validity check only (VALID/MISSING"
             "/CORRUPT; see validate_registry()) instead of the default "
             "staleness/market-session liveness check — never gates on "
             "time-of-day or market session",
    )
    args = ap.parse_args(argv)

    path = resolve_registry_path(
        registry=args.registry, data_root=args.data_root, broker=args.broker,
    )

    if args.validate_registry:
        code, message = validate_registry(path)
        print(message)
        return code

    now = datetime.datetime.fromisoformat(args.now) if args.now else None
    code, message = check(path, now=now, force_session=args.force_session)
    print(message)
    if code != OK and args.ntfy_topic:
        _post_ntfy(args.ntfy_topic, message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
