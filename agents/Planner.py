## Creates the execution plan and decides what to test or generate
from typing import TypedDict, List, Optional, Annotated
import logging
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)
import structlog
from langgraph.graph import StateGraph, END, add_messages
from LLM.llm import llm
from langchain_core.messages import SystemMessage, BaseMessage, AIMessage, HumanMessage
from prompts.planner_agent_prompt import planner_agent_prompt
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.runnables import RunnableConfig
from schemas.tools import ResearchOutput, ExecutionPlan
from settings.remem_client import remem, PLANNER_AGENT_ID

logger = structlog.get_logger("planner_agent")

# ── Confidence gate ──────────────────────────────────────────────────────────
# Research must score at or above this threshold before the Planner will act.
# Below this the pipeline stops and requests a recrawl.
CONFIDENCE_THRESHOLD = 0.50

# ── Structured LLM ───────────────────────────────────────────────────────────
# Fix #2: bind the LLM to the ExecutionPlan schema so the model MUST return
# valid structured output — no free-form text, no parsing guesswork.
llm_structured = llm.with_structured_output(ExecutionPlan)


# ── Agent state ───────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    research_result: Optional[ResearchOutput]   # ← from ResearchAgent
    plan: Optional[ExecutionPlan]               # ← populated by _planner_node
    api_url: Optional[str]                      # ← passed in from caller
    validation_passed: Optional[bool]           # ← set by validate_research
    validation_errors: List[str]               # ← reasons validation failed
    remem_context: str                          # ← prior plans from Remem (ephemeral)
    remem_user_id: str                          # ← Remem key for this API


# ── Helpers ───────────────────────────────────────────────────────────────────
def _plan_user_id(research_result: Optional[ResearchOutput], api_url: Optional[str]) -> str:
    """
    Derive a stable Remem user_id for plan memories.
    Prefer the base_url from the research result; fall back to api_url.
    """
    if research_result and research_result.base_url:
        return research_result.base_url
    return api_url or "unknown_api"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((TimeoutError, ConnectionError)),
    before_sleep=before_sleep_log(logging.getLogger("planner_agent"), logging.WARNING),
    reraise=True,
)
async def _invoke_structured_with_retry(system_msg: SystemMessage, human_msg: HumanMessage) -> ExecutionPlan:
    """Structured LLM invocation with tenacity retry for transient errors."""
    # Fix #4: only [SystemMessage, HumanMessage] — no conversation history
    return await llm_structured.ainvoke([system_msg, human_msg])


# ── Node 1: Validate research quality ────────────────────────────────────────
async def validate_research(state: AgentState) -> dict:
    """
    Fix #5 + #6: Gate node — inspects the ResearchOutput before doing any work.

    Hard stops:
      • No research result at all
      • endpoints list is empty (can't build a test plan with nothing to test)
      • confidence < CONFIDENCE_THRESHOLD (research is too incomplete to trust)

    If any check fails, sets validation_passed=False and lists the reasons in
    validation_errors.  The router will send the graph to abort_planning → END.
    """
    result = state.get("research_result")
    errors: List[str] = []

    if result is None:
        return {
            "validation_passed": False,
            "validation_errors": ["No research result in state — cannot plan."],
        }

    # Fix #5: hard stop on zero endpoints
    if not result.endpoints:
        errors.append(
            f"Research returned 0 endpoints for {result.base_url!r}. "
            "A test plan cannot be generated without documented endpoints. "
            "Request additional crawling before retrying."
        )

    # Fix #6: hard stop on low confidence
    if result.confidence < CONFIDENCE_THRESHOLD:
        errors.append(
            f"Research confidence is {result.confidence:.2f} "
            f"(required ≥ {CONFIDENCE_THRESHOLD}). "
            f"Missing fields: {result.quality_flags}. "
            "Recrawl the API documentation to improve completeness."
        )

    if errors:
        logger.warning(
            "planner.validation_failed",
            confidence=getattr(result, "confidence", "n/a"),
            endpoint_count=len(result.endpoints) if result else 0,
            errors=errors,
        )
        return {"validation_passed": False, "validation_errors": errors}

    logger.info(
        "planner.validation_passed",
        base_url=result.base_url,
        confidence=result.confidence,
        endpoint_count=len(result.endpoints),
        quality_flags=result.quality_flags,
    )
    return {"validation_passed": True, "validation_errors": []}


# ── Node 2: Abort path ────────────────────────────────────────────────────────
async def abort_planning(state: AgentState) -> dict:
    """
    Reached when validate_research fails.
    Emits a clear, structured error message and halts — no plan is generated.
    """
    errors = state.get("validation_errors", ["Unknown validation failure."])
    error_lines = "\n".join(f"  • {e}" for e in errors)
    msg = (
        "⛔ Planning aborted — research quality insufficient:\n"
        f"{error_lines}\n\n"
        "Next step: return to the ResearchAgent and recrawl with broader scope."
    )
    logger.error("planner.aborted", reasons=errors)
    return {
        "messages": [AIMessage(content=msg)],
        "plan": None,
    }


# ── Node 3: Load prior plan context from Remem ───────────────────────────────
async def load_remem_context(state: AgentState) -> dict:
    """
    Before generating a plan, ask Remem if we've planned for this API before.
    Prior plans are appended to the system prompt as reference context so the
    LLM can improve on previous work rather than starting from scratch.
    """
    research_result = state.get("research_result")
    api_url = state.get("api_url", "")
    user_id = _plan_user_id(research_result, api_url)

    prior_context = ""
    try:
        memories = remem.recall(
            query=f"execution test plan for API {user_id}",
            user_id=user_id,
            agent_id=PLANNER_AGENT_ID,
        )
        if memories:
            logger.info(
                "planner_remem.prior_plan_found",
                user_id=user_id,
                count=len(memories),
                score=memories[0].get("score"),
                score_detail=memories[0].get("score_detail"),
            )
            prior_text = "\n\n---\n\n".join(m.get("content", "") for m in memories)
            prior_context = (
                "\n\n## Prior Plans for This API (from Remem memory)\n"
                "A plan was previously generated for this same API. "
                "Reference it to avoid redundancy and improve on past work:\n\n"
                + prior_text
            )
        else:
            logger.info("planner_remem.no_prior_plans", user_id=user_id)
    except Exception as e:
        logger.warning("planner_remem.recall_error", error=str(e))

    return {
        "remem_context": prior_context,
        "remem_user_id": user_id,
    }


# ── Node 4: Generate the structured plan ─────────────────────────────────────
async def _planner_node(state: AgentState) -> dict:
    """
    Fix #2: uses llm.with_structured_output(ExecutionPlan) — the model MUST
            return a valid ExecutionPlan object, not free-form text.
    Fix #3: prompt instructs the LLM to organize (map research → tasks),
            not to reason or invent.
    Fix #4: only [SystemMessage, HumanMessage] are passed — no conversation
            history that would bloat tokens and inject irrelevant context.
    Fix #7: dependency scheduling is enforced by the prompt template which
            assigns depends_on values based on task type and order.
    """
    result = state.get("research_result")

    # Enrich the system prompt with any prior Remem context
    remem_ctx = state.get("remem_context", "")
    enriched_prompt = planner_agent_prompt + remem_ctx

    # Only pass the two messages the planner actually needs (Fix #4)
    system_msg = SystemMessage(content=enriched_prompt)
    human_msg  = HumanMessage(
        content=f"API Research Result (confidence={result.confidence:.2f}):\n\n"
                + result.model_dump_json(indent=2)
    )

    try:
        logger.info(
            "planner.generating",
            base_url=result.base_url,
            endpoints=len(result.endpoints),
            confidence=result.confidence,
        )
        plan: ExecutionPlan = await _invoke_structured_with_retry(system_msg, human_msg)
        logger.info(
            "planner.plan_generated",
            task_count=len(plan.tasks),
            summary=plan.summary,
        )
        return {
            "messages": [AIMessage(content=f"✅ Plan generated: {plan.summary}")],
            "plan": plan,
        }

    except RetryError as e:
        logger.error("planner.retry_exhausted", error=str(e))
        return {
            "messages": [AIMessage(content=f"Planner failed after retries: {e}")],
            "plan": None,
        }
    except Exception as e:
        logger.exception("planner.unexpected_error", error=str(e))
        return {
            "messages": [AIMessage(content=f"Planner error: {e}")],
            "plan": None,
        }


# ── Node 5: Persist the plan in Remem ────────────────────────────────────────
async def store_plan_in_remem(state: AgentState) -> dict:
    """
    After generating a plan, persist its JSON in Remem.
    Future runs for the same API will recall this as prior-plan context.
    Non-fatal: a storage failure never blocks the pipeline.
    """
    plan: Optional[ExecutionPlan] = state.get("plan")
    user_id = state.get("remem_user_id", "unknown_api")

    if not plan:
        logger.warning("planner_remem.no_plan_to_store")
        return {}

    # Serialize the structured plan so it can be recalled as text later
    plan_content = plan.model_dump_json()

    try:
        memory_id = remem.remember(
            plan_content,
            user_id=user_id,
            agent_id=PLANNER_AGENT_ID,
            memory_type="semantic",
            importance=0.85,  # slightly below research — plans improve over time
        )
        logger.info("planner_remem.plan_stored", user_id=user_id, memory_id=memory_id)
    except Exception as e:
        logger.warning("planner_remem.store_error", error=str(e))

    return {}


# ── Routing functions ─────────────────────────────────────────────────────────
def route_after_validation(state: AgentState) -> str:
    """
    After validate_research:
      passed  → load Remem context and plan
      failed  → abort and surface the errors
    """
    if state.get("validation_passed"):
        return "load_context"
    return "abort"


# ── Graph ─────────────────────────────────────────────────────────────────────

#
planner_workflow = StateGraph(AgentState)

planner_workflow.add_node("validate_research",   validate_research)
planner_workflow.add_node("abort_planning",       abort_planning)
planner_workflow.add_node("load_remem_context",  load_remem_context)
planner_workflow.add_node("planner",             _planner_node)
planner_workflow.add_node("store_plan_in_remem", store_plan_in_remem)

planner_workflow.set_entry_point("validate_research")

planner_workflow.add_conditional_edges(
    "validate_research",
    route_after_validation,
    {"abort": "abort_planning", "load_context": "load_remem_context"},
)
planner_workflow.add_edge("abort_planning",      END)
planner_workflow.add_edge("load_remem_context",  "planner")
planner_workflow.add_edge("planner",             "store_plan_in_remem")
planner_workflow.add_edge("store_plan_in_remem", END)

planner_app = planner_workflow.compile(checkpointer=MemorySaver())
config: RunnableConfig = {"configurable": {"thread_id": "planner-1"}}