"""Page Analysis + Structure Monitoring: parses fetched HTML into a
snapshot (title/meta/H1, content size, DOM element count, nav links, and
hashes used later for change detection)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from bs4 import BeautifulSoup

BLANK_PAGE_THRESHOLD = 50  # visible-text chars


@dataclass
class PageSnapshot:
    title: str | None
    meta_description: str | None
    h1: str | None
    content_length: int
    dom_element_count: int
    nav_link_count: int
    structure_hash: str
    content_hash: str
    is_blank: bool


def analyze_page(html: str) -> PageSnapshot:
    soup = BeautifulSoup(html or "", "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    meta_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_tag.get("content", "").strip() if meta_tag else None

    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(strip=True) if h1_tag else None

    visible_text = soup.get_text(separator=" ", strip=True)
    content_length = len(visible_text)

    all_tags = soup.find_all(True)
    dom_element_count = len(all_tags)

    nav = soup.find("nav")
    nav_links = nav.find_all("a") if nav else soup.find_all("a")
    nav_link_count = len(nav_links)

    # Structural fingerprint: sorted tag-name:count pairs. Catches layout
    # changes (sections added/removed) while ignoring text-only edits.
    tag_counts: dict[str, int] = {}
    for tag in all_tags:
        tag_counts[tag.name] = tag_counts.get(tag.name, 0) + 1
    structure_signature = ",".join(f"{name}:{count}" for name, count in sorted(tag_counts.items()))
    structure_hash = hashlib.sha256(structure_signature.encode("utf-8")).hexdigest()

    content_hash = hashlib.sha256(visible_text.encode("utf-8")).hexdigest()

    return PageSnapshot(
        title=title,
        meta_description=meta_description,
        h1=h1,
        content_length=content_length,
        dom_element_count=dom_element_count,
        nav_link_count=nav_link_count,
        structure_hash=structure_hash,
        content_hash=content_hash,
        is_blank=content_length < BLANK_PAGE_THRESHOLD,
    )
