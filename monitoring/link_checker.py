"""Link Checker: crawls a site's internal links (bounded, same-domain only)
and reports broken links (4xx/5xx or fetch failures) and redirects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Crawl4AI's HTTP strategy saves any non-HTML response to disk using
# os.O_NOFOLLOW to guard against symlink races -- a flag that doesn't exist
# on Windows, so it crashes on every non-HTML page it meets (there's no
# config to disable the download path). O_NOFOLLOW has no real symlink-race
# meaning on Windows anyway (creating filesystem symlinks there already
# requires elevated privileges), so defining it as a no-op open() flag here
# is a safe, narrow shim rather than a real security tradeoff.
if not hasattr(os, "O_NOFOLLOW"):
    os.O_NOFOLLOW = 0

from crawl4ai import (
    AsyncWebCrawler,
    BFSDeepCrawlStrategy,
    CacheMode,
    CrawlerRunConfig,
    DomainFilter,
    FilterChain,
    URLPatternFilter,
)

import re

from website_monitor import clean_reason, make_http_strategy

# Safety caps so "full site crawl" can't turn into an unbounded scrape of a
# huge site or hammer a client's server indefinitely.
MAX_PAGES_PER_SITE = 200
MAX_DEPTH = 4
PAGE_TIMEOUT_MS = 15000

# Skip binary assets: Crawl4AI's HTTP strategy saves any non-HTML response as
# a "downloaded file" using an os.O_NOFOLLOW open flag that doesn't exist on
# Windows, which makes every image/PDF/etc. crash and look like a broken
# link. We only care about broken *pages* here anyway.
_ASSET_PATTERNS = [
    "*.jpg", "*.jpeg", "*.png", "*.gif", "*.svg", "*.webp", "*.ico",
    "*.pdf", "*.zip", "*.css", "*.js", "*.woff", "*.woff2", "*.ttf",
    "*.mp4", "*.mp3", "*.avi", "*.mov", "*.doc", "*.docx", "*.xml",
]


@dataclass
class BrokenLink:
    url: str
    status_code: int  # real HTTP code, or 0 if no HTTP response was received
    reason: str


@dataclass
class LinkCheckResult:
    pages_crawled: int
    broken_links: list[BrokenLink] = field(default_factory=list)
    redirect_count: int = 0


async def crawl_site_links(start_url: str) -> LinkCheckResult:
    hostname = urlparse(start_url).hostname or ""

    strategy = BFSDeepCrawlStrategy(
        max_depth=MAX_DEPTH,
        max_pages=MAX_PAGES_PER_SITE,
        include_external=False,
        filter_chain=FilterChain([
            DomainFilter(allowed_domains=[hostname]),
            URLPatternFilter(patterns=_ASSET_PATTERNS, reverse=True),
        ]),
    )
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=PAGE_TIMEOUT_MS,
        deep_crawl_strategy=strategy,
        stream=False,
    )

    async with AsyncWebCrawler(crawler_strategy=make_http_strategy()) as crawler:
        results = await crawler.arun(url=start_url, config=config)

    if not isinstance(results, list):
        results = [results]

    broken: list[BrokenLink] = []
    redirect_count = 0

    for r in results:
        status_code = getattr(r, "status_code", None)
        if not r.success or (status_code is not None and status_code >= 400):
            reason = clean_reason(getattr(r, "error_message", None) or f"HTTP {status_code}")
            if status_code is None:
                # 0 = no HTTP response received (DNS/TLS/connection failure);
                # same convention as website_monitor.check_url_full.
                status_match = re.search(r"HTTP (\d{3})", reason)
                status_code = int(status_match.group(1)) if status_match else 0
            broken.append(BrokenLink(url=r.url, status_code=status_code, reason=reason[:300]))
        redirected_url = getattr(r, "redirected_url", None)
        if redirected_url and redirected_url != r.url:
            redirect_count += 1

    return LinkCheckResult(pages_crawled=len(results), broken_links=broken, redirect_count=redirect_count)
