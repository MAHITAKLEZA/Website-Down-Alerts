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
gets a card listing only the newly-down sites -- call send_outage_digest(conn)
once after each pass. Sites already reported in an earlier card are not
repeated, so a long outage doesn't reprint the same site every hour.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .change_detection import Change
from .notifiers import get_notifier

# Remembers which down sites the last Teams digest card covered, so an
# unchanged outage list isn't re-sent every pass.
DIGEST_STATE_FILE = Path(__file__).resolve().parent.parent / ".digest_state.json"

# A site that stays down is listed again in a card after this many days.
DIGEST_REPEAT_DAYS = int(os.environ.get("DIGEST_REPEAT_DAYS", "2"))

# No Teams cards before this moment (checks and logs still run).
NOTIFY_START = datetime.fromisoformat(os.environ.get("NOTIFY_START", "2026-10-23T00:00:00+05:30"))

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
    # Resolved per alert (not once at import) so a webhook saved in the
    # dashboard's Settings tab takes effect without a restart.
    await get_notifier().notify(site_name, alert)


def _load_digest_state() -> dict[str, str]:
    """site name -> ISO time its down-card was last sent."""
    try:
        raw = json.loads(DIGEST_STATE_FILE.read_text(encoding="utf-8")).get("notified", {})
    except (OSError, ValueError):
        return {}
    if isinstance(raw, list):  # older format: names only
        return {name: _now() for name in raw}
    return dict(raw)


def _save_digest_state(state: dict[str, str]) -> None:
    try:
        DIGEST_STATE_FILE.write_text(json.dumps({"notified": dict(sorted(state.items()))}, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - persistence is best-effort
        print(f"[TEAMS] could not write {DIGEST_STATE_FILE.name}: {exc}")


async def send_outage_digest(conn) -> None:
    """Post a Teams card when a site is newly down, or when a site has stayed
    down for DIGEST_REPEAT_DAYS since it was last sent. The card lists the new
    sites plus any sites due for that reminder; everything else stays quiet.
    Per-site alerts still hit the console + alerts.log as they happen."""
    if _now_dt() < NOTIFY_START:
        print(f"[TEAMS] notifications start {NOTIFY_START.isoformat()} -- nothing sent yet")
        return

    outages = db.get_open_outages(conn)
    current = {r["name"] for r in outages}
    # Recovered sites drop out of the state, so a later outage counts as new.
    state = {n: t for n, t in _load_digest_state().items() if n in current}

    repeat = timedelta(days=DIGEST_REPEAT_DAYS)
    new_sites = {n for n in current if n not in state}
    due_sites = {n for n, t in state.items() if _now_dt() - _parse_ts(t) >= repeat}
    to_send = new_sites | due_sites

    if not to_send:
        _save_digest_state(state)
        if current:
            print(f"[TEAMS] {len(current)} site(s) still down, nothing new or due -- card not re-sent")
        else:
            print("[TEAMS] no open outages -- nothing to send")
        return

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[TEAMS] sending card: {len(new_sites)} newly down, {len(due_sites)} still down after {DIGEST_REPEAT_DAYS}d")
    rows = sorted((r["name"], r["message"]) for r in outages if r["name"] in to_send)
    sent = await get_notifier().post_teams_digest(rows, checked_at, len(new_sites))
    # Only record the send if the card went out; on a webhook failure the next pass retries.
    if sent:
        now = _now()
        state.update({n: now for n in to_send})
    _save_digest_state(state)


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

    if current["status"] == "UNCERTAIN" and open_alert and any(w in open_alert["message"].lower() for w in ("timed out", "timeout")):
        # Timeouts no longer count as down; close an outage that one opened.
        db.resolve_open_alerts(conn, site_id, "availability", _now())
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


async def evaluate_security(conn, site_id: int, site_name: str, findings) -> None:
    """One open 'security' alert per site while it has high-severity findings.
    Console + alerts.log only (like broken links); never the Teams card."""
    serious = [f for f in findings if f.severity == "high"]
    open_alert = db.get_open_alert(conn, site_id, "security")

    if not serious:
        if open_alert:
            db.resolve_alert(conn, open_alert["id"], _now())
        return

    message = f"SECURITY: {len(serious)} high-severity finding(s): " + "; ".join(f.title for f in serious[:3])
    if open_alert and open_alert["message"] == message:
        return
    if open_alert:
        db.resolve_alert(conn, open_alert["id"], _now())
    # "warning" (not "high"): the console banner labels high/critical as "WEBSITE DOWN".
    await _fire(conn, site_id, site_name, "security", "warning", message)
