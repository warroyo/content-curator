"""Ingestion: sync the source registry, collect entries, extract text, and drop exact duplicates."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
import trafilatura

from content_curator.collectors import collector_for
from content_curator.config import Settings, Source, load_interests
from content_curator.db import Run, now
from content_curator.normalize import content_hash, normalize_url, word_count

log = logging.getLogger(__name__)


def http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        transport=httpx.HTTPTransport(retries=2),
    )


def sync_sources(conn: sqlite3.Connection, sources: list[Source]) -> None:
    """Make the sources table match sources.yaml. Sources removed from the file are disabled, not deleted."""
    conn.execute("BEGIN")
    for s in sources:
        conn.execute(
            """
            INSERT INTO sources(id, name, url, type, topics, enabled, approved, discovery, priority, fetch_interval_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, type=excluded.type, topics=excluded.topics,
                enabled=excluded.enabled, approved=excluded.approved, discovery=excluded.discovery,
                priority=excluded.priority, fetch_interval_hours=excluded.fetch_interval_hours
            """,
            (s.id, s.name, s.url, s.type, json.dumps(s.topics), int(s.enabled), int(s.approved),
             s.discovery, s.priority, s.fetch_interval_hours),
        )
    ids = [s.id for s in sources]
    conn.execute(
        f"UPDATE sources SET enabled=0 WHERE id NOT IN ({','.join('?' * len(ids))})", ids
    )
    conn.execute("COMMIT")


def _due(row: sqlite3.Row, force: bool) -> bool:
    if force or not row["last_fetch_at"]:
        return True
    last = datetime.fromisoformat(row["last_fetch_at"])
    # A little slack so a 4-hour schedule does not skip a source fetched 3h59m ago.
    return datetime.now(UTC) - last >= timedelta(hours=row["fetch_interval_hours"]) - timedelta(minutes=15)


def collect(conn: sqlite3.Connection, settings: Settings, sources: list[Source], force: bool = False) -> dict:
    sync_sources(conn, sources)
    by_id = {s.id: s for s in sources}
    run = Run(conn, "collect")
    stats = run.stats
    stats.update(sources=0, not_modified=0, fetch_failures=0, entries=0, new_items=0, known_items=0)
    started = time.monotonic()
    rows = conn.execute("SELECT * FROM sources WHERE enabled=1 AND approved=1").fetchall()
    with http_client(settings) as client:
        for row in rows:
            if not _due(row, force):
                continue
            source = by_id[row["id"]]
            stats["sources"] += 1
            try:
                result = collector_for(source.type)(client).collect(source, row["etag"], row["last_modified"])
            except Exception as e:  # one bad source must not stop the run
                stats["fetch_failures"] += 1
                log.warning("fetch failed", extra={"source": source.id, "error": str(e)})
                conn.execute(
                    "UPDATE sources SET last_fetch_at=?, consecutive_failures=consecutive_failures+1, last_error=? WHERE id=?",
                    (now(), str(e)[:500], source.id),
                )
                continue
            if result.not_modified:
                stats["not_modified"] += 1
            new = 0
            conn.execute("BEGIN")
            for entry in result.entries:
                stats["entries"] += 1
                norm = normalize_url(entry.url)
                exists = conn.execute(
                    "SELECT 1 FROM items WHERE normalized_url=? OR (source_id=? AND guid=?)",
                    (norm, source.id, entry.guid),
                ).fetchone()
                if exists:
                    stats["known_items"] += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO items(source_id, guid, url, normalized_url, title, author, published_at, fetched_at,
                                      summary, content_text, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source.id, entry.guid, entry.url, norm, entry.title, entry.author, entry.published_at, now(),
                     entry.summary, entry.content_text, json.dumps(entry.raw, default=str)),
                )
                new += 1
            conn.execute(
                """UPDATE sources SET etag=?, last_modified=?, last_fetch_at=?, last_success_at=?,
                   consecutive_failures=0, last_error=NULL, items_seen=items_seen+? WHERE id=?""",
                (result.etag, result.last_modified, now(), now(), new, source.id),
            )
            conn.execute("COMMIT")
            stats["new_items"] += new
            log.info("collected", extra={"source": source.id, "entries": len(result.entries), "new": new})
        extract_stats = extract_pending(conn, settings, client)
    stats.update(extract_stats)
    stats["duration_s"] = round(time.monotonic() - started, 1)
    run.finish()
    return stats


class Robots:
    """robots.txt checks, cached per host. An unreachable robots.txt allows fetching."""

    def __init__(self, client: httpx.Client, user_agent: str):
        self.client = client
        self.user_agent = user_agent
        self.cache: dict[str, robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.cache:
            rp = robotparser.RobotFileParser()
            try:
                resp = self.client.get(f"{origin}/robots.txt", timeout=10)
                if resp.status_code in (401, 403):
                    rp.disallow_all = True
                elif resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None
            except httpx.HTTPError:
                rp = None
            self.cache[origin] = rp
        rp = self.cache[origin]
        return rp is None or rp.can_fetch(self.user_agent, url)


def _fetch_article(client: httpx.Client, robots: Robots, url: str) -> tuple[str, str | None, str | None]:
    """Return (status, text, canonical_url)."""
    if not robots.allowed(url):
        return "robots", None, None
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.info("article fetch failed", extra={"url": url, "error": str(e)})
        return "failed", None, None
    if "html" not in resp.headers.get("content-type", "html"):
        return "not_html", None, None
    doc = trafilatura.bare_extraction(resp.text, url=str(resp.url), with_metadata=True, include_comments=False)
    if doc is None or not doc.text:
        return "empty", None, str(resp.url)
    return "ok", doc.text, doc.url or str(resp.url)


def extract_pending(
    conn: sqlite3.Connection, settings: Settings, client: httpx.Client, item_ids: list[int] | None = None
) -> dict:
    """Get the full text for new items (all pending, or only item_ids), then mark exact duplicates."""
    min_words = load_interests(settings.config_dir).scoring.full_text_min_words
    sql = "SELECT id, url, content_text FROM items WHERE extraction='pending'"
    if item_ids is not None:
        sql += f" AND id IN ({','.join('?' * len(item_ids))})"
    rows = conn.execute(sql, item_ids or []).fetchall()
    stats = {"extracted_from_feed": 0, "extracted_from_page": 0, "extraction_failed": 0, "duplicates": 0}
    to_fetch = []
    for row in rows:
        if word_count(row["content_text"]) >= min_words:
            _store_text(conn, row["id"], row["content_text"], None, "feed")
            stats["extracted_from_feed"] += 1
        else:
            to_fetch.append(row)

    robots = Robots(client, settings.user_agent)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(lambda r: _fetch_article(client, robots, r["url"]), to_fetch)
        for row, (status, text, canonical) in zip(to_fetch, results):
            if status == "ok":
                stats["extracted_from_page"] += 1
                _store_text(conn, row["id"], text, canonical, "page")
            else:
                # Keep whatever the feed gave; scoring marks it low confidence.
                stats["extraction_failed"] += 1
                _store_text(conn, row["id"], row["content_text"], canonical, status)

    for row in rows:
        if mark_duplicate(conn, row["id"]):
            stats["duplicates"] += 1
    return stats


def _store_text(conn: sqlite3.Connection, item_id: int, text: str | None, canonical: str | None, how: str) -> None:
    conn.execute(
        "UPDATE items SET content_text=?, word_count=?, content_hash=?, canonical_url=?, extraction=? WHERE id=?",
        (text, word_count(text), content_hash(text), normalize_url(canonical) if canonical else None, how, item_id),
    )


def mark_duplicate(conn: sqlite3.Connection, item_id: int) -> bool:
    """Point an item at the earliest item with the same canonical URL or content hash."""
    item = conn.execute(
        "SELECT id, normalized_url, canonical_url, content_hash FROM items WHERE id=?", (item_id,)
    ).fetchone()
    match = conn.execute(
        """
        SELECT id FROM items
        WHERE id < ? AND duplicate_of IS NULL AND (
            (? IS NOT NULL AND (normalized_url = ? OR canonical_url = ?))
            OR canonical_url = ?
            OR (? IS NOT NULL AND content_hash = ?)
        )
        ORDER BY id LIMIT 1
        """,
        (item["id"], item["canonical_url"], item["canonical_url"], item["canonical_url"],
         item["normalized_url"], item["content_hash"], item["content_hash"]),
    ).fetchone()
    if match:
        conn.execute("UPDATE items SET duplicate_of=? WHERE id=?", (match["id"], item_id))
        return True
    return False


def ingest_url(conn: sqlite3.Connection, settings: Settings, url: str, title: str | None = None) -> int | None:
    """Add a manually submitted URL as an item of the built-in 'manual' source."""
    conn.execute(
        """INSERT OR IGNORE INTO sources(id, name, url, type, enabled, approved, discovery)
           VALUES ('manual', 'Manual submissions', '', 'manual', 0, 1, 'manual')"""
    )
    norm = normalize_url(url)
    if conn.execute("SELECT 1 FROM items WHERE normalized_url=?", (norm,)).fetchone():
        return None
    item_id = conn.execute(
        """INSERT INTO items(source_id, url, normalized_url, title, fetched_at, published_at)
           VALUES ('manual', ?, ?, ?, ?, ?)""",
        (url, norm, title or url, now(), now()),
    ).lastrowid
    with http_client(settings) as client:
        extract_pending(conn, settings, client, item_ids=[item_id])
    return item_id
