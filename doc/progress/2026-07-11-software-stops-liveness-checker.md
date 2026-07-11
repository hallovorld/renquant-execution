# Software-stop liveness checker — moved out of the umbrella, into execution

STATUS: delivered (new module + tests; nothing scheduled — no runtime change)
DATE:   2026-07-11
PR:     (this PR)
CONTEXT: renquant-orchestrator#481 Codex review (2026-07-11):

> This is not aligned with the multi-repo target despite living in
> orchestrator: the proposed installed path invokes
> RenQuant/scripts/check_software_stops_liveness.py through RenQuant/.venv
> and writes logs under the umbrella. That creates a new production
> scheduling/runtime dependency on the deprecated umbrella.
>
> Split the ownership correctly: the software-stop registry/liveness
> checker and its schema belong to renquant-execution [...] orchestrator
> may own the pinned deployment/schedule and notification consumer, but
> must invoke a versioned execution-repo CLI/API [...]

## What this PR does

Ports the watchdog/liveness-CHECKING logic — as opposed to the registry
data model or the decision-time arming task, both of which stay in
`renquant-pipeline` (settled by RenQuant#440, 2026-07-04, not revisited
here) — from the umbrella's `RenQuant/scripts/check_software_stops_liveness.py`
into a new module here:

- `src/renquant_execution/software_stops_liveness.py` — market-session
  gating (via the canonical `renquant_common.market_calendar`), staleness
  computation (delegates to `renquant_pipeline.software_stops`, lazily
  imported), and the nagios-style exit-code contract (0 OK / 1 STALE /
  2 CORRUPT), unchanged from the ported script. Also a fail-closed
  registry-path resolver (`resolve_registry_path`) that never defaults to
  any repo's path — the caller (today: the orchestrator wrapper script)
  must supply `--registry` or `--data-root` explicitly, same RUNTIME
  CONTRACT discipline as orchestrator's `shadow_ab_daily.sh`.
- `tests/test_software_stops_liveness.py` — 21 tests: exit-code semantics
  (OK/STALE/CORRUPT, missing/empty/corrupt/fresh/stale/never-evaluated
  registries), market-session gating (open/closed/holiday/weekend +
  calendar-backend-unavailable fallback), path resolution (explicit vs.
  composed vs. fail-closed), and CLI wiring (`main()` argv, ntfy
  best-effort posting). See the test file's module docstring for exactly
  what is and is not covered hermetically and why.

## Why `renquant_pipeline` is a lazy import, and why CI does not install it

`renquant_pipeline.software_stops` only needs stdlib + its own
`state_paths` sibling — but Python triggers `renquant_pipeline/__init__.py`
on `import renquant_pipeline.software_stops`, and that package `__init__`
eagerly imports `inference`, `panel_scoring`, `native_inference`, and
`renquant_artifacts.contracts`, which pull in cvxpy + renquant-base-data +
renquant-artifacts as a side effect. None of that is needed to exercise
THIS module's own logic. So:

- The import is deferred to inside `check()`/`resolve_registry_path()` via
  `_pipeline_stops_api()` (same lazy-import discipline this repo already
  applies to `alpaca-py`, see `igv_short_monitor.get_market()`).
- Hermetic tests inject a small faithful fake `_PipelineStopsAPI` instead
  of requiring the real, dependency-heavy package — the pipeline's own
  staleness arithmetic is unit-tested in ITS repo
  (`renquant-pipeline/tests/test_software_stops.py`); this suite does not
  re-derive or re-test it, only the contract this module depends on.
- One additional test (`test_pipeline_stops_api_contract_if_pipeline_installed`)
  asserts the real pipeline module still exposes the exact symbols this
  checker's lazy import depends on, as a drift tripwire — but it is
  `pytest.importorskip`'d, so it SKIPS (not fails) in this repo's CI, which
  does not install pipeline's dependency chain. Verified locally: with the
  full stack on `PYTHONPATH` (common + base-data + artifacts + pipeline,
  cvxpy present), all 21 tests pass and this one runs for real instead of
  skipping.

## Invocation contract (what the orchestrator side now does)

The caller (renquant-orchestrator's `scripts/stops_liveness_pager.sh`, in
the companion PR to orchestrator#481) invokes:

```
PYTHONPATH="<pinned renquant-execution>/src:<pinned renquant-pipeline>/src:<pinned renquant-common>/src" \
    <pinned renquant-execution venv python> -m renquant_execution.software_stops_liveness \
    --data-root <explicit runtime data root> --broker alpaca
```

using the same pinned-sibling-checkout PYTHONPATH convention already used
elsewhere in that repo (e.g. its `renquant-pipeline` PYTHONPATH wiring),
not `RenQuant/.venv` or any umbrella script.

## Not in scope here

- Moving the registry DATA FILE's own location (`data/rq105/software_stops.<broker>.json`)
  off the umbrella-anchored runtime data root — that is still wherever the
  live sell-only loop writes it today; migrating that anchor is R-PIN
  territory (renquant-orchestrator `doc/design/2026-07-11-deployment-pin-authority-migration.md`),
  out of scope for this ops-tooling port.
- Any change to `max_staleness_minutes` or other arming-time parameters —
  those are pipeline/strategy-104 production risk knobs, not touched here.
- Scheduling/installing anything — this repo has no launchd plist for this
  checker; the schedule and notification-consumer wrapper are
  orchestrator-owned (see the companion PR).
