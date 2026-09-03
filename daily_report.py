"""Once-a-day status card to the Teams group chat (TEAMS_DAILY_WEBHOOK_URL).

Runs a fresh check pass, then posts ONE card listing every site currently
down -- or "all sites up" when there's nothing to report. Unlike the
real-time alert digest, this one always posts so the group gets a heartbeat.

Scheduled daily at 09:30 by setup_scheduled_tasks.ps1 (WebsiteMonitor-DailyReport).

Usage:
    setx TEAMS_DAILY_WEBHOOK_URL "https://...."   # once, in a new shell
    python daily_report.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from monitoring import db
from monitoring.notifiers import TeamsNotifier
from run_fast_checks import run_pass


async def main() -> None:
    url = os.environ.get("TEAMS_DAILY_WEBHOOK_URL")
    if not url:
        print("TEAMS_DAILY_WEBHOOK_URL is not set -- nothing to send. "
              'Set it: setx TEAMS_DAILY_WEBHOOK_URL "https://...."')
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
    finally:
        conn.close()

    as_of = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    print(f"[DAILY] {len(down)} site(s) down -- posting report")
    ok = await TeamsNotifier(url).post_daily_report([(r["name"], r["reason"]) for r in down], as_of)
    print("[DAILY] sent" if ok else "[DAILY] post failed")


if __name__ == "__main__":
    asyncio.run(main())
