# renquant-execution

Broker execution repository for RenQuant.

This repo owns broker adapters, order submission/cancel/reconcile workflows,
execution audit, and notifications. It consumes order intents from
`renquant-pipeline`; it does not train models or decide alpha.

## Pipeline Rule

Execution workflows are `renquant-common` Task/Job/Pipeline chains.

## Initial Split Source

`hallovorld/RenQuant` commit
`8f3e08d8d1ae1e402a78f4815efb59e3c7c66aa8`.

## Local Test

```bash
PYTHONPATH=../renquant-common/src:src python -m pytest -q
```
