import os
from dotenv import load_dotenv

load_dotenv()
firecraw_key = os.getenv("FIRECRAW_API_KEY",  "")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash")  # Default to "gemini-2.5-flash" if not set