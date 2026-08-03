from typing import TypedDict, List, Optional, Annotated
from urllib.parse import urlparse
import structlog
import datetime
import logging
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)
from langgraph.graph import StateGraph, END, add_messages
from tool.crawler import craw_tool
from tool.parser import parser_tool
from tool.validator import validate_url
from LLM.llm import llm
from langchain_core.messages import SystemMessage, BaseMessage, AIMessage, ToolMessage, HumanMessage
from langgraph.prebuilt import ToolNode
from prompts.reasearch_agent_prompt import research_agent_prompt
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.runnables import RunnableConfig
from schemas.tools import ResearchOutput
from settings.remem_client import remem, RESEARCH_AGENT_ID

logger = structlog.get_logger("research_agent")

prompt = research_agent_prompt


def _api_user_id(url: str) -> str:
    """
    Extract the netloc from the URL to use as a stable Remem key.
    e.g. "https://docs.stripe.com/api" → "docs.stripe.com"
    Falls back to the raw URL string if parsing fails.
    """
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def _compute_confidence(result: "ResearchOutput") -> tuple[float, list[str]]:
    """
    Score research completeness on a 0.0–1.0 scale.
    The Planner uses this to decide whether to proceed or request a recrawl.

    Weights reflect how critical each field is for building a test plan:
      endpoints    0.30  — can't plan without them
      auth_method  0.20  — required for any authenticated test
      rate_limits  0.15
      error_codes  0.15
      example      0.10  — confirms at least one real call is documented
      pagination   0.10

    Returns (score, quality_flags) where quality_flags names every missing field.
    """
    score = 0.0
    flags: list[str] = []

    if result.endpoints:
        score += 0.30
    else:
        flags.append("no_endpoints")

    if result.auth_method and result.auth_method.strip().lower() not in ("", "unknown", "none"):
        score += 0.20
    else:
        flags.append("no_auth")

    if result.rate_limits:
        score += 0.15
    else:
        flags.append("no_rate_limits")

    if result.error_codes:
        score += 0.15
    else:
        flags.append("no_error_codes")

    if result.example:
        score += 0.10
    else:
        flags.append("no_example")

    if result.pagination:
        score += 0.10
    else:
        flags.append("no_pagination")

    return round(score, 2), flags


class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    research_result: Optional[ResearchOutput]
    raw_docs: list[str]
    parsed_docs: list[str]
    errors: list[str]
    metadata: dict[str, str]
    api_url: Optional[str]
    cache_hit: bool


tools = [validate_url, craw_tool, parser_tool]
llm_with_tools = llm.bind_tools(tools)
llm_structured  = llm.with_structured_output(ResearchOutput)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((TimeoutError, ConnectionError)),
    before_sleep=before_sleep_log(logging.getLogger("research_agent"), logging.WARNING),
    reraise=True,
)
async def _invoke_llm_with_retry(messages: list) -> AIMessage:
    return await llm_with_tools.ainvoke(messages)


async def check_remem_cache(state: AgentState) -> dict:
    """
    Entry node. Checks Remem before doing any crawling.
    A cache hit skips the entire research loop — the stored ResearchOutput is
    returned directly. A parse failure on a cached result is treated as a miss
    so we fall back to fresh crawling rather than crashing.
    """
    api_url = state.get("api_url", "")
    if not api_url:
        logger.info("remem_cache.no_url_yet")
        return {"cache_hit": False}

    user_id = _api_user_id(api_url)
    logger.info("remem_cache.checking", user_id=user_id)

    try:
        memories = remem.recall(
            query=f"API research result for {api_url}",
            user_id=user_id,
            agent_id=RESEARCH_AGENT_ID,
        )

        if memories:
            cached_json = memories[0].get("content", "")
            logger.info(
                "remem_cache.hit",
                user_id=user_id,
                score=memories[0].get("score"),
                score_detail=memories[0].get("score_detail"),
            )
            try:
                cached_result = ResearchOutput.model_validate_json(cached_json)
                return {
                    "research_result": cached_result,
                    "cache_hit": True,
                    "metadata": {
                        **state.get("metadata", {}),
                        "remem_cache": "hit",
                        "remem_user_id": user_id,
                        "remem_score": str(memories[0].get("score", "")),
                        "cached_at": memories[0].get("created_at", ""),
                    },
                }
            except Exception as parse_err:
                logger.warning("remem_cache.parse_failed", error=str(parse_err))
                return {"cache_hit": False}

    except Exception as e:
        logger.warning("remem_cache.error", error=str(e))

    logger.info("remem_cache.miss", user_id=user_id)
    return {"cache_hit": False}


async def store_in_remem(state: AgentState) -> dict:
    """
    Persists the ResearchOutput after a fresh crawl so the next run for the
    same API domain hits the cache instead of re-crawling.
    Storage failure is non-fatal — the result is already in state.
    """
    result: Optional[ResearchOutput] = state.get("research_result")
    api_url = state.get("api_url", "")

    if result is None:
        logger.warning("remem_store.no_result_to_store")
        return {}

    user_id = _api_user_id(api_url)

    try:
        memory_id = remem.remember(
            result.model_dump_json(),
            user_id=user_id,
            agent_id=RESEARCH_AGENT_ID,
            memory_type="semantic",
            importance=0.9,
        )
        logger.info("remem_store.saved", user_id=user_id, memory_id=memory_id)
        return {
            "metadata": {
                **state.get("metadata", {}),
                "remem_store": "ok",
                "remem_memory_id": str(memory_id),
                "remem_user_id": user_id,
            }
        }
    except Exception as e:
        logger.warning("remem_store.error", error=str(e))
        return {}


async def research_agent(state: AgentState) -> dict:
    messages = state.get("messages", [])

    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=prompt)] + messages

    try:
        logger.info("agent.processing_query")
        llm_response = await _invoke_llm_with_retry(messages)
        logger.info("agent.response_received", response_type=type(llm_response).__name__)

        if hasattr(llm_response, "tool_calls") and llm_response.tool_calls:
            logger.info("agent.tool_calls_requested", tool_calls=llm_response.tool_calls)
            return {"messages": [llm_response]}

        logger.info("agent.reasoning_done")
        return {
            "messages": [llm_response],
            "metadata": {
                **state.get("metadata", {}),
                "agent_finished_at": datetime.datetime.utcnow().isoformat(),
            },
        }

    except RetryError as e:
        logger.error("agent.retry_exhausted", error=str(e))
        return {
            "messages": [AIMessage(content=f"LLM failed after all retries: {e}")],
            "errors": state.get("errors", []) + [f"RetryError: {e}"],
        }
    except Exception as e:
        logger.exception("agent.error", error=str(e))
        return {
            "messages": [AIMessage(content=f"I encountered an error: {e}")],
            "errors": state.get("errors", []) + [str(e)],
        }


def process_tool_outputs(state: AgentState) -> dict:
    """
    Reads ToolMessages from the most recent tool-call round and routes their
    content into the correct state bucket (raw_docs, parsed_docs, metadata).
    We scan backwards from the last AIMessage with tool_calls to avoid picking
    up tool outputs from earlier rounds.
    """
    messages = state.get("messages", [])

    last_ai_idx = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            last_ai_idx = i

    recent_tool_msgs: List[ToolMessage] = [
        m for m in messages[last_ai_idx + 1:]
        if isinstance(m, ToolMessage)
    ]

    raw_docs    = list(state.get("raw_docs", []))
    parsed_docs = list(state.get("parsed_docs", []))
    errors      = list(state.get("errors", []))
    metadata    = dict(state.get("metadata", {}))

    for tm in recent_tool_msgs:
        tool_name = getattr(tm, "name", "")
        content   = tm.content if isinstance(tm.content, str) else str(tm.content)

        if tool_name == "craw_tool":
            raw_docs.append(content)
            logger.info("state.raw_docs.updated", count=len(raw_docs))
        elif tool_name == "parser_tool":
            parsed_docs.append(content)
            logger.info("state.parsed_docs.updated", count=len(parsed_docs))
        elif tool_name == "validate_url":
            metadata[f"validated_url_{len(metadata)}"] = content
            logger.info("state.metadata.updated", url=content)
        else:
            raw_docs.append(f"[{tool_name}] {content}")
            logger.info("state.raw_docs.fallback", tool=tool_name)

    return {
        "raw_docs":    raw_docs,
        "parsed_docs": parsed_docs,
        "errors":      errors,
        "metadata":    metadata,
    }


async def extract_structured_output(state: AgentState) -> dict:
    """
    Converts accumulated raw_docs and parsed_docs into a structured ResearchOutput.
    Falls back to pulling AI message content if no docs were collected (edge case
    where the agent summarised findings in prose rather than calling tools).
    Confidence is computed and stamped here so the Planner receives it immediately.
    """
    raw_docs    = state.get("raw_docs", [])
    parsed_docs = state.get("parsed_docs", [])

    context_parts = []
    if raw_docs:
        context_parts.append("## Raw crawled content\n" + "\n---\n".join(raw_docs))
    if parsed_docs:
        context_parts.append("## Parsed API data\n" + "\n---\n".join(parsed_docs))

    if not context_parts:
        messages = state.get("messages", [])
        context_parts = [
            msg.content for msg in messages
            if isinstance(msg, AIMessage) and isinstance(msg.content, str)
        ]

    extraction_prompt = (
        "Based on the following research content, extract and return a structured ResearchOutput.\n\n"
        "Fields to populate:\n"
        "- base_url: the API base URL\n"
        "- auth_method: authentication type and how credentials are passed\n"
        "- endpoints: list of endpoints, each with:\n"
        "    - path, method, description\n"
        "    - parameters: list of {name, location, type, required, description}\n"
        "    - response_schema: {status_code, description, example}\n"
        "- rate_limits: requests per minute/hour/day and retry behaviour\n"
        "- pagination: {type, parameter, description} — cursor/page-number/offset/link-header\n"
        "- error_codes: list of {code, name, description} for every documented error\n"
        "- example: a concrete curl or code snippet\n"
        "- webhooks: list of webhook event details\n"
        "- api_versioning: how API versions are indicated\n\n"
        "Leave a field as null/empty if the information is not present in the content.\n\n"
        + "\n\n".join(context_parts)
    )

    try:
        logger.info("extractor.running")
        result: ResearchOutput = await llm_structured.ainvoke([
            HumanMessage(content=extraction_prompt)
        ])

        confidence, quality_flags = _compute_confidence(result)
        result.confidence    = confidence
        result.quality_flags = quality_flags

        logger.info(
            "extractor.succeeded",
            base_url=result.base_url,
            confidence=confidence,
            quality_flags=quality_flags,
            endpoint_count=len(result.endpoints),
        )
        return {
            "research_result": result,
            "metadata": {
                **state.get("metadata", {}),
                "completed_at": datetime.datetime.utcnow().isoformat(),
                "confidence": str(confidence),
                "quality_flags": ",".join(quality_flags),
            },
        }

    except Exception as e:
        logger.exception("extractor.error", error=str(e))
        return {
            "errors": state.get("errors", []) + [f"ExtractionError: {e}"],
        }


def route_after_cache_check(state: AgentState) -> str:
    if state.get("cache_hit", False):
        logger.info("router.cache_hit_skipping_research")
        return "skip"
    logger.info("router.cache_miss_starting_research")
    return "research"


def should_continue(state: AgentState) -> str:
    messages = state.get("messages", [])
    if not messages:
        return "extract"

    last_message = messages[-1]
    if isinstance(last_message, AIMessage) and getattr(last_message, "tool_calls", None):
        logger.info("router.continue_to_tools")
        return "continue"

    logger.info("router.to_extraction")
    return "extract"


# Graph:
#   check_remem_cache → [skip → END] or [agent ↔ tools → extract_structured_output → store_in_remem → END]
graph = StateGraph(AgentState)

graph.add_node("check_remem_cache",         check_remem_cache)
graph.add_node("agent",                     research_agent)
graph.add_node("tools",                     ToolNode(tools))
graph.add_node("process_tool_outputs",      process_tool_outputs)
graph.add_node("extract_structured_output", extract_structured_output)
graph.add_node("store_in_remem",            store_in_remem)

graph.set_entry_point("check_remem_cache")

graph.add_conditional_edges(
    "check_remem_cache",
    route_after_cache_check,
    {"skip": END, "research": "agent"},
)
graph.add_conditional_edges(
    "agent",
    should_continue,
    {"continue": "tools", "extract": "extract_structured_output"},
)
graph.add_edge("tools",                     "process_tool_outputs")
graph.add_edge("process_tool_outputs",      "agent")
graph.add_edge("extract_structured_output", "store_in_remem")
graph.add_edge("store_in_remem",            END)

checkpointer = MemorySaver()
app = graph.compile(checkpointer=checkpointer)
config: RunnableConfig = {"configurable": {"thread_id": "1"}, "recursion_limit": 10}