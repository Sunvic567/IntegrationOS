"""
TaskDispatcher — fans an ExecutionPlan out to the three execution workers,
respects depends_on ordering, and runs independent tasks concurrently.

Flow:
  ExecutionPlan
       │
       ▼ (topological sort by depends_on)
  Batches of independent tasks
       │
       ├── TesterWorker  (auth/endpoint/rate/error/webhook tasks)
       ├── SDKWorker     (sdk_generator tasks)
       └── WriterWorker  (doc_writer tasks)
       │
       ▼
  DispatchResult
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Set

import structlog

from agents.ExecutorAgent import SDKWorker, TesterWorker, WriterWorker
from schemas.tools import (
    DispatchResult,
    ExecutionPlan,
    ResearchOutput,
    Task,
    TaskResult,
)

logger = structlog.get_logger("task_dispatcher")

_tester = TesterWorker()
_sdk    = SDKWorker()
_writer = WriterWorker()


# ── Topological batcher ────────────────────────────────────────────────────────

def _topo_batches(tasks: List[Task]) -> List[List[Task]]:
    """
    Groups tasks into ordered batches where every task in a batch has all its
    dependencies satisfied by tasks in earlier batches.  Tasks within a batch
    are independent and can run concurrently.

    Uses Kahn's algorithm.
    """
    by_id: Dict[int, Task] = {t.id: t for t in tasks}
    in_degree: Dict[int, int] = {t.id: len(t.depends_on) for t in tasks}

    # Tasks that are ready to run (no unmet dependencies)
    ready: List[int] = [tid for tid, deg in in_degree.items() if deg == 0]
    batches: List[List[Task]] = []
    completed: Set[int] = set()

    while ready:
        batch = [by_id[tid] for tid in ready]
        batches.append(batch)
        completed.update(ready)
        ready = []

        for task in tasks:
            if task.id in completed:
                continue
            if all(dep in completed for dep in task.depends_on):
                ready.append(task.id)

    # Any tasks still not in a batch have unresolvable deps — append them anyway
    scheduled = {t.id for batch in batches for t in batch}
    leftover = [t for t in tasks if t.id not in scheduled]
    if leftover:
        logger.warning(
            "dispatcher.unresolvable_deps",
            task_ids=[t.id for t in leftover],
        )
        batches.append(leftover)

    return batches


# ── Per-task dispatcher ────────────────────────────────────────────────────────

async def _dispatch_one(
    task: Task,
    research: ResearchOutput,
    completed_results: Dict[int, TaskResult],
    sdk_output: str | None,
) -> TaskResult:
    """
    Routes a single task to the correct worker.
    Passes sdk_output to WriterWorker so it can include it in the doc.
    """
    if not isinstance(research, ResearchOutput):
        return TaskResult(
            task_id=task.id,
            task_name=task.name,
            tool=task.tool,
            execution_status="failed",
            verification_status="failed",
            error="No research context was provided for this task.",
        )

    base_url = research.base_url

    # Check that all dependencies passed — skip if any dep failed/skipped
    for dep_id in task.depends_on:
        dep_result = completed_results.get(dep_id)
        if dep_result and dep_result.execution_status != "completed":
            return TaskResult(
                task_id=task.id,
                task_name=task.name,
                tool=task.tool,
                execution_status="skipped",
                verification_status="not_applicable",
                error=f"Skipped because dependency task {dep_id} ({dep_result.task_name}) {dep_result.execution_status}.",
            )

    logger.info("dispatcher.running_task", task_id=task.id, task_name=task.name, tool=task.tool)

    if _tester.handles(task.tool):
        return await _tester.run(task, base_url)

    if _sdk.handles(task.tool):
        return await _sdk.run(task, research)

    if _writer.handles(task.tool):
        # Build a plain-text summary of test results so far for the doc writer
        test_summary = _build_test_summary(completed_results)
        return await _writer.run(task, research, test_summary, sdk_output)

    logger.warning("dispatcher.unknown_tool", tool=task.tool, task_id=task.id)
    return TaskResult(
        task_id=task.id,
        task_name=task.name,
        tool=task.tool,
        execution_status="failed",
        verification_status="failed",
        error=f"No worker registered for tool '{task.tool}'",
    )


def _build_test_summary(results: Dict[int, TaskResult]) -> str:
    lines = []
    for r in results.values():
        lines.append(f"- [{r.status.upper()}] Task {r.task_id}: {r.task_name} ({r.tool})")
        if r.error:
            lines.append(f"  Error: {r.error}")
    return "\n".join(lines) if lines else "No test results available yet."


# ── Main dispatcher entry point ────────────────────────────────────────────────

async def dispatch(plan: ExecutionPlan, research: ResearchOutput) -> DispatchResult:
    """
    Executes all tasks in the plan, respecting depends_on ordering.
    Independent tasks within each batch run concurrently via asyncio.gather.

    Returns a DispatchResult with all TaskResult objects plus extracted
    sdk_output and doc_output for convenient downstream consumption.
    """
    if not isinstance(plan, ExecutionPlan):
        raise TypeError("dispatch expected an ExecutionPlan instance")

    if not plan.tasks:
        logger.warning("dispatcher.empty_plan")
        return DispatchResult(
            total_tasks=0,
            passed=0,
            failed=0,
            skipped=0,
            results=[],
            sdk_output=None,
            doc_output=None,
        )

    logger.info(
        "dispatcher.start",
        total_tasks=len(plan.tasks),
        summary=plan.summary,
    )

    batches = _topo_batches(plan.tasks)
    completed_results: Dict[int, TaskResult] = {}
    all_results: List[TaskResult] = []
    sdk_output: str | None = None

    for batch_idx, batch in enumerate(batches):
        logger.info(
            "dispatcher.batch",
            batch=batch_idx + 1,
            task_ids=[t.id for t in batch],
        )

        # Run all tasks in this batch concurrently
        batch_results: List[TaskResult] = await asyncio.gather(*[
            _dispatch_one(task, research, completed_results, sdk_output)
            for task in batch
        ])

        for result in batch_results:
            completed_results[result.task_id] = result
            all_results.append(result)
            # Cache SDK output so WriterWorker can reference it
            if result.tool == "sdk_generator" and result.execution_status == "completed":
                sdk_output = result.output

    # Tally outcomes
    passed  = sum(1 for r in all_results if r.execution_status == "completed")
    failed  = sum(1 for r in all_results if r.execution_status == "failed")
    skipped = sum(1 for r in all_results if r.execution_status == "skipped")

    doc_output = next(
        (r.output for r in all_results if r.tool == "doc_writer" and r.execution_status == "completed"),
        None,
    )

    logger.info(
        "dispatcher.done",
        total=len(all_results),
        passed=passed,
        failed=failed,
        skipped=skipped,
    )

    return DispatchResult(
        total_tasks=len(all_results),
        passed=passed,
        failed=failed,
        skipped=skipped,
        results=all_results,
        sdk_output=sdk_output,
        doc_output=doc_output,
    )
