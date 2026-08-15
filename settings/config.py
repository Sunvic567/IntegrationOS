"""
Central configuration for the Agent-Powered API Onboarding pipeline.

Loads environment-provided secrets and settings shared across every agent in
the pipeline (Orchestrator -> ResearchAgent -> Planner -> TaskDispatcher ->
{TesterWorker, SDKWorker, WriterWorker} -> ReviewerAgent). Each agent's role
and its dependency on the values below is summarized here so the
configuration surface stays traceable to the agent that actually consumes it:

- Orchestrator: no external credentials of its own; sequences every other
  agent and aggregates their results using the config below.
- Research Agent: uses FIRECRAW_API_KEY to crawl live API documentation and
  GOOGLE_API_KEY / MODEL_NAME to run the extraction LLM that turns crawled
  pages into a structured research profile.
- Planner Agent: uses GOOGLE_API_KEY / MODEL_NAME to turn the research
  profile into a dependency-ordered execution plan.
- Task Dispatcher: no external credentials; routes plan tasks to workers.
- Tester Worker: makes live dry-run HTTP calls directly against the target
  API (no key from this module) to validate auth, endpoints, and webhooks.
- SDK Worker: uses GOOGLE_API_KEY / MODEL_NAME to generate the Python client
  SDK from the endpoint list.
- Writer Worker: uses GOOGLE_API_KEY / MODEL_NAME to generate the markdown
  integration guide in batches.
- Reviewer Agent: no external credentials; evaluates task results already
  produced by the workers above.
- REMEM_API_KEY is shared across the Research and Planner agents as the
  cross-run cache (Remem) that lets repeat runs against a known API domain
  skip redundant crawling and planning.
"""

import os
from typing import Final
from dotenv import load_dotenv

load_dotenv()


def _get_env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value is None:
        return ""
    return value.strip()


# Used by: Research Agent — authenticates Firecrawl crawl/sitemap requests
# against target API documentation sites.
FIRECRAW_API_KEY: Final[str] = _get_env("FIRECRAW_API_KEY")

# Used by: Research Agent, Planner Agent, SDK Worker, Writer Worker — Gemini
# credentials for every LLM-driven extraction/generation step in the pipeline.
GOOGLE_API_KEY: Final[str] = _get_env("GOOGLE_API_KEY")

# Used by: Research Agent, Planner Agent, SDK Worker, Writer Worker — which
# Gemini model those agents call through LangChain's init_chat_model.
MODEL_NAME: Final[str] = _get_env("MODEL_NAME", "gemini-2.0-flash")

# Used by: Research Agent, Planner Agent — Remem cache credentials so
# research profiles and execution plans are cached/reused per API domain.
REMEM_API_KEY: Final[str] = _get_env("REMEM_API_KEY")

# Backward-compatible alias used elsewhere in the project
firecraw_key: Final[str] = FIRECRAW_API_KEY