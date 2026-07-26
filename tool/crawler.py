import structlog
import logging
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin
from langchain_core.tools import tool
from firecrawl import Firecrawl
from firecrawl.v2.types import ScrapeOptions
from settings.config import firecraw_key
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)

logger = structlog.get_logger("research_agent")

firecraw_client = Firecrawl(api_key=firecraw_key)
scrape_opts = ScrapeOptions(
    only_main_content=True,
    max_age=172800000,
    parsers=["pdf"],
    formats=["markdown"]
)

# ── URL filter config ────────────────────────────────────────────────────────
INCLUDE_PATHS = ["/api/", "/docs/", "/reference/", "/authentication/", "/Webhooks/", "/api-versioning/"]
EXCLUDE_PATHS = ["/blog", "/changelog", "/news"]


def _base_url(url: str) -> str:
    """Return the scheme + netloc of a URL (e.g. https://example.com)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _fetch_sitemap_urls(base: str) -> list[str]:
    """
    Try common sitemap locations and return all <loc> URLs found.
    Falls back to an empty list if the sitemap is unreachable or unparsable.
    """
    candidates = [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap-index.xml",
    ]

    # Also check robots.txt for a Sitemap: directive
    try:
        robots = requests.get(f"{base}/robots.txt", timeout=10)
        for line in robots.text.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.insert(0, line.split(":", 1)[1].strip())
    except Exception:
        pass

    all_urls: list[str] = []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    for sitemap_url in candidates:
        try:
            resp = requests.get(sitemap_url, timeout=10)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)

            # Sitemap index — recurse into child sitemaps
            for child in root.findall("sm:sitemap/sm:loc", ns):
                child_urls = _fetch_sitemap_urls(child.text.strip())
                all_urls.extend(child_urls)

            # Regular sitemap
            for loc in root.findall("sm:url/sm:loc", ns):
                all_urls.append(loc.text.strip())

            if all_urls:
                logger.info("crawler.sitemap_found", sitemap=sitemap_url, count=len(all_urls))
                break  # stop once we have results

        except Exception as exc:
            logger.warning("crawler.sitemap_fetch_failed", sitemap=sitemap_url, error=str(exc))

    return all_urls


def _filter_urls(urls: list[str]) -> list[str]:
    """
    Keep only URLs whose path matches an INCLUDE_PATHS entry
    AND does not match any EXCLUDE_PATHS entry.
    """
    kept: list[str] = []
    for url in urls:
        path = urlparse(url).path.lower()
        if any(excl in path for excl in EXCLUDE_PATHS):
            continue
        if any(incl in path for incl in INCLUDE_PATHS):
            kept.append(url)
    return kept


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((RuntimeError, ConnectionError, TimeoutError)),
    before_sleep=before_sleep_log(logging.getLogger("research_agent"), logging.WARNING),
    reraise=True,
)
def _crawl_with_retry(url: str) -> str:
    """
    1. Fetch the sitemap for the given URL's domain.
    2. Filter URLs to docs/api/reference/authentication paths.
    3. Crawl the filtered URLs with Firecrawl.
    """
    base = _base_url(url)

    # Step 1 — discover URLs from the sitemap
    sitemap_urls = _fetch_sitemap_urls(base)

    # Step 2 — filter to relevant paths
    filtered = _filter_urls(sitemap_urls)

    if filtered:
        logger.info(
            "crawler.filtered_urls",
            total=len(sitemap_urls),
            kept=len(filtered),
            sample=filtered[:5],
        )
        # Derive include/exclude path patterns (relative, no domain)
        include_patterns = [urlparse(u).path for u in filtered]
        # Crawl starting from base URL, restricting to filtered paths
        response = firecraw_client.crawl(
            base,
            sitemap="include",
            crawl_entire_domain=False,
            include_paths=include_patterns,
            exclude_paths=EXCLUDE_PATHS,
            scrape_options=scrape_opts,
        )
    else:
        # Sitemap unavailable or no matching pages — fall back to direct crawl
        # with path-level filters so we still skip noise pages
        logger.warning(
            "crawler.sitemap_no_matches",
            url=url,
            fallback="direct crawl with path filters",
        )
        response = firecraw_client.crawl(
            url,
            sitemap="include",
            crawl_entire_domain=False,
            include_paths=INCLUDE_PATHS,
            exclude_paths=EXCLUDE_PATHS,
            scrape_options=scrape_opts,
        )

    if not response:
        raise RuntimeError("Firecrawl returned an empty response.")

    logger.info("crawler.succeeded", url=url, pages=len(response))
    content = "\n---\n".join(
        page.text for page in response
        if getattr(page, "text", None)
    )
    if not content:
        raise RuntimeError("Firecrawl returned pages with no extractable text content.")
    return content


# The provided tool
@tool
def craw_tool(url: str) -> str:
    """
    Crawl a URL for API documentation using Firecrawl.

    Automatically finds the site's sitemap, selects only relevant paths
    (/api/, /docs/, /reference/, /authentication/) and ignores noise
    pages (/blog, /changelog, /news).
    """
    if not url:
        raise ValueError("Please provide a non-empty URL.")
    try:
        return _crawl_with_retry(url)
    except RetryError as e:
        logger.error("crawler.retry_exhausted", url=url, error=str(e))
        raise RuntimeError(f"Firecrawl failed after all retries: {e}")
    except Exception as e:
        logger.exception("crawler.error", url=url, error=str(e))
        raise RuntimeError(f"Firecrawl craw failed: {e}")
