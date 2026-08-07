from __future__ import annotations

from typing import Any

from langchain.chat_models import init_chat_model

from settings.config import MODEL_NAME, OPENROUTER_API_KEY, OPENROUTER_BASE_URL


def _build_llm() -> Any:
    try:
        return init_chat_model(
            model=MODEL_NAME,
            model_provider="openai",
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE_URL,
            temperature=0.1,
            max_tokens=1024,
        )
    except Exception:
        return None


llm = _build_llm()

