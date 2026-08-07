import asyncio

from agents.TaskDispatcher import dispatch
from schemas.tools import ExecutionPlan, ResearchOutput


def make_research() -> ResearchOutput:
    return ResearchOutput(
        base_url="https://api.example.com",
        auth_method="Bearer token",
        endpoints=[],
        confidence=0.8,
    )


def test_dispatch_returns_empty_result_for_empty_plan():
    plan = ExecutionPlan(summary="No tasks", tasks=[])
    research = make_research()

    result = asyncio.run(dispatch(plan, research))

    assert result.total_tasks == 0
    assert result.passed == 0
    assert result.failed == 0
    assert result.skipped == 0
