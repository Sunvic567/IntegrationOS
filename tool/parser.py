from langchain_core.tools import tool
from schemas.tools import EndpointInfo, ResearchOutput

@tool
def parser_tool(payload: EndpointInfo) -> ResearchOutput:
    """Parse an EndpointInfo payload and return a formatted ResearchOutput string."""
    results = []
    response_results = getattr(payload, "results", None)
    if response_results is None:
        response_results = []

    for r in response_results or []:
        if isinstance(r, dict):
            base_url = r.get("url", "N/A")
            auth_method = r.get("auth_method", "N/A")
            endpoints = r.get("endpoints", "N/A")
            rate_limits = r.get("rate_limits", "N/A")
            raw_markdown = r.get("raw_markdown", "N/A")
        else:
            base_url = getattr(r, "url", "N/A")
            auth_method = getattr(r, "auth_method", "N/A")
            endpoints = getattr(r, "endpoints", "N/A")
            rate_limits = getattr(r, "rate_limits", "N/A")
            raw_markdown = getattr(r, "raw_markdown", "N/A")

        results.append(
            "URL: {}\nAuth Method: {}\nEndpoints: {}\nRate Limits: {}\nMarkdown: {}".format(
                base_url,
                auth_method,
                endpoints,
                rate_limits,
                raw_markdown,
            )
        )

    return "\n---\n".join(results) if results else "No results found."