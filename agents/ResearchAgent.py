from typing import TypedDict, List, Optional, Annotated
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
from langsmith import trace
from LLM.llm import llm
from langchain_core.messages import SystemMessage, BaseMessage, AIMessage, ToolMessage, HumanMessage
from langgraph.prebuilt import ToolNode
from prompts.reasearch_agent_prompt import research_agent_prompt
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.runnables import RunnableConfig
from schemas.tools import ResearchOutput

logger = structlog.get_logger("research_agent")

prompt = research_agent_prompt


# Agent state
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    research_result: Optional[ResearchOutput]   # ← structured output, not markdown
    raw_docs: list[str]
    parsed_docs: list[str]
    errors: list[str]
    metadata: dict[str, str]


# ── LLM variants ─────────────────────────────────────────────────────────────
tools = [validate_url, craw_tool, parser_tool]
llm_with_tools   = llm.bind_tools(tools)               # used during tool-calling loop
llm_structured   = llm.with_structured_output(ResearchOutput)  # used for final extraction


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((TimeoutError, ConnectionError)),
    before_sleep=before_sleep_log(logging.getLogger("research_agent"), logging.WARNING),
    reraise=True,
)
async def _invoke_llm_with_retry(messages: list) -> AIMessage:
    """LLM invocation wrapped with tenacity retry for transient errors."""
    return await llm_with_tools.ainvoke(messages)


# ── Agent node ────────────────────────────────────────────────────────────────
@trace
async def research_agent(state: AgentState) -> dict:
    """Reasons over the conversation and calls tools as needed."""
    messages = state.get("messages", [])

    # Inject system prompt if not already present
    if not messages or not isinstance(messages[0], SystemMessage):
        system_msg = SystemMessage(content=prompt)
        messages = [system_msg] + messages

    try:
        logger.info("agent.processing_query")
        llm_response = await _invoke_llm_with_retry(messages)
        logger.info("agent.response_received", response_type=type(llm_response).__name__)

        # Still has tool calls → keep looping
        if hasattr(llm_response, "tool_calls") and llm_response.tool_calls:
            logger.info("agent.tool_calls_requested", tool_calls=llm_response.tool_calls)
            return {"messages": messages + [llm_response]}

        # No more tool calls → pass control to extract_structured_output
        logger.info("agent.reasoning_done")
        return {
            "messages": messages + [llm_response],
            "metadata": {
                **state.get("metadata", {}),
                "agent_finished_at": datetime.datetime.utcnow().isoformat(),
            },
        }

    except RetryError as e:
        logger.error("agent.retry_exhausted", error=str(e))
        error_msg = AIMessage(content=f"LLM failed after all retries: {e}")
        return {
            "messages": [error_msg],
            "errors": state.get("errors", []) + [f"RetryError: {e}"],
        }
    except Exception as e:
        logger.exception("agent.error", error=str(e))
        error_msg = AIMessage(content=f"I encountered an error: {e}")
        return {
            "messages": [error_msg],
            "errors": state.get("errors", []) + [str(e)],
        }


# ── Process tool outputs ──────────────────────────────────────────────────────
@trace
def process_tool_outputs(state: AgentState) -> dict:
    """
    After ToolNode runs, read the latest ToolMessages and append their
    content to the correct state bucket based on tool name.
    """
    messages = state.get("messages", [])

    # Find ToolMessages from the most recent tool-call round
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


# ── Structured output extraction node ────────────────────────────────────────
@trace
async def extract_structured_output(state: AgentState) -> dict:
    """
    Final node: takes all collected raw_docs and parsed_docs from state,
    asks the LLM to extract a ResearchOutput object from them.
    """
    raw_docs    = state.get("raw_docs", [])
    parsed_docs = state.get("parsed_docs", [])

    context_parts = []
    if raw_docs:
        context_parts.append("## Raw crawled content\n" + "\n---\n".join(raw_docs))
    if parsed_docs:
        context_parts.append("## Parsed API data\n" + "\n---\n".join(parsed_docs))

    if not context_parts:
        # Nothing was collected — try to extract from the conversation itself
        messages = state.get("messages", [])
        context_parts = [
            msg.content for msg in messages
            if isinstance(msg, AIMessage) and isinstance(msg.content, str)
        ]

    extraction_prompt = (
        "Based on the following research content, extract and return a structured "
        "ResearchOutput with: base_url, auth_method, endpoints (list of path/method/description), "
        "rate_limits, example, webhooks, and api_versioning.\n\n"
        + "\n\n".join(context_parts)
    )

    try:
        logger.info("extractor.running")
        result: ResearchOutput = await llm_structured.ainvoke([
            HumanMessage(content=extraction_prompt)
        ])
        logger.info("extractor.succeeded", base_url=result.base_url)
        return {
            "research_result": result,
            "metadata": {
                **state.get("metadata", {}),
                "completed_at": datetime.datetime.utcnow().isoformat(),
            },
        }

    except Exception as e:
        logger.exception("extractor.error", error=str(e))
        return {
            "errors": state.get("errors", []) + [f"ExtractionError: {e}"],
        }


# ── Routing ───────────────────────────────────────────────────────────────────
@trace
def should_continue(state: AgentState) -> str:
    """Route to tools if tool calls pending, otherwise to structured extraction."""
    messages = state.get("messages", [])
    if not messages:
        logger.info("router.no_messages")
        return "extract"

    last_message = messages[-1]

    if isinstance(last_message, AIMessage) and getattr(last_message, "tool_calls", None):
        logger.info("router.continue_to_tools")
        return "continue"

    logger.info("router.to_extraction")
    return "extract"


# ── Graph ─────────────────────────────────────────────────────────────────────
graph = StateGraph(AgentState)
graph.add_node("agent", research_agent)
graph.add_node("tools", ToolNode(tools))
graph.add_node("process_tool_outputs", process_tool_outputs)
graph.add_node("extract_structured_output", extract_structured_output)

graph.set_entry_point("agent")
graph.add_conditional_edges(
    "agent",
    should_continue,
    {"continue": "tools", "extract": "extract_structured_output"},
)
graph.add_edge("tools", "process_tool_outputs")
graph.add_edge("process_tool_outputs", "agent")
graph.add_edge("extract_structured_output", END)

checkpointer = InMemorySaver()
app = graph.compile(checkpointer=checkpointer)
config: RunnableConfig = {"configurable": {"thread_id": "1"}}