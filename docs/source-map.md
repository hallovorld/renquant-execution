# Source Map From Monorepo

Initial source commit:
`8f3e08d8d1ae1e402a78f4815efb59e3c7c66aa8`.

Execution code should be ported in reviewed slices from:

- `live/`
- live-specific pieces of `backtesting/renquant_104/adapters/`
- notification/ntfy utilities
- order cancel/reconcile scripts

Do not port model training, QP solver internals, or raw artifacts into this
repo. This repo consumes order intents and mutates broker state.
