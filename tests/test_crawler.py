import types
from unittest.mock import Mock

import pytest

from tool import crawler


class DummyResponse:
    def __init__(self, status_code=200, text="", content=b"", json_data=None):
        self.status_code = status_code
        self.text = text
        self.content = content
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


def test_filter_urls_excludes_noise_and_keeps_relevant_paths():
    urls = [
        "https://example.com/api/users",
        "https://example.com/blog/post",
        "https://example.com/docs/getting-started",
        "https://example.com/support",
    ]

    assert crawler._filter_urls(urls) == [
        "https://example.com/api/users",
        "https://example.com/docs/getting-started",
    ]


def test_fetch_sitemap_urls_handles_missing_loc_tags(monkeypatch):
    class DummySitemapResponse:
        status_code = 200
        content = b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>"
        text = ""

    monkeypatch.setattr(crawler.requests, "get", lambda *args, **kwargs: DummySitemapResponse())

    urls = crawler._fetch_sitemap_urls("https://example.com")

    assert urls == []


def test_crawl_with_retry_raises_clear_error_when_no_content(monkeypatch):
    monkeypatch.setattr(crawler, "_fetch_sitemap_urls", lambda base: ["https://example.com/api"])
    monkeypatch.setattr(crawler, "_filter_urls", lambda urls: urls)

    dummy_job = types.SimpleNamespace(data=[types.SimpleNamespace(markdown=None)])
    monkeypatch.setattr(crawler.firecraw_client, "crawl", lambda *args, **kwargs: dummy_job)

    with pytest.raises(RuntimeError, match="no extractable text content"):
        crawler._crawl_with_retry("https://example.com")


def test_clean_markdown_removes_boilerplate_noise():
    noisy = """
    [Skip to content]

    hCaptcha

    hCaptcha logo, opens new window with more information)

    Search
    `/`Ask AI

    Real API content here.
    """

    cleaned = crawler._clean_markdown(noisy)

    assert "[Skip to content]" not in cleaned
    assert "hCaptcha" not in cleaned
    assert "Search" not in cleaned
    assert "Real API content here." in cleaned
