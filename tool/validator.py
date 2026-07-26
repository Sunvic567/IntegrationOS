from langchain_core.tools import tool

from urllib.parse import urlparse

@tool
def validate_url(url: str) -> str:
    """Validate that the URL is a valid HTTP/HTTPS URL and not an internal URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}")
    if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
        raise ValueError("Internal URLs are not allowed")
    return url