"""Single admin login + sessions for the live dashboard (serve_dashboard.py).

No self-service signup: there is one account, created from
DASHBOARD_ADMIN_EMAIL / DASHBOARD_ADMIN_PASSWORD by ensure_admin() on startup.

Standard library only: PBKDF2-HMAC-SHA256 password hashing with a per-user
salt, random opaque session tokens stored in the same monitoring.db. The
server binds to 127.0.0.1, but this is done properly regardless.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

_PBKDF2_ROUNDS = 210_000
SESSION_TTL = timedelta(days=30)
COOKIE_NAME = "session"

# There is no self-service signup -- the dashboard has a single admin login.
DEFAULT_ADMIN_EMAIL = "admin@kleza.io"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- password hashing -------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# --- users -----------------------------------------------------------------

def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def ensure_admin(conn: sqlite3.Connection) -> tuple[str, str | None]:
    """Make sure the single admin login exists. Email comes from
    DASHBOARD_ADMIN_EMAIL (default admin@kleza.io), password from
    DASHBOARD_ADMIN_PASSWORD. Returns (email, password) where password is set
    only when it was just auto-generated (so the caller can print it once).
    If DASHBOARD_ADMIN_PASSWORD is set and differs from the stored hash, the
    stored password is reset to match -- an env-var 'forgot password'.
    """
    email = (os.environ.get("DASHBOARD_ADMIN_EMAIL") or DEFAULT_ADMIN_EMAIL).strip()
    env_pw = os.environ.get("DASHBOARD_ADMIN_PASSWORD") or ""
    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()

    if row is not None:
        if env_pw and not verify_password(env_pw, row["password_hash"]):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(env_pw), row["id"]))
            conn.commit()
        return email, None

    generated = None
    password = env_pw
    if not password:
        password = generated = secrets.token_urlsafe(9)
    conn.execute(
        "INSERT INTO users (username, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (email, email, hash_password(password), _now().isoformat()),
    )
    conn.commit()
    return email, generated


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> sqlite3.Row | None:
    email = (email or "").strip()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
    ).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return row
    return None


def change_password(conn: sqlite3.Connection, user_id: int, old: str, new: str) -> tuple[bool, str]:
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not verify_password(old, row["password_hash"]):
        return False, "Current password is incorrect."
    if len(new or "") < 8:
        return False, "New password must be at least 8 characters."
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user_id))
    conn.commit()
    return True, "Password changed."


# --- sessions -------------------------------------------------------------

def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now.isoformat(), (now + SESSION_TTL).isoformat()),
    )
    conn.commit()
    return token


def get_session_user(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        """
        SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > ?
        """,
        (token, _now().isoformat()),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def purge_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now().isoformat(),))
    conn.commit()
