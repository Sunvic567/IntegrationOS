import os
import settings.config  # noqa: F401 — triggers load_dotenv()
from remem import RememClient

REMEM_API_KEY = os.getenv("REMEM_API_KEY", "")
REMEM_BASE_URL = os.getenv("REMEM_BASE_URL", "https://api.remem.online")

remem = RememClient(
    api_key=REMEM_API_KEY,
    base_url=REMEM_BASE_URL,
)

# Memories are keyed by API domain (user_id) + agent role (agent_id).
# This keeps research memories and plan memories separate even for the same API.
RESEARCH_AGENT_ID = "research_agent"
PLANNER_AGENT_ID  = "planner_agent"
