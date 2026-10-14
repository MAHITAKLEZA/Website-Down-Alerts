"""Once-a-day "sites still down" card to the Teams webhook (get_teams_webhook).

Runs a fresh check pass, then posts ONE card listing every site currently
down. When every site is up, nothing is sent. Unlike the real-time alert
(which only fires when a site newly goes down), this repeats each day while
an outage lasts, so it doesn't drop off the radar.

Runs every day (weekends too) at 09:30 IST on GitHub Actions
(.github/workflows/daily-report.yml).

Usage:
    python daily_report.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from monitoring import db
from monitoring.notifiers import TeamsNotifier, get_teams_webhook
from run_fast_checks import run_pass


async def main() -> None:
    url = get_teams_webhook()
    if not url:
        print("No Teams webhook set -- nothing to send. Add it in the dashboard's "
              'Settings tab, or: setx TEAMS_WEBHOOK_URL "https://...."')
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

    if not down:
        print("[DAILY] all sites up -- nothing sent")
        return

    as_of = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    print(f"[DAILY] {len(down)} site(s) down -- posting report")
    ok = await TeamsNotifier(url).post_daily_report([(r["name"], r["reason"]) for r in down], as_of)
    print("[DAILY] sent" if ok else "[DAILY] post failed")


if __name__ == "__main__":
    asyncio.run(main())
