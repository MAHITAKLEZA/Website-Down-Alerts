"""External (no-credentials) security checks for one site.

Everything here is what any visitor can see: the homepage HTML + headers, a
handful of well-known sensitive paths, public WordPress/RDAP APIs, and
(optionally) Google Safe Browsing. It cannot see server files -- real file-
integrity/malware scanning needs hosting access. Findings are indicators to
investigate, not proof of compromise.

Categories: headers, exposed_files, malware, vulnerability, domain, reputation.
Severities: high, medium, low.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10)
PAGE_TIMEOUT = aiohttp.ClientTimeout(total=20)
NEW_HOST_WINDOW_DAYS = 14  # a newly seen external script/iframe host stays flagged this long
DOMAIN_WARN_DAYS = 30
MAX_PLUGINS_CHECKED = 25


@dataclass
class Finding:
    category: str
    severity: str
    title: str
    detail: str = ""


@dataclass
class SecurityResult:
    reachable: bool
    findings: list[Finding] = field(default_factory=list)
    baseline: dict = field(default_factory=dict)


class Context:
    """Shared across all sites in one run: HTTP session + caches for the
    public APIs (so 36 sites don't each re-ask for the latest WP version)."""

    def __init__(self, session: aiohttp.ClientSession, safe_browsing_key: str | None):
        self.session = session
        self.safe_browsing_key = safe_browsing_key
        self.wp_latest: tuple[int, ...] | None | bool = False  # False = not fetched yet
        self.plugin_latest: dict[str, tuple[int, ...] | None] = {}
        self.rdap: dict[str, int | None] = {}
        self.probe_sem = asyncio.Semaphore(8)


async def _fetch(ctx: Context, url: str, *, method: str = "GET", max_bytes: int = 65536,
                 allow_redirects: bool = True, timeout: aiohttp.ClientTimeout = PROBE_TIMEOUT, **kw):
    """(status, headers, body_bytes, final_url) or None on any network error."""
    try:
        async with ctx.probe_sem:
            async with ctx.session.request(method, url, allow_redirects=allow_redirects, timeout=timeout, **kw) as resp:
                chunks, total = [], 0
                async for chunk in resp.content.iter_chunked(16384):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                return resp.status, resp.headers, b"".join(chunks), str(resp.url)
    except Exception:  # noqa: BLE001 - any network failure just means "no signal"
        return None


def _text(body: bytes) -> str:
    return body.decode("utf-8", errors="ignore")


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _registrable(host: str) -> str:
    labels = host.lower().removeprefix("www.").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "gov", "ac", "edu"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# --- security headers, HTTPS ------------------------------------------------

async def _check_headers(ctx, origin: str, headers, html: str, findings: list[Finding]) -> None:
    is_https = origin.startswith("https://")
    h = {k.lower(): v for k, v in headers.items()}
    missing = []
    if is_https and "strict-transport-security" not in h:
        missing.append("HSTS")
    csp = h.get("content-security-policy", "")
    if not csp:
        missing.append("CSP")
    if "x-frame-options" not in h and "frame-ancestors" not in csp.lower():
        missing.append("X-Frame-Options")
    if h.get("x-content-type-options", "").lower() != "nosniff":
        missing.append("X-Content-Type-Options")
    if missing:
        findings.append(Finding("headers", "low", f"Missing security headers: {', '.join(missing)}",
                                "Browsers rely on these headers to block clickjacking, MIME sniffing and downgrade attacks."))

    leaks = []
    server = h.get("server", "")
    if re.search(r"\d+\.\d+", server):
        leaks.append(f"Server: {server}")
    if h.get("x-powered-by"):
        leaks.append(f"X-Powered-By: {h['x-powered-by']}")
    if leaks:
        findings.append(Finding("headers", "low", "Server software version disclosed", "; ".join(leaks)))

    if is_https:
        mixed = re.findall(r"""<(?:script|iframe|link)[^>]+(?:src|href)=["'](http://[^"']+)""", html, re.I)
        if mixed:
            findings.append(Finding("headers", "medium", "Mixed content (insecure http:// resources on an https page)",
                                    ", ".join(sorted(set(mixed))[:3])))
        # Does plain http redirect to https?
        r = await _fetch(ctx, "http://" + urlparse(origin).netloc + "/", allow_redirects=False, max_bytes=1024)
        if r and r[0] == 200:
            findings.append(Finding("headers", "medium", "HTTP is served without redirecting to HTTPS",
                                    "Visitors on http:// are never upgraded to https://."))


# --- exposed files ----------------------------------------------------------

def _is_html(headers) -> bool:
    return "html" in headers.get("Content-Type", "").lower()


def _sig_env(t, b, h): return not _is_html(h) and re.search(r"^[A-Z][A-Z0-9_]{2,}\s*=", t, re.M) is not None
def _sig_git_config(t, b, h): return "[core]" in t
def _sig_git_head(t, b, h): return t.startswith("ref:")
def _sig_wpconfig(t, b, h): return "DB_PASSWORD" in t or "DB_NAME" in t
def _sig_phpinfo(t, b, h): return "phpinfo()" in t or "PHP Version" in t
def _sig_sql(t, b, h): return not _is_html(h) and ("CREATE TABLE" in t or "INSERT INTO" in t)
def _sig_zip(t, b, h): return b[:4] == b"PK\x03\x04"
def _sig_gzip(t, b, h): return b[:2] == b"\x1f\x8b"
def _sig_dsstore(t, b, h): return b[:8] == b"\x00\x00\x00\x01Bud1"
def _sig_htpasswd(t, b, h): return re.search(r"^[\w.-]+:(\$|\{SHA\})", t, re.M) is not None
def _sig_status(t, b, h): return "Apache Server Status" in t
def _sig_debuglog(t, b, h): return not _is_html(h) and re.search(r"PHP (Notice|Warning|Fatal|Deprecated)", t) is not None

EXPOSED_PATHS = [
    ("/.env", "high", "Exposed .env file (secrets)", _sig_env),
    ("/.git/config", "high", "Exposed .git repository", _sig_git_config),
    ("/.git/HEAD", "high", "Exposed .git repository", _sig_git_head),
    ("/wp-config.php.bak", "high", "Exposed wp-config backup (database password)", _sig_wpconfig),
    ("/wp-config.php~", "high", "Exposed wp-config backup (database password)", _sig_wpconfig),
    ("/wp-config.php.old", "high", "Exposed wp-config backup (database password)", _sig_wpconfig),
    ("/wp-config.php.save", "high", "Exposed wp-config backup (database password)", _sig_wpconfig),
    ("/wp-config.txt", "high", "Exposed wp-config backup (database password)", _sig_wpconfig),
    ("/.htpasswd", "high", "Exposed .htpasswd (password hashes)", _sig_htpasswd),
    ("/backup.sql", "high", "Exposed database dump", _sig_sql),
    ("/db.sql", "high", "Exposed database dump", _sig_sql),
    ("/dump.sql", "high", "Exposed database dump", _sig_sql),
    ("/database.sql", "high", "Exposed database dump", _sig_sql),
    ("/backup.zip", "high", "Exposed site backup archive", _sig_zip),
    ("/site.zip", "high", "Exposed site backup archive", _sig_zip),
    ("/www.zip", "high", "Exposed site backup archive", _sig_zip),
    ("/public_html.zip", "high", "Exposed site backup archive", _sig_zip),
    ("/backup.tar.gz", "high", "Exposed site backup archive", _sig_gzip),
    ("/wp-content/debug.log", "high", "Exposed WordPress debug.log", _sig_debuglog),
    ("/phpinfo.php", "medium", "Exposed phpinfo() page", _sig_phpinfo),
    ("/info.php", "medium", "Exposed phpinfo() page", _sig_phpinfo),
    ("/server-status", "medium", "Exposed Apache server-status", _sig_status),
    ("/error_log", "medium", "Exposed PHP error_log", _sig_debuglog),
    ("/.DS_Store", "low", "Exposed .DS_Store file (directory listing leak)", _sig_dsstore),
]


async def _check_exposed_files(ctx, origin: str, findings: list[Finding]) -> None:
    async def probe(path, sev, title, sig):
        r = await _fetch(ctx, origin + path, allow_redirects=False, max_bytes=65536)
        if r and r[0] == 200:
            status, headers, body, _ = r
            if sig(_text(body), body, headers):
                return path, sev, title
        return None

    hits = [x for x in await asyncio.gather(*(probe(*p) for p in EXPOSED_PATHS)) if x]
    seen = set()
    for path, sev, title in hits:
        if title in seen:  # one finding per kind, e.g. several wp-config variants
            continue
        seen.add(title)
        paths = ", ".join(p for p, _, t in hits if t == title)
        findings.append(Finding("exposed_files", sev, title, f"Publicly readable: {paths}"))


# --- malware indicators (homepage HTML) -------------------------------------

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|(?:left|top|text-indent)\s*:\s*-\d{3,}px|"
    r"font-size\s*:\s*0|(?:height|width)\s*:\s*0(?:px)?\s*(?:;|$)", re.I)
_OBFUSCATION = [
    (re.compile(r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e"), "packed eval(function(p,a,c,k,e,d))"),
    (re.compile(r"eval\s*\(\s*(?:atob|unescape)\s*\("), "eval(atob/unescape(...))"),
    (re.compile(r"document\.write\s*\(\s*unescape\s*\("), "document.write(unescape(...))"),
    (re.compile(r"(?:\\x[0-9a-fA-F]{2}){20,}"), "long \\x hex-escaped string"),
    (re.compile(r"atob\s*\(\s*[\"'][A-Za-z0-9+/=]{200,}"), "large base64 blob decoded at runtime"),
]
_MINERS = re.compile(r"coinhive|coin-hive|cryptonight|minero\.cc|authedmine|webminepool|crypto-loot|jsecoin", re.I)
_SPAM_WORDS = ("viagra", "cialis", "levitra", "payday loan", "casino", "poker", "porn", "sex cam",
               "replica watches", "escort", "betting", "essay writing")
_TRACKER_HOSTS = ("googletagmanager.com", "google-analytics.com", "doubleclick.net", "facebook.com", "facebook.net",
                  "hotjar.com", "clarity.ms", "linkedin.com", "bing.com")
_COMMON_HOSTS = ("facebook.", "twitter.", "x.com", "instagram.", "linkedin.", "youtube.", "google.", "goo.gl",
                 "wa.me", "whatsapp.", "pinterest.", "t.me", "maps.")


def _first_seen_map(current: set[str], previous: dict | None, now_iso: str) -> dict[str, str]:
    """host -> when we first saw it ('baseline' for everything in the very first run)."""
    out = {}
    for host in current:
        if previous is None:
            out[host] = "baseline"
        else:
            out[host] = previous.get(host, now_iso)
    return out


def _recent(first_seen: str, now: datetime) -> bool:
    if first_seen == "baseline":
        return False
    try:
        return (now - datetime.fromisoformat(first_seen)).days < NEW_HOST_WINDOW_DAYS
    except ValueError:
        return False


def _check_malware_indicators(base_url: str, html: str, prev_baseline: dict | None, findings: list[Finding]) -> dict:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    site_reg = _registrable(_host(base_url))
    soup = BeautifulSoup(html, "lxml")

    def external(url: str) -> str | None:
        full = urljoin(base_url, url)
        host = _host(full)
        return host if host and _registrable(host) != site_reg else None

    script_hosts = {h for s in soup.find_all("script", src=True) if (h := external(s["src"]))}
    iframe_hosts = {h for f in soup.find_all("iframe", src=True) if (h := external(f["src"]))}

    prev_scripts = prev_baseline.get("script_hosts") if prev_baseline else None
    prev_iframes = prev_baseline.get("iframe_hosts") if prev_baseline else None
    scripts_map = _first_seen_map(script_hosts, prev_scripts, now_iso)
    iframes_map = _first_seen_map(iframe_hosts, prev_iframes, now_iso)
    for host, seen in scripts_map.items():
        if _recent(seen, now):
            findings.append(Finding("malware", "medium", f"New external script host: {host}",
                                    f"First seen {seen[:10]}; not present in earlier scans. Verify it's expected."))
    for host, seen in iframes_map.items():
        if _recent(seen, now):
            findings.append(Finding("malware", "medium", f"New external iframe host: {host}",
                                    f"First seen {seen[:10]}; not present in earlier scans."))

    # Hidden iframes to other domains.
    for f in soup.find_all("iframe", src=True):
        host = external(f["src"])
        # Tag Manager / pixel snippets legitimately ship 0x0 iframes (usually inside <noscript>).
        if not host or any(host.endswith(t) for t in _TRACKER_HOSTS) or f.find_parent("noscript"):
            continue
        style = f.get("style", "")
        w, hgt = str(f.get("width", "")).strip(), str(f.get("height", "")).strip()
        if _HIDDEN_STYLE.search(style) or w in ("0", "1") or hgt in ("0", "1"):
            findings.append(Finding("malware", "high", f"Hidden iframe pointing to {host}",
                                    "Zero-size/hidden iframes to other domains are a common drive-by-malware technique."))

    # Hidden blocks stuffed with external links (SEO-spam injection).
    for el in soup.find_all(style=_HIDDEN_STYLE):
        hosts = {h for a in el.find_all("a", href=True) if (h := external(a["href"])) and not any(c in h for c in _COMMON_HOSTS)}
        if len(hosts) >= 3:
            findings.append(Finding("malware", "high", "Hidden block of external links (possible SEO spam)",
                                    ", ".join(sorted(hosts)[:5])))
            break

    # Spam keywords in links leaving the site.
    spam: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        host = external(a["href"])
        blob = (a["href"] + " " + a.get_text(" ")).lower()
        if host and not any(c in host for c in _COMMON_HOSTS):
            for w in _SPAM_WORDS:
                if w in blob:
                    spam.setdefault(host, w)
    if spam:
        findings.append(Finding("malware", "medium", "Links to spam-type content",
                                "Outbound links: " + ", ".join(f"{h} ({w})" for h, w in list(spam.items())[:5])))

    inline = "\n".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))
    for pattern, label in _OBFUSCATION:
        if pattern.search(inline):
            findings.append(Finding("malware", "medium", "Obfuscated inline JavaScript", label))
            break
    if _MINERS.search(html):
        findings.append(Finding("malware", "high", "Cryptocurrency-miner script reference",
                                "Page source references a known in-browser miner."))

    meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if meta:
        m = re.search(r"url\s*=\s*['\"]?([^'\";]+)", meta.get("content", ""), re.I)
        host = external(m.group(1)) if m else None
        if host:
            findings.append(Finding("malware", "high", f"Meta-refresh redirect to {host}", "Page redirects visitors to another domain."))
    for m in re.finditer(r"""(?:window\.)?location(?:\.href)?\s*=\s*["'](https?://[^"']+)|location\.replace\(\s*["'](https?://[^"']+)""", inline):
        host = external(m.group(1) or m.group(2))
        if host and not any(c in host for c in _COMMON_HOSTS):
            findings.append(Finding("malware", "medium", f"JavaScript redirect to {host}", "Inline script sends visitors to another domain."))
            break

    return {"script_hosts": scripts_map, "iframe_hosts": iframes_map}


# --- vulnerability surface --------------------------------------------------

async def _wp_latest(ctx: Context) -> tuple[int, ...] | None:
    if ctx.wp_latest is False:
        r = await _fetch(ctx, "https://api.wordpress.org/core/version-check/1.7/", max_bytes=32768)
        ctx.wp_latest = None
        if r and r[0] == 200:
            try:
                ctx.wp_latest = _ver(json.loads(_text(r[2]))["offers"][0]["current"])
            except (ValueError, KeyError, IndexError):
                pass
    return ctx.wp_latest  # type: ignore[return-value]


async def _plugin_latest(ctx: Context, slug: str) -> tuple[int, ...] | None:
    if slug not in ctx.plugin_latest:
        r = await _fetch(ctx, f"https://api.wordpress.org/plugins/info/1.0/{slug}.json", max_bytes=131072)
        latest = None
        if r and r[0] == 200:
            try:
                latest = _ver(json.loads(_text(r[2]))["version"])
            except (ValueError, KeyError, TypeError):
                pass
        ctx.plugin_latest[slug] = latest
    return ctx.plugin_latest[slug]


async def _check_vulnerabilities(ctx, origin: str, html: str, findings: list[Finding]) -> None:
    # Outdated jQuery (< 3.5.0 has known XSS CVEs).
    for m in re.finditer(r"jquery[-./](\d+\.\d+(?:\.\d+)?)(?:\.min)?(?:\.js|/)", html, re.I):
        if _ver(m.group(1)) < (3, 5, 0):
            findings.append(Finding("vulnerability", "medium", f"Outdated jQuery {m.group(1)}",
                                    "jQuery before 3.5.0 has known XSS vulnerabilities (CVE-2020-11022/11023)."))
            break

    is_wp = "/wp-content/" in html or "/wp-includes/" in html or re.search(r"generator[^>]+WordPress", html, re.I)
    if not is_wp:
        return

    gen = re.search(r"""<meta[^>]+name=["']generator["'][^>]+content=["']WordPress\s+([\d.]+)""", html, re.I)
    installed = _ver(gen.group(1)) if gen else None
    if installed:
        latest = await _wp_latest(ctx)
        if latest and installed[:2] < latest[:2]:
            # WordPress branches run x.0..x.9 then roll to the next major, so 6.9 -> 7.0 is one release.
            behind = (latest[0] - installed[0]) * 10 + (latest[1] - installed[1])
            sev = "high" if behind >= 4 else "medium"
            findings.append(Finding("vulnerability", sev, f"Outdated WordPress {gen.group(1)}",
                                    f"Latest is {'.'.join(map(str, latest))}. Old core versions accumulate public exploits."))
        else:
            findings.append(Finding("vulnerability", "low", "WordPress version disclosed in page source",
                                    f"Generator tag reveals WordPress {gen.group(1)}."))

    # Plugins: slug -> most common ?ver= seen on its assets.
    vers: dict[str, Counter] = {}
    for slug, ver in re.findall(r"/wp-content/plugins/([\w-]+)/[^\"'\s>]*?\?ver=(\d{1,2}\.\d+(?:\.\d+)*)", html):
        vers.setdefault(slug, Counter())[ver] += 1
    outdated = []
    for slug, counter in list(vers.items())[:MAX_PLUGINS_CHECKED]:
        ver = counter.most_common(1)[0][0]
        if _ver(ver) in ((1, 0), (1, 0, 0)):
            continue  # commonly a hard-coded asset version, not the plugin's real version
        latest = await _plugin_latest(ctx, slug)
        if latest and _ver(ver)[:2] < latest[:2]:
            outdated.append(f"{slug} {ver} (latest {'.'.join(map(str, latest))})")
    if outdated:
        findings.append(Finding("vulnerability", "medium", f"{len(outdated)} WordPress plugin(s) appear outdated",
                                "; ".join(outdated[:6]) + ". Version is read from asset URLs, so verify in wp-admin."))

    # Directory listing, user enumeration, xmlrpc.
    r = await _fetch(ctx, origin + "/wp-content/uploads/", max_bytes=8192)
    if r and r[0] == 200 and "Index of /" in _text(r[2]):
        findings.append(Finding("vulnerability", "medium", "Directory listing enabled on /wp-content/uploads/",
                                "Anyone can browse every uploaded file."))
    r = await _fetch(ctx, origin + "/wp-json/wp/v2/users", max_bytes=32768)
    if r and r[0] == 200 and not _is_html(r[1]):
        try:
            users = json.loads(_text(r[2]))
            if isinstance(users, list) and users and "slug" in users[0]:
                findings.append(Finding("vulnerability", "low", "WordPress usernames exposed via REST API",
                                        "Login names: " + ", ".join(u["slug"] for u in users[:5]) + " (helps brute-force attacks)."))
        except ValueError:
            pass
    r = await _fetch(ctx, origin + "/xmlrpc.php", max_bytes=2048)
    if r and r[0] in (200, 405) and "XML-RPC server accepts POST requests only" in _text(r[2]):
        findings.append(Finding("vulnerability", "low", "xmlrpc.php is enabled",
                                "Commonly abused for brute-force and DDoS amplification; disable if unused."))


# --- domain expiry + reputation --------------------------------------------

async def _check_domain(ctx, host: str, findings: list[Finding]) -> None:
    domain = _registrable(host)
    if domain not in ctx.rdap:
        days = None
        r = await _fetch(ctx, f"https://rdap.org/domain/{domain}", max_bytes=262144)
        if r and r[0] == 200:
            try:
                for ev in json.loads(_text(r[2])).get("events", []):
                    if ev.get("eventAction") == "expiration":
                        exp = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                        days = (exp - datetime.now(timezone.utc)).days
            except (ValueError, KeyError, TypeError):
                pass
        ctx.rdap[domain] = days
    days = ctx.rdap[domain]
    if days is not None and days < DOMAIN_WARN_DAYS:
        sev = "high" if days < 14 else "medium"
        msg = "Domain registration has expired" if days < 0 else f"Domain {domain} expires in {days} day(s)"
        findings.append(Finding("domain", sev, msg, "Renew at the registrar to avoid losing the site."))


async def _check_safe_browsing(ctx, url: str, findings: list[Finding]) -> None:
    if not ctx.safe_browsing_key:
        return
    payload = {
        "client": {"clientId": "website-monitor", "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"],
            "platformTypes": ["ANY_PLATFORM"], "threatEntryTypes": ["URL"], "threatEntries": [{"url": url}],
        },
    }
    r = await _fetch(ctx, f"https://safebrowsing.googleapis.com/v4/threatMatches:find?key={ctx.safe_browsing_key}",
                     method="POST", json=payload, max_bytes=65536)
    if r and r[0] == 200:
        try:
            matches = json.loads(_text(r[2])).get("matches", [])
        except ValueError:
            return
        if matches:
            kinds = ", ".join(sorted({m.get("threatType", "?") for m in matches}))
            findings.append(Finding("reputation", "high", "Google Safe Browsing flags this site", f"Threat type(s): {kinds}"))


# --- entry point -------------------------------------------------------------

async def run_security_checks(ctx: Context, url: str, prev_baseline_json: str | None) -> SecurityResult:
    page = None
    for attempt in range(3):  # transient timeouts / rate limits are common when scanning many sites at once
        page = await _fetch(ctx, url, max_bytes=1_500_000, timeout=PAGE_TIMEOUT)
        if page and page[0] < 400:
            break
        await asyncio.sleep(3 * (attempt + 1))
    if not page or page[0] >= 400:
        return SecurityResult(reachable=False)  # availability check already reports this
    _, headers, body, final_url = page
    html = _text(body)
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        prev = json.loads(prev_baseline_json) if prev_baseline_json else None
    except ValueError:
        prev = None

    findings: list[Finding] = []
    await asyncio.gather(
        _check_headers(ctx, origin, headers, html, findings),
        _check_exposed_files(ctx, origin, findings),
        _check_vulnerabilities(ctx, origin, html, findings),
        _check_domain(ctx, parsed.hostname or "", findings),
        _check_safe_browsing(ctx, final_url, findings),
    )
    baseline = _check_malware_indicators(final_url, html, prev, findings)

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f.severity], f.category, f.title))
    return SecurityResult(reachable=True, findings=findings, baseline=baseline)


def new_context(session: aiohttp.ClientSession) -> Context:
    return Context(session, os.environ.get("SAFE_BROWSING_API_KEY") or None)


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        # gzip/deflate only: the installed aiohttp can't decode brotli responses.
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        connector=aiohttp.TCPConnector(limit=20, limit_per_host=6),
    )
