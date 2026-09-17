"""Once-a-day status card to the Teams group chat (TEAMS_DAILY_WEBHOOK_URL).

Runs a fresh check pass, then posts ONE card listing every site currently
down -- or "all sites up" when there's nothing to report. Unlike the
real-time alert digest, this one always posts so the group gets a heartbeat.

Skips Saturday & Sunday (no card on weekends). Set DAILY_REPORT_WEEKENDS=1
to send every day.

Scheduled Mon-Fri at 09:30 by setup_scheduled_tasks.ps1 (WebsiteMonitor-DailyReport).

Usage:
    setx TEAMS_DAILY_WEBHOOK_URL "https://...."   # once, in a new shell
    python daily_report.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from monitoring import db
from monitoring.notifiers import TeamsNotifier
from run_fast_checks import run_pass


def _broken_pages(conn) -> list[tuple[str, int]]:
    """Sites whose latest full link crawl (run_full_crawl.py) found broken
    top-level pages returning HTTP 404 (e.g. /about/), one count per site.
    Sourced from link_runs/broken_links, which the fast pass run here does
    not populate -- so this reflects the last scheduled full crawl, not
    this run.

    Only top-level pages count (a single path segment, like /about/) --
    deeper nested pages (e.g. /practice-areas/family-law/) are dropped since
    they're usually just fallout from the same broken parent nav, not a
    separate issue. A broken root ("/") alone is also dropped: that's the
    same failure the availability check already reports in the down-sites
    section (or, for a 403/anti-bot block, an UNCERTAIN check that isn't a
    broken *link*), so repeating it here would just be noise."""
    rows = conn.execute(
        """
        SELECT s.name, lr.id AS link_run_id
        FROM sites s JOIN link_runs lr ON lr.id = (
            SELECT id FROM link_runs WHERE site_id = s.id ORDER BY run_at DESC LIMIT 1
        )
        WHERE lr.broken_count > 0
        ORDER BY lr.broken_count DESC
        """
    ).fetchall()
    result = []
    for r in rows:
        links = conn.execute(
            "SELECT url FROM broken_links WHERE link_run_id = ? AND status_code = 404",
            (r["link_run_id"],),
        ).fetchall()
        top_level = {
            p for l in links
            if len(p := urlparse(l["url"]).path.strip("/")) and "/" not in p
        }
        if not top_level:
            continue
        result.append((r["name"], len(top_level)))
    result.sort(key=lambda t: t[1], reverse=True)
    return result


async def main() -> None:
    url = os.environ.get("TEAMS_DAILY_WEBHOOK_URL")
    if not url:
        print("TEAMS_DAILY_WEBHOOK_URL is not set -- nothing to send. "
              'Set it: setx TEAMS_DAILY_WEBHOOK_URL "https://...."')
        return

    # weekday() is Mon=0 .. Sun=6; skip Sat(5)/Sun(6) unless overridden.
    if datetime.now().astimezone().weekday() >= 5 and os.environ.get("DAILY_REPORT_WEEKENDS") != "1":
        print("[DAILY] weekend -- skipping (set DAILY_REPORT_WEEKENDS=1 to send)")
        return

    conn = db.get_connection()
    try:
        await run_pass(conn)
        down = conn.execute(
            """
            SELECT s.name, c.reason
            FROM sites s JOIN checks c ON c.id = (
                SELECT id FROM checks WHERE site_id = s.id ORDER BY run_at DESC LIMIT 1
            )
            WHERE c.status = 'DOWN'
            ORDER BY s.name
            """
        ).fetchall()
        broken_pages = _broken_pages(conn)
    finally:
        conn.close()

    as_of = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    print(f"[DAILY] {len(down)} site(s) down, {len(broken_pages)} site(s) with broken pages -- posting report")
    ok = await TeamsNotifier(url).post_daily_report(
        [(r["name"], r["reason"]) for r in down], as_of, broken_pages
    )
    print("[DAILY] sent" if ok else "[DAILY] post failed")


if __name__ == "__main__":
    asyncio.run(main())
