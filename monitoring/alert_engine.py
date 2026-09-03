"""Turns check/change results into alerts, with de-dup and recovery detection:

- Availability: fires as soon as a check comes back DOWN (DOWN_CONFIRM_CHECKS,
  default 1 -- set it to 2+ to require that many DOWN checks in a row before
  alerting). The first alert fires once; after that, while the site stays
  DOWN, a reminder re-fires every DOWN_REALERT_SECONDS (default 1h) so an
  ongoing outage doesn't drop off the radar. UNCERTAIN never alerts -- it
  isn't a confirmed failure, just something a browser-based check would need
  to confirm.
- Structural changes: alert only for medium+ severity, so routine text edits
  don't spam notifications.
- Broken links: severity scales with how many were found.

The console + alerts.log get one line per alert as it happens. Teams instead
gets a single combined "N sites down" card -- call send_outage_digest(conn)
once after each pass. That card is only sent when the set of down sites has
CHANGED since the last card (a newly-down site); an unchanged list is not
re-sent, so a long outage doesn't reprint the same card every hour.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .change_detection import Change
from .notifiers import get_notifier

_notifier = get_notifier()

# Remembers which down sites the last Teams digest card covered, so an
# unchanged outage list isn't re-sent every pass.
DIGEST_STATE_FILE = Path(__file__).resolve().parent.parent / ".digest_state.json"

# While a site stays DOWN, re-send the "still down" alert this often (seconds)
# so an ongoing outage that affects many users keeps nagging instead of going
# quiet after the first notification. Default 1 hour; override with
# DOWN_REALERT_SECONDS (set to 0 to disable repeat alerts).
REALERT_SECONDS = int(os.environ.get("DOWN_REALERT_SECONDS", "3600"))

# How many DOWN checks in a row before the first alert fires. Default 1 (alert
# immediately) -- appropriate when checks run infrequently (e.g. hourly), where
# waiting for a second check just delays a real outage by a full cycle. Raise
# to 2+ if checks run every few minutes and you want blip protection.
CONFIRM_CHECKS = max(1, int(os.environ.get("DOWN_CONFIRM_CHECKS", "1")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


async def _fire(conn, site_id: int, site_name: str, alert_type: str, severity: str, message: str) -> None:
    alert = db.create_alert(conn, site_id, alert_type, severity, message, _now())
    await _notifier.notify(site_name, alert)


def _load_digest_state() -> set[str]:
    try:
        return set(json.loads(DIGEST_STATE_FILE.read_text(encoding="utf-8")).get("notified", []))
    except (OSError, ValueError):
        return set()


def _save_digest_state(names: set[str]) -> None:
    try:
        DIGEST_STATE_FILE.write_text(json.dumps({"notified": sorted(names)}, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - persistence is best-effort
        print(f"[TEAMS] could not write {DIGEST_STATE_FILE.name}: {exc}")


async def send_outage_digest(conn) -> None:
    """Post ONE combined Teams card listing every site currently in an open
    outage -- but only when the down-site list has changed since the last card
    (a newly-down site). An unchanged list is not re-sent. Per-site alerts
    still hit the console + alerts.log as they happen."""
    outages = db.get_open_outages(conn)
    current = {r["name"] for r in outages}
    already_notified = _load_digest_state()
    new_sites = current - already_notified

    if not new_sites:
        # Drop recovered sites from the stored set so a re-outage later counts
        # as new, but send nothing -- the list hasn't gained anything.
        _save_digest_state(current)
        if current:
            print(f"[TEAMS] {len(current)} site(s) still down, nothing new -- card not re-sent")
        else:
            print("[TEAMS] no open outages -- nothing to send")
        return

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[TEAMS] sending digest: {len(current)} site(s) down, {len(new_sites)} new")
    rows = sorted(((r["name"], r["message"]) for r in outages), key=lambda x: (x[0] not in new_sites, x[0]))
    sent = await _notifier.post_teams_digest(rows, checked_at, new_count=len(new_sites))
    # Only mark the new sites as notified if the card actually went out; on a
    # webhook failure keep them pending so the next pass retries.
    _save_digest_state(current if sent else (already_notified & current))


async def evaluate_availability(conn, site_id: int, site_name: str) -> None:
    recent = db.get_recent_checks(conn, site_id, limit=CONFIRM_CHECKS)
    if not recent:
        return
    current = recent[0]

    open_alert = db.get_open_alert(conn, site_id, "availability")

    if current["status"] == "DOWN":
        confirmed_down = len(recent) >= CONFIRM_CHECKS and all(c["status"] == "DOWN" for c in recent)
        if not confirmed_down:
            return
        if not open_alert:
            await _fire(conn, site_id, site_name, "availability", "critical", f"Site is down: {current['reason']}")
            return
        # Already alerted and still down: re-fire on the reminder cadence so the
        # outage stays visible to everyone watching the channel.
        if REALERT_SECONDS > 0:
            last = db.get_last_alert(conn, site_id, "availability")
            if last and (_now_dt() - _parse_ts(last["created_at"])).total_seconds() >= REALERT_SECONDS:
                down_for = _fmt_duration((_now_dt() - _parse_ts(open_alert["created_at"])).total_seconds())
                await _fire(
                    conn, site_id, site_name, "availability", "critical",
                    f"Site is down: {current['reason']} (still down after {down_for})",
                )
        return

    if current["status"] == "UP" and open_alert:
        db.resolve_open_alerts(conn, site_id, "availability", _now())
        await _fire(conn, site_id, site_name, "recovery", "info", "Site has recovered and is UP again")


async def evaluate_ssl_expiry(conn, site_id: int, site_name: str, days_remaining: int | None, warn_below_days: int) -> None:
    open_alert = db.get_open_alert(conn, site_id, "ssl_expiring")

    if days_remaining is None or days_remaining >= warn_below_days:
        if open_alert:
            db.resolve_alert(conn, open_alert["id"], _now())
        return

    if not open_alert:
        await _fire(conn, site_id, site_name, "ssl_expiring", "warning", f"SSL certificate expires in {days_remaining} day(s)")


async def evaluate_structure_changes(conn, site_id: int, site_name: str, changes: list[Change]) -> None:
    now = _now()
    for change in changes:
        db.insert_change(conn, site_id, now, change.change_type, change.description, change.severity)
        if change.severity in ("medium", "high"):
            await _fire(conn, site_id, site_name, f"change:{change.change_type}", change.severity, change.description)


async def evaluate_broken_links(conn, site_id: int, site_name: str, broken_count: int, pages_crawled: int) -> None:
    open_alert = db.get_open_alert(conn, site_id, "broken_links")

    if broken_count == 0:
        if open_alert:
            db.resolve_alert(conn, open_alert["id"], _now())
        return

    if open_alert:
        return  # already alerted for this ongoing issue

    # Broken links are never "critical" -- the site is still up. Warning at most,
    # and this only goes to alerts.log (not Teams, not the dashboard).
    message = f"{broken_count} broken link(s) found across {pages_crawled} crawled pages"
    await _fire(conn, site_id, site_name, "broken_links", "warning", message)
