"""
ExecutorAgent — three execution workers that the Task Dispatcher calls.

Workers:
  TesterWorker  — dry-run validation for auth / endpoint / rate / error / webhook tasks
  SDKWorker     — LLM-generated Python SDK client
  WriterWorker  — LLM-generated markdown integration guide
"""
from __future__ import annotations

import ast
import asyncio
import json
from typing import Any, Literal, Optional

import httpx
import structlog

from LLM.llm import llm
from langchain_core.messages import HumanMessage, SystemMessage
from prompts.sdk_generator_prompt import sdk_generator_prompt
from prompts.doc_writer_prompt import doc_writer_prompt
from schemas.tools import Task, TaskResult, ExecutionPlan, ResearchOutput

logger = structlog.get_logger("executor_agent")

# ── Shared HTTP client (re-used across all Tester calls in a run) ──────────────
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    return _http_client


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_llm_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested_content = item.get("content")
                    if isinstance(nested_content, str):
                        parts.append(nested_content)
                    elif nested_content is not None:
                        parts.append(str(nested_content))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts).strip()

    if content is None:
        return ""

    return str(content).strip()


def _result(
    task: Task,
    execution_status: Literal["completed", "failed", "skipped"],
    verification_status: Literal["verified", "failed", "inconclusive", "not_applicable"] = "not_applicable",
    output: str = "",
    error: str = "",
    **meta,
) -> TaskResult:
    return TaskResult(
        task_id=task.id,
        task_name=task.name,
        tool=task.tool,
        execution_status=execution_status,
        verification_status=verification_status,
        output=output or None,
        error=error or None,
        metadata=meta,
    )


def _skipped(task: Task, reason: str) -> TaskResult:
    logger.warning("worker.skipped", task_id=task.id, task_name=task.name, reason=reason)
    return _result(task, "skipped", verification_status="not_applicable", error=reason)


# ══════════════════════════════════════════════════════════════════════════════
# TesterWorker
# ══════════════════════════════════════════════════════════════════════════════

class TesterWorker:
    """
    Performs *dry-run* validation of auth / endpoint / rate / error / webhook tasks.

    Dry-run means:
      - auth_tester     → HEAD request to the first endpoint; checks 200/401 only
      - endpoint_tester → OPTIONS or HEAD on the path; verifies reachability
      - rate_tester     → Confirms rate-limit headers appear in a HEAD response
      - error_tester    → Validates that the error codes in the plan are plausible HTTP codes
      - webhook_tester  → Asserts webhook event names are non-empty strings
    """

    TESTER_TOOLS = {
        "auth_tester",
        "endpoint_tester",
        "rate_tester",
        "error_tester",
        "webhook_tester",
    }

    def handles(self, tool: str) -> bool:
        return tool in self.TESTER_TOOLS

    async def run(self, task: Task, base_url: str) -> TaskResult:
        handler = {
            "auth_tester":     self._auth_test,
            "endpoint_tester": self._endpoint_test,
            "rate_tester":     self._rate_test,
            "error_tester":    self._error_test,
            "webhook_tester":  self._webhook_test,
        }.get(task.tool)

        if handler is None:
            return _result(task, "failed", verification_status="failed", error=f"Unknown tool '{task.tool}'")

        try:
            return await handler(task, base_url)
        except Exception as exc:
            logger.exception("tester.unexpected_error", task_id=task.id, error=str(exc))
            return _result(task, "failed", verification_status="failed", error=str(exc))

    # ── Auth tester ───────────────────────────────────────────────────────────
    async def _auth_test(self, task: Task, base_url: str) -> TaskResult:
        inputs   = task.inputs
        method   = inputs.get("method", "unknown")
        endpoint = inputs.get("endpoint", "/")
        url      = base_url.rstrip("/") + "/" + endpoint.lstrip("/")

        logger.info("tester.auth", url=url, method=method)
        client = _get_http_client()
        try:
            resp = await client.head(url)
            if resp.status_code in {401, 403}:
                verification_status = "verified"
                output = f"Auth dry-run: {method} auth on {url} → HTTP {resp.status_code} (authentication challenge observed)."
            elif 200 <= resp.status_code < 300:
                verification_status = "inconclusive"
                output = f"Auth dry-run: {method} auth on {url} → HTTP {resp.status_code} (endpoint responded without an auth challenge)."
            elif resp.status_code >= 500:
                verification_status = "inconclusive"
                output = f"Auth dry-run: {method} auth on {url} → HTTP {resp.status_code} (server error, unable to verify auth)."
            else:
                verification_status = "inconclusive"
                output = f"Auth dry-run: {method} auth on {url} → HTTP {resp.status_code} (response was inconclusive for auth enforcement)."
            return _result(
                task,
                "completed",
                verification_status=verification_status,
                output=output,
                http_status=resp.status_code,
            )
        except httpx.RequestError as exc:
            return _result(task, "failed", verification_status="failed", error=f"Network error reaching {url}: {exc}")

    # ── Endpoint tester ───────────────────────────────────────────────────────
    async def _endpoint_test(self, task: Task, base_url: str) -> TaskResult:
        inputs      = task.inputs
        path        = inputs.get("path", "/")
        http_method = inputs.get("http_method", "GET").upper()
        url         = base_url.rstrip("/") + "/" + path.lstrip("/")

        logger.info("tester.endpoint", url=url, method=http_method)
        client = _get_http_client()
        try:
            if http_method in {"GET", "HEAD", "OPTIONS"}:
                method_name = http_method.lower()
                request_fn = getattr(client, method_name)
                resp = await request_fn(url)
                verification_status = "inconclusive"
                output = f"Endpoint dry-run: executed {http_method} {url} → HTTP {resp.status_code}."
            else:
                verification_status = "inconclusive"
                output = (
                    f"Endpoint dry-run: skipped live execution for {http_method} {url} because "
                    "mutation methods require a sandboxed test setup or explicit permission."
                )
                return _result(task, "completed", verification_status=verification_status, output=output)
            return _result(
                task,
                "completed",
                verification_status=verification_status,
                output=output,
                http_status=resp.status_code,
            )
        except httpx.RequestError as exc:
            return _result(task, "failed", verification_status="failed", error=f"Network error reaching {url}: {exc}")

    # ── Rate tester ───────────────────────────────────────────────────────────
    async def _rate_test(self, task: Task, base_url: str) -> TaskResult:
        inputs         = task.inputs
        limit_str      = inputs.get("limit", "")
        probe_endpoint = inputs.get("probe_endpoint", "/")
        url            = base_url.rstrip("/") + "/" + probe_endpoint.lstrip("/")

        logger.info("tester.rate", url=url, limit=limit_str)
        client = _get_http_client()
        try:
            resp    = await client.head(url)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            rate_headers = {
                k: v for k, v in headers.items()
                if any(kw in k for kw in ("ratelimit", "rate-limit", "x-rate", "retry"))
            }
            verification_status = "verified" if rate_headers else "inconclusive"
            output = (
                f"Rate limit dry-run on {url} → HTTP {resp.status_code}\n"
                f"Documented limit: {limit_str}\n"
                f"Rate-limit headers found: {rate_headers or 'none (headers may be absent or returned on a different request type)'}"
            )
            return _result(task, "completed", verification_status=verification_status, output=output, rate_headers=rate_headers)
        except httpx.RequestError as exc:
            return _result(task, "failed", verification_status="failed", error=f"Network error reaching {url}: {exc}")

    # ── Error tester ──────────────────────────────────────────────────────────
    async def _error_test(self, task: Task, base_url: str) -> TaskResult:
        codes  = task.inputs.get("codes", [])
        logger.info("tester.error", codes=codes)

        valid_http = set(range(400, 600))
        issues: list[str] = []
        for code in codes:
            try:
                numeric = int(code)
                if numeric not in valid_http and numeric < 1000:
                    issues.append(f"Unexpected HTTP code: {code}")
            except ValueError:
                # Non-numeric codes (e.g. "RATE_LIMIT_EXCEEDED") are API-specific — allowed
                pass

        verification_status = "failed" if issues else "verified"
        output = (
            f"Error code dry-run: validated {len(codes)} codes.\n"
            + (f"Issues: {issues}" if issues else "All codes are plausible.")
        )
        return _result(task, "completed", verification_status=verification_status, output=output, validated_codes=codes, issues=issues)

    # ── Webhook tester ────────────────────────────────────────────────────────
    async def _webhook_test(self, task: Task, base_url: str) -> TaskResult:
        events = task.inputs.get("events", [])
        logger.info("tester.webhook", events=events)

        empty_events = [e for e in events if not str(e).strip()]
        verification_status = "failed" if empty_events else "verified"
        output = (
            f"Webhook dry-run: {len(events)} event(s) documented.\n"
            + (f"Empty/invalid events: {empty_events}" if empty_events else "All events are valid strings.")
        )
        return _result(task, "completed", verification_status=verification_status, output=output, events=events)


# ══════════════════════════════════════════════════════════════════════════════
# SDKWorker
# ══════════════════════════════════════════════════════════════════════════════

class SDKWorker:
    """
    Uses the LLM to generate a complete Python httpx client SDK from the ExecutionPlan.
    """

    def handles(self, tool: str) -> bool:
        return tool == "sdk_generator"

    async def run(self, task: Task, research: ResearchOutput) -> TaskResult:
        inputs = task.inputs
        base_url     = inputs.get("base_url", research.base_url)
        auth_method  = inputs.get("auth_method", research.auth_method)
        endpoints    = research.endpoints

        endpoint_summary = "\n".join(
            f"  - {ep.method} {ep.path}: {ep.description or 'no description'}"
            for ep in endpoints
        )

        human_content = (
            f"Generate a Python SDK for the following API:\n\n"
            f"Base URL: {base_url}\n"
            f"Auth method: {auth_method}\n\n"
            f"Endpoints:\n{endpoint_summary}\n\n"
            f"Return only the Python source code."
        )

        logger.info("sdk_worker.generating", base_url=base_url, endpoint_count=len(endpoints))
        try:
            response = await llm.ainvoke([
                SystemMessage(content=sdk_generator_prompt),
                HumanMessage(content=human_content),
            ])
            sdk_code = _normalize_llm_text(getattr(response, "content", ""))
            if not sdk_code.strip():
                raise ValueError("LLM returned no SDK code")
            try:
                ast.parse(sdk_code)
            except SyntaxError as exc:
                logger.warning("sdk_worker.syntax_error", error=str(exc))
                return _result(task, "failed", verification_status="failed", error=f"Generated SDK contains invalid Python: {exc}")
            logger.info("sdk_worker.done", chars=len(sdk_code))
            return _result(task, "completed", verification_status="inconclusive", output=sdk_code)
        except Exception as exc:
            logger.exception("sdk_worker.error", error=str(exc))
            return _result(task, "failed", verification_status="failed", error=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# WriterWorker
# ══════════════════════════════════════════════════════════════════════════════

def _validate_doc_output(research: ResearchOutput, doc_md: str) -> bool:
    if not doc_md.strip():
        return False

    lowered = doc_md.lower()
    if research.base_url and research.base_url.lower() not in lowered:
        return False

    if research.auth_method and research.auth_method.lower() not in lowered:
        return False

    for endpoint in research.endpoints:
        path = endpoint.path.strip().lstrip("/")
        if path and path.lower() not in lowered:
            return False
    return True


class WriterWorker:
    """
    Uses the LLM to generate a markdown integration guide from the research output
    and a summary of test results.
    """

    def handles(self, tool: str) -> bool:
        return tool == "doc_writer"

    async def run(
        self,
        task: Task,
        research: ResearchOutput,
        test_summary: str,
        sdk_code: Optional[str],
    ) -> TaskResult:
        inputs   = task.inputs
        base_url = inputs.get("base_url", research.base_url)

        endpoint_detail = json.dumps(
            [ep.model_dump() for ep in research.endpoints],
            indent=2,
        )

        human_content = (
            f"Write a complete integration guide for:\n\n"
            f"Base URL: {base_url}\n"
            f"Auth method: {research.auth_method}\n"
            f"Rate limits: {research.rate_limits or 'not documented'}\n"
            f"Error codes: {[e.model_dump() for e in (research.error_codes or [])]}\n\n"
            f"Endpoints:\n{endpoint_detail}\n\n"
            f"Test results summary:\n{test_summary}\n\n"
            + (f"Generated SDK (reference only):\n```python\n{sdk_code}\n```\n" if sdk_code else "")
        )

        logger.info("writer_worker.generating", base_url=base_url)
        try:
            response = await llm.ainvoke([
                SystemMessage(content=doc_writer_prompt),
                HumanMessage(content=human_content),
            ])
            doc_md = _normalize_llm_text(getattr(response, "content", ""))
            verification_status = "verified" if _validate_doc_output(research, doc_md) else "failed"
            logger.info("writer_worker.done", chars=len(doc_md))
            return _result(task, "completed", verification_status=verification_status, output=doc_md)
        except Exception as exc:
            logger.exception("writer_worker.error", error=str(exc))
            return _result(task, "failed", verification_status="failed", error=str(exc))