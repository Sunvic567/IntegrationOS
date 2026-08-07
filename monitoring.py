import os
import json
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger("monitoring")


def configure_logging() -> None:
    """Set up simple structured logging defaults for local and container runs."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(__import__("logging"), "getLevelName")(log_level)),
    )


def log_run_summary(api_url: str, verdict: str, errors: list[str], duration_seconds: float) -> None:
    """Emit a concise summary for operational monitoring."""
    logger.info(
        "pipeline.summary",
        api_url=api_url,
        verdict=verdict,
        error_count=len(errors),
        duration_seconds=round(duration_seconds, 3),
        errors=errors,
    )


def save_monitoring_snapshot(output_dir: Path, payload: dict[str, Any]) -> None:
    """Persist a JSON snapshot for later inspection or CI collection."""
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "monitoring_snapshot.json"
    snapshot_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
