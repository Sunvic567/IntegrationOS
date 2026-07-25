import logging
from langchain_core.tools import tool
from firecrawl import Firecrawl
from firecrawl.v2.types import ScrapeOptions
from settings.config import firecraw_key

logger = logging.getLogger("research_agent")

firecraw_client = Firecrawl(api_key=firecraw_key)
scrape_opts = ScrapeOptions(
    only_main_content=True,
    max_age=172800000,
    parsers=["pdf"],
    formats=["markdown"]
)


# The provided tool
@tool
def craw_tool(url) -> str:
    """Crawl a URL for information using Firecrawl and return the main content."""
    if not url:
        raise ValueError("Please provide a non-empty URL.")
    try:
        response = firecraw_client.crawl(
            url,
            sitemap="include",
            crawl_entire_domain=False,
            limit=10,
            scrape_options=scrape_opts
        )
        logger.info(f"Firecrawl craw succeeded: {response}")
        res= response[0].text

        return res
    except Exception as e:
        logger.exception("Error using craw tool")
        raise RuntimeError(f"Firecrawl craw failed: {e}")
