"""
ReviewerAgent — post-dispatch quality gate.

Responsibilities:
  1. Inspect the DispatchResult for failed tasks
  2. Retry each failed task once via the Task Dispatcher
  3. Return a final PASS / FAIL verdict with a human-readable report
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

import structlog

from agents.TaskDispatcher import _dispatch_one
from schemas.tools import (
    DispatchResult,
    ExecutionPlan,
    ResearchOutput,
    TaskResult,
)

logger = structlog.get_logger("reviewer_agent")


# ── Review result ──────────────────────────────────────────────────────────────

class ReviewResult:
    """
    Simple container for the reviewer's final verdict.
    """
    def __init__(
        self,
        verdict: str,           # "PASS" or "FAIL"
        report: str,            # human-readable summary
        final_dispatch: DispatchResult,
    ) -> None:
        self.verdict        = verdict
        self.report         = report
        self.final_dispatch = final_dispatch

    def __repr__(self) -> str:
        return f"ReviewResult(verdict={self.verdict!r}, passed={self.final_dispatch.passed}, failed={self.final_dispatch.failed})"


# ── Reviewer ───────────────────────────────────────────────────────────────────

async def review(
    dispatch_result: DispatchResult,
    plan: ExecutionPlan,
    research: ResearchOutput,
) -> ReviewResult:
    """
    Examines the DispatchResult.  If any tasks failed, retries them once.
    Returns a ReviewResult with the final verdict and a markdown report.
    """
    logger.info(
        "reviewer.start",
        passed=dispatch_result.passed,
        failed=dispatch_result.failed,
        skipped=dispatch_result.skipped,
    )

    failed_results = [r for r in dispatch_result.results if r.execution_status == "failed"]

    if not failed_results:
        logger.info("reviewer.all_passed")
        return ReviewResult(
            verdict="PASS",
            report=_build_report(dispatch_result, retried=[], retry_results=[]),
            final_dispatch=dispatch_result,
        )

    # ── Retry failed tasks once ────────────────────────────────────────────────
    logger.info("reviewer.retrying", count=len(failed_results))

    # Build a lookup of ALL results so dependency checks work
    completed_lookup: Dict[int, TaskResult] = {
        r.task_id: r for r in dispatch_result.results
    }
    # Mark failed tasks as "pass" in the lookup before retry so their deps
    # don't block the retry attempt
    sdk_output = dispatch_result.sdk_output

    retry_results: List[TaskResult] = await asyncio.gather(*[
        _dispatch_one(
            task=next(t for t in plan.tasks if t.id == r.task_id),
            research=research,
            completed_results={
                tid: res for tid, res in completed_lookup.items()
                if res.execution_status == "completed" or res.task_id == r.task_id
            },
            sdk_output=sdk_output,
        )
        for r in failed_results
    ])

    # Merge retry results back into the full result list
    retry_by_id: Dict[int, TaskResult] = {r.task_id: r for r in retry_results}
    final_results: List[TaskResult] = []
    for original in dispatch_result.results:
        if original.task_id in retry_by_id:
            retried = retry_by_id[original.task_id]
            logger.info(
                "reviewer.retry_outcome",
                task_id=retried.task_id,
                task_name=retried.task_name,
                status=retried.status,
            )
            final_results.append(retried)
        else:
            final_results.append(original)

    # Recompute tallies
    passed  = sum(1 for r in final_results if r.execution_status == "completed")
    failed  = sum(1 for r in final_results if r.execution_status == "failed")
    skipped = sum(1 for r in final_results if r.execution_status == "skipped")

    final_sdk_output = next(
        (r.output for r in final_results if r.tool == "sdk_generator" and r.execution_status == "completed"),
        dispatch_result.sdk_output,
    )
    final_doc_output = next(
        (r.output for r in final_results if r.tool == "doc_writer" and r.execution_status == "completed"),
        dispatch_result.doc_output,
    )

    final_dispatch = DispatchResult(
        total_tasks=len(final_results),
        passed=passed,
        failed=failed,
        skipped=skipped,
        results=final_results,
        sdk_output=final_sdk_output,
        doc_output=final_doc_output,
    )

    verdict = "PASS" if failed == 0 else "FAIL"
    report  = _build_report(final_dispatch, retried=failed_results, retry_results=retry_results)

    logger.info("reviewer.done", verdict=verdict, passed=passed, failed=failed)
    return ReviewResult(verdict=verdict, report=report, final_dispatch=final_dispatch)


# ── Report builder ─────────────────────────────────────────────────────────────

def _build_report(
    dispatch: DispatchResult,
    retried: List[TaskResult],
    retry_results: List[TaskResult],
) -> str:
    lines: List[str] = [
        "# Review Report\n",
        f"**Overall**: {'✅ PASS' if dispatch.failed == 0 else '❌ FAIL'}  ",
        f"**Tasks**: {dispatch.total_tasks} total — "
        f"{dispatch.passed} passed, {dispatch.failed} failed, {dispatch.skipped} skipped\n",
    ]

    if retried:
        lines.append("## Retried Tasks\n")
        retry_map = {r.task_id: r for r in retry_results}
        for original in retried:
            retried_r = retry_map.get(original.task_id)
            if retried_r:
                icon  = "✅" if retried_r.execution_status == "completed" else "❌"
                lines.append(
                    f"- Task {original.task_id} **{original.task_name}**: "
                    f"original={original.status} → retry={icon} {retried_r.status}"
                )
        lines.append("")

    lines.append("## Task Results\n")
    for r in dispatch.results:
        icon = {"completed": "✅", "failed": "❌", "skipped": "⏭️"}.get(r.execution_status, "?")
        lines.append(f"- {icon} [{r.tool}] **{r.task_name}**")
        if r.error:
            lines.append(f"  - Error: {r.error}")

    if dispatch.sdk_output:
        lines.append("\n## SDK Generated\n")
        lines.append("Python SDK was successfully generated (see `dispatch_result.sdk_output`).\n")

    if dispatch.doc_output:
        lines.append("\n## Documentation Generated\n")
        lines.append("Integration guide was successfully written (see `dispatch_result.doc_output`).\n")

    return "\n".join(lines)