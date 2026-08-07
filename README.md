# Agent-Powered API Onboarding

This project is an experimental agent-driven pipeline for onboarding an API from its documentation URL. It researches the API, builds an execution plan, dispatches work to specialized agents, and produces artifacts such as an SDK stub and integration guide.

## What it does

Given a documentation URL, the system will:

1. Validate the input URL.
2. Research the API surface and relevant details.
3. Create a structured execution plan.
4. Dispatch tasks to workers for testing, SDK generation, and documentation writing.
5. Review the results and emit a final verdict.

## Architecture

The workflow is orchestrated by a LangGraph-based pipeline:

- Orchestrator: coordinates the full workflow.
- Research Agent: extracts API details from documentation.
- Planner Agent: turns research into an execution plan.
- Task Dispatcher: orders tasks and runs independent work in parallel.
- Executor Agents: handle testing, SDK generation, and documentation output.
- Reviewer Agent: evaluates results and retries where appropriate.

## Project structure

- main.py: entry point for running the pipeline.
- agents/: workflow agents and dispatch logic.
- guardrails/: safety checks for input handling.
- LLM/: model initialization and provider configuration.
- schemas/: shared Pydantic models for research, plans, and results.
- tool/: crawling, parsing, and validation helpers.
- settings/: configuration and environment handling.
- tests/: regression tests for the core workflow components.

## Requirements

- Python 3.13+
- Access to an LLM provider compatible with OpenRouter/OpenAI-style APIs
- Optional: Firecrawl API key and Remem API key depending on runtime configuration

## Setup

### 1. Create and activate a virtual environment

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -e .
```

### 3. Configure environment variables

Create a `.env` file in the project root with values such as:

```env
OPENROUTER_API_KEY=your_key_here
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=openai/gpt-4o-mini
FIRECRAW_API_KEY=your_firecrawl_key
REMEM_API_KEY=your_remem_key
```

> The project will also accept `OPENAI_API_KEY` as a fallback source.

## Run the pipeline

```bash
python main.py --url https://docs.stripe.com/api
```

The run will write output artifacts into the `output/` directory, including:

- generated_sdk.py
- integration_guide.md
- task_results.json

## Run the smoke test

```bash
python test_research.py
```

## Run tests

```bash
pytest -q
```

## Notes

This repository is currently structured as a strong prototype and workflow scaffold. It is suitable for experimentation and iterative development, but real-world runtime validation depends on the availability of the configured external services and model credentials.
