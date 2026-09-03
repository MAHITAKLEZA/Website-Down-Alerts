"""
Website uptime/health monitor built on Crawl4AI.

Crawls each given URL with Crawl4AI's HTTP crawler strategy (no browser
download required) and flags a site as "not working" on connection
failures, DNS failures, timeouts, 4xx/5xx status codes, or a 200-status
page whose body is an error/maintenance page or is suspiciously empty.

"Overloaded" counts as down too: HTTP 429/502/503/504, an error page that
says so ("too many requests", "server is busy", ...), or a page that does
load but takes >= SLOW_RESPONSE_MS (default 15000) -- the signatures of a
section getting more traffic than it can serve.

Usage:
    python website_monitor.py https://example.com https://another.com
    python website_monitor.py --file urls.txt
    python website_monitor.py --file urls.txt --interval 300   # loop every 5 min
    python website_monitor.py --file urls.txt --json           # machine-readable output

In --interval (continuous) mode, alerts go through the shared notifier stack:
console + alerts.log always, and the Microsoft Teams channel when
TEAMS_WEBHOOK_URL is set. A site alerts as soon as a check returns DOWN
(raise DOWN_CONFIRM_CHECKS above 1 to require that many DOWN checks in a row
first). The alert then repeats every DOWN_REALERT_SECONDS (default 3600 = 1
hour) for as long as the site stays down, and the last alert time is tracked
in --alert-state so a restart doesn't re-spam or reset the hourly clock; a
recovery notice follows when the site comes back UP.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig, HTTPCrawlerConfig
from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

from monitoring.notifiers import get_notifier
from monitoring.ssl_check import get_ssl_days_remaining

# Phrases that show up in the page body when a server is up but the site
# behind it isn't -- e.g. a 200-status "friendly" error/maintenance page.
ERROR_SIGNATURES = [
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "bad gateway",
    "service unavailable",
    "internal server error",
    "site can't be reached",
    "this site can’t be reached",
    "err_connection",
    "err_name_not_resolved",
    "database connection error",
    "database error",
    "under maintenance",
    "site is temporarily unavailable",
    "account suspended",
    "domain has expired",
    "too many requests",
    "rate limit exceeded",
    "server is busy",
    "traffic overload",
]

MIN_CONTENT_LENGTH = 50  # markdown chars; below this a "success" page is suspect

# A page that loads but takes at least this long is treated as DOWN: for a
# real visitor a section this slow is effectively unusable, and it's the
# classic signature of a site buckling under more traffic than it can serve.
# Override with the SLOW_RESPONSE_MS env var; set it to 0 to disable.
DEFAULT_SLOW_RESPONSE_MS = 15000

# HTTP statuses that mean "the server is up but can't serve this request right
# now" -- almost always load/capacity related. Treated as DOWN (with the
# 2-consecutive-check debounce in the alert engine absorbing brief spikes).
OVERLOAD_STATUSES = {
    429: "HTTP 429 Too Many Requests -- server is rate-limiting; likely more traffic than it can handle",
    502: "HTTP 502 Bad Gateway -- the upstream/app server is overwhelmed or not responding",
    503: "HTTP 503 Service Unavailable -- server overloaded or in maintenance",
    504: "HTTP 504 Gateway Timeout -- the upstream/app server is too slow to respond",
}


def clean_reason(raw: str) -> str:
    """Crawl4AI wraps internal errors with a source line + code snippet;
    pull out just the underlying "Error: ..." message for a readable reason."""
    match = re.search(r"Error:\s*(.+?)(?:\n\nCode context:|$)", raw, re.DOTALL)
    text = (match.group(1) if match else raw).strip()
    return " ".join(text.split())


# HTTP statuses that WAFs/CDNs commonly return to non-browser clients even
# when the site is fine for real visitors (Cloudflare/Akamai bot challenges).
# 429 is deliberately NOT here -- see OVERLOAD_STATUSES above.
AMBIGUOUS_BLOCK_STATUSES = {403}


@dataclass
class WebsiteStatus:
    url: str
    status: str  # "UP" | "DOWN" | "UNCERTAIN"
    status_code: int  # real HTTP code, or 0 if no HTTP response was received
    reason: str
    response_time_ms: int
    checked_at: str
    ssl_days_remaining: int | None = None


def make_http_strategy() -> AsyncHTTPCrawlerStrategy:
    """Plain HTTP strategy: no browser/Chromium download needed, fast, good
    enough for up/down detection (no JS-rendered content is inspected).
    Brotli ("br") is excluded because aiohttp only decodes it if the
    optional `Brotli` package is installed; without it a perfectly healthy
    br-compressed response looks like a client error."""
    return AsyncHTTPCrawlerStrategy(browser_config=HTTPCrawlerConfig(headers={"Accept-Encoding": "gzip, deflate"}))


async def check_url_full(crawler: AsyncWebCrawler, url: str, timeout_ms: int):
    """Like check_url, but also returns the raw CrawlResult (or None) so
    callers that need the fetched HTML (e.g. page analysis) don't have to
    fetch the page a second time."""
    started = time.perf_counter()
    checked_at = datetime.now(timezone.utc).isoformat()
    config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=timeout_ms)

    try:
        result = await crawler.arun(url=url, config=config)
    except Exception as exc:  # noqa: BLE001 - surface any crawl failure as "down"
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # 0 = no HTTP response was ever received (standard convention, same as
        # curl's "000"): covers DNS failures, TLS/SSL handshake failures, and
        # refused connections -- there's genuinely no HTTP status at that point.
        return WebsiteStatus(url, "DOWN", 0, clean_reason(str(exc)), elapsed_ms, checked_at), None

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    status_code = getattr(result, "status_code", None)

    if not result.success:
        reason = clean_reason(getattr(result, "error_message", None) or "crawl did not succeed")
        if status_code is None:
            # The HTTP strategy raises on non-2xx before result.status_code is
            # set, but the code is still embedded in the error text.
            status_match = re.search(r"HTTP (\d{3})", reason)
            # 0 = no HTTP response received at all (DNS/TLS/connection failure);
            # see the except-block comment above for the convention.
            status_code = int(status_match.group(1)) if status_match else 0

        # Crawl4AI's own anti-bot heuristic flags any "script-heavy, no
        # visible text" page as blocked -- which is exactly what a normal
        # JS-rendered SPA looks like over a plain HTTP fetch (no browser, no
        # JS execution here). Can't tell a real block from a healthy SPA
        # shell without rendering it, so this is uncertain, not confirmed down.
        if "anti-bot" in reason.lower() or "structural:" in reason.lower():
            reason += " (page may just be a JS-rendered app; this checker does not run a browser)"
            return WebsiteStatus(url, "UNCERTAIN", status_code, reason, elapsed_ms, checked_at), result

        if status_code in AMBIGUOUS_BLOCK_STATUSES:
            reason += " (could be a WAF/bot-protection block rather than the site being down; verify manually)"
            return WebsiteStatus(url, "UNCERTAIN", status_code, reason, elapsed_ms, checked_at), result

        if status_code in OVERLOAD_STATUSES:
            return WebsiteStatus(
                url, "DOWN", status_code, f"overloaded: {OVERLOAD_STATUSES[status_code]}", elapsed_ms, checked_at
            ), result

        return WebsiteStatus(url, "DOWN", status_code, reason, elapsed_ms, checked_at), result

    if status_code in OVERLOAD_STATUSES:
        return WebsiteStatus(
            url, "DOWN", status_code, f"overloaded: {OVERLOAD_STATUSES[status_code]}", elapsed_ms, checked_at
        ), result

    if status_code is not None and status_code >= 400:
        status = "UNCERTAIN" if status_code in AMBIGUOUS_BLOCK_STATUSES else "DOWN"
        return WebsiteStatus(url, status, status_code, f"HTTP {status_code}", elapsed_ms, checked_at), result

    content = (result.markdown or "").strip() if hasattr(result, "markdown") else ""
    content_lower = content.lower()

    for signature in ERROR_SIGNATURES:
        if signature in content_lower:
            return (
                WebsiteStatus(url, "DOWN", status_code, f"error page detected: '{signature}'", elapsed_ms, checked_at),
                result,
            )

    if len(content) < MIN_CONTENT_LENGTH:
        return (
            WebsiteStatus(
                url,
                "UNCERTAIN",
                status_code,
                "page loaded but content is empty/too thin (could be a JS-rendered app)",
                elapsed_ms,
                checked_at,
            ),
            result,
        )

    slow_ms = int(os.environ.get("SLOW_RESPONSE_MS", str(DEFAULT_SLOW_RESPONSE_MS)))
    if slow_ms and elapsed_ms >= slow_ms:
        return (
            WebsiteStatus(
                url,
                "DOWN",
                status_code,
                f"overloaded: page loaded but took {elapsed_ms} ms (>= {slow_ms} ms) -- too slow to be usable",
                elapsed_ms,
                checked_at,
            ),
            result,
        )

    ssl_days_remaining = await get_ssl_days_remaining(url)
    return WebsiteStatus(url, "UP", status_code, "OK", elapsed_ms, checked_at, ssl_days_remaining), result


async def check_url(crawler: AsyncWebCrawler, url: str, timeout_ms: int) -> WebsiteStatus:
    status, _ = await check_url_full(crawler, url, timeout_ms)
    return status


async def check_urls(urls: list[str], timeout_ms: int = 20000, concurrency: int = 5) -> list[WebsiteStatus]:
    semaphore = asyncio.Semaphore(concurrency)

    async with AsyncWebCrawler(crawler_strategy=make_http_strategy()) as crawler:

        async def bound_check(url: str) -> WebsiteStatus:
            async with semaphore:
                return await check_url(crawler, url, timeout_ms)

        return await asyncio.gather(*(bound_check(u) for u in urls))


def load_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            urls.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))
    if not urls:
        raise SystemExit("No URLs given. Pass them as arguments or via --file.")
    return urls


def print_report(results: list[WebsiteStatus]) -> None:
    for r in results:
        print(f"[{r.status:^9}] {r.url}  ({r.response_time_ms}ms, status={r.status_code}) - {r.reason}")
    up = sum(1 for r in results if r.status == "UP")
    down = sum(1 for r in results if r.status == "DOWN")
    uncertain = sum(1 for r in results if r.status == "UNCERTAIN")
    print(f"\n{up}/{len(results)} confirmed up, {down} confirmed down, {uncertain} uncertain (need manual/browser check).")


DEFAULT_ALERT_STATE_FILE = ".monitor_alert_state.json"


class OutageTracker:
    """Keeps state across interval-loop iterations so the standalone monitor
    can alert with the same discipline as the DB-backed fast-check path:

    - a newly-DOWN site alerts as soon as a check returns DOWN
      (DOWN_CONFIRM_CHECKS, default 1; raise to 2+ to require that many DOWN
      checks in a row first)
    - once when the outage starts, then again every DOWN_REALERT_SECONDS
      (default 3600) while the site stays down; the per-URL last-alert time is
      persisted to a small JSON state file so restarting the monitor doesn't
      re-spam the channel or reset the reminder clock
    - a recovery notice when a previously-alerted site comes back UP

    UNCERTAIN is treated as "not confirmed down": it resets the DOWN streak
    but does not itself clear an open outage. Alerts go through the shared
    notifier stack -- console + alerts.log always, Teams when TEAMS_WEBHOOK_URL
    is set (Teams only receives the DOWN alerts, matching the DB path).
    """

    def __init__(self, state_path: str | None = DEFAULT_ALERT_STATE_FILE) -> None:
        self._notifier = get_notifier()
        self._down_streak: dict[str, int] = {}
        self._state_path = state_path
        # url -> epoch seconds of the last availability alert sent for the
        # current outage. Presence in this dict means "outage already alerted".
        self._alerted: dict[str, float] = {}
        # URLs already covered by a Teams digest card -- so an unchanged
        # down-list isn't re-sent to Teams every pass.
        self._digested: set[str] = set()
        self._load_state()
        # While a site stays down, re-send the alert this often (seconds) so an
        # ongoing outage keeps nagging. Default 1h; DOWN_REALERT_SECONDS=0 off.
        self._realert_seconds = int(os.environ.get("DOWN_REALERT_SECONDS", "3600"))
        # DOWN checks in a row before the first alert (default 1 = immediate).
        self._confirm_checks = max(1, int(os.environ.get("DOWN_CONFIRM_CHECKS", "1")))

    def _load_state(self) -> None:
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        alerted = data.get("alerted", [])
        # Back-compat: old state files stored a plain list of URLs with no
        # timestamp. Treat those as "just alerted" so upgrading doesn't fire a
        # burst of reminders on the first pass.
        if isinstance(alerted, dict):
            self._alerted = {u: float(t) for u, t in alerted.items()}
        else:
            self._alerted = {u: time.time() for u in alerted}
        self._digested = set(data.get("digested", []))

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump({"alerted": self._alerted, "digested": sorted(self._digested)}, f, indent=2)
        except OSError as exc:  # noqa: BLE001 - persistence is best-effort
            print(f"[ALERT-STATE] could not write {self._state_path}: {exc}")

    async def process(self, results: list[WebsiteStatus]) -> None:
        now = time.time()
        for r in results:
            if r.status == "DOWN":
                streak = self._down_streak.get(r.url, 0) + 1
                self._down_streak[r.url] = streak
                if streak < self._confirm_checks:
                    continue
                if r.url not in self._alerted:
                    self._alerted[r.url] = now
                    await self._notify(r, "availability", "critical", f"Site is down: {r.reason}")
                elif self._realert_seconds > 0 and now - self._alerted[r.url] >= self._realert_seconds:
                    self._alerted[r.url] = now
                    await self._notify(r, "availability", "critical", f"Site is down: {r.reason} (still down)")
            else:
                self._down_streak[r.url] = 0
                if r.status == "UP" and r.url in self._alerted:
                    del self._alerted[r.url]
                    await self._notify(r, "recovery", "info", "Site has recovered and is UP again")
        self._save_state()

    async def _notify(self, r: WebsiteStatus, alert_type: str, severity: str, message: str) -> None:
        alert_row = {
            "severity": severity,
            "alert_type": alert_type,
            "message": message,
            "created_at": r.checked_at,
        }
        await self._notifier.notify(r.url, alert_row)

    async def send_digest(self, results: list[WebsiteStatus]) -> None:
        """One combined 'N sites down' card to Teams -- but only when the
        down-list has changed since the last card (a newly-down site). An
        unchanged list is not re-sent. Per-site lines still go to console/log."""
        down = [(r.url, r.reason) for r in results if r.status == "DOWN" and r.url in self._alerted]
        current = {url for url, _ in down}
        new_sites = current - self._digested

        if not new_sites:
            self._digested = current  # drop recovered sites; a re-outage re-alerts
            self._save_state()
            return

        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sent = await self._notifier.post_teams_digest(down, checked_at, new_count=len(new_sites))
        self._digested = current if sent else (self._digested & current)
        self._save_state()


async def run_once(urls: list[str], timeout_ms: int, as_json: bool, log_file: str | None) -> list[WebsiteStatus]:
    results = await check_urls(urls, timeout_ms=timeout_ms)

    if as_json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print_report(results)

    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(asdict(r)) + "\n")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect websites that are not working, using Crawl4AI.")
    parser.add_argument("urls", nargs="*", help="One or more URLs to check")
    parser.add_argument("--file", help="Path to a text file with one URL per line")
    parser.add_argument("--interval", type=int, help="Re-check every N seconds (runs forever)")
    parser.add_argument("--timeout", type=int, default=20000, help="Per-site timeout in ms (default 20000)")
    parser.add_argument("--json", action="store_true", help="Print results as JSON instead of a table")
    parser.add_argument("--log-file", help="Append each check's results as JSONL to this file")
    parser.add_argument(
        "--alert-state",
        default=DEFAULT_ALERT_STATE_FILE,
        help="JSON file tracking which outages have already alerted, so a restart "
        f"doesn't re-notify (default: {DEFAULT_ALERT_STATE_FILE}). Pass '' to disable.",
    )
    args = parser.parse_args()

    urls = load_urls(args)

    async def loop() -> None:
        # Alerting only makes sense across repeated checks (the 2-consecutive-DOWN
        # rule needs history), so it's wired into interval mode only.
        tracker = OutageTracker(args.alert_state or None) if args.interval else None
        if tracker and not (os.environ.get("TEAMS_WEBHOOK_URL") or os.environ.get("TEAMS_DM_WEBHOOK_URL")):
            print("[TEAMS] no TEAMS_WEBHOOK_URL / TEAMS_DM_WEBHOOK_URL -- alerts go to console + alerts.log only.\n")

        while True:
            results = await run_once(urls, args.timeout, args.json, args.log_file)
            if not args.interval:
                sys.exit(0 if all(r.status == "UP" for r in results) else 1)
            if tracker:
                await tracker.process(results)
                await tracker.send_digest(results)
            print(f"\nNext check in {args.interval}s...\n")
            await asyncio.sleep(args.interval)

    asyncio.run(loop())


if __name__ == "__main__":
    main()
