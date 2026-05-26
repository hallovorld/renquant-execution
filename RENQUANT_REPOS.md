# RenQuant Repository Map

This file gives agents local big-picture context when they start inside any
RenQuant subrepo. The canonical cross-repo operating model is:

https://github.com/hallovorld/RenQuant/blob/main/doc/arch/subrepo-operating-model.md

The original `/Users/renhao/git/github/RenQuant` repo is never deleted,
emptied, or rewritten as part of this split.

## Repositories

| Repo | Local Path | Remote | Role |
|---|---|---|---|
| `RenQuant` | `/Users/renhao/git/github/RenQuant` | `https://github.com/hallovorld/RenQuant` | Permanent umbrella/orchestrator, integration harness, rollback source |
| `renquant-common` | `/Users/renhao/git/github/renquant-common` | `https://github.com/hallovorld/renquant-common` | Shared contracts and pipeline primitives |
| `renquant-strategy-104` | `/Users/renhao/git/github/renquant-strategy-104` | `https://github.com/hallovorld/renquant-strategy-104` | Active 104 policy/config bundle |
| `renquant-model-gbdt` | `/Users/renhao/git/github/renquant-model-gbdt` | `https://github.com/hallovorld/renquant-model-gbdt` | Production GBDT/panel-LTR model training |
| `renquant-model-patchtst` | `/Users/renhao/git/github/renquant-model-patchtst` | `https://github.com/hallovorld/renquant-model-patchtst` | PatchTST/PatchTXT shadow model training |
| `renquant-pipeline` | `/Users/renhao/git/github/renquant-pipeline` | `https://github.com/hallovorld/renquant-pipeline` | Runtime inference, decision tree, QP/order intents |
| `renquant-execution` | `/Users/renhao/git/github/renquant-execution` | `https://github.com/hallovorld/renquant-execution` | Broker execution, cancel/reconcile, notifications |
| `renquant-backtesting` | `/Users/renhao/git/github/renquant-backtesting` | `https://github.com/hallovorld/renquant-backtesting` | Simulation, LEAN assembly, WF validation, forensics |
| `renquant-base-data` | `/Users/renhao/git/github/renquant-base-data` | `https://github.com/hallovorld/renquant-base-data` | Data manifests, freshness, fingerprints, materialization policy |
| `renquant-artifacts` | `/Users/renhao/git/github/renquant-artifacts` | `https://github.com/hallovorld/renquant-artifacts` | Model/artifact manifests, metrics, promotion registry |
| `renquant-orchestrator` | `/Users/renhao/git/github/renquant-orchestrator` | `https://github.com/hallovorld/renquant-orchestrator` | Pinned-subrepo daily orchestration and run bundles |

## System Flow

1. `renquant-base-data` publishes data manifests and freshness/fingerprint
   contracts.
2. `renquant-strategy-104` publishes strategy policy/config.
3. `renquant-model-gbdt` and `renquant-model-patchtst` train against data and
   strategy contracts, then publish artifact manifests to `renquant-artifacts`.
4. `renquant-pipeline` consumes strategy, data, and artifact manifests to emit
   full decision traces and order intents.
5. `renquant-execution` consumes order intents and performs explicit broker
   actions with audit records.
6. `renquant-backtesting` consumes the same manifests and pipeline contracts for
   simulation, LEAN assembly, acceptance, and trade forensics.
7. `renquant-orchestrator` stitches the pinned repos into one daily run and
   persists an auditable run bundle.
8. `RenQuant` pins the whole assembly in `subrepos.lock.json` and remains the
   permanent integration harness and rollback source.

## Cross-Repo Rules

- Use `renquant-common` pipeline primitives for every workflow.
- Do not duplicate ownership: strategy config stays in strategy, data in data,
  artifact metadata in artifacts, alpha training in model repos, order intents
  in pipeline, broker mutation in execution, daily composition in orchestrator.
- Large data, checkpoints, DBs, and experiment dumps are referenced by manifest
  and fingerprint, not committed to normal Git.
- A subrepo commit is not production-active until the umbrella repo pins it and
  integration checks pass.
