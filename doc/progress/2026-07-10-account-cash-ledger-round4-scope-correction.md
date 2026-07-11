# Account cash ledger — round 4: scope correction (same-filesystem library primitive only)

STATUS:   delivered (docs/docstrings only — no runtime behavior change)
DATE:     2026-07-10
PR:       #28 (this PR, round-4 revision)
CONTEXT:  Codex re-review (D-C4 round 4, commit 9de920bf):

> The acknowledgement and hostname stamp improve operator visibility but do
> not establish the required account-wide topology. Two independently
> configured containers can each set the acknowledgement, have the same
> effective hostname (or simply use separate local filesystems), resolve
> different HOME/override roots, and create separate databases; neither
> process observes a mismatch. The hostname check only detects a second
> host after both happen to open the same file, while the dangerous case is
> two distinct files. This cannot be solved inside a local SQLite library
> by an env declaration. Keep this code default-off and re-scope it as a
> same-filesystem library primitive; do not claim both launch paths are
> wired or that D-C4 is production-delivered. Require a separate
> orchestrator design/implementation PR to make activation conditional on a
> single-host/co-resident launch topology and perform a control-plane
> preflight for both 104 and 105. That PR must carry the bounded shadow
> evidence before the flag can be enabled.

Codex is correct, and this is a genuine architectural limit, not a bug to
iterate on: no mechanism internal to a local SQLite file can detect that
two OTHER processes resolved to two DIFFERENT files when it was never
asked to open either of them. Round 3's hostname stamp is real evidence
against the "same file, wrong host" failure signature, but it structurally
cannot see the "two distinct files" failure signature — that requires an
external party positioned to observe both processes' resolved paths BEFORE
either opens its db, i.e. a control-plane preflight. This PR does not own
that preflight (renquant-execution is a library repo; launch orchestration
is renquant-orchestrator's).

## What changed this round

Documentation and docstrings only — `src/renquant_execution/account_cash_ledger.py`
module docstring, the `open_session_order_book` docstring, and the
`build_shared_account_cash_ledger_for_broker` docstring — corrected to:

1. Stop implying (via present-tense "THE session constructor both launch
   paths use") that any real 104 batch or 24/7 crypto-loop entry point is
   wired through this module today. Grepped: zero non-test, non-library
   callers of `open_session_order_book` or
   `build_shared_account_cash_ledger_for_broker` exist in this repo. This
   is a library contract for adopters, not a completed integration.
2. Explicitly state the hostname stamp's actual (narrower) guarantee and
   its blind spot (two distinct files), rather than describing it as
   catching "the failure signature a genuine cross-host misconfiguration
   would produce" — that phrasing overstated coverage.
3. Name this module, in the module docstring, as a SAME-FILESYSTEM-ONLY
   LIBRARY PRIMITIVE: correct and safe when every process sharing an
   account's ledger is genuinely co-resident on one host/filesystem;
   explicitly not validated for cross-host/cross-container deployment by
   this module or anything else today.

No runtime logic changed. `RENQUANT_ACCOUNT_CASH_LEDGER` remains
default-OFF (verified: `account_cash_ledger_enabled()` reads
`os.environ.get(ACCOUNT_CASH_LEDGER_FLAG, "")`, empty string is not in
`_TRUTHY`). The round-3 acknowledgment gate and hostname stamp are kept as
same-host defense-in-depth — they are still real, useful checks for the
scope this module is now explicitly limited to — but are no longer
documented as sufficient for multi-host activation.

## Follow-up filed

`hallovorld/renquant-orchestrator` issue (see PR #28 comment for link):
before `RENQUANT_ACCOUNT_CASH_LEDGER` is enabled for a real 104+24/7-crypto
co-deployment, the orchestrator needs a design+implementation PR that (a)
enforces a single-host/co-resident launch topology for both launch paths,
(b) performs a control-plane preflight verifying both processes resolve to
the identical ledger file before either launches, and (c) carries bounded
shadow evidence before the flag is turned on in any real deployment. This
PR (#28) ships the library primitive that follow-up work will sit on top
of; it does not itself claim to close that gap.

## Tests

No test changes — this round is documentation-only. Full suite unchanged
from round 3 (343 passed).
