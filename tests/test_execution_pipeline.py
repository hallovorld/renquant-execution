from __future__ import annotations

import pytest

from renquant_execution import ExecutionContext, ExecutionPipeline


def test_execution_pipeline_submits_via_injected_broker() -> None:
    calls = []

    def submitter(broker_name, intents, dry_run):
        calls.append((broker_name, dry_run, len(intents)))
        return [{"id": "dry-1", **intents[0]}]

    ctx = ExecutionContext(
        broker_name="paper",
        order_intents=[{"ticker": "AAPL", "action": "buy"}],
        dry_run=True,
    )
    result = ExecutionPipeline(submitter).run(ctx)

    assert result.ok is True
    assert calls == [("paper", True, 1)]
    assert ctx.submitted_orders[0]["ticker"] == "AAPL"
    assert ctx.audit_rows == [{"broker": "paper", "dry_run": True, "n_intents": 1, "n_submitted": 1}]


def test_execution_pipeline_rejects_malformed_intent() -> None:
    ctx = ExecutionContext(broker_name="paper", order_intents=[{"ticker": "AAPL"}])

    with pytest.raises(ValueError, match="missing action"):
        ExecutionPipeline(lambda *_: []).run(ctx)
