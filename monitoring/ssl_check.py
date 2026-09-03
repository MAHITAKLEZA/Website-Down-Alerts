"""Standalone SSL certificate expiry check (independent of the crawl itself,
since Crawl4AI's HTTP strategy doesn't expose the peer certificate)."""

from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography import x509


def _get_days_remaining_sync(hostname: str, timeout: float = 8.0) -> int | None:
    # verify_mode=CERT_NONE so we can still read the expiry date off an
    # otherwise-invalid cert (expired/self-signed/hostname-mismatched) --
    # those failure modes are already caught as connection errors by the
    # main HTTP check; this is purely for "expiring soon" early warning.
    # NOTE: with CERT_NONE, ssl's own getpeercert() returns {} (Python only
    # parses the cert dict for verified connections), so we pull the raw DER
    # bytes instead and parse them with `cryptography`.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((hostname, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                der_bytes = ssock.getpeercert(binary_form=True)
    except Exception:
        return None

    if not der_bytes:
        return None

    cert = x509.load_der_x509_certificate(der_bytes)
    expires_at = cert.not_valid_after_utc
    return (expires_at - datetime.now(timezone.utc)).days


async def get_ssl_days_remaining(url: str) -> int | None:
    hostname = urlparse(url).hostname
    if not hostname:
        return None
    return await asyncio.to_thread(_get_days_remaining_sync, hostname)
