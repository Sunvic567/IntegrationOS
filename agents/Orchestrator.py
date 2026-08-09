"""
Workflow Orchestrator — top-level LangGraph that sequences the full pipeline:

  User Input
      │
      ▼
  validate_input     ← checks URL is non-empty and reachable
      │ pass / fail
      ▼
  run_research       ← calls ResearchAgent sub-graph
      │
      ▼
  run_planner        ← calls Planner sub-graph
      │
      ▼
  run_dispatcher     ← calls TaskDispatcher (fans to Tester / SDK / Writer)
      │
      ▼
  run_reviewer       ← ReviewerAgent retries failures + emits final report
      │
      ▼
  END
"""
from __future__ import annotations

from typing import Annotated, List, Optional, TypedDict, cast

import httpx
import uuid
import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph, add_messages

from agents import TaskDispatcher
from agents.Planner import AgentState as PlannerAgentState, planner_app, config as planner_config
from agents.ResearchAgent import AgentState as ResearchAgentState, app as research_app, config as research_config
from agents.ReviewerAgent import review
from guardrails import run_guardrail, GuardrailResult
from guardrails.guardrail import format_violation
from schemas.tools import DispatchResult, ExecutionPlan, ResearchOutput

logger = structlog.get_logger("orchestrator")


# ── Orchestrator state ─────────────────────────────────────────────────────────

class OrchestratorState(TypedDict):
    messages:        Annotated[List[BaseMessage], add_messages]
    api_url:         Optional[str]
    run_id: Optional[str]
    research_result: Optional[ResearchOutput]
    plan:            Optional[ExecutionPlan]
    dispatch_result: Optional[DispatchResult]
    review_report:   Optional[str]
    review_verdict:  Optional[str]   # "PASS" | "FAIL"
    errors:          List[str]


# ── Node 1: Validate user input ────────────────────────────────────────────────

async def validate_input(state: OrchestratorState) -> dict:
    """
    Checks that api_url is:
      1. Non-empty
      2. Free of toxic language or jailbreak attempts (guardrail)
      3. Reachable (HTTP HEAD, accepts any non-5xx response)

    A 401/403 is acceptable — it proves the host exists but requires auth.
    """
    api_url = (state.get("api_url") or "").strip()

    if not api_url:
        msg = "⛔ No API URL provided. Please supply a URL to the API documentation."
        logger.error("orchestrator.validate.no_url")
        return {
            "messages": [AIMessage(content=msg)],
            "errors":   [msg],
        }

    # ── Guardrail: screen all user-supplied text ───────────────────────────────
    # Collect everything the user sent: the URL + any message content.
    user_messages = state.get("messages", [])
    user_text_parts = [api_url] + [
        m.content for m in user_messages
        if hasattr(m, "content") and isinstance(m.content, str)
    ]
    combined_input = "\n".join(user_text_parts)

    guard: GuardrailResult = await run_guardrail(
        combined_input, context="orchestrator.validate_input"
    )
    if not guard.is_safe:
        msg = format_violation(guard, agent_name="Orchestrator")
        logger.warning(
            "orchestrator.validate.guardrail_blocked",
            violation_type=guard.violation_type,
            severity=guard.severity,
        )
        return {
            "messages": [AIMessage(content=msg)],
            "errors":   [msg],
        }

    logger.info("orchestrator.validate.guardrail_passed", url=api_url)

    # ── Reachability check ─────────────────────────────────────────────────────
    logger.info("orchestrator.validate.checking", url=api_url)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.head(api_url)
        if resp.status_code >= 500:
            msg = (
                f"⛔ URL '{api_url}' returned HTTP {resp.status_code}. "
                "The server appears to be down. Please check the URL and retry."
            )
            logger.error("orchestrator.validate.server_error", status=resp.status_code)
            return {"messages": [AIMessage(content=msg)], "errors": [msg]}
    except httpx.RequestError as exc:
        msg = f"⛔ Could not reach '{api_url}': {exc}. Check the URL and your network connection."
        logger.error("orchestrator.validate.network_error", error=str(exc))
        return {"messages": [AIMessage(content=msg)], "errors": [msg]}

    logger.info("orchestrator.validate.passed", url=api_url)
    return {
        "messages": [AIMessage(content=f"✅ URL validated: {api_url}")],
        "errors":   [],
    }


# ── Node 2: Run Research Agent ─────────────────────────────────────────────────

async def run_research(state: OrchestratorState) -> dict:
    api_url = state.get("api_url", "")
    logger.info("orchestrator.research.start", url=api_url)

    try:
        # Invoke the ResearchAgent sub-graph
        research_input: ResearchAgentState = {
            "messages": [HumanMessage(content=f"Research the API at: {api_url}")],
            "api_url": api_url,
            "research_result": None,
            "raw_docs": [],
            "parsed_docs": [],
            "errors": [],
            "metadata": {},
            "cache_hit": False,
            "guardrail_blocked": False,
        }
        research_state = await research_app.ainvoke(
            cast(ResearchAgentState, research_input),
            config={**research_config,  "configurable": {"thread_id": f"research-{api_url}-{state.get('run_id', '')}"},},
        )
        result: Optional[ResearchOutput] = research_state.get("research_result")

        if result is None:
            msg = "⛔ ResearchAgent produced no output. Aborting pipeline."
            logger.error("orchestrator.research.no_output")
            return {
                "messages": [AIMessage(content=msg)],
                "errors": state.get("errors", []) + [msg],
            }

        logger.info(
            "orchestrator.research.done",
            confidence=result.confidence,
            endpoints=len(result.endpoints),
        )
        return {
            "messages":        [AIMessage(content=f"✅ Research done — confidence {result.confidence:.2f}, {len(result.endpoints)} endpoints.")],
            "research_result": result,
        }

    except Exception as exc:
        msg = f"⛔ ResearchAgent error: {exc}"
        logger.exception("orchestrator.research.error", error=str(exc))
        return {
            "messages": [AIMessage(content=msg)],
            "errors": state.get("errors", []) + [msg],
        }


# ── Node 3: Run Planner Agent ──────────────────────────────────────────────────

async def run_planner(state: OrchestratorState) -> dict:
    research_result = state.get("research_result")
    api_url         = state.get("api_url", "")
    logger.info("orchestrator.planner.start")

    try:
        planner_input: PlannerAgentState = {
            "messages": [HumanMessage(content="Generate an execution plan.")],
            "research_result": research_result,
            "plan": None,
            "api_url": api_url,
            "validation_passed": None,
            "validation_errors": [],
            "remem_context": "",
            "remem_user_id": "",
        }
        planner_state = await planner_app.ainvoke(
            cast(PlannerAgentState, planner_input),
            config={**planner_config,  "configurable": {"thread_id": f"research-{api_url}-{state.get('run_id', '')}"} },
        )
        plan: Optional[ExecutionPlan] = planner_state.get("plan")

        if plan is None:
            msg = "⛔ PlannerAgent produced no plan. Aborting pipeline."
            logger.error("orchestrator.planner.no_plan")
            return {
                "messages": [AIMessage(content=msg)],
                "errors": state.get("errors", []) + [msg],
            }

        logger.info("orchestrator.planner.done", tasks=len(plan.tasks))
        return {
            "messages": [AIMessage(content=f"✅ Plan ready — {len(plan.tasks)} tasks: {plan.summary}")],
            "plan":     plan,
        }

    except Exception as exc:
        msg = f"⛔ PlannerAgent error: {exc}"
        logger.exception("orchestrator.planner.error", error=str(exc))
        return {
            "messages": [AIMessage(content=msg)],
            "errors": state.get("errors", []) + [msg],
        }


# ── Node 4: Run Task Dispatcher ────────────────────────────────────────────────

async def run_dispatcher(state: OrchestratorState) -> dict:
    plan = state.get("plan")
    research_result = state.get("research_result")

    if plan is None or research_result is None:
        msg = "⛔ Dispatcher could not run because the plan or research result is missing."
        logger.error("orchestrator.dispatcher.missing_inputs")
        return {
            "messages": [AIMessage(content=msg)],
            "errors": state.get("errors", []) + [msg],
        }

    logger.info("orchestrator.dispatcher.start", tasks=len(plan.tasks) if plan else 0)

    try:
        dispatch_result = await TaskDispatcher.dispatch(plan, research_result)
        logger.info(
            "orchestrator.dispatcher.done",
            passed=dispatch_result.passed,
            failed=dispatch_result.failed,
        )
        status_str = (
            f"✅ Dispatch done — {dispatch_result.passed} passed, "
            f"{dispatch_result.failed} failed, {dispatch_result.skipped} skipped."
        )
        return {
            "messages":        [AIMessage(content=status_str)],
            "dispatch_result": dispatch_result,
        }

    except Exception as exc:
        msg = f"⛔ Dispatcher error: {exc}"
        logger.exception("orchestrator.dispatcher.error", error=str(exc))
        return {
            "messages": [AIMessage(content=msg)],
            "errors": state.get("errors", []) + [msg],
        }


# ── Node 5: Run Reviewer ───────────────────────────────────────────────────────

async def run_reviewer(state: OrchestratorState) -> dict:
    dispatch_result = state.get("dispatch_result")
    plan = state.get("plan")
    research_result = state.get("research_result")

    if dispatch_result is None or plan is None or research_result is None:
        msg = "⛔ Reviewer could not run because the dispatch result, plan, or research result is missing."
        logger.error("orchestrator.reviewer.missing_inputs")
        return {
            "messages": [AIMessage(content=msg)],
            "errors": state.get("errors", []) + [msg],
        }

    logger.info("orchestrator.reviewer.start")

    try:
        review_result = await review(dispatch_result, plan, research_result)
        logger.info("orchestrator.reviewer.done", verdict=review_result.verdict)
        return {
            "messages":        [AIMessage(content=review_result.report)],
            "review_report":   review_result.report,
            "review_verdict":  review_result.verdict,
            "dispatch_result": review_result.final_dispatch,  # updated after retries
        }

    except Exception as exc:
        msg = f"⛔ Reviewer error: {exc}"
        logger.exception("orchestrator.reviewer.error", error=str(exc))
        return {
            "messages": [AIMessage(content=msg)],
            "errors": state.get("errors", []) + [msg],
        }


# ── Routing functions ──────────────────────────────────────────────────────────

def route_after_validation(state: OrchestratorState) -> str:
    errors = state.get("errors") or []
    return "abort" if errors else "research"


def route_after_research(state: OrchestratorState) -> str:
    result = state.get("research_result")
    errors = state.get("errors") or []
    if errors or result is None:
        return "abort"
    return "plan"


def route_after_planning(state: OrchestratorState) -> str:
    plan   = state.get("plan")
    errors = state.get("errors") or []
    if errors or plan is None:
        return "abort"
    return "dispatch"


# ── Abort node ─────────────────────────────────────────────────────────────────

async def abort(state: OrchestratorState) -> dict:
    errors = state.get("errors") or ["Unknown error — pipeline aborted."]
    logger.error("orchestrator.aborted", errors=errors)
    return {
        "messages": [AIMessage(content=f"⛔ Pipeline aborted:\n" + "\n".join(f"  • {e}" for e in errors))],
    }


# ── Build the graph ────────────────────────────────────────────────────────────

orchestrator_workflow = StateGraph(OrchestratorState)

orchestrator_workflow.add_node("validate_input",  validate_input)
orchestrator_workflow.add_node("run_research",    run_research)
orchestrator_workflow.add_node("run_planner",     run_planner)
orchestrator_workflow.add_node("run_dispatcher",  run_dispatcher)
orchestrator_workflow.add_node("run_reviewer",    run_reviewer)
orchestrator_workflow.add_node("abort",           abort)

orchestrator_workflow.set_entry_point("validate_input")

orchestrator_workflow.add_conditional_edges(
    "validate_input",
    route_after_validation,
    {"research": "run_research", "abort": "abort"},
)
orchestrator_workflow.add_conditional_edges(
    "run_research",
    route_after_research,
    {"plan": "run_planner", "abort": "abort"},
)
orchestrator_workflow.add_conditional_edges(
    "run_planner",
    route_after_planning,
    {"dispatch": "run_dispatcher", "abort": "abort"},
)
orchestrator_workflow.add_edge("run_dispatcher", "run_reviewer")
orchestrator_workflow.add_edge("run_reviewer",   END)
orchestrator_workflow.add_edge("abort",          END)

orchestrator_app = orchestrator_workflow.compile(checkpointer=MemorySaver())
orchestrator_config: RunnableConfig = {
    "configurable": {"thread_id": "orchestrator-default"},
    "recursion_limit": 50,
}
