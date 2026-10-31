from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from content_curator.config import Source


@dataclass
class Entry:
    url: str
    title: str
    guid: str | None = None
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    # Full text when the source provides it (for example a feed's content:encoded).
    content_text: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class CollectResult:
    entries: list[Entry]
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class Collector(Protocol):
    def __init__(self, client: httpx.Client) -> None: ...

    def collect(self, source: Source, etag: str | None, last_modified: str | None) -> CollectResult: ...
