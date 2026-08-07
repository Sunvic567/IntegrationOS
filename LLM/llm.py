from __future__ import annotations

from typing import Any
from langchain.chat_models import init_chat_model
from settings.config import MODEL_NAME, GOOGLE_API_KEY
from dotenv import load_dotenv

load_dotenv()

def _build_llm() -> Any:
    try:
        return init_chat_model(
            model=MODEL_NAME,
            model_provider="google_genai",
            api_key=GOOGLE_API_KEY,
            temperature=0.1,
            max_retries=3,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to initialize LLM: {e}") from e


llm = _build_llm()