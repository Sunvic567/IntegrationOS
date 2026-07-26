from settings.config import GOOGLE_API_KEY, MODEL_NAME
from langchain.chat_models import init_chat_model

llm = init_chat_model(
    model=MODEL_NAME,
    model_provider="google_genai",
    api_key=GOOGLE_API_KEY,
    temperature=0.1,
    max_tokens=1024,
)

