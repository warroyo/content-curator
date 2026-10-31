"""Podcast collector: an RSS feed read for episode metadata, show notes and published transcripts.

No audio is downloaded or transcribed. The episode is scored on its show notes, plus its
transcript when the feed links one with the Podcasting 2.0 `podcast:transcript` tag.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

import feedparser
import httpx
import lxml.etree

from content_curator.collectors.base import CollectResult, Entry
from content_curator.collectors.rss import html_to_text
from content_curator.config import Source

PODCAST_NS = "https://podcastindex.org/namespace/1.0"
# Most recent episodes considered per fetch. Older ones are outside the freshness window anyway.
MAX_ENTRIES = 30
# Transcript formats in order of preference: cheapest to turn into clean text first.
TRANSCRIPT_TYPES = ["text/plain", "text/vtt", "application/x-subrip", "application/srt",
                    "application/json", "text/html"]
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")


def parse_duration(value: str | None) -> int | None:
    """itunes:duration is seconds ("3254") or [[HH:]MM:]SS."""
    if not value:
        return None
    try:
        parts = [int(float(p)) for p in str(value).strip().split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds or None


def transcripts_by_guid(content: bytes) -> dict[str, tuple[str, str]]:
    """Best transcript (url, type) per episode GUID. feedparser keeps only one tag per item."""
    out: dict[str, tuple[str, str]] = {}
    try:
        root = lxml.etree.fromstring(content, parser=lxml.etree.XMLParser(recover=True, huge_tree=True))
    except lxml.etree.XMLSyntaxError:
        return out
    if root is None:
        return out
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        tags = [(t.get("url"), (t.get("type") or "").lower()) for t in item.iter(f"{{{PODCAST_NS}}}transcript")]
        tags = [(u, t) for u, t in tags if u and t in TRANSCRIPT_TYPES]
        if guid and tags:
            out[guid] = min(tags, key=lambda ut: TRANSCRIPT_TYPES.index(ut[1]))
    return out


def _date(entry: dict) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), UTC).isoformat(timespec="seconds")
    return None


class PodcastCollector:
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
        show_link = feed.feed.get("link") or source.url
        transcripts = transcripts_by_guid(resp.content)

        entries = []
        for e in feed.entries[:MAX_ENTRIES]:
            guid = e.get("id") or e.get("link")
            if not guid:
                continue
            audio = next((x.get("href") for x in e.get("enclosures", []) if x.get("href")), None)
            link = e.get("link")
            # Some feeds point the item link at the audio file or the show's home page; neither is
            # the episode, so fall back to the show page for display and key the episode by GUID.
            own_page = bool(link) and link != show_link and not link.lower().split("?")[0].endswith(AUDIO_SUFFIXES)
            notes = None
            if e.get("content"):
                notes = html_to_text(max((c.get("value", "") for c in e.content), key=len))
            notes = notes or html_to_text(e.get("summary"))
            t_url, t_type = transcripts.get(guid, (None, None))
            entries.append(
                Entry(
                    url=link if own_page else show_link,
                    key=None if own_page else f"podcast://{source.id}/{guid}",
                    title=(e.get("title") or guid).strip(),
                    guid=guid,
                    author=e.get("author") or feed.feed.get("title"),
                    published_at=_date(e),
                    summary=notes,
                    content_text=notes,
                    media="podcast",
                    duration_s=parse_duration(e.get("itunes_duration")),
                    audio_url=audio,
                    transcript_url=t_url,
                    transcript_type=t_type,
                    raw={k: e.get(k) for k in ("id", "link", "title", "published", "itunes_duration")},
                )
            )
        return CollectResult(
            entries=entries,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
            title=feed.feed.get("title"),
        )
