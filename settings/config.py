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
GOOGLE_API_KEY: Final[str] = _get_env("GOOGLE_API_KEY")
MODEL_NAME: Final[str] = _get_env("MODEL_NAME", "gemini-2.0-flash")
REMEM_API_KEY: Final[str] = _get_env("REMEM_API_KEY")

# Backward-compatible alias used elsewhere in the project
firecraw_key: Final[str] = FIRECRAW_API_KEY