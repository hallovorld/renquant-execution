# renquant-execution

Broker execution repository for RenQuant.

Operating model: https://github.com/hallovorld/RenQuant/blob/main/doc/arch/subrepo-operating-model.md

Repository map: [RENQUANT_REPOS.md](RENQUANT_REPOS.md)

Local automation:

```bash
make test
make doctor
```

This repo owns broker adapters, order submission/cancel/reconcile workflows,
execution audit, and notifications. It consumes order intents from
`renquant-pipeline`; it does not train models or decide alpha.

Broker SDKs are optional imports. `paper` and `alpaca-shadow` modes must be
importable without live broker credentials; live `alpaca` mode requires
`RENQUANT_EXPECTED_LIVE_ACCOUNT` before connect.

## Pipeline Rule

Execution workflows are `renquant-common` Task/Job/Pipeline chains.

## Initial Split Source

`hallovorld/RenQuant` commit
`8f3e08d8d1ae1e402a78f4815efb59e3c7c66aa8`.

## Local Test

```bash
PYTHONPATH=../renquant-common/src:src python -m pytest -q
```
