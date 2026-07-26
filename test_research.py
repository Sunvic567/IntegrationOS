"""
Quick smoke test for the Research Agent.
Run from the project root:  python test_research.py
"""
import asyncio
from langchain_core.messages import HumanMessage
from agents.ResearchAgent import app, config

# ── Swap this URL to test any API ────────────────────────────────────────────
TEST_URL = "https://pokeapi.co/docs/v2"
# ─────────────────────────────────────────────────────────────────────────────


async def run():
    print(f"\n🔍  Researching: {TEST_URL}\n{'─' * 50}")

    result = await app.ainvoke(
        {
            "messages": [HumanMessage(content=f"Research the API at {TEST_URL}")],
            "raw_docs": [],
            "parsed_docs": [],
            "errors": [],
            "metadata": {},
            "research_result": None,
        },
        config=config,
    )

    errors = result.get("errors", [])
    if errors:
        print(f"❌  Errors:\n  " + "\n  ".join(errors))

    research = result.get("research_result")
    if research:
        print("✅  Result:")
        print(f"  base_url     : {research.base_url}")
        print(f"  auth_method  : {research.auth_method}")
        print(f"  endpoints    : {len(research.endpoints)} found")
        for ep in research.endpoints[:5]:          # show first 5
            print(f"    [{ep.method}] {ep.path} — {ep.description}")
        print(f"  rate_limits  : {research.rate_limits}")
        print(f"  webhooks     : {research.webhooks}")
        print(f"  api_version  : {research.api_versioning}")
        if research.example:
            print(f"  example      : {research.example[:120]}...")
    else:
        print("⚠️  No structured result returned.")
        msgs = result.get("messages", [])
        if msgs:
            print(f"  Last message : {msgs[-1].content[:200]}")


if __name__ == "__main__":
    asyncio.run(run())
