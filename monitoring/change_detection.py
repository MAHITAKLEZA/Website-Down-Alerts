"""Compares a fresh PageSnapshot against the last one stored for a site."""

from __future__ import annotations

from dataclasses import dataclass

from .page_analysis import PageSnapshot


@dataclass
class Change:
    change_type: str
    description: str
    severity: str  # "info" | "low" | "medium" | "high"


def detect_changes(previous, new: PageSnapshot) -> list[Change]:
    """`previous` is a sqlite3.Row from page_snapshots, or None on first run."""
    if previous is None:
        return []

    changes: list[Change] = []

    if previous["title"] != new.title:
        changes.append(
            Change("title_changed", f"Title changed: '{previous['title']}' -> '{new.title}'", "medium")
        )

    if previous["structure_hash"] != new.structure_hash:
        changes.append(
            Change(
                "structure_changed",
                "Page HTML structure changed (sections/elements added, removed, or reordered)",
                "medium",
            )
        )
    elif previous["content_hash"] != new.content_hash:
        changes.append(Change("content_changed", "Visible page text changed", "low"))

    prev_nav = previous["nav_link_count"] or 0
    if prev_nav > 0 and abs(new.nav_link_count - prev_nav) / prev_nav > 0.2:
        changes.append(
            Change(
                "nav_links_changed",
                f"Navigation link count changed: {prev_nav} -> {new.nav_link_count}",
                "medium",
            )
        )

    return changes
