import os
from typing import Final
from dotenv import load_dotenv

load_dotenv()


def _get_env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value is None:
        return ""
    return value.strip()


FIRECRAW_API_KEY: Final[str] = _get_env("FIRECRAW_API_KEY")
OPENROUTER_API_KEY: Final[str] = _get_env("OPENROUTER_API_KEY") or _get_env("OPENAI_API_KEY")
OPENROUTER_BASE_URL: Final[str] = _get_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL_NAME: Final[str] = _get_env("MODEL_NAME", "openai/gpt-4o-mini")
REMEM_API_KEY: Final[str] = _get_env("REMEM_API_KEY")

# Backward-compatible alias used elsewhere in the project
firecraw_key: Final[str] = FIRECRAW_API_KEY