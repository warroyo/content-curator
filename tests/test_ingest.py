"""Collection and exact deduplication against a mocked network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

from content_curator import ingest
from content_curator.config import Source

LONG = " ".join(f"word{i}" for i in range(400))
RECENT = format_datetime(datetime.now(UTC) - timedelta(hours=6), usegmt=True)

FEED_A = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>A</title>
<item><title>Post one</title><link>https://a.test/one?utm_source=rss</link><guid>a-1</guid>
  <pubDate>{RECENT}</pubDate><content:encoded><![CDATA[<p>{LONG}</p>]]></content:encoded></item>
<item><title>Short post</title><link>https://a.test/two</link><guid>a-2</guid>
  <pubDate>{RECENT}</pubDate><description>Teaser only</description></item>
</channel></rss>"""

# Same story as A's first post under a different URL, plus a link to A's second post.
FEED_B = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>B</title>
<item><title>Post one (syndicated)</title><link>https://b.test/copy-of-one</link><guid>b-1</guid>
  <content:encoded><![CDATA[<div>{LONG}</div>]]></content:encoded></item>
<item><title>Short post</title><link>http://www.a.test/two/</link><guid>b-2</guid></item>
</channel></rss>"""

PAGE_TWO = f"""<html><head><title>Short post</title><link rel="canonical" href="https://a.test/two"></head>
<body><article><h1>Short post</h1><p>{" ".join(f"page{i}" for i in range(300))}</p></article></body></html>"""


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/robots.txt"):
        return httpx.Response(404)
    if url == "https://a.test/feed":
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text=FEED_A, headers={"ETag": '"v1"'})
    if url == "https://b.test/feed":
        return httpx.Response(200, text=FEED_B)
    if url == "https://a.test/two":
        return httpx.Response(200, text=PAGE_TWO, headers={"content-type": "text/html"})
    if url == "https://broken.test/feed":
        return httpx.Response(500)
    return httpx.Response(404)


SOURCES = [
    Source(id="a", name="A", url="https://a.test/feed"),
    Source(id="b", name="B", url="https://b.test/feed"),
    Source(id="broken", name="Broken", url="https://broken.test/feed"),
]


def _mock(monkeypatch):
    monkeypatch.setattr(
        ingest, "http_client",
        lambda s: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )


def test_collect_dedupes_and_extracts(monkeypatch, conn, settings):
    _mock(monkeypatch)
    stats = ingest.collect(conn, settings, SOURCES)

    assert stats["fetch_failures"] == 1
    assert stats["new_items"] == 3  # b-2 normalizes to a's /two and is never inserted
    assert stats["known_items"] == 1
    assert stats["duplicates"] == 1  # b-1 has the same text as a-1

    items = {r["guid"]: r for r in conn.execute("SELECT * FROM items")}
    assert items["a-1"]["extraction"] == "feed"
    assert items["a-2"]["extraction"] == "page"
    assert items["a-2"]["word_count"] >= 250
    assert items["b-1"]["duplicate_of"] == items["a-1"]["id"]

    broken = conn.execute("SELECT * FROM sources WHERE id='broken'").fetchone()
    assert broken["consecutive_failures"] == 1


def test_rerun_is_idempotent_and_uses_etag(monkeypatch, conn, settings):
    _mock(monkeypatch)
    ingest.collect(conn, settings, SOURCES)
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    stats = ingest.collect(conn, settings, SOURCES, force=True)
    assert stats["new_items"] == 0
    assert stats["not_modified"] == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == count


def test_sources_removed_from_yaml_are_disabled(monkeypatch, conn, settings):
    _mock(monkeypatch)
    ingest.collect(conn, settings, SOURCES)
    ingest.sync_sources(conn, SOURCES[:1])
    enabled = {r[0] for r in conn.execute("SELECT id FROM sources WHERE enabled=1")}
    assert enabled == {"a"}


def test_robots_disallow_blocks_page_fetch(monkeypatch, conn, settings):
    def strict(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return handler(request)

    monkeypatch.setattr(
        ingest, "http_client",
        lambda s: httpx.Client(transport=httpx.MockTransport(strict), follow_redirects=True),
    )
    ingest.collect(conn, settings, SOURCES[:1])
    row = conn.execute("SELECT extraction FROM items WHERE guid='a-2'").fetchone()
    assert row["extraction"] == "robots"
