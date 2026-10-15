"""Once-a-day "sites still down" card to the daily-report Teams webhook
(TEAMS_DAILY_WEBHOOK_URL -- separate from the immediate down alerts).

Runs a fresh check pass, then posts ONE card listing every site currently
down. When every site is up, nothing is sent. Unlike the real-time alert
(which only fires when a site newly goes down), this repeats each day while
an outage lasts, so it doesn't drop off the radar.

Runs every day (weekends too) at 09:30 IST on GitHub Actions
(.github/workflows/daily-report.yml). This PC runs it again at 09:32 with
--fallback (WebsiteMonitor-DailyReport task): it first asks GitHub whether
today's workflow has already run (or is running), and only sends the card
itself if GitHub hasn't. If GitHub can't be reached, the PC sends.

Usage:
    python daily_report.py               # always check + send
    python daily_report.py --fallback    # only if GitHub hasn't run today
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

import aiohttp

from monitoring import db
from monitoring.notifiers import TeamsNotifier, get_daily_webhook
from run_fast_checks import run_pass


GITHUB_RUNS_URL = (
    "https://api.github.com/repos/MAHITAKLEZA/Website-Down-Alerts"
    "/actions/workflows/daily-report.yml/runs"
)


async def github_ran_today() -> bool:
    """True if GitHub's daily-report workflow has a run today (UTC date, same as
    the 09:30 IST slot) that succeeded or is still queued/running. Any error
    reaching GitHub counts as "didn't run" so the PC sends instead."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                GITHUB_RUNS_URL,
                params={"created": f">={today}", "per_page": "20"},
                headers={"Accept": "application/vnd.github+json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    print(f"[DAILY] GitHub API returned {resp.status} -- sending from this PC")
                    return False
                runs = (await resp.json()).get("workflow_runs", [])
    except Exception as exc:  # noqa: BLE001 - fall back to sending locally
        print(f"[DAILY] could not reach GitHub ({exc}) -- sending from this PC")
        return False
    return any(r["status"] != "completed" or r["conclusion"] == "success" for r in runs)


async def main() -> None:
    if "--fallback" in sys.argv[1:] and await github_ran_today():
        print("[DAILY] GitHub already ran the daily report today -- skipping")
        return

    url = get_daily_webhook()
    if not url:
        print("No daily-report webhook set -- nothing to send. Add it in the dashboard's "
              'Settings tab, or: setx TEAMS_DAILY_WEBHOOK_URL "https://...."')
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
