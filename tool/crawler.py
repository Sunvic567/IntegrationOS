import logging
from typing import Any
import requests
import structlog
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
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
INCLUDE_PATHS = ["/api/", "/docs/", "/reference/", "/authentication/", "/Webhooks/", "/api-versioning/", "/developers/"]
EXCLUDE_PATHS = ["/blog", "/changelog", "/news", "/partnerships", "/community", "/support"]


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
        if robots.ok:
            for line in robots.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_value = line.split(":", 1)[1].strip()
                    if sitemap_value:
                        candidates.insert(0, sitemap_value)
    except Exception:
        pass

    all_urls: list[str] = []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    for sitemap_url in candidates:
        try:
            resp = requests.get(sitemap_url, timeout=10)
            if not resp.ok:
                continue

            content = resp.content.decode("utf-8", errors="ignore") if isinstance(resp.content, (bytes, bytearray)) else str(resp.content)
            root = ET.fromstring(content)

            # Sitemap index — recurse into child sitemaps
            for child in root.findall("sm:sitemap/sm:loc", ns):
                child_text = (child.text or "").strip()
                if child_text:
                    child_urls = _fetch_sitemap_urls(child_text)
                    all_urls.extend(child_urls)

            # Regular sitemap
            for loc in root.findall("sm:url/sm:loc", ns):
                loc_text = (loc.text or "").strip()
                if loc_text:
                    all_urls.append(loc_text)

            if all_urls:
                logger.info("crawler.sitemap_found", sitemap=sitemap_url, count=len(all_urls))
                break  # stop once we have results

        except ET.ParseError:
            logger.warning("crawler.sitemap_parse_failed", sitemap=sitemap_url)
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
    wait=wait_exponential(multiplier=1, min=15, max=60),
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
             limit=20,
        )
        # Derive include/exclude path patterns (relative, no domain)
        include_patterns = [urlparse(u).path for u in filtered]
        # Crawl starting from base URL, restricting to filtered paths
        crawl_job = firecraw_client.crawl(
            base,
            sitemap="include",
            crawl_entire_domain=False,
            include_paths=include_patterns,
            exclude_paths=EXCLUDE_PATHS,
            scrape_options=scrape_opts,
             limit=20,
        )
    else:
        # Sitemap unavailable or no matching pages — fall back to direct crawl
        # with path-level filters so we still skip noise pages
        logger.warning(
            "crawler.sitemap_no_matches",
            url=url,
            fallback="direct crawl with path filters",
        )
        crawl_job = firecraw_client.crawl(
            url,
            sitemap="include",
            crawl_entire_domain=False,
            include_paths=INCLUDE_PATHS,
            exclude_paths=EXCLUDE_PATHS,
            scrape_options=scrape_opts,
        )

    # firecrawl v4 crawl() returns a CrawlJob — extract the actual page list
    response = crawl_job.data if hasattr(crawl_job, "data") else list(crawl_job)

    if not response:
        raise RuntimeError("Firecrawl returned an empty response.")

    logger.info("crawler.succeeded", url=url, pages=len(response))

    markdown_chunks: list[str] = []
    for page in response:
        if isinstance(page, tuple):
            page = page[1] if len(page) > 1 else None
        if page is None:
            continue
        markdown = getattr(page, "markdown", None)
        if isinstance(markdown, str) and markdown.strip():
            markdown_chunks.append(markdown.strip())

    content = "\n---\n".join(markdown_chunks)
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
