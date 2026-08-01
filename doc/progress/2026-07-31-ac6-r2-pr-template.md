# GOAL-5 AC6 R2 — the review surface reaches the order-level gates

**Date:** 2026-07-31 · `renquant-execution` · GOAL-5 (P0) / AC6 R2, tracked in
`renquant-orchestrator`#564

## What this is

Third of four repos. AC6's rule was canonical everywhere and present on the review
surface of `renquant-orchestrator` only; `renquant-pipeline` landed earlier today
(#241), and this repo had **no PR template at all**.

Measured here, not quoted `[本次实测 2026-07-31]`:

```
grep -rlE "admission|_veto|sell_only|hard.?gate|fail.?closed" src ops  ->  10 files
```

including `alpaca_broker.py`, `order_state_machine.py`, `readonly_broker.py`,
`software_stops_liveness.py`, `live_commit.py`.

## The repo-specific half, and why a generic copy would under-fire

**In this repo the gate is usually order-level**, and that is easy to miss. An author
reading *"HARD capital-admission gate"* pictures a panel veto. But a broker that refuses
to submit, an order state machine that will not leave a state, a liveness or software-stop
check that blocks placement, and a read-only broker mode each take names from
tradeable → not-tradeable just as surely — and none of them looks like an admission gate
in the code.

So the item names those four shapes explicitly. A test asserts they are named, because
that sentence is the difference between the checklist firing here and being read as N/A.

The rule itself is **delegated**, not paraphrased: a per-repo copy drifts from the rule it
copies.

## What it is NOT — stated on the template

> *This checklist item is a review surface, not enforcement.*

Measured, not hedged: `renquant-orchestrator`#690 established that the shared
`LiveRunBundle` schema declares **7** fields and silently drops the rest, so a provenance
field added to that path would be validated by nothing
`[早前实测 2026-07-31, orch#690]`. Until R4 closes, this item and the reviewer reading it
*are* the gate.

## Tests

7. Existence; the AC6 item present; all **three** properties named — *identity*,
*expiry*, *binding* (two of three yields a checklist that passes on a gate nobody can
lift, or one nobody can find); the **order-level** framing and its four examples present;
**"Temporary" refused** as an expiry; a pointer to the **canonical** rule rather than a
paraphrase; and the not-enforcement line asserted present.

Suite: **594 passed, 1 skipped**.

## Remaining

R2 now in **3 of 4** repos. `renquant-strategy-104` remains — it holds no gate *code*,
but it holds the threshold *config values* pipeline gates read, so a config PR that
tightens a threshold is an in-scope gate change in effect. **R4 is blocked, not pending.**
