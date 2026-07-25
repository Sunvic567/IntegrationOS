from langchain_google_genai import GoogleGenerativeAI
from settings.config import GOOGLE_API_KEY, MODEL_NAME
from langchain.chat_models import init_chat_model

llm = init_chat_model(
    model=MODEL_NAME,
    api_key=GOOGLE_API_KEY,
    temperature=0.6,
    max_output_tokens=1024
)

