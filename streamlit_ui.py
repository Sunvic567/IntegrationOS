"""
app.py — Streamlit interface for the API Onboarding Agent.

Run with:
    streamlit run app.py
"""
import asyncio
import json
import re
import uuid
from typing import Any, cast, Optional

try:
    import streamlit as streamlit_module
except ImportError:  # pragma: no cover - handled for environments without Streamlit installed
    streamlit_module = None

from langchain_core.runnables import RunnableConfig

from agents.Orchestrator import OrchestratorState, orchestrator_app, orchestrator_config

if streamlit_module is None:
    raise RuntimeError("Streamlit is not installed. Install it with 'pip install streamlit' before running this UI.")

st = cast(Any, streamlit_module)
st.set_page_config(page_title="API Onboarding Assistant", page_icon="🧩", layout="wide")


def _sanitize_markdown(text: str) -> str:
    """Collapse malformed/repeated table-separator runs the writer LLM can
    occasionally produce on very large docs, so broken markdown never renders."""
    if not text:
        return text
    # Collapse runs of 4+ repeated "| :--- |"-style separator fragments
    text = re.sub(r'(\|\s*:?-{1,}:?\s*)\1{3,}', '', text)
    # Collapse excessive blank lines left behind by the above
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🧩 Your API Onboarding Assistant")
    st.markdown(
        "Give it a link to any API's documentation and it will research the "
        "endpoints, generate an integration guide, and produce starter SDK "
        "code — no need to run anything yourself."
    )
    st.markdown("---")
    st.caption("Paste a docs URL (e.g. `https://docs.stripe.com/api`) and click Run.")

# ── Status mapping: pipeline event names → friendly text ──────────────────
STATUS_MESSAGES = {
    "orchestrator.validate_input": "Validating your request…",
    "guardrail.checking": "Running safety check…",
    "guardrail.safe": "Safety check passed…",
    "orchestrator.validate.checking": "Checking the URL is reachable…",
    "orchestrator.validate.passed": "URL validated…",
    "orchestrator.research.start": "Starting research…",
    "remem_cache.checking": "Checking cache for prior research…",
    "remem_cache.miss": "No cached result found, researching fresh…",
    "remem_cache.hit": "Found cached research — reusing it…",
    "agent.processing_query": "Planning next step…",
    "crawler.sitemap_found": "Found the site's sitemap…",
    "crawler.filtered_urls": "Filtering to relevant docs pages…",
    "crawler.succeeded": "Finished crawling docs…",
    "extractor.running": "Extracting structured API data…",
    "orchestrator.planner.start": "Planning the onboarding tasks…",
    "orchestrator.dispatcher.start": "Completing onboarding guide…",
    "orchestrator.reviewer.start": "Reviewing generated output…",
}

# ── Main area ────────────────────────────────────────────────────────────
st.title("API Onboarding Assistant")

query = st.text_input(
    "API documentation URL",
    placeholder="https://docs.stripe.com/api",
)

run_clicked = st.button("Run", type="primary", disabled=not query.strip())

status_placeholder = st.empty()
result_placeholder = st.container()
session_state = getattr(st, "session_state", None)
components = getattr(st, "components", None)

if run_clicked and query.strip():
    api_url = query.strip()
    run_id = uuid.uuid4().hex[:8]  # unique per invocation — never reuse thread state

    config: RunnableConfig = {
        **orchestrator_config,
        "configurable": {"thread_id": f"streamlit-{api_url}-{run_id}"},
    }

    initial_state = cast(
        OrchestratorState,
        {
            "messages": [],
            "api_url": api_url,
            "run_id": run_id,
            "research_result": None,
            "plan": None,
            "dispatch_result": None,
            "review_report": None,
            "review_verdict": None,
            "errors": [],
        },
    )

    async def run_pipeline() -> dict[str, Any]:
        final_state: Optional[dict[str, Any]] = None
        async for event in orchestrator_app.astream_events(
            initial_state, config=config, version="v2"
        ):
            name = event.get("name", "")
            msg = STATUS_MESSAGES.get(name)
            if msg:
                status_placeholder.info(f"⏳ {msg}")
            if event.get("event") == "on_chain_end" and name == "LangGraph":
                final_state = event.get("data", {}).get("output")
        return final_state

    with st.spinner("Working on it…"):
        try:
            final_state = asyncio.run(run_pipeline())
            status_placeholder.success("✅ Pipeline completed")
        except Exception as e:
            final_state = None
            status_placeholder.error(f"Pipeline failed: {e}")

    if final_state:
        verdict = final_state.get("review_verdict", "UNKNOWN")
        errors = final_state.get("errors", [])
        dispatch_result = final_state.get("dispatch_result")

        with result_placeholder:
            if verdict == "PASS":
                st.success("✅ Onboarding complete")
            elif errors:
                st.error("⛔ Pipeline aborted")
                for err in errors:
                    st.write(err)
            else:
                st.warning("Pipeline finished without a clear pass verdict.")

            if dispatch_result:
                doc_output = getattr(dispatch_result, "doc_output", None)
                sdk_output = getattr(dispatch_result, "sdk_output", None)

                if doc_output:
                    doc_output_clean = _sanitize_markdown(doc_output)
                    st.subheader("Integration Guide")
                    col1, col2 = st.columns([0.94, 0.06])
                    with col1:
                        st.markdown(doc_output_clean)
                    with col2:
                        st.button(
                            "📋",
                            key="copy_doc",
                            help="Copy integration guide",
                            on_click=lambda: (
                                session_state.update({"_copy_target": doc_output_clean})
                                if session_state is not None
                                else None
                            ),
                        )

                if sdk_output:
                    st.subheader("Generated SDK")
                    st.code(sdk_output, language="python")
                    st.button(
                        "📋 Copy SDK code",
                        key="copy_sdk",
                        on_click=lambda: (
                            session_state.update({"_copy_target": sdk_output})
                            if session_state is not None
                            else None
                        ),
                    )

# ── Copy-to-clipboard support ───────────────────────────────────────────
# Streamlit's st.button can't write to the OS clipboard directly, so we
# render a small JS snippet that copies whatever the user last clicked.
if session_state is not None and session_state.get("_copy_target"):
    text_to_copy = json.dumps(session_state["_copy_target"])
    if components is not None:
        components.v1.html(
            f"""
            <script>
            navigator.clipboard.writeText({text_to_copy});
            </script>
            """,
            height=0,
        )
    st.toast("Copied to clipboard!")
    session_state["_copy_target"] = None