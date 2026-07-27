from langchain_core.tools import tool
from urllib.parse import urlparse, urlunparse, quote


@tool
def validate_url(url: str) -> str:
    """
    Sanitize and validate a URL, then return the cleaned version.

    Sanitization steps applied:
    - Strip leading/trailing whitespace
    - Add https:// if no scheme is present
    - Lowercase the scheme and hostname
    - Remove URL fragments (#section)
    - Strip trailing slash from the path
    - Percent-encode any unsafe characters in the path
    - Block internal/loopback hostnames
    """
    if not url or not url.strip():
        raise ValueError("URL must not be empty.")

    url = url.strip()

    # Add scheme if missing (e.g. "stripe.com/docs" → "https://stripe.com/docs")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    # Validate scheme
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}'. Only http and https are allowed.")

    # Validate hostname exists
    if not parsed.hostname:
        raise ValueError("URL has no hostname.")

    # Block internal/loopback addresses
    _blocked = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    if parsed.hostname in _blocked:
        raise ValueError(f"Internal URLs are not allowed: {parsed.hostname}")

    # Sanitize: lowercase scheme + host, strip fragment, clean path
    clean_path = parsed.path.rstrip("/") or ""
    # Percent-encode unsafe characters in the path (preserve already-encoded sequences)
    clean_path = quote(clean_path, safe="/-._~!$&'()*+,;=:@%")

    clean = urlunparse((
        parsed.scheme.lower(),       # lowercase scheme
        parsed.netloc.lower(),       # lowercase host (incl. port)
        clean_path,                  # cleaned path
        parsed.params,               # keep params
        parsed.query,                # keep query string
        "",                          # drop fragment (#section)
    ))

    return clean