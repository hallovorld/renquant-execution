# 2026-07-12 fix(crypto): account status enum repr

## What

`AlpacaBroker.get_account_info()` returned `"AccountStatus.ACTIVE"` instead
of `"ACTIVE"` because `str()` on a Python enum includes the class name.
Stage-0 battery `status != "ACTIVE"` check false-negatives on a healthy
paper account.

## Fix

Use `.name` attribute (returns bare enum member name `"ACTIVE"`) with
fallback to `str()` for plain strings. Applied to both `status` and
`crypto_status` fields.

## Evidence

- 581 execution tests pass
- Battery dry-run previously showed all checks PASS except status (false negative)
- `str(AccountStatus.ACTIVE)` = `"AccountStatus.ACTIVE"`;
  `AccountStatus.ACTIVE.name` = `"ACTIVE"`

## PR

- execution #38
