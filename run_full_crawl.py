"""Full site crawl: follows internal links (same-domain, capped at 200 pages
per site) to find broken links and count redirects. This is much heavier
than run_fast_checks.py, so it's meant to run on a slower cadence (e.g. once
a day -- see setup_scheduled_tasks.ps1) rather than every few minutes.

Usage:
    python run_full_crawl.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from monitoring import alert_engine, db
from monitoring.link_checker import crawl_site_links
from monitoring.sites import load_sites

CONCURRENCY = 3  # full crawls are heavier per-site; keep total load down


async def process_site(conn, site) -> None:
    site_id = db.get_or_create_site(conn, site.name, site.url)
    result = await crawl_site_links(site.url)

    run_at = datetime.now(timezone.utc).isoformat()
    link_run_id = db.insert_link_run(
        conn, site_id, run_at, result.pages_crawled, len(result.broken_links), result.redirect_count
    )
    for broken in result.broken_links:
        db.insert_broken_link(conn, link_run_id, broken.url, broken.status_code, broken.reason)

    await alert_engine.evaluate_broken_links(conn, site_id, site.name, len(result.broken_links), result.pages_crawled)


async def main() -> None:
    # Sections are same-domain sub-pages of a site already reached by that
    # site's link crawl, so crawling each one again would just multiply load.
    sites = load_sites(include_sections=False)
    conn = db.get_connection()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def bound(site):
        async with semaphore:
            try:
                await process_site(conn, site)
            except Exception as exc:  # noqa: BLE001 - one site's failure must not stop the run
                print(f"[ERROR] {site.url}: {exc}", file=sys.stderr)

    await asyncio.gather(*(bound(s) for s in sites))
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
