from dotenv import load_dotenv
import os, requests

load_dotenv()
key = os.getenv('OPENROUTER_API_KEY')
print(repr(key))

r = requests.post(
    'https://openrouter.ai/api/v1/chat/completions',
    headers={'Authorization': f'Bearer {key}'},
    json={'model': 'openai/gpt-4o-mini', 'messages': [{'role': 'user', 'content': 'hi'}]}
)
print(r.status_code, r.text[:300])