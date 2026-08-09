"""
main.py — entry point for the API Onboarding Agent pipeline.

Usage:
    uv run python main.py
    uv run python main.py --url https://docs.stripe.com/api

The pipeline runs end-to-end:
  validate_input → research → plan → dispatch (Tester/SDK/Writer) → review → report
"""
import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import structlog
from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig

load_dotenv()

from agents.Orchestrator import orchestrator_app, orchestrator_config
from monitoring import configure_logging, log_run_summary, save_monitoring_snapshot
from schemas.tools import DispatchResult

logger = structlog.get_logger("main")

configure_logging()


def _save_outputs(dispatch_result: DispatchResult, output_dir: Path) -> None:
    """Persist SDK and documentation outputs to files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if dispatch_result.sdk_output:
        sdk_path = output_dir / "generated_sdk.py"
        sdk_path.write_text(dispatch_result.sdk_output, encoding="utf-8")
        print(f"\n📦 SDK saved → {sdk_path}")

    if dispatch_result.doc_output:
        doc_path = output_dir / "integration_guide.md"
        doc_path.write_text(dispatch_result.doc_output, encoding="utf-8")
        print(f"📄 Docs saved → {doc_path}")

    # Always write a JSON summary of all task results
    results_path = output_dir / "task_results.json"
    results_path.write_text(
        json.dumps(
            [r.model_dump() for r in dispatch_result.results],
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"📊 Task results → {results_path}")


async def run(api_url: str) -> int:
    """
    Invoke the Orchestrator and print a summary.
    Returns exit code 0 (pass) or 1 (fail / abort).
    """
    start_time = time.perf_counter()
    print(f"\n🚀 Starting API Onboarding Pipeline for: {api_url}\n")

    logger.info("pipeline.start", api_url=api_url)

    run_id = uuid.uuid4().hex[:8]

    config: RunnableConfig = {
        **orchestrator_config,
         "configurable": {"thread_id": f"orchestrator-{api_url}-{run_id}"},
    }

    final_state = await orchestrator_app.ainvoke(
        {
            "messages":        [],
            "api_url":         api_url,
            "run_id":          run_id,
            "research_result": None,
            "plan":            None,
            "dispatch_result": None,
            "review_report":   None,
            "review_verdict":  None,
            "errors":          [],
        },
        config=config,
    )

    # ── Print the conversation log ─────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("PIPELINE LOG")
    print("═" * 60)
    for msg in final_state.get("messages", []):
        print(f"\n{msg.content}")

    # ── Save outputs ───────────────────────────────────────────────────────────
    dispatch_result: DispatchResult | None = final_state.get("dispatch_result")
    if dispatch_result:
        _save_outputs(dispatch_result, Path("output"))

    # ── Final verdict ──────────────────────────────────────────────────────────
    verdict = final_state.get("review_verdict", "UNKNOWN")
    errors  = final_state.get("errors", [])
    duration_seconds = time.perf_counter() - start_time

    log_run_summary(api_url, verdict, errors, duration_seconds)
    save_monitoring_snapshot(
        Path("output"),
        {
            "api_url": api_url,
            "verdict": verdict,
            "error_count": len(errors),
            "duration_seconds": round(duration_seconds, 3),
            "errors": errors,
        },
    )

    print("\n" + "═" * 60)
    if verdict == "PASS":
        print("✅  PIPELINE PASSED")
    elif errors:
        print(f"⛔  PIPELINE ABORTED — {len(errors)} error(s)")
    else:
        print("❌  PIPELINE FAILED — check task_results.json for details")
    print("═" * 60 + "\n")

    return 0 if verdict == "PASS" else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent-powered API Onboarding Pipeline",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="",
        help="URL of the API documentation to onboard (e.g. https://docs.stripe.com/api)",
    )
    args = parser.parse_args()

    api_url = args.url.strip()
    if not api_url:
        api_url = input("Enter API docs URL: ").strip()

    if not api_url:
        print("Error: no URL provided.", file=sys.stderr)
        sys.exit(1)

    exit_code = asyncio.run(run(api_url))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
