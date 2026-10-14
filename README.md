# Website Monitor

Detects whether websites are working, using [Crawl4AI](https://github.com/unclecode/crawl4ai)'s
HTTP crawler strategy (no browser install required).

A site is flagged **DOWN** when:
- the connection fails, DNS fails, or the request times out
- the response status is 4xx/5xx
- the page loads with 200 but its body matches a known error/maintenance
  signature (e.g. "503 Service Unavailable", "database error")
- the page loads with 200 but the content is suspiciously empty
- **the site is overloaded** — HTTP 429/502/503/504, an error page saying so
  ("too many requests", "server is busy", …), or a page that loads but takes
  ≥ `SLOW_RESPONSE_MS` (default 15000). These are what a section under more
  traffic than it can handle looks like.

## Setup

Needs **Python 3.10+**. From the project folder:

```bash
python -m venv .venv                 # optional but recommended
.\.venv\Scripts\Activate.ps1         # Windows PowerShell   (macOS/Linux: source .venv/bin/activate)

pip install -r requirements.txt
```

The sites to monitor live in `urls.txt` (already populated; `urls.example.txt`
shows the format).

## Putting it on GitHub

`.gitignore` already excludes the sensitive/generated files — `monitoring.db`
(admin password hash + sessions), the `.json` state files, logs, and
`dashboard.html`. Nothing committed contains a secret (webhook URLs and the
admin password are environment variables only).

**Use a private repo** — `urls.txt` is your client list.

```bash
# install Git first: https://git-scm.com/download/win   (then reopen the terminal)
cd "c:\Mahi\0023\Website Montoring"
git init
git add .
git commit -m "Website monitor"

# create the repo + push (with GitHub CLI: https://cli.github.com/)
gh auth login
gh repo create kleza/website-monitor --private --source=. --push

# ...or make an empty private repo on github.com, then:
git remote add origin https://github.com/<you>/website-monitor.git
git branch -M main
git push -u origin main
```

**Give teammates access:** repo → **Settings → Collaborators → Add people**,
or `gh repo add-collaborator <user> --permission write`. Each teammate then
follows [Setup](#setup) and sets their own `DASHBOARD_ADMIN_PASSWORD` /
`TEAMS_*_WEBHOOK_URL` locally (those never leave their machine).

That installs:

| Package | Used for |
|---|---|
| `crawl4ai` (>=0.4.0) | the HTTP fetch/crawl engine (availability + full link crawl) |
| `aiohttp` | async HTTP — page checks, SSL checks, posting Teams webhooks |
| `beautifulsoup4`, `lxml` | parsing page HTML for the structure/change analysis |
| `cryptography` | reading TLS certificates for the SSL-expiry check |

The dashboard, login/auth, and scheduling use only the Python standard library —
nothing extra to install. **No browser/Playwright download is needed** (the
monitor uses Crawl4AI's plain-HTTP strategy).

> If you see `RequestsDependencyWarning: urllib3 ... doesn't match a supported
> version` — it's harmless (a transitive dep of `crawl4ai`). To silence it:
> `pip install -U requests urllib3 charset-normalizer`.

## Monitoring sections of a site

In `urls.txt`, indent a URL under its site to also monitor that section (a
booking page, a login page, a portal). Each section gets its own check
history, its own dashboard row, and its own alerts — so a broken section
pages you even when the homepage is up:

```
# Eesha Hospitals
https://eeshahospitals.com
  https://eeshahospitals.com/book-appointment   # Appointments
  https://eeshahospitals.com/patient-portal     # Patient Portal
```

## Adding sites

Edit `urls.txt` directly, **or** run the live dashboard (`python
serve_dashboard.py`) → **Websites** tab → paste a URL in the "Add a website"
box. It appends to `urls.txt` and the site shows up on the next check pass.

## Usage

```bash
python website_monitor.py https://example.com https://another.com   # check one or more URLs
python website_monitor.py --file urls.txt                           # check everything in urls.txt
python website_monitor.py --file urls.txt --interval 3600           # re-check every hour, forever
python website_monitor.py --file urls.txt --json                    # machine-readable output
python website_monitor.py --file urls.txt --log-file log.jsonl      # append each check to a log

python run_fast_checks.py            # one full pass (availability + SSL + structure), writes to monitoring.db
python show_status.py                # print the latest status of every site   (--down = only DOWN ones)
python serve_dashboard.py            # live web dashboard at http://127.0.0.1:8765/  (needs login)
python generate_dashboard.py         # (re)build the static dashboard.html snapshot
python daily_report.py               # post the "sites down" card to the Teams group chat
python run_full_crawl.py             # heavy: follow internal links to find broken links
python send_test_teams_alert.py      # verify a TEAMS_*_WEBHOOK_URL is wired up
```

`website_monitor.py` exits with code `1` if any site is down (single-run mode
only) — useful for CI or a scheduled task.

## Continuous monitoring in the cloud (laptop off)

`.github/workflows/monitor.yml` runs `run_fast_checks.py` on GitHub Actions
every 30 minutes (fits the free Actions minutes of a private repo). When a site newly goes down, one card is posted to Teams right
away; a site that stays down isn't re-posted every run (state is kept in the
Actions cache).

One-time setup: repo → **Settings → Secrets and variables → Actions → New
repository secret** → `TEAMS_WEBHOOK_URL` = the Teams Workflows webhook link
(optionally `TEAMS_DM_WEBHOOK_URL` too). Then push the workflow. Watch runs in
the repo's **Actions** tab.

## Automate it (Windows)

```powershell
.\setup_scheduled_tasks.ps1          # register the scheduled tasks
.\setup_scheduled_tasks.ps1 -Remove  # delete them
```

| Task | Runs | Command |
|---|---|---|
| `WebsiteMonitor-FullCrawl` | daily 03:00 | `run_full_crawl.py` — broken-link crawl (`alerts.log` only) |
| `WebsiteMonitor-Security` | daily 03:30 | `run_security_checks.py` — security scan (dashboard + `alerts.log`) |

The old `WebsiteMonitor-DailyReport` task is removed by the setup script —
the down alert and the daily down report both run in the cloud (see above). For fresh data on demand, open the live dashboard (it
re-checks every site on load) or run `python run_fast_checks.py`.

## Live dashboard & login

```bash
python serve_dashboard.py          # then open http://127.0.0.1:8765/
```

The live dashboard requires a login. There is **one admin account** (no
self-service signup), created on startup from:

```bash
setx DASHBOARD_ADMIN_EMAIL "admin@kleza.io"       # default if unset
setx DASHBOARD_ADMIN_PASSWORD "your-password"     # if unset, one is generated and printed on first run
```

- `/login` — email + password. Session is a 30-day `HttpOnly` cookie in
  `monitoring.db`.
- Changing `DASHBOARD_ADMIN_PASSWORD` and restarting resets the password
  (env-var "forgot password").
- The **Settings** tab shows the account, a change-password form, and **Sign
  out**.
- Passwords are PBKDF2-HMAC-SHA256, 210k rounds, per-user salt. The server
  binds to `127.0.0.1` only.

The account + sessions live in `monitoring.db` (see
[monitoring/auth.py](monitoring/auth.py)). The static `dashboard.html` from
`generate_dashboard.py` has no login — it's a snapshot.

## Alert cadence

The scheduled check runs **once a day on weekdays** at 09:30 (inside
`daily_report.py`). The live dashboard also re-checks every site whenever it's
opened, and `website_monitor.py --interval N` polls every N seconds if you run it.

Alert behaviour (console + `alerts.log`):

- a site alerts **as soon as one check returns DOWN** — including "overloaded"
  (429/503/slow). `DOWN_CONFIRM_CHECKS=2` requires two DOWN checks in a row.
- while it stays down the alert repeats every `DOWN_REALERT_SECONDS` (default
  3600; `0` = alert once).
- a recovery notice follows when the site comes back UP.

SSL-expiry, broken-link and structure-change checks still run, but their
alerts go to `alerts.log` only — the dashboard shows outages (DOWN /
overloaded) and nothing else. There is no "warnings" category; a site the
checker can't classify (WAF block, JS-only page) shows as **UNVERIFIED** and
never alerts.

Alerts always go to the console and `alerts.log`. Teams gets **one webhook**:
the "sites down" card is posted to it as soon as a site goes down (only when
the down list gains a site). Set it in the live dashboard's **Settings → Teams
alerts**, or as an env var (the Settings value wins):

```bash
setx TEAMS_WEBHOOK_URL "https://.../workflows/..."        # the one Teams webhook
setx TEAMS_DM_WEBHOOK_URL "https://.../workflows/..."     # optional extra 1:1 copy
python send_test_teams_alert.py                           # verify (posts a test card)
```

`daily_report.py` posts a "sites still down" card to the same webhook **every
day at 09:30 IST** (weekends too) via `.github/workflows/daily-report.yml` —
**only when at least one site is down**. When everything is up, nothing is sent.

**Making the DM webhook** (Teams → Apps → **Workflows** → New):

1. Trigger: *When a Teams webhook request is received*.
2. Action: *Post message in a chat or channel* → **Post in: Chat** →
   **Recipient: the person to alert**.
3. For the message, use *Post card in a chat or channel* and pass the incoming
   request body through, so the same adaptive card renders.
4. Save → **Copy webhook link** → `setx TEAMS_DM_WEBHOOK_URL "…"`.

Teams gets **one combined card**, and only when the list of down sites has
**changed** since the last card — a newly-down site. If the same sites are
still down on the next check, no card is re-sent; a site recovering doesn't
send one either. So an ongoing outage produces exactly one card, not one per
hour.

```
🔴 4 sites down · 1 new
  Ojas Infinity      HTTP 500 (server error)          ← the new one, listed first
  Accounts Sage      SSL certificate has expired
  Ankrotech          HTTP 500 (server error)
  Cinfosys           connection refused / host unreachable
  Checked 2026-09-01 06:10 UTC
```

State is kept in `.digest_state.json` (delete it to force a fresh card next
run). Per-site detail, recoveries, and SSL/link/structure alerts all still go
to the console + `alerts.log`, just not to Teams.

## Security scan (no credentials)

`run_security_checks.py` (scheduled daily 03:30, task `WebsiteMonitor-Security`) runs external checks from
`monitoring/security_checks.py` against every site and stores findings for the dashboard's **Security**
column / "Security scan" panel. High-severity findings also log to `alerts.log` (never the Teams card).

- **Headers/HTTPS:** missing HSTS/CSP/X-Frame-Options/nosniff, version disclosure, mixed content, http not redirecting.
- **Exposed files:** `.env`, `.git`, wp-config backups, DB dumps, backup archives, phpinfo, debug logs (content-verified).
- **Malware indicators:** hidden iframes, hidden external-link blocks, spam links, obfuscated JS, miners, redirects,
  and *new* external script/iframe hosts (flagged for 14 days after first seen; first run is the baseline).
- **Vulnerabilities (surface):** outdated WordPress core / plugins (via wordpress.org APIs), jQuery < 3.5,
  directory listing, user enumeration, xmlrpc.
- **Domain expiry** via RDAP; **Google Safe Browsing** if `SAFE_BROWSING_API_KEY` is set.

These are indicators, not proof; server-side file/malware scanning needs hosting access.
