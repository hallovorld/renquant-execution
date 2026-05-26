# CLAUDE.md

Canonical operating model:
https://github.com/hallovorld/RenQuant/blob/main/doc/arch/subrepo-operating-model.md

Local repo map: `RENQUANT_REPOS.md`.

Branch policy: `main` is the stable interface consumed by other repos and
automation. Experiments, optimizations, and large upgrades happen on feature
branches, then merge back only after tests and integration checks pass.

## Repo Role

`renquant-execution` owns broker execution, order submission/cancel/reconcile,
execution audit, and notifications.

## Hard Boundaries

- Consume explicit order intents from `renquant-pipeline`.
- Do not decide alpha, train models, tune strategy thresholds, or mutate
  artifact/data manifests.
- Broker mode must be explicit and audited.
- Duplicate-order, cancel/reopen, and reconciliation flows need regression
  tests before live use.
- Large broker-flow changes use a feature branch.
- Do not delete or empty the source umbrella repo at
  `/Users/renhao/git/github/RenQuant`.

## Workflow

```bash
make test
make doctor
```
