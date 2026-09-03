"""Live dashboard server: every page request runs a real fast-check pass
(availability, SSL, page/structure analysis) against all sites, then serves
the dashboard rendered from those fresh results. Unlike dashboard.html
(a static file someone has to regenerate), every refresh here does real
work and shows genuinely current data.

This only runs the FAST checks on each request (a few seconds for the whole
fleet) -- the full link crawl (run_full_crawl.py) is deliberately excluded
because it can take several minutes per site and would make every page load
unusably slow, and would hammer client servers on every browser refresh.

Access is gated by a single admin login (no self-service signup). The account
is created on startup from DASHBOARD_ADMIN_EMAIL / DASHBOARD_ADMIN_PASSWORD
(ensure_admin); /login signs in (session cookie, 30 days); the Settings tab
shows the account + a change-password form + sign out. See monitoring/auth.py.

The Websites tab has an "add a site" box: type a URL, submit, and it's
appended to urls.txt and picked up on the next pass.

Usage:
    python serve_dashboard.py [port]
    Then open http://127.0.0.1:8765/ in a browser.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auth_pages import render_login_page
from generate_dashboard import fetch_data, render
from monitoring import auth, db, sites
from run_fast_checks import run_pass

DEFAULT_PORT = 8765
SESSION_MAX_AGE = int(auth.SESSION_TTL.total_seconds())

# Serializes check passes so two overlapping browser refreshes don't launch
# duplicate concurrent crawls against the same client sites.
_pass_lock = threading.Lock()


async def _run_live_pass_and_render(user, flash: str | None) -> str:
    conn = db.get_connection()
    try:
        await run_pass(conn)
        data = fetch_data(conn)
        return render(data, live=True, flash=flash, user=user)
    finally:
        conn.close()


def _safe_next(raw: str | None) -> str:
    """Only allow same-site absolute paths as a post-login redirect target."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


class DashboardHandler(BaseHTTPRequestHandler):
    # ---- small helpers ----------------------------------------------------
    def _token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            morsel = SimpleCookie(raw).get(auth.COOKIE_NAME)
            return morsel.value if morsel else None
        except Exception:  # noqa: BLE001 - a malformed cookie is just "no session"
            return None

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, *, set_cookie: str | None = None, clear_cookie: bool = False) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie is not None:
            self.send_header(
                "Set-Cookie",
                f"{auth.COOKIE_NAME}={set_cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_MAX_AGE}",
            )
        if clear_cookie:
            self.send_header("Set-Cookie", f"{auth.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        self.end_headers()

    # ---- GET ------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/login":
            self._send_html(render_login_page(
                error=(query.get("err") or [""])[0],
                info=(query.get("info") or [""])[0],
                next_url=_safe_next((query.get("next") or ["/"])[0]),
            ))
            return

        if parsed.path not in ("/", "/dashboard"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found. Try / or /dashboard.")
            return

        conn = db.get_connection()
        try:
            user = auth.get_session_user(conn, self._token())
        finally:
            conn.close()

        if user is None:
            self._redirect("/login?next=" + urllib.parse.quote(parsed.path))
            return

        flash = (query.get("msg") or [None])[0]
        started = time.perf_counter()
        with _pass_lock:
            print(f"[{user['username']}] checking all sites live...", flush=True)
            html = asyncio.run(_run_live_pass_and_render(user, flash))
        print(f"Done in {time.perf_counter() - started:.1f}s -- serving fresh dashboard.", flush=True)
        self._send_html(html)

    # ---- POST ---------------------------------------------------------
    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        handler = {
            "/login": self._post_login,
            "/logout": self._post_logout,
            "/add-site": self._post_add_site,
            "/change-password": self._post_change_password,
        }.get(path)
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return
        handler(self._form())

    def _post_login(self, form) -> None:
        login = (form.get("login") or [""])[0]
        password = (form.get("password") or [""])[0]
        nxt = _safe_next((form.get("next") or ["/"])[0])
        conn = db.get_connection()
        try:
            auth.purge_expired_sessions(conn)
            user = auth.authenticate(conn, login, password)
            if user is None:
                time.sleep(0.4)  # gentle brake on guessing
                self._redirect(f"/login?err={urllib.parse.quote('Wrong email or password.')}"
                               f"&next={urllib.parse.quote(nxt)}")
                return
            token = auth.create_session(conn, user["id"])
        finally:
            conn.close()
        print(f"[AUTH] {login} signed in", flush=True)
        self._redirect(nxt, set_cookie=token)

    def _post_logout(self, form) -> None:
        conn = db.get_connection()
        try:
            auth.delete_session(conn, self._token())
        finally:
            conn.close()
        self._redirect("/login?info=" + urllib.parse.quote("Signed out."), clear_cookie=True)

    def _require_user(self):
        conn = db.get_connection()
        user = auth.get_session_user(conn, self._token())
        if user is None:
            conn.close()
            self._redirect("/login")
            return None, None
        return conn, user

    def _post_add_site(self, form) -> None:
        conn, user = self._require_user()
        if user is None:
            return
        try:
            added, message = sites.add_site((form.get("url") or [""])[0], (form.get("name") or [""])[0])
        finally:
            conn.close()
        print(f"[ADD-SITE] ({user['username']}) {message}", flush=True)
        self._redirect("/?msg=" + urllib.parse.quote(message) + "#websites")

    def _post_change_password(self, form) -> None:
        conn, user = self._require_user()
        if user is None:
            return
        try:
            _, message = auth.change_password(
                conn, user["id"], (form.get("old") or [""])[0], (form.get("new") or [""])[0]
            )
        finally:
            conn.close()
        self._redirect("/?msg=" + urllib.parse.quote(message))

    def log_message(self, format: str, *args) -> None:
        pass  # we print our own concise status lines above instead


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT

    conn = db.get_connection()
    admin_email, generated_pw = auth.ensure_admin(conn)
    conn.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Live dashboard: http://127.0.0.1:{port}/")
    print(f"Admin login: {admin_email}")
    if generated_pw:
        print(f"Admin password (auto-generated -- set DASHBOARD_ADMIN_PASSWORD to pick your own): {generated_pw}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
