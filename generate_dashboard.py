"""Generates dashboard.html from monitoring.db -- a self-contained, auto-
refreshing status page for the whole monitored fleet. Regenerate anytime
with `python generate_dashboard.py`, or call render() from another script
right after a check run so the dashboard stays current.

Usage:
    python generate_dashboard.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "monitoring.db"
OUT_PATH = Path(__file__).resolve().parent / "dashboard.html"

STATUS_META = {
    "UP": ("good", "UP", "check"),
    "DOWN": ("critical", "DOWN", "cross"),
    # Not a confirmed failure (WAF/bot block, JS-only page, thin content) -- it
    # never alerts, so it's shown as a neutral "couldn't verify", not a warning.
    "UNCERTAIN": ("neutral", "UNVERIFIED", "warning"),
}

# Response-time thresholds (ms), per spec.
RTT_GOOD_MAX = 800
RTT_WARN_MAX = 2000

ROWS_PER_PAGE = 15

# How often the dashboard reloads itself. On the live server (serve_dashboard.py)
# each reload runs a full check pass against every site, so this is effectively
# the monitoring cadence -- keep it at an hour, not minutes.
AUTO_REFRESH_SECONDS = 3600

def fetch_data(conn: sqlite3.Connection) -> dict:
    sites = conn.execute(
        """
        SELECT s.id, s.name, s.url, c.status, c.status_code, c.response_time_ms,
               c.ssl_days_remaining, c.reason, c.run_at
        FROM sites s
        LEFT JOIN checks c ON c.id = (
            SELECT id FROM checks WHERE site_id = s.id ORDER BY run_at DESC LIMIT 1
        )
        -- Problems first: DOWN, then UNVERIFIED (uncertain), then UP, then
        -- sites with no check yet; alphabetical within each group.
        ORDER BY CASE c.status
                     WHEN 'DOWN' THEN 0
                     WHEN 'UNCERTAIN' THEN 1
                     WHEN 'UP' THEN 2
                     ELSE 3
                 END,
                 s.name
        """
    ).fetchall()

    # Uptime is "share of CONFIRMED checks that were UP" -- UNCERTAIN checks
    # are excluded from both sides of the ratio entirely, because they never
    # confirmed the site was either up or down. Without this, a site that has
    # only ever returned UNCERTAIN shows a misleading "0% uptime" next to a
    # WARNING badge, which looks like a contradiction (0% reads as "always
    # down"). With confirmed_total=0 for such a site, uptime instead shows
    # "no data yet" -- honest, and never contradicts the status badge.
    uptime_rows = conn.execute(
        """
        SELECT site_id,
               SUM(CASE WHEN status='UP' THEN 1 ELSE 0 END) up_count,
               SUM(CASE WHEN status='DOWN' THEN 1 ELSE 0 END) down_count
        FROM checks GROUP BY site_id
        """
    ).fetchall()
    uptime_by_site = {r["site_id"]: (r["up_count"], r["down_count"]) for r in uptime_rows}

    broken_by_site = {
        r["site_id"]: r["broken_count"]
        for r in conn.execute(
            """
            SELECT site_id, broken_count FROM link_runs
            WHERE id IN (SELECT MAX(id) FROM link_runs GROUP BY site_id)
            """
        ).fetchall()
    }

    # Has this site had a structure change flagged in the last 24h?
    recent_change_sites = {
        r["site_id"]
        for r in conn.execute(
            """
            SELECT DISTINCT site_id FROM changes
            WHERE change_type = 'structure_changed'
              AND detected_at >= datetime('now', '-1 day')
            """
        ).fetchall()
    }

    # The dashboard only surfaces outage alerts (site DOWN / overloaded) and
    # the matching recovery notices. SSL-expiry, broken-link and structure-
    # change alerts are still recorded and still written to alerts.log -- they
    # just don't appear here.
    OUTAGE_TYPES = "('availability', 'recovery')"
    alerts = conn.execute(
        f"""
        SELECT a.site_id, s.name AS site_name, a.alert_type, a.severity, a.message,
               a.created_at, a.resolved_at
        FROM alerts a JOIN sites s ON s.id = a.site_id
        WHERE a.alert_type IN {OUTAGE_TYPES}
          AND a.id IN (SELECT MAX(id) FROM alerts WHERE alert_type IN {OUTAGE_TYPES} GROUP BY site_id, alert_type)
        ORDER BY a.created_at DESC LIMIT 30
        """
    ).fetchall()

    # Sites with an open outage right now, and how many had one 24h ago -- a
    # real comparison point for the trend arrow (DISTINCT collapses the hourly
    # repeat-alert rows for a single ongoing outage down to one).
    open_outage_alerts = conn.execute(
        "SELECT COUNT(DISTINCT site_id) c FROM alerts WHERE alert_type = 'availability' AND resolved_at IS NULL"
    ).fetchone()["c"]
    alerts_24h_ago = conn.execute(
        """
        SELECT COUNT(DISTINCT site_id) c FROM alerts
        WHERE alert_type = 'availability'
          AND created_at <= datetime('now', '-1 day')
          AND (resolved_at IS NULL OR resolved_at > datetime('now', '-1 day'))
        """
    ).fetchone()["c"]

    return {
        "sites": sites,
        "uptime_by_site": uptime_by_site,
        "broken_by_site": broken_by_site,
        "recent_change_sites": recent_change_sites,
        "alerts": alerts,
        "open_outage_alerts": open_outage_alerts,
        "alerts_24h_ago": alerts_24h_ago,
    }


def fmt_ago(iso_ts: str | None) -> str:
    if not iso_ts:
        return "never"
    then = datetime.fromisoformat(iso_ts)
    delta = datetime.now(timezone.utc) - then
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def epoch_or(iso_ts: str | None, default: float = 0) -> float:
    if not iso_ts:
        return default
    return datetime.fromisoformat(iso_ts).timestamp()


# A single reserved status palette (good/warning/critical/neutral) used
# everywhere -- pills, KPI icons, alert accents. The teal "accent" is the
# brand/interactive color and is never used to mean a status.
ICONS = {
    "globe": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 4 6 4 9s-1.5 6.4-4 9c-2.5-2.6-4-6-4-9s1.5-6.4 4-9z"/></svg>',
    "check": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7"/></svg>',
    "cross": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>',
    "warning": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 4l9.5 16.5H2.5z"/><path d="M12 10v4.5" stroke-linecap="round"/><circle cx="12" cy="17.3" r=".9" fill="currentColor" stroke="none"/></svg>',
    "siren": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v2M4.2 6.2l1.4 1.4M19.8 6.2l-1.4 1.4"/><path d="M6 14a6 6 0 0 1 12 0v4H6z"/><path d="M4 21h16"/></svg>',
    "bell": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 10a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z"/><path d="M9.5 19a2.5 2.5 0 0 0 5 0"/></svg>',
    "grid": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.2"/></svg>',
    "report": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3.5h7l4 4V20.5H7z"/><path d="M14 3.5V8h4M9.5 12.5h6M9.5 15.5h6M9.5 18h4"/></svg>',
    "gear": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 3v2.2M12 18.8V21M21 12h-2.2M5.2 12H3M18 6l-1.6 1.6M7.6 16.4L6 18M18 18l-1.6-1.6M7.6 7.6L6 6"/></svg>',
    "pulse": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h4l2 7 4-14 2 7h8"/></svg>',
}


def render(data: dict, live: bool = False, flash: str | None = None, user=None) -> str:
    """live=True enables the in-page "add a site" form (needs serve_dashboard.py
    running to handle the POST); flash is a one-line banner shown at the top;
    user is the signed-in account row (Settings tab shows its details)."""
    sites = data["sites"]
    total = len(sites)
    critical = sum(1 for s in sites if s["status"] == "DOWN")
    healthy = total - critical  # everything that isn't confirmed down/overloaded

    open_alerts = data["open_outage_alerts"]
    alerts_prev = data["alerts_24h_ago"]
    if alerts_prev == open_alerts:
        alerts_trend_html = '<span class="trend trend-flat" title="Unchanged from 24h ago">— vs 24h ago</span>'
    elif open_alerts > alerts_prev:
        alerts_trend_html = f'<span class="trend trend-bad" title="{alerts_prev} open 24h ago">&#8593; {open_alerts - alerts_prev} vs 24h ago</span>'
    else:
        alerts_trend_html = f'<span class="trend trend-good" title="{alerts_prev} open 24h ago">&#8595; {alerts_prev - open_alerts} vs 24h ago</span>'

    site_rows = []
    site_details = {}
    default_id = None
    status_order = {"DOWN": 0, "UNCERTAIN": 1, "UP": 2}
    for s in sites:
        css_class, label, icon_key = STATUS_META.get(s["status"] or "UNCERTAIN", ("neutral", "UNKNOWN", "warning"))
        status_sort = status_order.get(s["status"], 3)
        up_count, down_count = data["uptime_by_site"].get(s["id"], (0, 0))
        confirmed_total = up_count + down_count
        uptime_pct = f"{(up_count / confirmed_total * 100):.0f}%" if confirmed_total else None
        uptime_sort = (up_count / confirmed_total * 100) if confirmed_total else -1

        broken = data["broken_by_site"].get(s["id"])
        code = s["status_code"]
        has_real_code = code is not None and code != 0

        rtt_ms = s["response_time_ms"]
        if rtt_ms is None:
            rtt_html, rtt_class, rtt_sort = "TIMEOUT", "rt-bad", 10**9
        elif rtt_ms < RTT_GOOD_MAX:
            rtt_html, rtt_class, rtt_sort = f"{rtt_ms} ms", "rt-good", rtt_ms
        elif rtt_ms < RTT_WARN_MAX:
            rtt_html, rtt_class, rtt_sort = f"{rtt_ms} ms", "rt-warn", rtt_ms
        else:
            rtt_html, rtt_class, rtt_sort = f"{rtt_ms} ms", "rt-bad", rtt_ms

        if css_class == "critical" and default_id is None:
            default_id = s["id"]  # surface a real problem by default

        # Uptime cell: always show a real % or an explicit, explained "no
        # data" state -- never a bare, unexplained dash.
        if uptime_pct is None:
            uptime_display, uptime_cell_class, uptime_tip = (
                "No data yet", "mono num uptime-nodata", "Insufficient data: no confirmed up/down check yet."
            )
        elif uptime_pct == "0%" and css_class != "critical":
            uptime_display, uptime_cell_class, uptime_tip = (
                uptime_pct, "mono num", "Every confirmed check was down; the current check was inconclusive."
            )
        else:
            uptime_display, uptime_cell_class, uptime_tip = uptime_pct, "mono num", None
        uptime_tip_attr = f' title="{escape(uptime_tip)}"' if uptime_tip else ""

        # --- Website Health panel rows, grouped, no redundant fields ---
        not_tracked_tip = "This checker doesn't run a browser, so it can't observe this."
        rows = [
            ("Availability & uptime", [
                ["Availability", css_class, label, (s["reason"] or None) if css_class != "good" else None],
                ["Uptime", "neutral", uptime_pct if uptime_pct else "No data yet",
                 None if uptime_pct else "No confirmed up/down check yet -- excludes uncertain results."],
            ]),
            ("Security", [
                ["SSL Certificate",
                 "good" if (s["ssl_days_remaining"] or 0) > 14 else ("warning" if s["ssl_days_remaining"] is not None else "neutral"),
                 f"{s['ssl_days_remaining']}d left" if s["ssl_days_remaining"] is not None else "Unavailable",
                 "Site is unreachable, so its certificate couldn't be read." if s["ssl_days_remaining"] is None else None],
            ]),
            ("Content & structure", [
                ["Page structure", "neutral" if s["id"] in data["recent_change_sites"] else "good",
                 "Changed" if s["id"] in data["recent_change_sites"] else "Stable",
                 "Changed within the last 24 hours" if s["id"] in data["recent_change_sites"] else None],
                # Broken links are a content issue, not an outage -- shown as a
                # neutral count, never red, and never affect the site's status.
                ["Broken links", "neutral" if broken else "good",
                 f"{broken} broken" if broken else "0", None],
                ["Page rendering", "muted", "Not monitored", not_tracked_tip],
                ["Console errors", "muted", "Not monitored", not_tracked_tip],
            ]),
        ]
        # HTTP Status only earns its own row when it says something Availability
        # doesn't already say (an actual code) -- otherwise "Availability: DOWN"
        # + "HTTP Status: No response" repeats the same fact twice.
        if has_real_code:
            rows[0][1].insert(1, ["HTTP status", "good" if code == 200 else "critical", str(code), None])

        site_details[s["id"]] = {"name": s["name"], "url": s["url"], "groups": rows}

        site_rows.append(f"""
        <tr class="site-row" data-site-id="{s['id']}" data-status="{css_class}" tabindex="0"
            data-statusorder="{status_sort}" data-rtt="{rtt_sort}" data-uptime="{uptime_sort}" data-lastcheck="{epoch_or(s['run_at'])}">
          <td><a class="site-link" href="{escape(s['url'])}" target="_blank" rel="noopener" onclick="event.stopPropagation()">{escape(s['name'])}</a>
              <div class="site-url">{escape(s['url'].replace('https://', '').replace('http://', ''))}</div></td>
          <td><span class="pill pill-{css_class}"><i class="pill-icon">{ICONS[icon_key]}</i>{label}</span></td>
          <td class="mono num {rtt_class}">{rtt_html}</td>
          <td class="muted last-check" data-epoch="{epoch_or(s['run_at'])}">{fmt_ago(s['run_at'])}</td>
          <td class="{uptime_cell_class}"{uptime_tip_attr}>{uptime_display}</td>
        </tr>""")

    if default_id is None and sites:
        default_id = sites[0]["id"]

    # --- Alerts: outages only (site DOWN / overloaded) plus their recovery
    # notices. A recovery is always "good" and never counts as open.
    alert_items = []
    for a in data["alerts"][:15]:
        is_recovery = a["alert_type"] == "recovery"
        is_open = a["resolved_at"] is None and not is_recovery
        state = "good" if is_recovery else "critical"
        badge_state = state if is_open else "good"
        badge_text = "RECOVERED" if is_recovery else ("DOWN" if is_open else "RESOLVED")
        alert_items.append(f"""
        <li class="alert-item alert-{state if is_open else 'good'}">
          <div class="alert-top">
            <span class="pill pill-{badge_state}">{badge_text}</span>
            <span class="muted mono">{fmt_ago(a['created_at'])}</span>
          </div>
          <div class="alert-site">{escape(a['site_name'])}</div>
          <div class="alert-msg">{escape(a['message'])}</div>
        </li>""")

    priority = [s for s in sites if s["status"] == "DOWN"]
    report_rows = "".join(
        f"""<tr><td>{escape(s['name'])}</td><td class="mono">{escape(s['url'])}</td>
            <td>{escape(s['reason'] or '—')[:90]}</td></tr>"""
        for s in priority
    ) or '<tr><td colspan="3" class="muted" style="padding:16px;">No sites currently down.</td></tr>'

    # --- Settings tab = the signed-in user's account ---
    def _acct_row(label: str, value: str) -> str:
        return f'<div class="setting-row"><span>{escape(label)}</span><span class="setting-val">{escape(value)}</span></div>'

    if user is not None:
        created = str(user["created_at"])[:10]
        account_rows = _acct_row("Email", user["email"]) + _acct_row("Member since", created)
        settings_html = f"""
      <section class="card">
        <h2 class="card-title">Account</h2>
        {account_rows}
      </section>
      <section class="card">
        <h2 class="card-title">Change password</h2>
        <form class="acct-form" method="post" action="/change-password">
          <input name="old" type="password" placeholder="Current password" autocomplete="current-password" required>
          <input name="new" type="password" placeholder="New password (min 8 chars)" autocomplete="new-password" minlength="8" required>
          <button type="submit" class="acct-btn">Update password</button>
        </form>
      </section>
      <section class="card">
        <h2 class="card-title">Session</h2>
        <form method="post" action="/logout" style="padding:18px 22px;">
          <button type="submit" class="acct-btn acct-btn-danger">Sign out</button>
        </form>
      </section>"""
    else:
        settings_html = """
      <section class="card">
        <div class="health-empty">Account details appear here when you sign in through the live dashboard
        (<span class="mono">python serve_dashboard.py</span>).</div>
      </section>"""

    if user is not None:
        email = user["email"]
        handle = email.split("@")[0]
        sidebar_user_html = (
            f'<div class="sidebar-user" title="{escape(email)}">'
            f'<span class="avatar">{escape(handle[:1].upper())}</span>'
            f'<span class="uname">{escape(handle)}</span></div>'
        )
    else:
        sidebar_user_html = ""

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    details_json = json.dumps(site_details)
    critical_attention = " kpi-attention" if critical > 0 else ""

    flash_html = f'<div class="flash">{escape(flash)}</div>' if flash else ""
    if live:
        add_site_html = (
            '<form class="add-site" method="post" action="/add-site">'
            '<input name="url" class="add-site-input" type="text" '
            'placeholder="https://example.com" autofocus required>'
            '<button type="submit" class="add-site-btn">+ Add</button>'
            '</form>'
        )
    else:
        add_site_html = (
            '<div class="add-site-hint">To add a site from here, run '
            '<span class="mono">python serve_dashboard.py</span> — otherwise add it to '
            '<span class="mono">urls.txt</span>.</div>'
        )

    # Just the names of every monitored site, for the Websites tab.
    ws_rows = [
        f'<li><a href="{escape(s["url"])}" target="_blank" rel="noopener">{escape(s["name"])}</a></li>'
        for s in sites
    ]
    websites_list_html = "".join(ws_rows) or '<li class="empty-state">No sites yet.</li>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fleet Watch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800;900&family=Manrope:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #ffffff; --surface: #ffffff; --surface-2: #f5f7fa; --border: #dbe1ea;
    --text: #10151f; --text-2: #45536a; --text-3: #5f6d84;
    --accent: #0e8f8f; --accent-soft: #0e8f8f1a;
    --good: #17a562; --good-soft: #17a5621a; --good-dark: #0c6b3f;
    --warning: #c07f00; --warning-soft: #c07f001a; --warning-dark: #8a5c00;
    --critical: #d21f2a; --critical-soft: #d21f2a1a; --critical-orange: #e8622a;
    --neutral: #5a6a80; --neutral-soft: #5a6a801a; --neutral-dark: #3c485a;
    --shadow: 0 1px 2px rgba(16,21,31,.04), 0 8px 24px -12px rgba(16,21,31,.12);
    --sidebar-bg: #f7f9fb; --sidebar-text: #45536a; --sidebar-active: #10151f;
    --overlay-bg: rgba(255,255,255,.8);
  }}
  /* Deliberately committed to a single light theme -- no dark-mode variant.
     Every color below is the one actually used, regardless of the viewer's
     system setting, per explicit request for a white background. */

  * {{ box-sizing: border-box; }}
  /* rem is relative to the ROOT element, not body -- this is the one line
     that scales every rem-sized thing on the page (KPIs, table, sidebar,
     health panel, alerts, settings) up together. */
  html {{ font-size: 24px; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font-family: 'Manrope', system-ui, sans-serif; -webkit-font-smoothing: antialiased; }}
  .mono {{ font-family: 'JetBrains Mono', ui-monospace, monospace; font-variant-numeric: tabular-nums; }}
  h1, h2, h3 {{ font-family: 'Archivo', system-ui, sans-serif; text-wrap: balance; margin: 0; }}
  .muted {{ color: var(--text-3); font-size: 0.88rem; }}
  a {{ color: inherit; }}
  svg {{ width: 100%; height: 100%; }}
  button {{ font: inherit; }}

  .layout {{ display: grid; grid-template-columns: 262px 1fr; min-height: 100vh; }}
  @media (max-width: 860px) {{
    .layout {{ grid-template-columns: 76px 1fr; }}
    .sidebar-brand span:not(.icon) {{ display: none; }}
    .nav-item span:not(.icon) {{ display: none; }}
    .nav-item {{ justify-content: center; padding: 15px; }}
  }}

  .sidebar {{ background: var(--sidebar-bg); color: var(--sidebar-text); padding: 24px 16px; position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; border-right: 1px solid var(--border); }}
  .sidebar-brand {{ display: flex; align-items: center; gap: 11px; padding: 6px 10px 26px; color: var(--sidebar-active); font-family: 'Archivo', sans-serif; font-weight: 800; font-size: 1.3rem; }}
  .sidebar-brand .icon {{ width: 24px; height: 24px; color: var(--accent); flex-shrink: 0; }}
  .sidebar-nav {{ display: flex; flex-direction: column; gap: 6px; }}
  .nav-item {{
    display: flex; align-items: center; gap: 13px; padding: 15px 16px; border-radius: 10px;
    font-size: 1.18rem; font-weight: 600; text-decoration: none; cursor: pointer; white-space: nowrap;
    border: none; background: none; color: var(--sidebar-text); width: 100%; text-align: left; font-family: 'Manrope', sans-serif;
  }}
  .nav-item .icon {{ width: 24px; height: 24px; flex-shrink: 0; }}
  .nav-item:hover {{ background: var(--surface-2); color: var(--sidebar-active); }}
  .nav-item.active {{ background: var(--accent); color: #ffffff; }}
  .nav-item:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

  .content {{ padding: 30px 34px 64px; width: 100%; }}
  section.view {{ display: none; }}
  section.view.active {{ display: block; }}

  .page-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 26px; flex-wrap: wrap; gap: 12px; }}
  .page-header h1 {{ font-size: 1.7rem; font-weight: 800; }}
  .live-clock {{ display: flex; align-items: center; gap: 12px; font-size: 1rem; }}
  .live-dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--good); animation: pulse 2s infinite; flex-shrink: 0; }}
  @media (prefers-reduced-motion: reduce) {{ .live-dot, .spinner {{ animation: none; }} }}
  @keyframes pulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(52,199,123,.5); }} 70% {{ box-shadow: 0 0 0 8px rgba(52,199,123,0); }} 100% {{ box-shadow: 0 0 0 0 rgba(52,199,123,0); }} }}
  .refresh-btn {{ display: inline-flex; align-items: center; gap: 9px; border: 1px solid var(--border); background: var(--surface); color: var(--text-2); font-family: 'Manrope', sans-serif; font-size: 0.9rem; font-weight: 600; padding: 9px 16px; border-radius: 8px; cursor: pointer; }}
  .refresh-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .refresh-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .refresh-btn:disabled {{ opacity: .7; cursor: default; }}
  .spinner {{ width: 15px; height: 15px; border-radius: 50%; border: 2px solid currentColor; border-top-color: transparent; animation: spin .7s linear infinite; flex-shrink: 0; display: none; }}
  .refresh-btn.loading .spinner {{ display: inline-block; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  .refresh-overlay {{
    position: fixed; inset: 0; background: var(--overlay-bg); backdrop-filter: blur(2px);
    display: none; align-items: center; justify-content: center; z-index: 50; flex-direction: column; gap: 14px;
  }}
  .refresh-overlay.active {{ display: flex; }}
  .refresh-overlay .spinner {{ display: inline-block; width: 34px; height: 34px; border-width: 3px; color: var(--accent); }}
  .refresh-overlay-text {{ font-weight: 600; color: var(--text); }}

  .kpi-row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin-bottom: 30px; }}
  @media (max-width: 900px) {{ .kpi-row {{ grid-template-columns: repeat(2, 1fr); }} }}
  @media (max-width: 560px) {{ .kpi-row {{ grid-template-columns: 1fr; }} }}
  .kpi {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 18px; padding: 26px 28px;
    box-shadow: var(--shadow); display: flex; align-items: center; gap: 22px;
    transition: transform .18s ease, box-shadow .18s ease;
  }}
  .kpi:hover {{ transform: translateY(-3px); box-shadow: 0 4px 8px rgba(16,21,31,.08), 0 16px 32px -12px rgba(16,21,31,.22); }}
  @media (prefers-reduced-motion: reduce) {{ .kpi, .kpi:hover {{ transition: none; transform: none; }} }}
  .kpi.kpi-attention {{ border-color: var(--critical); box-shadow: 0 0 0 1px var(--critical), var(--shadow); animation: attention-ring 1.8s ease-in-out infinite; }}
  @keyframes attention-ring {{
    0%, 100% {{ box-shadow: 0 0 0 1px var(--critical), var(--shadow); }}
    50% {{ box-shadow: 0 0 0 4px var(--critical-soft), 0 0 0 1px var(--critical), var(--shadow); }}
  }}
  @media (prefers-reduced-motion: reduce) {{ .kpi.kpi-attention {{ animation: none; }} }}
  .kpi.kpi-attention .kpi-icon {{ animation: icon-pulse 1.8s ease-in-out infinite; }}
  @keyframes icon-pulse {{ 0%, 100% {{ transform: scale(1); }} 50% {{ transform: scale(1.08); }} }}
  @media (prefers-reduced-motion: reduce) {{ .kpi.kpi-attention .kpi-icon {{ animation: none; }} }}

  .trend {{ display: inline-block; font-size: 0.78rem; font-weight: 700; margin-top: 3px; }}
  .trend-good {{ color: var(--good); }}
  .trend-bad {{ color: var(--critical); }}
  .trend-flat {{ color: var(--text-3); font-weight: 500; }}

  .kpi-icon {{ width: 68px; height: 68px; border-radius: 15px; display: flex; align-items: center; justify-content: center; padding: 15px; flex-shrink: 0; }}
  .kpi.k-total .kpi-icon {{ background: linear-gradient(135deg, var(--neutral), var(--neutral-dark)); color: #fff; }}
  .kpi.k-good .kpi-icon {{ background: linear-gradient(135deg, var(--good), var(--good-dark)); color: #fff; }}
  .kpi.k-critical .kpi-icon {{ background: linear-gradient(135deg, var(--critical), var(--critical-orange)); color: #fff; }}
  .kpi.k-alerts .kpi-icon {{ background: linear-gradient(135deg, var(--neutral), var(--neutral-dark)); color: #fff; }}
  .kpi-text {{ display: flex; flex-direction: column; gap: 6px; min-width: 0; }}
  .kpi-label {{ font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-3); font-weight: 700; }}
  .kpi-value {{ font-family: 'Archivo', sans-serif; font-size: 3.2rem; font-weight: 800; line-height: 1; }}
  .kpi.k-critical .kpi-value {{ color: var(--critical); font-size: 3.5rem; font-weight: 900; animation: critical-pulse 1.8s ease-in-out infinite; }}
  @keyframes critical-pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .55; }} }}
  @media (prefers-reduced-motion: reduce) {{ .kpi.k-critical .kpi-value {{ animation: none; }} }}
  .kpi-label-row {{ display: flex; align-items: center; gap: 7px; }}
  .kpi-info-toggle {{
    width: 17px; height: 17px; border-radius: 50%; border: 1px solid var(--text-3); background: none;
    color: var(--text-3); font-size: 0.68rem; font-style: italic; font-family: Georgia, serif; line-height: 1;
    display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0; flex-shrink: 0;
  }}
  .kpi-info-toggle:hover, .kpi-info-toggle[aria-expanded="true"] {{ border-color: var(--accent); color: var(--accent); }}
  .kpi-info-toggle:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .kpi-detail {{ display: none; font-size: 0.78rem; color: var(--text-3); margin-top: 4px; white-space: normal; }}
  .kpi-detail.expanded {{ display: block; }}

  .main-grid {{ display: grid; grid-template-columns: 1fr 400px; gap: 24px; align-items: start; }}
  @media (max-width: 1100px) {{ .main-grid {{ grid-template-columns: 1fr; }} }}

  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); overflow: hidden; }}
  .card-title {{ font-size: 1.05rem; font-weight: 700; padding: 19px 22px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
  .card + .card {{ margin-top: 24px; }}

  .flash {{
    background: var(--accent-soft); border: 1px solid var(--accent); color: var(--accent);
    border-radius: 10px; padding: 12px 18px; margin-bottom: 20px; font-weight: 600; font-size: 0.95rem;
  }}

  .add-site {{ display: flex; gap: 10px; padding: 20px 22px; flex-wrap: wrap; align-items: center; }}
  .add-site-input {{
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 9px 14px; font-size: 0.92rem; color: var(--text); font-family: 'Manrope', sans-serif;
  }}
  .add-site-input:first-of-type {{ flex: 1; min-width: 220px; }}
  .add-site-name {{ width: 170px; }}
  .add-site-input:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  .add-site-btn {{
    border: 1px solid var(--accent); background: var(--accent); color: #fff; font-family: 'Manrope', sans-serif;
    font-size: 0.9rem; font-weight: 700; padding: 9px 18px; border-radius: 8px; cursor: pointer; white-space: nowrap;
  }}
  .add-site-btn:hover {{ filter: brightness(1.08); }}
  .add-site-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .add-site-hint {{ padding: 20px 22px; color: var(--text-3); font-size: 0.85rem; }}

  .site-names {{ list-style: none; margin: 0; padding: 6px 0; }}
  .site-names li {{ padding: 13px 22px; border-bottom: 1px solid var(--border); font-size: 1rem; }}
  .site-names li:last-child {{ border-bottom: none; }}
  .site-names a {{ color: var(--text); text-decoration: none; font-weight: 600; }}
  .site-names a:hover {{ color: var(--accent); }}

  .acct-form {{ display: flex; flex-direction: column; gap: 12px; padding: 20px 22px; max-width: 380px; }}
  .acct-form input {{
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 13px; font-size: 0.92rem; color: var(--text); font-family: 'Manrope', sans-serif;
  }}
  .acct-form input:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  .acct-btn {{
    align-self: flex-start; border: 1px solid var(--accent); background: var(--accent); color: #fff;
    font-family: 'Manrope', sans-serif; font-size: 0.9rem; font-weight: 700; padding: 10px 20px;
    border-radius: 8px; cursor: pointer;
  }}
  .acct-btn:hover {{ filter: brightness(1.08); }}
  .acct-btn-danger {{ background: var(--critical); border-color: var(--critical); }}
  .sidebar-user {{
    margin-top: auto; padding: 14px 12px 4px; border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px; font-size: 0.92rem; color: var(--text-2);
  }}
  .sidebar-user .avatar {{
    width: 30px; height: 30px; border-radius: 50%; background: var(--accent); color: #fff;
    display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 0.85rem; flex-shrink: 0;
  }}
  .sidebar-user .uname {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

  .table-filters {{ display: flex; align-items: center; gap: 14px; padding: 16px 22px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }}
  .search-input {{
    flex: 1; min-width: 200px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 9px 14px; font-size: 0.92rem; color: var(--text); font-family: 'Manrope', sans-serif;
  }}
  .search-input::placeholder {{ color: var(--text-3); }}
  .search-input:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  .filter-chips {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .filter-chip {{
    border: 1px solid var(--border); background: var(--surface); color: var(--text-2); padding: 7px 14px;
    border-radius: 100px; font-size: 0.82rem; font-weight: 600; cursor: pointer; font-family: 'Manrope', sans-serif;
  }}
  .filter-chip:hover {{ border-color: var(--accent); color: var(--accent); }}
  .filter-chip.active {{ background: var(--accent); border-color: var(--accent); color: #06231f; }}
  .filter-chip:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}

  .table-wrap {{ overflow: auto; max-height: 720px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.98rem; }}
  th {{ text-align: left; font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-3); font-weight: 700; padding: 13px 22px; border-bottom: 1px solid var(--border); background: var(--surface-2); white-space: nowrap; position: sticky; top: 0; z-index: 1; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:hover {{ color: var(--text); }}
  th .sort-arrow {{ margin-left: 5px; opacity: .5; }}
  th.sort-active .sort-arrow {{ opacity: 1; color: var(--accent); }}
  td {{ padding: 16px 22px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  th.num, td.num {{ text-align: right; }}
  tr:last-child td {{ border-bottom: none; }}
  .site-row {{ cursor: pointer; transition: box-shadow .15s ease; }}
  .site-row td {{ transition: background .15s ease; }}
  .site-row:hover td {{ background: var(--surface-2); }}
  .site-row:hover {{ box-shadow: 0 2px 10px -4px rgba(0,0,0,.25); }}
  @media (prefers-reduced-motion: reduce) {{ .site-row, .site-row td {{ transition: none; }} }}
  /* Status accent stripe -- fast left-edge scan without reading every row.
     Higher specificity than .site-row.selected td, so it correctly wins
     over the generic selected-row accent stripe on this one cell. */
  tr[data-status="good"] td:first-child {{ box-shadow: inset 3px 0 0 var(--good); }}
  tr[data-status="warning"] td:first-child {{ box-shadow: inset 3px 0 0 var(--warning); }}
  tr[data-status="critical"] td:first-child {{ box-shadow: inset 3px 0 0 var(--critical); }}


  .last-check.flash {{ animation: flash-update 1.1s ease; }}
  @keyframes flash-update {{ 0% {{ background: var(--accent-soft); }} 100% {{ background: transparent; }} }}
  @media (prefers-reduced-motion: reduce) {{ .last-check.flash {{ animation: none; }} }}

  @keyframes row-enter {{ from {{ opacity: 0; transform: translateY(-3px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  .site-row.row-enter {{ animation: row-enter .22s ease; }}
  @media (prefers-reduced-motion: reduce) {{ .site-row.row-enter {{ animation: none; }} }}
  .site-row.selected td {{ background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent); }}
  .site-row:focus-visible {{ outline: 2px solid var(--accent); outline-offset: -2px; }}
  .site-link {{ color: var(--text); text-decoration: none; font-weight: 600; font-size: 1.02rem; }}
  .site-link:hover {{ color: var(--accent); }}
  .site-url {{ font-size: 0.8rem; color: var(--text-3); font-family: 'JetBrains Mono', monospace; margin-top: 3px; }}
  .rt-good {{ color: var(--good); }} .rt-warn {{ color: var(--warning); }} .rt-bad {{ color: var(--critical); font-weight: 700; }}
  .uptime-nodata {{ color: var(--text-3); font-style: italic; cursor: help; }}

  .table-footer {{ display: flex; align-items: center; justify-content: space-between; padding: 14px 22px; border-top: 1px solid var(--border); background: var(--surface-2); gap: 12px; flex-wrap: wrap; }}
  .page-btn {{ border: 1px solid var(--border); background: var(--surface); color: var(--text-2); padding: 7px 14px; border-radius: 7px; cursor: pointer; font-weight: 600; font-size: 0.85rem; }}
  .page-btn:hover:not(:disabled) {{ border-color: var(--accent); color: var(--accent); }}
  .page-btn:disabled {{ opacity: .4; cursor: default; }}

  .pill {{ display: inline-flex; align-items: center; gap: 7px; padding: 5px 12px; border-radius: 100px; font-size: 0.8rem; font-weight: 700; letter-spacing: 0.02em; white-space: nowrap; }}
  .pill-icon {{ width: 13px; height: 13px; display: inline-flex; flex-shrink: 0; }}
  .pill-good {{ background: var(--good-soft); color: var(--good); }}
  .pill-warning {{ background: var(--warning-soft); color: var(--warning); }}
  .pill-critical {{ background: var(--critical-soft); color: var(--critical); }}
  .pill-neutral {{ background: var(--neutral-soft); color: var(--neutral); }}
  /* Deliberately quieter than pill-neutral: an unmeasured check ("Not
     monitored") isn't a real state, so it must read as less prominent than
     a genuine neutral fact (like "no data yet") -- no filled background. */
  .pill-muted {{ background: none; border: 1px solid var(--border); color: var(--text-3); }}

  .group-label {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-3); font-weight: 700; padding: 16px 22px 6px; display: flex; align-items: center; gap: 7px; }}
  .group-dot {{ width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }}
  .group-dot.dot-good {{ background: var(--good); }}
  .group-dot.dot-warning {{ background: var(--warning); }}
  .group-dot.dot-critical {{ background: var(--critical); }}
  .group-dot.dot-neutral, .group-dot.dot-muted {{ background: var(--neutral); }}
  .group-block:first-child .group-label {{ padding-top: 18px; }}
  .health-row {{ display: flex; justify-content: space-between; align-items: center; gap: 14px; padding: 13px 22px; border-bottom: 1px solid var(--border); font-size: 0.98rem; }}
  .group-block:last-child .health-row:last-child {{ border-bottom: none; }}
  .health-row span:first-child {{ color: var(--text-2); font-weight: 500; flex-shrink: 0; }}
  .health-row .pill {{ flex-shrink: 0; cursor: default; }}
  .health-empty {{ padding: 24px 22px; color: var(--text-3); }}

  .alert-list {{ list-style: none; margin: 0; padding: 0; max-height: 620px; overflow-y: auto; }}
  .alert-item {{ padding: 18px 22px; border-bottom: 1px solid var(--border); border-left: 4px solid transparent; }}
  .alert-item:last-child {{ border-bottom: none; }}
  .alert-critical {{ border-left-color: var(--critical); }} .alert-warning {{ border-left-color: var(--warning); }} .alert-good {{ border-left-color: var(--good); }} .alert-neutral {{ border-left-color: var(--neutral); }}
  .alert-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .alert-site {{ font-weight: 700; font-size: 1rem; margin-bottom: 3px; }}
  .alert-msg {{ font-size: 0.9rem; color: var(--text-2); line-height: 1.45; }}
  .empty-state {{ padding: 40px 22px; text-align: center; color: var(--text-3); }}

  .setting-row {{ display: flex; justify-content: space-between; gap: 24px; padding: 16px 22px; border-bottom: 1px solid var(--border); font-size: 0.98rem; }}
  .setting-row:last-child {{ border-bottom: none; }}
  .setting-row span:first-child {{ color: var(--text-2); font-weight: 500; }}
  .setting-val {{ text-align: right; color: var(--text); }}

  footer.foot {{ margin-top: 28px; text-align: center; color: var(--text-3); font-size: 0.72rem; opacity: .8; }}
</style>
</head>
<body>
<div class="refresh-overlay" id="refresh-overlay" role="status" aria-live="polite">
  <span class="spinner" aria-hidden="true"></span>
  <span class="refresh-overlay-text">Refreshing…</span>
</div>
<div class="layout">

  <nav class="sidebar">
    <div class="sidebar-brand"><span class="icon">{ICONS['pulse']}</span><span>MONITOR</span></div>
    <div class="sidebar-nav">
      <button class="nav-item active" data-view="view-dashboard"><span class="icon">{ICONS['grid']}</span><span>Dashboard</span></button>
      <button class="nav-item" data-view="view-websites"><span class="icon">{ICONS['globe']}</span><span>Websites</span></button>
      <button class="nav-item" data-view="view-alerts"><span class="icon">{ICONS['bell']}</span><span>Alerts</span></button>
      <button class="nav-item" data-view="view-reports"><span class="icon">{ICONS['siren']}</span><span>Sites Down</span></button>
      <button class="nav-item" data-view="view-settings"><span class="icon">{ICONS['gear']}</span><span>Settings</span></button>
    </div>
    {sidebar_user_html}
  </nav>

  <div class="content">
    {flash_html}

    <section id="view-dashboard" class="view active">
      <div class="page-header">
        <h1>Monitoring Dashboard</h1>
        <div class="live-clock">
          <span class="live-dot" aria-hidden="true"></span>
          <span class="muted mono">{generated_at}</span>
          <button class="refresh-btn" id="refresh-btn"><span class="spinner" aria-hidden="true"></span><span class="refresh-label">Refresh now</span></button>
        </div>
      </div>

      <div class="kpi-row">
        <div class="kpi k-total"><span class="kpi-icon">{ICONS['globe']}</span><div class="kpi-text"><div class="kpi-label">Total sites</div><div class="kpi-value" data-count="{total}">0</div></div></div>
        <div class="kpi k-good"><span class="kpi-icon">{ICONS['check']}</span><div class="kpi-text"><div class="kpi-label">Healthy</div><div class="kpi-value" data-count="{healthy}">0</div></div></div>
        <div class="kpi k-critical{critical_attention}"><span class="kpi-icon">{ICONS['siren']}</span><div class="kpi-text"><div class="kpi-label">Down / Overloaded</div><div class="kpi-value" data-count="{critical}">0</div></div></div>
        <div class="kpi k-alerts"><span class="kpi-icon">{ICONS['bell']}</span><div class="kpi-text">
          <div class="kpi-label-row"><div class="kpi-label">Open outages</div></div>
          <div class="kpi-value" data-count="{open_alerts}">0</div>
          {alerts_trend_html}
        </div></div>
      </div>

      <div class="main-grid">
        <section class="card">
          <h2 class="card-title"><span>Websites</span><span class="muted">click a row for details</span></h2>
          <div class="table-filters">
            <input type="search" id="search-input" class="search-input" placeholder="Search by name or URL…" aria-label="Search sites">
            <div class="filter-chips" role="group" aria-label="Filter by status">
              <button class="filter-chip active" data-filter="all">All</button>
              <button class="filter-chip" data-filter="ok">OK</button>
              <button class="filter-chip" data-filter="critical">Down / Overloaded</button>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>Website</th>
                <th class="sortable sort-active" data-sort="statusorder">Status<span class="sort-arrow">▲</span></th>
                <th class="num sortable" data-sort="rtt">Response Time<span class="sort-arrow">▲▼</span></th>
                <th class="sortable" data-sort="lastcheck">Last Check<span class="sort-arrow">▲▼</span></th>
                <th class="num sortable" data-sort="uptime">Uptime<span class="sort-arrow">▲▼</span></th>
              </tr></thead>
              <tbody id="site-tbody">{''.join(site_rows) if site_rows else '<tr><td colspan="5" class="empty-state">No sites configured yet. Add URLs to urls.txt to start monitoring.</td></tr>'}
              <tr id="no-match-row" style="display:none;"><td colspan="5" class="empty-state">No sites match your search or filter.</td></tr></tbody>
            </table>
          </div>
          <div class="table-footer">
            <span class="muted mono" id="page-caption">—</span>
            <div style="display:flex; gap:8px;">
              <button class="page-btn" id="prev-page">Previous</button>
              <button class="page-btn" id="next-page">Next</button>
            </div>
          </div>
        </section>

        <section class="card">
          <h2 class="card-title"><span>Website Health</span></h2>
          <div class="muted mono" style="padding: 0 22px 4px;" id="health-url">—</div>
          <div id="health-body"></div>
        </section>
      </div>
    </section>

    <section id="view-websites" class="view">
      <div class="page-header"><h1>Websites</h1></div>
      <section class="card">
        <h2 class="card-title">Add a website</h2>
        {add_site_html}
      </section>
      <section class="card">
        <h2 class="card-title">Websites <span class="muted">({total})</span></h2>
        <ul class="site-names">{websites_list_html}</ul>
      </section>
    </section>

    <section id="view-alerts" class="view">
      <div class="page-header"><h1>Alerts</h1></div>
      <section class="card">
        <ul class="alert-list">{''.join(alert_items) if alert_items else '<li class="empty-state">No alerts. Everything is quiet.</li>'}</ul>
      </section>
    </section>

    <section id="view-reports" class="view">
      <div class="page-header"><h1>Sites Down</h1></div>
      <section class="card">
        <h2 class="card-title">Sites currently down / overloaded <span class="muted">({critical})</span></h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Site</th><th>URL</th><th>Reason</th></tr></thead>
            <tbody>{report_rows}</tbody>
          </table>
        </div>
      </section>
    </section>

    <section id="view-settings" class="view">
      <div class="page-header"><h1>Settings</h1></div>
      {settings_html}
    </section>

    <footer class="foot">Data from monitoring.db · regenerate with <span class="mono">python generate_dashboard.py</span></footer>
  </div>
</div>

<script>
  const details = {details_json};
  const ROWS_PER_PAGE = {ROWS_PER_PAGE};

  // ---- Website Health panel (grouped, honest empty/"not monitored" states) ----
  // Worst-status-wins, so a group's dot reflects real problems only --
  // "muted"/"neutral" informational rows (not monitored, no data yet) never
  // outrank an actual good/warning/critical finding in the same group.
  const STATE_PRIORITY = {{ critical: 3, warning: 2, good: 1, neutral: 0, muted: 0 }};
  function worstState(rows) {{
    let worst = 'neutral';
    rows.forEach(([, state]) => {{ if ((STATE_PRIORITY[state] ?? 0) > STATE_PRIORITY[worst]) worst = state; }});
    return worst;
  }}

  function showSite(id) {{
    const body = document.getElementById('health-body');
    const d = details[id];
    if (!d) {{
      document.getElementById('health-url').textContent = '—';
      body.innerHTML = '<div class="health-empty">Select a site to see its health checks.</div>';
      return;
    }}
    document.getElementById('health-url').textContent = d.url;
    body.innerHTML = d.groups.map(([groupLabel, rows]) => {{
      const rowsHtml = rows.map(([label, state, value, tip]) => {{
        const titleAttr = tip ? ` title="${{String(tip).replace(/"/g, '&quot;')}}"` : '';
        return `<div class="health-row"><span>${{label}}</span><span class="pill pill-${{state}}"${{titleAttr}}>${{value}}</span></div>`;
      }}).join('');
      const dot = worstState(rows);
      return `<div class="group-block"><div class="group-label"><i class="group-dot dot-${{dot}}"></i>${{groupLabel}}</div>${{rowsHtml}}</div>`;
    }}).join('');
    document.querySelectorAll('.site-row').forEach(r => r.classList.toggle('selected', r.dataset.siteId === String(id)));
  }}

  document.querySelectorAll('.site-row').forEach(row => {{
    row.addEventListener('click', () => showSite(row.dataset.siteId));
    row.addEventListener('keydown', e => {{ if (e.key === 'Enter') showSite(row.dataset.siteId); }});
  }});

  // ---- Nav (with #hash deep-linking, e.g. /?msg=...#websites) ----
  function showView(target) {{
    if (!document.getElementById(target)) return;
    document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === target));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === target));
  }}
  document.querySelectorAll('.nav-item').forEach(btn => {{
    btn.addEventListener('click', () => {{
      history.replaceState(null, '', '#' + btn.dataset.view.replace('view-', ''));
      showView(btn.dataset.view);
    }});
  }});
  if (location.hash.length > 1) showView('view-' + location.hash.slice(1));

  // ---- Sorting ----
  const tbody = document.getElementById('site-tbody');
  // Rows arrive from the server already ordered by status (DOWN, then
  // UNVERIFIED, then UP), so that's the initial sort state.
  let sortKey = 'statusorder', sortDir = 1;

  function applySort(key) {{
    sortDir = (sortKey === key) ? -sortDir : 1;
    sortKey = key;
    const rows = Array.from(tbody.querySelectorAll('tr[data-site-id]'));
    rows.sort((a, b) => (parseFloat(a.dataset[key]) - parseFloat(b.dataset[key])) * sortDir);
    rows.forEach(r => tbody.appendChild(r));
    document.querySelectorAll('th.sortable').forEach(th => {{
      const active = th.dataset.sort === key;
      th.classList.toggle('sort-active', active);
      th.querySelector('.sort-arrow').textContent = active ? (sortDir === 1 ? '▲' : '▼') : '▲▼';
    }});
    currentPage = 1;
    renderPage();
  }}

  document.querySelectorAll('th.sortable').forEach(th => {{
    th.addEventListener('click', () => applySort(th.dataset.sort));
  }});

  // ---- Search + status filter (combined with pagination below) ----
  const searchInput = document.getElementById('search-input');
  let statusFilter = 'all';

  document.querySelectorAll('.filter-chip').forEach(chip => {{
    chip.addEventListener('click', () => {{
      document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      statusFilter = chip.dataset.filter;
      currentPage = 1;
      renderPage();
    }});
  }});
  searchInput.addEventListener('input', () => {{ currentPage = 1; renderPage(); }});

  function getFilteredRows() {{
    const term = searchInput.value.trim().toLowerCase();
    return Array.from(tbody.querySelectorAll('tr[data-site-id]')).filter(r => {{
      if (statusFilter === 'ok') {{ if (r.dataset.status === 'critical') return false; }}
      else if (statusFilter !== 'all' && r.dataset.status !== statusFilter) return false;
      if (!term) return true;
      const name = r.querySelector('.site-link').textContent.toLowerCase();
      const url = r.querySelector('.site-url').textContent.toLowerCase();
      return name.includes(term) || url.includes(term);
    }});
  }}

  // ---- Pagination (operates over the filtered set) ----
  let currentPage = 1;
  function renderPage() {{
    const allRows = Array.from(tbody.querySelectorAll('tr[data-site-id]'));
    const wasVisible = new Set(allRows.filter(r => r.style.display !== 'none').map(r => r.dataset.siteId));
    allRows.forEach(r => {{ r.style.display = 'none'; r.classList.remove('row-enter'); }});
    const filtered = getFilteredRows();
    const totalRows = filtered.length;
    const totalPages = Math.max(1, Math.ceil(totalRows / ROWS_PER_PAGE));
    currentPage = Math.min(currentPage, totalPages);
    const start = (currentPage - 1) * ROWS_PER_PAGE;
    const end = Math.min(start + ROWS_PER_PAGE, totalRows);
    filtered.forEach((r, i) => {{
      const show = i >= start && i < end;
      r.style.display = show ? '' : 'none';
      // Fade/slide in rows that are newly appearing this render (a filter,
      // search, or sort just changed what's visible) rather than an
      // instant jump -- but not on first paint, and not for repeat-mount.
      if (show && !wasVisible.has(r.dataset.siteId) && wasVisible.size > 0) r.classList.add('row-enter');
    }});
    document.getElementById('no-match-row').style.display = (totalRows === 0 && allRows.length > 0) ? '' : 'none';
    const caption = document.getElementById('page-caption');
    const filteredNote = totalRows !== allRows.length ? ` (filtered from ${{allRows.length}})` : '';
    caption.textContent = totalRows ? `Showing ${{start + 1}}-${{end}} of ${{totalRows}} sites${{filteredNote}}` : 'No sites match';
    document.getElementById('prev-page').disabled = currentPage <= 1;
    document.getElementById('next-page').disabled = currentPage >= totalPages;
  }}
  document.getElementById('prev-page').addEventListener('click', () => {{ currentPage--; renderPage(); }});
  document.getElementById('next-page').addEventListener('click', () => {{ currentPage++; renderPage(); }});
  renderPage();

  // ---- KPI "why doesn't this add up" expandable detail ----
  document.querySelectorAll('.kpi-info-toggle').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const detail = btn.closest('.kpi').querySelector('.kpi-detail');
      const expanded = detail.classList.toggle('expanded');
      btn.setAttribute('aria-expanded', String(expanded));
    }});
  }});

  showSite({default_id if default_id is not None else 'null'});

  // ---- Purely cosmetic enhancements below. Each is wrapped separately so
  // that if any one of them throws (missing browser API, odd embedding
  // context, etc.) the others -- and, critically, the refresh button wiring
  // further down -- still run. A liveliness feature must never be able to
  // take core functionality down with it. ----

  // ---- Count-up on the KPI numbers (skips entirely for reduced-motion) ----
  try {{
    const prefersReducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    document.querySelectorAll('.kpi-value[data-count]').forEach(el => {{
      const target = parseInt(el.dataset.count, 10) || 0;
      if (prefersReducedMotion || target === 0) {{ el.textContent = target; return; }}
      const duration = 700, startTime = performance.now();
      function tick(now) {{
        const progress = Math.min((now - startTime) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
        el.textContent = Math.round(eased * target);
        if (progress < 1) requestAnimationFrame(tick);
      }}
      requestAnimationFrame(tick);
    }});
  }} catch (e) {{ /* count-up is cosmetic only */ }}

  // ---- Live-ticking "Last Check" timestamps ----
  try {{
    function formatAgo(epochSeconds) {{
      const s = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
      if (s < 60) return `${{s}}s ago`;
      if (s < 3600) return `${{Math.floor(s / 60)}}m ago`;
      if (s < 86400) return `${{Math.floor(s / 3600)}}h ago`;
      return `${{Math.floor(s / 86400)}}d ago`;
    }}
    function tickTimestamps() {{
      document.querySelectorAll('.last-check[data-epoch]').forEach(el => {{
        const epoch = parseFloat(el.dataset.epoch);
        if (epoch > 0) el.textContent = formatAgo(epoch);
      }});
    }}
    tickTimestamps();
    setInterval(tickTimestamps, 1000);
  }} catch (e) {{ /* ticking clock is cosmetic only */ }}

  // ---- Soft flash on rows whose check genuinely got fresher since your
  // last visit (compared via localStorage -- per-viewer only, never sent
  // anywhere). ----
  try {{
    const STORE_KEY = 'fleetwatch-last-checks';
    const prevChecks = JSON.parse(localStorage.getItem(STORE_KEY) || '{{}}');
    const nextChecks = {{}};
    document.querySelectorAll('.site-row[data-site-id]').forEach(row => {{
      const id = row.dataset.siteId;
      const epoch = parseFloat(row.querySelector('.last-check')?.dataset.epoch || '0');
      nextChecks[id] = epoch;
      if (prevChecks[id] && epoch > prevChecks[id] + 1) {{
        row.querySelector('.last-check').classList.add('flash');
      }}
    }});
    localStorage.setItem(STORE_KEY, JSON.stringify(nextChecks));
  }} catch (e) {{ /* localStorage unavailable (private mode, etc.) -- flash is cosmetic only */ }}

  // ---- Refresh: manual button + auto every {AUTO_REFRESH_SECONDS}s -- both
  // show a loading state immediately (the page's own content stays on screen,
  // unchanged, for the whole reload, so the overlay is what the user actually
  // sees while a live check runs on the server). ----
  function startRefresh() {{
    document.getElementById('refresh-overlay').classList.add('active');
    const btn = document.getElementById('refresh-btn');
    btn.classList.add('loading');
    btn.disabled = true;
    btn.querySelector('.refresh-label').textContent = 'Refreshing…';
    location.reload();
  }}
  document.getElementById('refresh-btn').addEventListener('click', startRefresh);

  let seconds = {AUTO_REFRESH_SECONDS};
  setInterval(() => {{ seconds -= 1; if (seconds <= 0) startRefresh(); }}, 1000);
</script>
</body>
</html>"""


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    data = fetch_data(conn)
    html = render(data)
    OUT_PATH.write_text(html, encoding="utf-8")
    conn.close()
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
