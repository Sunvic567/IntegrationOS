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
            example = r.get("example", "N/A")
            webhooks = r.get("webhooks", "N/A")
            api_versioning = r.get("api_versioning", "N/A")
        else:
            base_url = getattr(r, "url", "N/A")
            auth_method = getattr(r, "auth_method", "N/A")
            endpoints = getattr(r, "endpoints", "N/A")
            rate_limits = getattr(r, "rate_limits", "N/A")
            example = getattr(r, "example", "N/A")
            webhooks = getattr(r, "webhooks", "N/A")
            api_versioning = getattr(r, "api_versioning", "N/A")
        results.append(
            "URL: {}\nAuth Method: {}\nEndpoints: {}\nRate Limits: {}\nExample: {}\nWebhooks: {}\nAPI Versioning: {}".format(
                base_url,
                auth_method,
                endpoints,
                rate_limits,
                example,
                webhooks,
                api_versioning,
            )
        )

    return "\n---\n".join(results) if results else "No results found."