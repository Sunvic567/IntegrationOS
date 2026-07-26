import structlog
from langchain_core.tools import tool

logger = structlog.get_logger("research_agent")

# Keywords that signal API-relevant content sections
_RELEVANT_KEYWORDS = [
    "authentication", "auth", "api key", "api-key", "oauth", "bearer", "token",
    "endpoint", "route", "path", "method", "get ", "post ", "put ", "patch ", "delete ",
    "rate limit", "rate-limit", "throttle", "quota", "429",
    "webhook", "event", "callback", "payload",
    "versioning", "api-version", "api version",
    "example", "curl", "sample request", "sample response",
    "base url", "base_url", "host", "https://",
]


@tool
def parser_tool(content: str) -> str:
    """
    Parse and clean raw API documentation text.

    Accepts the raw crawled markdown/text from craw_tool and returns only
    the sections relevant to authentication, endpoints, rate limits,
    webhooks, and API versioning — discarding marketing copy and unrelated prose.
    """
    if not content or not content.strip():
        logger.warning("parser.empty_input")
        return "No content provided to parse."

    # Split on page-separator used by craw_tool, filter to relevant pages
    pages = content.split("\n---\n")
    relevant_pages: list[str] = []

    for page in pages:
        page_lower = page.lower()
        if any(kw in page_lower for kw in _RELEVANT_KEYWORDS):
            relevant_pages.append(page.strip())

    if not relevant_pages:
        # Keyword filter stripped everything — return the original rather than silence
        logger.warning("parser.no_relevant_sections", total_pages=len(pages))
        return content.strip()

    result = "\n---\n".join(relevant_pages)
    logger.info(
        "parser.succeeded",
        original_pages=len(pages),
        kept_pages=len(relevant_pages),
        original_length=len(content),
        parsed_length=len(result),
    )
    return result