"""Fast monitoring pass: for every site in urls.txt, runs the availability
check, SSL expiry check, and page/structure analysis, stores results, detects
changes vs the last snapshot, and fires alerts. Meant to run every few
minutes (see setup_scheduled_tasks.ps1).

Usage:
    python run_fast_checks.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from crawl4ai import AsyncWebCrawler

from monitoring import alert_engine, db
from monitoring.change_detection import detect_changes
from monitoring.page_analysis import analyze_page
from monitoring.sites import load_sites
from website_monitor import check_url_full, make_http_strategy

CONCURRENCY = 5
TIMEOUT_MS = 20000
SSL_WARNING_DAYS = 30


async def process_site(crawler: AsyncWebCrawler, conn, site) -> None:
    site_id = db.get_or_create_site(conn, site.name, site.url)

    status, result = await check_url_full(crawler, site.url, TIMEOUT_MS)
    # status.ssl_days_remaining is already populated by check_url_full for UP sites.

    db.insert_check(conn, site_id, status)
    await alert_engine.evaluate_availability(conn, site_id, site.name)
    await alert_engine.evaluate_ssl_expiry(conn, site_id, site.name, status.ssl_days_remaining, SSL_WARNING_DAYS)

    if status.status != "UP" or result is None:
        return  # no page content to analyze when the site isn't reachable

    snapshot = analyze_page(result.html or "")
    previous = db.get_last_snapshot(conn, site_id)
    run_at = datetime.now(timezone.utc).isoformat()
    db.insert_snapshot(conn, site_id, run_at, snapshot)

    changes = detect_changes(previous, snapshot)
    if changes:
        await alert_engine.evaluate_structure_changes(conn, site_id, site.name, changes)


async def run_pass(conn) -> None:
    """Runs one fast-check pass over every site, using the given (already open)
    db connection. Reused by both the CLI entry point and the live dashboard
    server so there's exactly one place that defines what a "pass" does."""
    sites = load_sites()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with AsyncWebCrawler(crawler_strategy=make_http_strategy()) as crawler:

        async def bound(site):
            async with semaphore:
                try:
                    await process_site(crawler, conn, site)
                except Exception as exc:  # noqa: BLE001 - one site's failure must not stop the run
                    print(f"[ERROR] {site.url}: {exc}", file=sys.stderr)

        await asyncio.gather(*(bound(s) for s in sites))


async def main() -> None:
    if not (os.environ.get("TEAMS_WEBHOOK_URL") or os.environ.get("TEAMS_DM_WEBHOOK_URL")):
        print(
            "[TEAMS] neither TEAMS_WEBHOOK_URL nor TEAMS_DM_WEBHOOK_URL is set in this "
            "shell -- no Teams card will be sent (open a NEW terminal after `setx`).",
            file=sys.stderr,
        )

    conn = db.get_connection()
    await run_pass(conn)
    # One combined "N sites down" card to Teams (per-site lines already went to
    # console + alerts.log during the pass). Done here, not in run_pass(), so
    # the live dashboard server doesn't fire a card on every browser refresh.
    await alert_engine.send_outage_digest(conn)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
