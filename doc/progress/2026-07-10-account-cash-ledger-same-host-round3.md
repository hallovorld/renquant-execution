# Account cash ledger — round 3: same-host / local-filesystem deployment constraint

STATUS:   delivered
DATE:     2026-07-10
PR:       #28 (this PR, round-3 revision)
CONTEXT:  Codex re-review finding (D-C4 round-3): the ledger's cross-process
          coordination guarantee (SQLite WAL mode + `BEGIN IMMEDIATE`) rests
          entirely on the OS's POSIX advisory file locking (`fcntl`) working
          correctly on the volume the db file lives on. That holds for a
          local disk on one machine. It does NOT reliably hold over NFS,
          SMB, or most cloud-mounted/network-attached filesystems — a
          well-known SQLite operational gotcha, not hypothetical. Nothing in
          round 1/2 stopped `~/.renquant/account_cash_ledger` from silently
          resolving onto a network-mounted home directory, which would
          quietly degrade the "exactly one grant" guarantee the two-process
          tests (round 1) prove for local disk.

## Two-layer defense

Docs-only warnings are not enforcement (this codebase's own standard: see
CLAUDE.md's "prompt that raises compliance — not enforcement" framing). This
round adds one honor-system gate plus one structural check that fires from
real, unavoidable evidence — mirroring the existing
`RENQUANT_EXPECTED_LIVE_ACCOUNT` idiom in `alpaca_broker.py` (an operator
must consciously assert a fact before the code trusts it).

1. **Explicit operator acknowledgment gate**: new env var
   `RENQUANT_ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST`.
   `build_shared_account_cash_ledger_for_broker` now raises
   `AccountCashLedgerError` if `RENQUANT_ACCOUNT_CASH_LEDGER` is truthy but
   this acknowledgment is not — before any db file is touched. This does not
   detect a violation; it forces the deployment decision to be a recorded,
   conscious act rather than an assumption, matching the existing pattern
   for asserting the live account identity.
2. **Structural hostname-consistency check**: `_ensure_schema()` already
   stamps `schema_version` and `account_id` into `ledger_meta` on first
   creation and raises "refusing to mix ledgers" on any later mismatch
   (round 1). This round adds `hostname` (`socket.gethostname()`) as a third
   stamped key in the exact same loop. A ledger created on host A that a
   process on host B later opens (the actual signature of a network-mount
   deployment violation — two machines, one file) now fails closed with a
   message that names the constraint by pointing at the module docstring,
   not a generic mismatch string. This is real evidence (a different
   hostname genuinely opened this file), not a proxy.

Why not detect the network filesystem directly (e.g. inspect the mount
table for the db path)? Mount-type detection is platform-specific
(`/proc/mounts` on Linux, `getmntinfo` on macOS, no portable Python stdlib
equivalent), fragile against bind mounts and container overlays, and would
add a dependency this repo doesn't otherwise carry. The hostname stamp
instead catches the deployment mistake this constraint actually cares about
(two hosts sharing one db file) using the exact mechanism already trusted
for `schema_version`/`account_id`, with zero new surface area.

## Tests

- `test_flag_on_without_same_host_acknowledgment_fails_closed`: flag on,
  acknowledgment absent -> `AccountCashLedgerError` before any file is
  created (asserts the target dir stays empty).
- `test_flag_on_with_same_host_acknowledgment_builds_ledger`: flag on,
  acknowledgment present -> ledger builds normally (no behavior change to
  the round-1/round-2 contract once acknowledged).
- `test_ledger_stamps_hostname_and_refuses_to_open_from_a_different_one`:
  create a ledger, hand-edit the stamped `hostname` row to a different
  value (simulating a second host's stamp), re-open -> "refusing to mix
  ledgers" with the same-host hint.
- All pre-existing tests that enable `RENQUANT_ACCOUNT_CASH_LEDGER` (both
  `test_account_cash_ledger.py` dict-literal env fixtures and
  `test_account_cash_ledger_shared_process.py`'s `_run_worker`) updated to
  also set the acknowledgment var, since it is now a hard prerequisite of
  the flag being on.
- Verified meaningfulness: reverted only the source change
  (`account_cash_ledger.py`) with the new tests + updated env fixtures in
  place -> import fails outright (`ACCOUNT_CASH_LEDGER_ACKNOWLEDGE_SAME_HOST`
  does not exist), confirming the tests exercise real, non-vacuous behavior.
  Restored -> full suite green.
- Full suite: **343 passed** (up from round 2's 338; +3 new, +2 from the
  common#28 r2 real-contract run already counted in that baseline).

## Coordination note

No new cross-repo surface: this is entirely internal to
`account_cash_ledger.py`'s existing flag/env-gating pattern, additive and
backward compatible (flag OFF is untouched; flag ON now requires one more
explicit env var, which is a behavior change for anyone who already turned
the flag on in an environment without this ack — none exist yet, since the
flag has never shipped enabled).
