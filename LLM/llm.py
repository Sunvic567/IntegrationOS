from settings.config import MODEL_NAME, OPENROUTER_API_KEY, OPENROUTER_BASE_URL
from langchain.chat_models import init_chat_model

llm = init_chat_model(
    model=MODEL_NAME,
    model_provider="openai",
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
    temperature=0.1,
    max_tokens=1024,
)

