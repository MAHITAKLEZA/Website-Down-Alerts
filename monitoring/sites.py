"""Loads the monitored site list from urls.txt.

Format (see urls.txt for the live copy):

    # Friendly Name              <- comment directly above a URL names it
    https://example.com          <- a top-level site (flush to the left margin)
      https://example.com/login  # Login   <- an INDENTED URL is a "section" of
      https://example.com/booking          the site above it; the text after
                                           an inline "# ..." names the section

Each section is monitored exactly like a full site (its own check history,
its own alerts, its own dashboard row) -- so a section going down pages you
even when the homepage is fine. Sections are named "<Site> > <Section>".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_URLS_FILE = Path(__file__).resolve().parent.parent / "urls.txt"


@dataclass
class Site:
    name: str
    url: str
    parent: str | None = None  # None for a top-level site; the parent URL for a section


def _split_inline_comment(line: str) -> tuple[str, str | None]:
    """'https://x/login  # Login' -> ('https://x/login', 'Login').

    Only a '#' with whitespace before it starts the label, so a URL fragment
    ('https://x/page#tab') is left intact."""
    match = re.search(r"\s#\s*(.*)$", line)
    if not match:
        return line.strip(), None
    return line[: match.start()].strip(), (match.group(1).strip() or None)


def _section_label(url: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    path = urlparse(url).path.strip("/")
    return path.replace("-", " ").replace("/", " / ").title() if path else url


def load_sites(path: Path = DEFAULT_URLS_FILE, include_sections: bool = True) -> list[Site]:
    sites: list[Site] = []
    pending_name: str | None = None
    last_top_level: Site | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Each comment line overwrites the pending name; only the one
            # immediately before a URL line actually gets used as its name.
            pending_name = stripped.lstrip("#").strip() or None
            continue

        indented = raw_line[:1] in (" ", "\t")
        url, inline_label = _split_inline_comment(stripped)
        if not url:
            continue

        if indented and last_top_level is not None:
            if include_sections:
                label = _section_label(url, inline_label or pending_name)
                sites.append(
                    Site(name=f"{last_top_level.name} > {label}", url=url, parent=last_top_level.url)
                )
        else:
            site = Site(name=pending_name or url, url=url)
            sites.append(site)
            last_top_level = site
        pending_name = None

    return sites


def add_site(url: str, name: str | None = None, path: Path = DEFAULT_URLS_FILE) -> tuple[bool, str]:
    """Append a new top-level site to urls.txt. Returns (added, message).
    Used by the dashboard's "add a site" form (serve_dashboard.py)."""
    url = (url or "").strip()
    if not url:
        return False, "Enter a URL."
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1]  # ignore any user:pass@
    if parsed.scheme not in ("http", "https") or not re.match(
        r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?$", host
    ):
        return False, f"'{url}' is not a valid http(s) URL."

    # Normalise to scheme://host[/path] with no trailing slash or fragment.
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

    existing = {s.url.rstrip("/").lower() for s in load_sites(path)}
    if clean.lower() in existing:
        return False, f"{clean} is already being monitored."

    name = " ".join((name or "").split()) or parsed.netloc.replace("www.", "")
    text = path.read_text(encoding="utf-8")
    sep = "" if text.endswith("\n") else "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{sep}# {name}\n{clean}\n")
    return True, f"Added {name} ({clean}). It will appear after the next check."
