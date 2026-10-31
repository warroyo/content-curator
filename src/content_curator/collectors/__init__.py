"""Collectors turn a source into feed entries. The pipeline only sees the Collector interface."""

from __future__ import annotations

from content_curator.collectors.base import CollectResult, Collector, Entry
from content_curator.collectors.rss import RSSCollector

COLLECTORS: dict[str, type[Collector]] = {
    "rss": RSSCollector,
}


def collector_for(source_type: str) -> type[Collector]:
    try:
        return COLLECTORS[source_type]
    except KeyError:
        raise ValueError(f"no collector for source type {source_type!r}") from None


__all__ = ["COLLECTORS", "CollectResult", "Collector", "Entry", "collector_for"]
