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

from content_curator import transcripts
from content_curator.apple import Apple
from content_curator.collectors import collector_for
from content_curator.config import Settings, Source, load_interests
from content_curator.db import Run, now
from content_curator.normalize import content_hash, normalize_url, word_count

log = logging.getLogger(__name__)

TRIAL_MAX_ENTRIES = 5


def http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        transport=httpx.HTTPTransport(retries=2),
    )


def sync_sources(conn: sqlite3.Connection, sources: list[Source]) -> None:
    """Make the sources table match sources.yaml. Sources removed from the file are disabled, not deleted.

    Trial sources found by discovery are not in the file and are left alone; adding one to the file
    takes it over as an ordinary approved source.
    """
    conn.execute("BEGIN")
    for s in sources:
        conn.execute(
            """
            INSERT INTO sources(id, name, url, type, topics, enabled, approved, discovery, priority,
                                fetch_interval_hours, apple_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, type=excluded.type, topics=excluded.topics,
                enabled=excluded.enabled, approved=excluded.approved, discovery=excluded.discovery,
                priority=excluded.priority, fetch_interval_hours=excluded.fetch_interval_hours,
                apple_id=COALESCE(excluded.apple_id, sources.apple_id)
            """,
            (s.id, s.name, s.url, s.type, json.dumps(s.topics), int(s.enabled), int(s.approved),
             s.discovery, s.priority, s.fetch_interval_hours, s.apple_id),
        )
    ids = [s.id for s in sources]
    conn.execute(
        f"UPDATE sources SET enabled=0 WHERE discovery != 'trial' AND id NOT IN ({','.join('?' * len(ids))})", ids
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
    rows = conn.execute(
        "SELECT * FROM sources WHERE enabled=1 AND (approved=1 OR discovery='trial')"
    ).fetchall()
    with http_client(settings) as client:
        for row in rows:
            if not _due(row, force):
                continue
            source = by_id.get(row["id"]) or _source_from_row(row)
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
            if row["discovery"] == "trial":
                # A trial show contributes only its newest episodes, so one feed that republishes its
                # back catalogue with fresh dates cannot flood the night's scoring.
                result.entries = result.entries[:TRIAL_MAX_ENTRIES]
            new = 0
            conn.execute("BEGIN")
            for entry in result.entries:
                stats["entries"] += 1
                norm = entry.key or normalize_url(entry.url)
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
                                      summary, content_text, raw, media, duration_s, audio_url, transcript_url,
                                      transcript_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source.id, entry.guid, entry.url, norm, entry.title, entry.author, entry.published_at, now(),
                     entry.summary, entry.content_text, json.dumps(entry.raw, default=str), entry.media,
                     entry.duration_s, entry.audio_url, entry.transcript_url, entry.transcript_type),
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
            if source.type == "podcast" and new:
                linked = link_apple(conn, client, source, row["apple_id"], result.title)
                stats["apple_links"] = stats.get("apple_links", 0) + linked
        extract_stats = extract_pending(conn, settings, client)
    stats.update(extract_stats)
    stats["duration_s"] = round(time.monotonic() - started, 1)
    run.finish()
    return stats


def _source_from_row(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"], name=row["name"], url=row["url"], type=row["type"], topics=json.loads(row["topics"]),
        enabled=bool(row["enabled"]), approved=bool(row["approved"]), discovery=row["discovery"],
        priority=row["priority"], fetch_interval_hours=row["fetch_interval_hours"], apple_id=row["apple_id"],
    )


_apple: Apple | None = None


def link_apple(conn: sqlite3.Connection, client: httpx.Client, source: Source, apple_id: int | None,
               feed_title: str | None) -> int:
    """Give a podcast's episodes their Apple Podcasts link, matched by GUID. Returns how many were linked."""
    global _apple
    _apple = _apple if _apple and _apple.client is client else Apple(client)
    try:
        show = source.apple_id or apple_id
        if show is None:
            show = _apple.find_show(source.name, source.url)
            if show is None and feed_title and feed_title != source.name:
                show = _apple.find_show(feed_title, source.url)
            if show is None:
                log.info("not on apple podcasts", extra={"source": source.id})
                return 0
            conn.execute("UPDATE sources SET apple_id=? WHERE id=?", (show, source.id))
        links = _apple.episode_links(show)
    except httpx.HTTPError as e:
        log.warning("apple lookup failed", extra={"source": source.id, "error": str(e)})
        return 0
    linked = 0
    for guid, url in links.items():
        linked += conn.execute(
            "UPDATE items SET listen_url=? WHERE source_id=? AND guid=? AND listen_url IS NULL",
            (url, source.id, guid),
        ).rowcount
    return linked


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
    """Get the full text for new items (all pending, or only item_ids), then mark exact duplicates.

    Articles: the feed's text when long enough, else the page. Podcasts: show notes plus the published
    transcript, never the episode page. Items already older than the freshness window are not fetched.
    """
    interests = load_interests(settings.config_dir)
    min_words = interests.scoring.full_text_min_words
    sql = f"""SELECT id, url, content_text, media, transcript_url, transcript_type,
                     datetime(COALESCE(published_at, fetched_at)) <
                         datetime('now', '-{int(interests.selection.max_age_days)} days') AS stale
              FROM items WHERE extraction='pending'"""
    if item_ids is not None:
        sql += f" AND id IN ({','.join('?' * len(item_ids))})"
    rows = conn.execute(sql, item_ids or []).fetchall()
    stats = {"extracted_from_feed": 0, "extracted_from_page": 0, "extraction_failed": 0, "transcripts": 0,
             "stale": 0, "duplicates": 0}
    to_fetch, episodes = [], []
    for row in rows:
        if row["stale"] and item_ids is None:
            _store_text(conn, row["id"], row["content_text"], None, "stale")
            stats["stale"] += 1
        elif row["media"] == "podcast":
            episodes.append(row)
        elif word_count(row["content_text"]) >= min_words:
            _store_text(conn, row["id"], row["content_text"], None, "feed")
            stats["extracted_from_feed"] += 1
        else:
            to_fetch.append(row)

    for row in episodes:
        transcript = None
        if row["transcript_url"]:
            transcript = transcripts.fetch(client, row["transcript_url"], row["transcript_type"])
        if transcript:
            stats["transcripts"] += 1
            notes = row["content_text"] or ""
            _store_text(conn, row["id"], f"{notes}\n\nTranscript:\n{transcript}".strip(), None, "transcript")
        else:
            _store_text(conn, row["id"], row["content_text"], None, "notes")

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
