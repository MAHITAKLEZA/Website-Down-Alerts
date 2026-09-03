"""Print the current status of every monitored site, from the latest check
stored in monitoring.db. DOWN sites are listed first.

Usage:
    python show_status.py            # all sites, grouped by status
    python show_status.py --down     # only DOWN / UNCERTAIN sites
"""

from __future__ import annotations

import argparse
from collections import Counter

from monitoring import db

_ORDER = {"DOWN": 0, "UNCERTAIN": 1, "UP": 2}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--down", action="store_true", help="show only DOWN/UNCERTAIN sites")
    args = parser.parse_args()

    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT s.name, s.url, ch.status, ch.status_code, ch.reason, ch.run_at
        FROM sites s
        JOIN checks ch ON ch.id = (
            SELECT id FROM checks WHERE site_id = s.id ORDER BY run_at DESC LIMIT 1
        )
        ORDER BY ch.status, s.name
        """
    ).fetchall()
    conn.close()

    if not rows:
        print("No checks recorded yet. Run:  python run_fast_checks.py")
        return

    rows = sorted(rows, key=lambda r: (_ORDER.get(r["status"], 9), r["name"]))
    counts = Counter(r["status"] for r in rows)
    last_run = max(r["run_at"] for r in rows)

    print(f"Latest check per site (most recent run: {last_run})")
    print(f"  UP: {counts.get('UP', 0)}   DOWN: {counts.get('DOWN', 0)}   UNCERTAIN: {counts.get('UNCERTAIN', 0)}\n")

    for r in rows:
        if args.down and r["status"] == "UP":
            continue
        code = r["status_code"] if r["status_code"] else "no HTTP response"
        print(f"[{r['status']:^9}] {r['name']:<28} {r['url']:<38} ({code})")
        if r["status"] != "UP":
            print(f"             -> {r['reason']}")


if __name__ == "__main__":
    main()
