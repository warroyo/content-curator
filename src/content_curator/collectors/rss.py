"""RSS and Atom collector."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

import feedparser
import httpx
import lxml.etree
import lxml.html

from content_curator.collectors.base import CollectResult, Entry
from content_curator.config import Source

MAX_ENTRIES = 100


def _date(entry: dict) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), UTC).isoformat(timespec="seconds")
    return None


def html_to_text(html: str | None) -> str | None:
    """Plain text of an HTML fragment from a feed. trafilatura wants whole pages, so use lxml directly."""
    if not html or not html.strip():
        return None
    try:
        root = lxml.html.fragment_fromstring(html, create_parent="div")
    except (lxml.etree.ParserError, ValueError):
        return html.strip() or None
    for bad in root.xpath("//script|//style"):
        bad.drop_tree()
    text = " ".join(" ".join(root.itertext()).split())
    return text or None


class RSSCollector:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def collect(self, source: Source, etag: str | None, last_modified: str | None) -> CollectResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resp = self.client.get(source.url, headers=headers)
        if resp.status_code == 304:
            return CollectResult(entries=[], etag=etag, last_modified=last_modified, not_modified=True)
        resp.raise_for_status()

        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"unparseable feed: {feed.get('bozo_exception')}")

        entries = []
        for e in feed.entries[:MAX_ENTRIES]:
            link = e.get("link")
            if not link:
                continue
            content = None
            if e.get("content"):
                content = html_to_text(max((c.get("value", "") for c in e.content), key=len))
            entries.append(
                Entry(
                    url=link,
                    title=(e.get("title") or link).strip(),
                    guid=e.get("id") or None,
                    author=e.get("author"),
                    published_at=_date(e),
                    summary=html_to_text(e.get("summary")),
                    content_text=content,
                    raw={k: e.get(k) for k in ("id", "link", "title", "author", "published", "updated", "tags")},
                )
            )
        return CollectResult(
            entries=entries,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )
