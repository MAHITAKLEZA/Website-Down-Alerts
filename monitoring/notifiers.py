"""Alert delivery. Always logs to console + alerts.log; also posts to
Microsoft Teams via one or both of these environment variables:

  TEAMS_WEBHOOK_URL       -> posts the alert card to a Teams *channel*
  TEAMS_DM_WEBHOOK_URL    -> sends the same card as a 1:1 *chat* to one person
  TEAMS_DAILY_WEBHOOK_URL -> target for the once-a-day status card (daily_report.py)

Set any combination. TEAMS_WEBHOOK_URL / TEAMS_DM_WEBHOOK_URL get ONE combined
"N sites down" card, and only when the down-site list has changed since the last
card (see alert_engine.send_outage_digest). TEAMS_DAILY_WEBHOOK_URL instead gets
one card every day at a scheduled time (even when all sites are up). Recovery,
SSL, links and structure-change alerts stay on the console + alerts.log only.

Getting a channel webhook URL (Microsoft retired the old "Incoming Webhook"
connector -- Teams now uses the Workflows app / Power Automate):
  1. In the Teams channel, hover the channel name -> "..." -> Workflows.
  2. Search "webhook" and choose "Post to a channel when a webhook request is
     received".
  3. Sign in / finish the wizard, picking the target team + channel.
  4. On the workflow's page click "Copy webhook link".
  5. setx TEAMS_WEBHOOK_URL "https://...."   (new terminals/scheduled tasks
     pick it up; restart the current one).

Getting a direct-message webhook URL (alerts one specific person):
  1. Teams -> Apps -> "Workflows" -> Create -> start from blank, or search the
     template "Post to a chat when a webhook request is received".
  2. Trigger: "When a Teams webhook request is received".
  3. Action: "Post message in a chat or channel" -> Post in: Chat ->
     Recipient: the person to alert (they must allow messages from the flow).
  4. In that action's Message box switch to the code/expression view and paste
     the adaptive-card body, or use "Post card in a chat or channel" and map
     the incoming request body straight through.
  5. Save, "Copy webhook link", then: setx TEAMS_DM_WEBHOOK_URL "https://...."

Both Workflows webhooks ONLY accept an Adaptive Card payload (wrapped in a
{"type": "message", "attachments": [...]} envelope) via POST, max 256 KB --
plain JSON is silently rejected. TeamsNotifier below builds that card.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

LOG_PATH = Path(__file__).resolve().parent.parent / "alerts.log"

# ANSI colours for the terminal banner, keyed by alert severity.
_TERM_COLOR = {
    "critical": "\033[97;41m",  # white on red
    "high": "\033[97;41m",
    "warning": "\033[30;43m",  # black on yellow
    "medium": "\033[30;43m",
    "info": "\033[97;42m",  # white on green
    "low": "\033[97;42m",
}
_TERM_RESET = "\033[0m"


def _supports_ansi() -> bool:
    """True when stdout is a real terminal that can render ANSI colour."""
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Modern Windows Terminal / VS Code set WT_SESSION / TERM_PROGRAM;
        # classic conhost needs virtual-terminal processing enabled.
        if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
            return True
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:  # noqa: BLE001
            return False
    return True

# Severity -> emoji, used in the console banner.
_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🔴",
    "warning": "🟡",
    "medium": "🟡",
    "info": "🟢",
    "low": "🟢",
}


class ConsoleNotifier:
    async def notify(self, site_name: str, alert_row) -> None:
        severity = (alert_row["severity"] or "info").lower()
        emoji = _SEVERITY_EMOJI.get(severity, "🔔")
        line = f"[{alert_row['created_at']}] [{severity.upper()}] {site_name}: {alert_row['message']}"

        # Plain line always goes to the log file.
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        # Prominent, attention-grabbing banner in the terminal. \a rings the
        # terminal bell so a site going down is noticed even if you're not
        # watching the window.
        local_time = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        header = f"{emoji}  WEBSITE {'DOWN' if severity in ('critical', 'high') else severity.upper()}: {site_name}"
        body = f"   {alert_row['message']}"
        footer = f"   type={alert_row['alert_type']}   {local_time}"
        width = max(len(header), len(body), len(footer)) + 2

        if _supports_ansi():
            color = _TERM_COLOR.get(severity, "")
            print(f"\a{color}")
            print(f" {header.ljust(width)}")
            print(f" {body.ljust(width)}")
            print(f" {footer.ljust(width)}{_TERM_RESET}")
        else:
            bar = "=" * width
            print(f"\a{bar}")
            print(header)
            print(body)
            print(footer)
            print(bar)


# Long crawler/SSL error strings -> a short phrase a human can scan. First
# matching (case-insensitive) substring wins; anything unmatched is clipped.
_REASON_SIGNATURES = [
    ("getaddrinfo failed", "DNS lookup failed (domain not resolving)"),
    ("name or service not known", "DNS lookup failed (domain not resolving)"),
    ("certificate has expired", "SSL certificate has expired"),
    ("certificate_verify_failed", "SSL certificate error"),
    ("ssl:", "SSL / TLS handshake failed"),
    ("tlsv1_alert", "TLS handshake failed"),
    ("cannot connect to host", "connection refused / host unreachable"),
    ("connection timeout", "connection timed out"),
    ("timeouterror", "connection timed out"),
    ("http 500", "HTTP 500 (server error)"),
    ("http 502", "HTTP 502 (overloaded / bad gateway)"),
    ("http 503", "HTTP 503 (overloaded / unavailable)"),
    ("http 504", "HTTP 504 (overloaded / gateway timeout)"),
    ("http 429", "HTTP 429 (too many requests / overloaded)"),
    ("overloaded:", None),  # keep the monitor's own "overloaded: ..." text as-is
]


def _short_reason(message: str, limit: int = 120) -> str:
    """Turn the stored 'Site is down: <reason>' message into a short, readable
    phrase for the digest card."""
    _, sep, tail = str(message).partition("Site is down:")
    reason = (tail.strip() if sep else str(message)).strip()

    # Carry a trailing "(still down after ...)" note through unchanged.
    still_down = ""
    if "(still down after" in reason:
        reason, _, rest = reason.partition("(still down after")
        reason = reason.strip()
        still_down = f" (down {rest.strip(') ')})"

    low = reason.lower()
    for needle, phrase in _REASON_SIGNATURES:
        if needle in low:
            if phrase is not None:
                reason = phrase
            break

    if len(reason) > limit:
        reason = reason[: limit - 1].rstrip() + "…"
    return reason + still_down


class TeamsNotifier:
    """Posts to a Teams Workflows webhook (channel or 1:1 chat). Per-site
    alerts are NOT sent here -- Teams gets a single combined 'N sites down'
    card per check pass via post_digest(). Individual alerts still go to the
    console + alerts.log through ConsoleNotifier."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def notify(self, site_name: str, alert_row) -> None:  # noqa: ARG002 - part of the notifier interface
        return

    @staticmethod
    def _build_digest_card(down_sites: list[tuple[str, str]], checked_at: str, new_count: int = 0) -> dict:
        n = len(down_sites)
        heading = f"🔴 {n} site{'s' if n != 1 else ''} down"
        if 0 < new_count <= n:
            heading += f" · {new_count} new" if new_count < n else ""
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "size": "Large",
                    "weight": "Bolder",
                    "color": "attention",
                    "text": heading,
                    "wrap": True,
                },
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": name, "value": _short_reason(reason)} for name, reason in down_sites
                    ],
                },
                {
                    "type": "TextBlock",
                    "size": "Small",
                    "isSubtle": True,
                    "wrap": True,
                    "text": f"Checked {checked_at}",
                },
            ],
        }
        return {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
        }

    @staticmethod
    def _build_report_card(down_sites: list[tuple[str, str]], as_of: str) -> dict:
        """Daily status card -- always has content, even when nothing is down."""
        n = len(down_sites)
        if n == 0:
            body = [{"type": "TextBlock", "size": "Large", "weight": "Bolder", "color": "good",
                     "text": "✅ Daily report — all sites up", "wrap": True}]
        else:
            body = [
                {"type": "TextBlock", "size": "Large", "weight": "Bolder", "color": "attention",
                 "text": f"\U0001f534 Daily report — {n} site{'s' if n != 1 else ''} down", "wrap": True},
                {"type": "FactSet",
                 "facts": [{"title": name, "value": _short_reason(reason)} for name, reason in down_sites]},
            ]
        body.append({"type": "TextBlock", "size": "Small", "isSubtle": True, "wrap": True, "text": f"As of {as_of}"})
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard", "version": "1.4", "body": body},
            }],
        }

    async def _post(self, payload: dict, tag: str = "TEAMS") -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status >= 300:
                        print(f"[{tag}] webhook post failed ({resp.status}): {(await resp.text())[:300]}")
                        return False
                    return True
        except Exception as exc:  # noqa: BLE001 - a failed notification must not crash the run
            print(f"[{tag}] webhook post error: {exc}")
            return False

    async def post_digest(self, down_sites: list[tuple[str, str]], checked_at: str, new_count: int = 0) -> bool:
        """Returns True if the card was accepted by the webhook (so callers
        can hold off marking the outage 'notified' when a post fails)."""
        if not down_sites:
            return False
        return await self._post(self._build_digest_card(down_sites, checked_at, new_count))

    async def post_daily_report(self, down_sites: list[tuple[str, str]], as_of: str) -> bool:
        """One daily status card -- sent unconditionally (used by daily_report.py)."""
        return await self._post(self._build_report_card(down_sites, as_of), tag="DAILY")


class CompositeNotifier:
    def __init__(self, notifiers: list):
        self.notifiers = notifiers

    async def notify(self, site_name: str, alert_row) -> None:
        for notifier in self.notifiers:
            await notifier.notify(site_name, alert_row)

    async def post_teams_digest(
        self, down_sites: list[tuple[str, str]], checked_at: str, new_count: int = 0
    ) -> bool:
        """True if at least one Teams webhook accepted the card."""
        sent = False
        for notifier in self.notifiers:
            post_digest = getattr(notifier, "post_digest", None)
            if post_digest and await post_digest(down_sites, checked_at, new_count):
                sent = True
        return sent


def get_notifier():
    notifiers = [ConsoleNotifier()]
    # A channel post, a 1:1 DM, or both -- whichever webhook URLs are set.
    for env_var in ("TEAMS_WEBHOOK_URL", "TEAMS_DM_WEBHOOK_URL"):
        webhook_url = os.environ.get(env_var)
        if webhook_url:
            notifiers.append(TeamsNotifier(webhook_url))
    return CompositeNotifier(notifiers)
