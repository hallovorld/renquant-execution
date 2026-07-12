"""Hermetic tests for the software-stop liveness watchdog
(``renquant_execution.software_stops_liveness``).

Ported from ``RenQuant/scripts/check_software_stops_liveness.py`` per the
renquant-orchestrator#481 Codex review (2026-07-11) — the checker is a
broker/order-management runtime-monitoring concern, not umbrella ops
tooling. See the module docstring for the full ownership rationale.

What is (and is not) covered hermetically:

  * Exit-code semantics (OK/STALE/CORRUPT) and message content: covered
    fully, using a FAKE ``_PipelineStopsAPI`` adapter instead of the real
    ``renquant_pipeline.software_stops`` module. The real module pulls in
    ``renquant_pipeline``'s full package ``__init__`` (cvxpy,
    renquant-base-data, renquant-artifacts) purely as a side effect of
    Python package import semantics — none of that is needed to exercise
    THIS module's own logic (path resolution, session gating, exit-code
    mapping), and installing it here would add a heavy, unrelated
    dependency chain to this repo's CI for a thin watchdog wrapper. The
    pipeline's OWN staleness arithmetic is unit-tested in ITS repo
    (``renquant-pipeline/tests/test_software_stops.py``); this suite does
    not re-test it, only the contract this module depends on
    (``compute_staleness`` / ``_validate_snapshot`` / ``registry_path_for``
    / ``DEFAULT_REGISTRY_PATH``).
  * Market-session gating: covered against the REAL
    ``renquant_common.market_calendar`` (an existing, already-declared,
    cheap dependency of this repo) by monkeypatching its
    ``session_bounds`` function directly — no ``pandas_market_calendars``
    backend install required. The weekday-fallback branch (calendar
    backend unavailable) is covered by simulating
    ``CalendarUnavailableError``, exactly the failure mode the real
    backend raises when ``pandas_market_calendars`` is not installed.
  * NOT covered here: a live end-to-end run against the real, fully
    dependency-installed ``renquant_pipeline`` package. That is verified
    at deploy time by the orchestrator wrapper against the pinned
    checkouts (same discipline as the ported script's own "verified
    manually" note) — see renquant-orchestrator
    ``doc/progress/2026-07-11-stops-liveness-pager-package.md``.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from renquant_execution.software_stops_liveness import (
    CORRUPT,
    OK,
    REGISTRY_CORRUPT,
    REGISTRY_MISSING,
    REGISTRY_VALID,
    STALE,
    _PipelineStopsAPI,
    check,
    main,
    market_session_open,
    resolve_registry_path,
    validate_registry,
)


# --------------------------------------------------------------- fake API

def _fake_validate_snapshot(raw):
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("bad snapshot")
    return raw


def _fake_compute_staleness(snapshot, *, now, corrupt=False):
    """Minimal faithful stand-in for
    ``renquant_pipeline.software_stops.compute_staleness``: same field
    names/semantics, deliberately reimplemented small enough to audit at a
    glance (the real arithmetic is exercised in renquant-pipeline's own
    suite, not here)."""
    stops = snapshot.get("stops") or {}
    n = len(stops)
    budget = float(snapshot.get("max_staleness_minutes", 30.0))
    hb = snapshot.get("last_evaluated_at")
    age_minutes = None
    if hb:
        hb_dt = datetime.datetime.fromisoformat(hb)
        age_minutes = (now - hb_dt).total_seconds() / 60.0
    stale = n > 0 and (age_minutes is None or age_minutes > budget)
    return {
        "n_stops": n, "age_minutes": age_minutes,
        "max_staleness_minutes": budget, "stale": stale,
    }


def _fake_registry_path_for(base_path, broker_name):
    p = Path(base_path)
    if not broker_name:
        return p
    return p.with_stem(f"{p.stem}.{broker_name}")


FAKE_API = _PipelineStopsAPI(
    default_registry_path="data/rq105/software_stops.json",
    validate_snapshot=_fake_validate_snapshot,
    compute_staleness=_fake_compute_staleness,
    registry_path_for=_fake_registry_path_for,
)

MARKET_OPEN_ET = datetime.datetime(2026, 7, 13, 11, 0, tzinfo=datetime.timezone.utc)  # Mon 07:00 ET -> not open
MARKET_OPEN_NOON_ET = datetime.datetime(2026, 7, 13, 15, 0, tzinfo=datetime.timezone.utc)  # Mon 11:00 ET -> open
WEEKEND = datetime.datetime(2026, 7, 11, 15, 0, tzinfo=datetime.timezone.utc)  # Saturday


def _write_registry(tmp_path: Path, *, stops: dict, last_evaluated_at=None,
                    max_staleness_minutes=30.0) -> Path:
    path = tmp_path / "software_stops.json"
    path.write_text(json.dumps({
        "version": 1,
        "stops": stops,
        "last_evaluated_at": last_evaluated_at,
        "max_staleness_minutes": max_staleness_minutes,
    }))
    return path


# --------------------------------------------------------------- check()

def test_missing_registry_is_ok():
    code, msg = check(Path("/nonexistent/software_stops.json"), _api=FAKE_API)
    assert code == OK
    assert "no software-stop registry" in msg


def test_empty_registry_is_ok(tmp_path):
    path = _write_registry(tmp_path, stops={})
    code, msg = check(path, force_session=True, _api=FAKE_API)
    assert code == OK
    assert "0 armed stops" in msg


def test_corrupt_registry_is_corrupt(tmp_path):
    path = tmp_path / "software_stops.json"
    path.write_text("not json{{{")
    code, msg = check(path, _api=FAKE_API)
    assert code == CORRUPT
    assert "CORRUPT" in msg
    assert "OPERATOR ACTION REQUIRED" in msg


def test_bad_schema_is_corrupt(tmp_path):
    path = tmp_path / "software_stops.json"
    path.write_text(json.dumps({"version": 99, "stops": {}}))
    code, msg = check(path, _api=FAKE_API)
    assert code == CORRUPT


def test_fresh_heartbeat_in_session_is_ok(tmp_path):
    now = MARKET_OPEN_NOON_ET
    fresh = (now - datetime.timedelta(minutes=5)).isoformat()
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=fresh,
    )
    code, msg = check(path, now=now, force_session=True, _api=FAKE_API)
    assert code == OK
    assert "armed stop(s), heartbeat" in msg


def test_stale_heartbeat_in_session_is_stale(tmp_path):
    now = MARKET_OPEN_NOON_ET
    stale_ts = (now - datetime.timedelta(minutes=45)).isoformat()
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=stale_ts,
    )
    code, msg = check(path, now=now, force_session=True, _api=FAKE_API)
    assert code == STALE
    assert "UNPROTECTED" in msg


def test_never_evaluated_armed_stop_in_session_is_stale(tmp_path):
    now = MARKET_OPEN_NOON_ET
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=None,
    )
    code, msg = check(path, now=now, force_session=True, _api=FAKE_API)
    assert code == STALE


def test_stale_but_market_closed_is_ok_not_stale(tmp_path):
    """Off-session, a stale heartbeat cannot be evaluated by design — an
    armed-but-unevaluated stop off-hours is not a page."""
    now = WEEKEND
    stale_ts = (now - datetime.timedelta(hours=5)).isoformat()
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=stale_ts,
    )
    code, msg = check(path, now=now, _api=FAKE_API)
    assert code == OK
    assert "market session closed" in msg


def test_force_session_overrides_market_gate(tmp_path):
    now = WEEKEND
    stale_ts = (now - datetime.timedelta(hours=5)).isoformat()
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=stale_ts,
    )
    code, _ = check(path, now=now, force_session=True, _api=FAKE_API)
    assert code == STALE


# ------------------------------------------------------ market_session_open

def test_market_session_open_uses_injected_session_bounds(monkeypatch):
    from renquant_common import market_calendar

    class _Bounds:
        open = datetime.datetime(2026, 7, 13, 13, 30, tzinfo=datetime.timezone.utc)
        close = datetime.datetime(2026, 7, 13, 20, 0, tzinfo=datetime.timezone.utc)

    monkeypatch.setattr(market_calendar, "session_bounds", lambda day: _Bounds())
    now_in = datetime.datetime(2026, 7, 13, 15, 0, tzinfo=datetime.timezone.utc)
    now_out = datetime.datetime(2026, 7, 13, 21, 0, tzinfo=datetime.timezone.utc)
    assert market_session_open(now_in) is True
    assert market_session_open(now_out) is False


def test_market_session_open_holiday_returns_false(monkeypatch):
    from renquant_common import market_calendar

    monkeypatch.setattr(market_calendar, "session_bounds", lambda day: None)
    now = datetime.datetime(2026, 7, 13, 15, 0, tzinfo=datetime.timezone.utc)
    assert market_session_open(now) is False


def test_market_session_open_falls_back_when_calendar_backend_missing(monkeypatch):
    """When the real NYSE backend is unavailable (pandas_market_calendars not
    installed — exactly this repo's CI posture today), the checker fails
    open toward CHECKING via the weekday 09:30-16:00 ET fallback, never a
    silent miss."""
    from renquant_common import market_calendar

    def _raise(day):
        raise market_calendar.CalendarUnavailableError("no pandas_market_calendars")

    monkeypatch.setattr(market_calendar, "session_bounds", _raise)
    weekday_open = datetime.datetime(2026, 7, 13, 15, 0, tzinfo=datetime.timezone.utc)  # Mon 11:00 ET
    weekday_closed = datetime.datetime(2026, 7, 13, 10, 0, tzinfo=datetime.timezone.utc)  # Mon 06:00 ET
    weekend = datetime.datetime(2026, 7, 11, 15, 0, tzinfo=datetime.timezone.utc)  # Saturday
    assert market_session_open(weekday_open) is True
    assert market_session_open(weekday_closed) is False
    assert market_session_open(weekend) is False


# ------------------------------------------------------ resolve_registry_path

def test_resolve_registry_path_explicit_registry_wins():
    path = resolve_registry_path(registry="/explicit/path.json", data_root="/ignored", broker="alpaca")
    assert path == Path("/explicit/path.json")


def test_resolve_registry_path_composes_from_data_root():
    path = resolve_registry_path(registry=None, data_root="/root", broker="alpaca", _api=FAKE_API)
    assert path == Path("/root/data/rq105/software_stops.alpaca.json")


def test_resolve_registry_path_fails_closed_with_neither():
    """RUNTIME CONTRACT: no default may point at any repo — omitting both
    --registry and --data-root must fail closed, not silently resolve
    somewhere."""
    with pytest.raises(SystemExit):
        resolve_registry_path(registry=None, data_root=None, broker="alpaca")


# --------------------------------------------------------------------- CLI

def test_main_fails_closed_without_registry_or_data_root(capsys):
    with pytest.raises(SystemExit):
        main(["--broker", "alpaca"])


def test_main_reports_ok_for_missing_registry(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    code = main(["--registry", str(tmp_path / "nope.json")])
    assert code == OK
    out = capsys.readouterr().out
    assert "no software-stop registry" in out


def test_main_data_root_and_broker_composition(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    code = main(["--data-root", str(tmp_path), "--broker", "alpaca"])
    assert code == OK
    out = capsys.readouterr().out
    assert "no software-stop registry" in out
    assert "software_stops.alpaca.json" in out


def test_main_stale_exit_code_and_ntfy_not_posted_without_topic(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    now = MARKET_OPEN_NOON_ET
    stale_ts = (now - datetime.timedelta(minutes=45)).isoformat()
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=stale_ts,
    )
    posted = []
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._post_ntfy",
        lambda topic, msg: posted.append((topic, msg)),
    )
    code = main([
        "--registry", str(path), "--now", now.isoformat(), "--force-session",
    ])
    assert code == STALE
    assert posted == []  # no --ntfy-topic supplied -> best-effort post skipped


def test_main_posts_ntfy_on_non_ok_when_topic_supplied(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    now = MARKET_OPEN_NOON_ET
    stale_ts = (now - datetime.timedelta(minutes=45)).isoformat()
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=stale_ts,
    )
    posted = []
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._post_ntfy",
        lambda topic, msg: posted.append((topic, msg)),
    )
    code = main([
        "--registry", str(path), "--now", now.isoformat(), "--force-session",
        "--ntfy-topic", "some-topic",
    ])
    assert code == STALE
    assert len(posted) == 1
    assert posted[0][0] == "some-topic"


# ------------------------------------------------------- validate_registry()

def test_validate_registry_missing_path_is_missing():
    code, msg = validate_registry(Path("/nonexistent/software_stops.json"), _api=FAKE_API)
    assert code == REGISTRY_MISSING
    assert code == 1
    assert "MISSING" in msg
    assert "no software-stop registry file" in msg


def test_validate_registry_valid_file_is_valid(tmp_path):
    path = _write_registry(tmp_path, stops={})
    code, msg = validate_registry(path, _api=FAKE_API)
    assert code == REGISTRY_VALID
    assert code == 0
    assert "VALID" in msg


def test_validate_registry_malformed_json_is_corrupt(tmp_path):
    path = tmp_path / "software_stops.json"
    path.write_text("not json{{{")
    code, msg = validate_registry(path, _api=FAKE_API)
    assert code == REGISTRY_CORRUPT
    assert code == 2
    assert "CORRUPT" in msg


def test_validate_registry_fails_schema_is_corrupt(tmp_path):
    """Valid JSON, but fails validate_snapshot's schema check (version != 1
    per the fake API's schema, mirroring the real pipeline's contract)."""
    path = tmp_path / "software_stops.json"
    path.write_text(json.dumps({"version": 99, "stops": {}}))
    code, msg = validate_registry(path, _api=FAKE_API)
    assert code == REGISTRY_CORRUPT
    assert "CORRUPT" in msg


def test_validate_registry_never_reports_stale():
    """This mode has no staleness/session concept at all — REGISTRY_VALID
    and STALE happen to collide numerically with OK==0 but are a wholly
    separate verdict space (see module docstring)."""
    assert REGISTRY_VALID == OK == 0
    assert REGISTRY_MISSING == STALE == 1
    assert REGISTRY_CORRUPT == CORRUPT == 2


# --------------------------------------------------- CLI --validate-registry

def test_main_validate_registry_exits_0_for_valid_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    path = _write_registry(tmp_path, stops={})
    code = main(["--validate-registry", "--registry", str(path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "VALID" in out


def test_main_validate_registry_exits_1_for_missing_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    code = main(["--validate-registry", "--registry", str(tmp_path / "nope.json")])
    assert code == 1
    out = capsys.readouterr().out
    assert "MISSING" in out


def test_main_validate_registry_exits_2_for_corrupt_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    path = tmp_path / "software_stops.json"
    path.write_text("not json{{{")
    code = main(["--validate-registry", "--registry", str(path)])
    assert code == 2
    out = capsys.readouterr().out
    assert "CORRUPT" in out


def test_main_validate_registry_ignores_market_session_and_staleness(
    tmp_path, capsys, monkeypatch,
):
    """--validate-registry must never gate on market session or staleness:
    a registry with a very stale/never-evaluated armed stop, checked with
    NO --force-session and no --now override (i.e. real wall-clock time,
    whatever it is), must still report VALID/0 purely on structural
    grounds — proving this mode does not touch check()'s
    session/staleness path at all."""
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness._pipeline_stops_api",
        lambda: FAKE_API,
    )
    # Force market_session_open to explode if it's ever called by this
    # mode -- validate-registry must not reach it.
    monkeypatch.setattr(
        "renquant_execution.software_stops_liveness.market_session_open",
        lambda now: (_ for _ in ()).throw(AssertionError(
            "validate-registry must never evaluate market session"
        )),
    )
    path = _write_registry(
        tmp_path,
        stops={"AAPL": {"symbol": "AAPL", "qty": 1.0, "stop_price": 100.0, "source": "z9"}},
        last_evaluated_at=None,  # would be STALE under check()'s semantics
    )
    code = main(["--validate-registry", "--registry", str(path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "VALID" in out


# ------------------------------------------------ real pipeline (optional)

def test_pipeline_stops_api_contract_if_pipeline_installed():
    """If renquant_pipeline happens to be importable in this environment
    (e.g. a developer's shared venv), assert the real module still exposes
    the exact symbols this checker's lazy import depends on — a drift
    tripwire. Skipped (not failed) when the package isn't installed, since
    this repo's CI deliberately does not install pipeline's heavy
    dependency chain for this thin watchdog module (see file docstring)."""
    pytest.importorskip("renquant_pipeline")
    from renquant_execution.software_stops_liveness import _pipeline_stops_api

    api = _pipeline_stops_api()
    assert isinstance(api.default_registry_path, str)
    assert callable(api.validate_snapshot)
    assert callable(api.compute_staleness)
    assert callable(api.registry_path_for)
