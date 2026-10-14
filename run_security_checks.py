"""Security pass: for every site in urls.txt, runs the external (no-credentials)
checks in monitoring/security_checks.py -- security headers, exposed sensitive
files, malware/injection indicators, WordPress/jQuery version exposure, domain
expiry, and (if SAFE_BROWSING_API_KEY is set) Google Safe Browsing -- and stores
the findings for the dashboard. High-severity findings also write to alerts.log.

Meant to run once a day (see setup_scheduled_tasks.ps1).

Usage:
    python run_security_checks.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from monitoring import alert_engine, db
from monitoring.security_checks import new_context, new_session, run_security_checks
from monitoring.sites import load_sites

CONCURRENCY = 4


async def process_site(conn, ctx, site) -> None:
    site_id = db.get_or_create_site(conn, site.name, site.url)
    result = await run_security_checks(ctx, site.url, db.get_last_security_baseline(conn, site_id))
    if not result.reachable:
        print(f"[SECURITY] {site.name}: unreachable, skipped")
        return
    run_at = datetime.now(timezone.utc).isoformat()
    db.insert_security_run(conn, site_id, run_at, result.findings, json.dumps(result.baseline))
    await alert_engine.evaluate_security(conn, site_id, site.name, result.findings)
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in ("high", "medium", "low")}
    print(f"[SECURITY] {site.name}: {counts['high']} high, {counts['medium']} medium, {counts['low']} low")


async def main() -> None:
    sites = load_sites(include_sections=False)  # sections share their site's domain/headers
    conn = db.get_connection()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with new_session() as session:
        ctx = new_context(session)

        async def bound(site):
            async with semaphore:
                try:
                    await process_site(conn, ctx, site)
                except Exception as exc:  # noqa: BLE001 - one site's failure must not stop the run
                    print(f"[ERROR] {site.url}: {exc}", file=sys.stderr)

        await asyncio.gather(*(bound(s) for s in sites))
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
