"""Execution-pipeline contract.

The broker implementation is injected behind this contract so tests can verify
order handling without live account mutation.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from renquant_common import Job, Pipeline, Task


@dataclass
class ExecutionContext:
    broker_name: str
    order_intents: list[dict[str, Any]]
    submitted_orders: list[dict[str, Any]] = field(default_factory=list)
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True


BrokerSubmitter = Callable[[str, list[dict[str, Any]], bool], list[dict[str, Any]]]


class ValidateOrderIntentsTask(Task):
    def run(self, ctx: ExecutionContext) -> bool | None:
        if not ctx.broker_name:
            raise ValueError("broker_name is required")
        for idx, intent in enumerate(ctx.order_intents):
            for key in ("ticker", "action"):
                if not intent.get(key):
                    raise ValueError(f"order_intents[{idx}] missing {key}")
        return True


class SubmitOrdersTask(Task):
    def __init__(self, submitter: BrokerSubmitter) -> None:
        self.submitter = submitter

    def run(self, ctx: ExecutionContext) -> bool | None:
        ctx.submitted_orders = self.submitter(ctx.broker_name, ctx.order_intents, ctx.dry_run)
        return True


class AuditExecutionTask(Task):
    def run(self, ctx: ExecutionContext) -> bool | None:
        ctx.audit_rows.append({
            "broker": ctx.broker_name,
            "dry_run": ctx.dry_run,
            "n_intents": len(ctx.order_intents),
            "n_submitted": len(ctx.submitted_orders),
        })
        return True


class ExecutionJob(Job):
    def __init__(self, submitter: BrokerSubmitter) -> None:
        self._tasks = [ValidateOrderIntentsTask(), SubmitOrdersTask(submitter), AuditExecutionTask()]

    @property
    def tasks(self) -> list[Task]:
        return self._tasks


class ExecutionPipeline(Pipeline):
    def __init__(self, submitter: BrokerSubmitter) -> None:
        super().__init__([ExecutionJob(submitter)], name="execution")
