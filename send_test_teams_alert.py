"""Sends one fake combined "sites down" digest card through the real notifier
stack so you can confirm TEAMS_WEBHOOK_URL / TEAMS_DM_WEBHOOK_URL are wired up
correctly before relying on them.

Usage:
    python send_test_teams_alert.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from monitoring.notifiers import get_notifier


async def main() -> None:
    targets = [v for v in ("TEAMS_WEBHOOK_URL", "TEAMS_DM_WEBHOOK_URL") if os.environ.get(v)]
    if not targets:
        print("Neither TEAMS_WEBHOOK_URL nor TEAMS_DM_WEBHOOK_URL is set in this")
        print("shell -- nothing will be posted to Teams.\n")
    else:
        print(f"Posting test card via: {', '.join(targets)}\n")

    notifier = get_notifier()
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    down_sites = [
        ("Example Site (test)", "Site is down: connection refused. If you see this in Teams, alerting works."),
        ("Another Example (test)", "Site is down: overloaded: HTTP 503 Service Unavailable"),
    ]
    await notifier.post_teams_digest(down_sites, checked_at)
    print("Done. Check the Teams channel / DM.")


if __name__ == "__main__":
    asyncio.run(main())
