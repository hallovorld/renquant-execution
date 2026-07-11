# Account cash ledger — round 2: runtime-owned topology + fee-inclusive reservations

STATUS:   delivered
DATE:     2026-07-10
PR:       #28 (this PR, round-2 revision)
CONTEXT:  Codex re-review blockers on #28 after cba1dd9:
          (1) TOPOLOGY — `build_shared_account_cash_ledger_for_broker` still
          accepted a caller-controlled `data_dir`, so two sleeves passing
          different dirs would create independent "shared" ledgers;
          (2) FEE-INCLUSIVE RESERVATION — `submit_remainder` reserved
          `qty x price` (notional only); sequencing on common#28 was not
          accepted as a reason to defer.

## (1) Topology: the ledger location is owned by the execution runtime

- `account_cash_ledger_data_dir()` (landed in 5984a5f, kept as the merged
  canonical resolver): a FIXED machine-scoped root
  (`~/.renquant/account_cash_ledger`) independent of every per-deployment
  variable a sleeve's launch environment could differ on
  (`RENQUANT_REPO_ROOT`, cwd, `RENQUANT_SUBREPO_ROOT`, ...), with exactly
  one override hook (`RENQUANT_ACCOUNT_CASH_LEDGER_DATA_DIR`, for tests /
  a single recorded operator decision — never a per-sleeve setting). A
  fixed default is deliberately stronger here than a required env var:
  two sleeves launched with no ledger config at all still resolve to ONE
  file, instead of each failing (or worse, diverging) on its own value.
- **Per-sleeve path overrides are structurally rejected**:
  `build_shared_account_cash_ledger_for_broker(broker, *, ttl_seconds, env)`
  has NO path parameter (`data_dir` removed; a divergent-path attempt is a
  `TypeError` at the call site); `maybe_build_account_cash_ledger` (the
  round-1 constructor that accepted `data_dir` + a caller `account_id`
  string) is DELETED from the API — this revision closes the remaining
  public door 5984a5f had kept as an internal delegate. Both identity slots
  are now non-negotiable: account id from `broker.get_account_id()`,
  location from `account_cash_ledger_data_dir()`.
- **Both real launch paths wire through one constructor**:
  `open_session_order_book(broker, *, sleeve_tag, trading_day,
  cost_model_spec, env)` — THE execution-owned session-book factory for the
  two stacks that drive `submit_remainder` through a `BrokerPort` (the 104
  batch process and the 105-style/crypto 24/7 loop). Grep-verified: NO
  in-repo production code constructs `OrderStateBook` directly (the loop
  drivers live in orchestrator/pipeline and consume this seam); this factory
  is the mandated entry point they call. Constructing
  `OrderStateBook(cash_ledger=...)` directly (restore/attach paths, tests)
  still enforces the same cost-spec guard at construction — there is no
  route to a ledger-attached book that skips it. Flag OFF -> plain book,
  byte-identical.
- **E2E divergence tests (fail BEFORE order submission)**:
  - real two-OS-process worker passing `data_dir=` dies with `TypeError`
    before any ledger/book/order exists (nothing created on disk); same
    for the session factory in-process (broker port receives nothing);
  - two real processes with deliberately DIFFERENT unrelated env vars
    (`RENQUANT_REPO_ROOT`/`RENQUANT_SUBREPO_ROOT` — the old exploitable
    vector) still resolve to the identical canonical file (from 5984a5f);
  - two sleeves, two broker instances, one runtime env -> the SAME db file
    (in-process e2e through the factory + the two-real-process test), and
    the second sleeve's over-committing BUY refused at submit.

## (2) Fee-inclusive reservation via the REQUIRED canonical cost contract

- **Required contract**: `renquant_common.cost_model` (D-C8a).
  **Coordinated version: renquant-common>=0.12.0** — fixed in common#28 r2
  (9b95b9a, "version-addressable 0.12.0"; its docstring: "A consumer that
  REQUIRES the cost primitive pins renquant-common>=0.12.0 and fails closed
  below it"). Enforcement is STRUCTURAL (`load_cost_contract()`: module
  import + `COST_MODEL_FINGERPRINT_SCHEMA_VERSION == 1` + frozen callable
  surface) rather than `importlib.metadata`, because this fleet consumes
  renquant-common as a source checkout on PYTHONPATH where package metadata
  reports the stale pip install (0.8.1 on this machine, per common#28's own
  test notes) — `cost_model` first ships in 0.12.0, so a verified import IS
  >=0.12.0 content, while a metadata floor would fail closed spuriously on
  every correctly-deployed machine. `REQUIRED_COST_MODEL_PACKAGE_FLOOR =
  "0.12.0"` is exported for evidence/documentation.
- **Worst-case executable debit**: `worst_case_entry_debit(notional, spec)`
  = `notional * (1 + per_side_cost_bps(spec)/1e4)` (fee + half-spread +
  slippage + increment rounding — the contract's own per-side formula, never
  re-derived locally). `AccountCashLedger.reserve_entry()` is the ONLY
  reservation seam the state machine sees (`CashLedgerPort` now exposes
  `reserve_entry`, not raw `reserve`); `submit_remainder` reserves the debit,
  not the notional.
- **Evidence stamping**: every reservation row carries
  `cost_model_sha256` (= `cost_model_content_sha256` of the exact spec used)
  + `cost_model_params` (canonical JSON); the session book carries
  `book.cost_model_sha256` (stamped by `open_session_order_book`, included
  in snapshots only when set — flag-OFF snapshots stay byte-identical).
- **Fail closed when the contract is absent**: `CostContractUnavailableError`
  (module missing / wrong schema version / missing surface) -> BUY entries
  refused with the new reason `account_cash_cost_contract_unavailable`; no
  notional-only fallback exists in the code. A ledger-attached
  `OrderStateBook` REQUIRES `cost_model_spec` at construction/attach (fails
  at wiring time, not at first BUY); `open_session_order_book` probes the
  contract before the session opens. Exits are never routed through any of
  this (§5.4 precedence, re-pinned by test with the contract absent).
- **Boundary case proven** (both against a frozen-API stub in the default
  env AND against the REAL module): 100.00 cash, 99.80 notional — notional
  alone fits (zero-cost control grants), worst-case debit 99.80 x 1.003 =
  100.0994 refused; e2e through `submit_remainder` (no child, nothing
  reaches the broker).

## Tests

- Default env (renquant-common main, contract absent): **333 passed,
  1 skipped** — the fail-closed paths run for REAL (genuine ImportError),
  the cost math runs against a frozen-API stub replica, and
  `tests/test_account_cash_ledger_cost_contract.py` skips.
- Against common#28 r2 (`origin/feat/net-cost-primitives` @ 9b95b9a via a
  scratch worktree on PYTHONPATH): **338 passed, 0 skipped** — the REAL
  contract loads, debits/shas match the same hand-computed canonical-JSON
  values the stub tests pin (parity: no drift room between stub and real),
  and the boundary case passes end-to-end through `submit_remainder`.
- New/updated: env-dir resolution, per-sleeve-override rejection
  (signature + TypeError + two-process worker), session-factory e2e (shared
  db file, sha stamped on both sleeves' books, cross-sleeve refusal),
  worst-case debit + row stamping, boundary case (unit + e2e, stub + real),
  contract-absent/wrong-schema/missing-surface fail-closed, construction-
  time cost-spec guard, exits-unblocked-with-contract-absent.

## Coordination note (for common#28)

This PR consumes exactly the r2 surface: `CostModelSpec`,
`cost_model_spec_from_dict` (strict), `per_side_cost_bps`,
`cost_model_content_sha256`, `COST_MODEL_FINGERPRINT_SCHEMA_VERSION == 1`,
release 0.12.0. If common#28 rebases above common#27 and renumbers (its
stated plan when landing second), only `REQUIRED_COST_MODEL_PACKAGE_FLOOR`'s
documentation string needs the new number — the structural check is
number-independent by design.
