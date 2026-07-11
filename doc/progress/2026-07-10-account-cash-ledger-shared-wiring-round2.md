# Account cash ledger: shared-wiring contract round-2 (Codex re-review of cba1dd9)

STATUS:   fixed (shared-ledger topology finding) — fee-inclusive reservation
          math still deliberately blocked, see below
DATE:     2026-07-10
PR:       (this PR)
SPEC:     Codex re-review of #28 (D-C4), on commit cba1dd9.

## What

Round-1 (cba1dd9) derived `account_id` from the broker but still accepted an
arbitrary caller-supplied `data_dir` argument in
`build_shared_account_cash_ledger_for_broker`. Codex correctly flagged this
as insufficient: two sleeves could still pass different `data_dir` values
and create independent per-account databases, and the round-1 test only
proved that two callers who *deliberately* passed the same `tmp_path`
coordinated — not that divergence was actually prevented.

Round-2 removes `data_dir` from the function signature entirely. The shared
ledger's data root is now resolved by a new canonical function,
`account_cash_ledger_data_dir()`, which takes NO caller-supplied path and
consults nothing that could legitimately vary between two sleeves' launch
environments (not `RENQUANT_REPO_ROOT`, not `RENQUANT_SUBREPO_ROOT`, not
cwd) — exactly one override hook
(`RENQUANT_ACCOUNT_CASH_LEDGER_DATA_DIR`, intended for tests or a single
recorded operator decision, never a per-sleeve setting) and otherwise a
fixed, machine-scoped default (`~/.renquant/account_cash_ledger`). This
mirrors the existing `preopen_cancel_gate._preopen_cancel_ledger()`
env-driven-canonical-path convention already used elsewhere in this repo.

`build_shared_account_cash_ledger_for_broker(broker, *, ttl_seconds=...,
env=None)` now takes no path/account argument at all — passing `data_dir=`
is a `TypeError`, not a silently-honored override. There is no parameter
through which a per-sleeve path could be threaded, by construction, not by
convention.

## Not addressed

"No real execution entry point invokes it" (Codex, both rounds) remains
true and is out of scope for this repo: there is genuinely no batch/24-7
launch-path entry point anywhere in renquant-execution (confirmed via
`grep -rn "OrderStateBook(" src/` — zero non-test call sites); those launch
paths live in renquant-orchestrator. What this repo owns and now delivers
is making the CONTRACT airtight (no caller-controlled path, single
canonical resolution) so that whichever repo eventually wires the real
launch paths cannot misconfigure them into diverging.

The fee-inclusive reservation blocker remains deliberately unaddressed,
per Codex's explicit rejection of the sequencing argument: "Sequencing is
not a reason to merge an incomplete safety control." This PR stays blocked
on `renquant-common#28`'s cost API actually being released/merged; no
workaround or local duplicate was introduced.

## Tests

`tests/test_account_cash_ledger_shared_process.py`:
- `test_data_dir_is_not_an_accepted_parameter` — passing `data_dir=` raises
  `TypeError`.
- `test_two_independent_broker_processes_resolve_the_same_ledger_file` —
  unchanged intent, updated to use the override hook instead of a
  positional `data_dir`.
- `test_over_committing_reserves_serialize_across_real_processes` —
  unchanged.
- `test_unrelated_per_sleeve_env_divergence_cannot_move_the_ledger_path`
  (NEW) — two real processes with deliberately DIFFERENT
  `RENQUANT_REPO_ROOT`/`RENQUANT_SUBREPO_ROOT` (the OLD exploitable vector,
  now closed) still resolve to the identical canonical path. `HOME` is
  pinned to a shared tmp dir in both workers so the test never touches the
  operator's real home directory.
- `test_flag_off_returns_none_in_every_process` — updated call signature.
- `TestCanonicalDataDirResolver` (NEW, 2 cases) — override hook wins;
  default is fixed and independent of `RENQUANT_REPO_ROOT`.

Verified meaningful via stash-revert: reverting only the source change
breaks the test module's import entirely (`ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE`
doesn't exist pre-fix). Full suite: 321 passed, no regressions.

## Next

Fee-inclusive reservation math remains blocked on `renquant-common#28`
merging with its cost-model fingerprint helpers, per Codex's explicit
instruction not to work around this with sequencing.
