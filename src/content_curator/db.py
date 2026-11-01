"""SQLite storage. Schema changes are numbered migrations tracked in PRAGMA user_version."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        type TEXT NOT NULL,
        topics TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1,
        approved INTEGER NOT NULL DEFAULT 1,
        discovery TEXT NOT NULL DEFAULT 'manual',
        priority INTEGER NOT NULL DEFAULT 0,
        fetch_interval_hours REAL NOT NULL DEFAULT 4,
        etag TEXT,
        last_modified TEXT,
        last_fetch_at TEXT,
        last_success_at TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        items_seen INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(id),
        guid TEXT,
        url TEXT NOT NULL,
        normalized_url TEXT NOT NULL UNIQUE,
        canonical_url TEXT,
        title TEXT NOT NULL,
        author TEXT,
        published_at TEXT,
        fetched_at TEXT NOT NULL,
        summary TEXT,
        content_text TEXT,
        content_hash TEXT,
        word_count INTEGER NOT NULL DEFAULT 0,
        extraction TEXT NOT NULL DEFAULT 'pending',
        duplicate_of INTEGER REFERENCES items(id),
        raw TEXT
    );
    CREATE UNIQUE INDEX items_source_guid ON items(source_id, guid) WHERE guid IS NOT NULL;
    CREATE INDEX items_canonical ON items(canonical_url);
    CREATE INDEX items_hash ON items(content_hash);

    CREATE TABLE scores (
        item_id INTEGER PRIMARY KEY REFERENCES items(id),
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        relevance REAL NOT NULL,
        novelty REAL NOT NULL,
        depth REAL NOT NULL,
        source_quality REAL NOT NULL,
        actionability REAL NOT NULL,
        discovery_value REAL NOT NULL,
        outside_interests INTEGER NOT NULL,
        topics TEXT NOT NULL,
        explanation TEXT NOT NULL,
        confidence TEXT NOT NULL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        duration_ms INTEGER,
        scored_at TEXT NOT NULL
    );

    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        stats TEXT NOT NULL DEFAULT '{}',
        error TEXT
    );

    CREATE TABLE selections (
        run_id INTEGER NOT NULL REFERENCES runs(id),
        item_id INTEGER NOT NULL REFERENCES items(id),
        rank INTEGER NOT NULL,
        selected INTEGER NOT NULL,
        bucket TEXT NOT NULL,
        overall REAL NOT NULL,
        reason TEXT NOT NULL,
        dry_run INTEGER NOT NULL,
        PRIMARY KEY (run_id, item_id)
    );
    """,
    # 2: embeddings and same-story grouping
    """
    CREATE TABLE embeddings (
        item_id INTEGER PRIMARY KEY REFERENCES items(id),
        model TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        dim INTEGER NOT NULL,
        vector BLOB NOT NULL,
        created_at TEXT NOT NULL
    );

    ALTER TABLE items ADD COLUMN story_id INTEGER REFERENCES items(id);
    CREATE INDEX items_story ON items(story_id);

    CREATE TABLE story_judgements (
        a INTEGER NOT NULL REFERENCES items(id),
        b INTEGER NOT NULL REFERENCES items(id),
        similarity REAL NOT NULL,
        same INTEGER NOT NULL,
        reason TEXT NOT NULL,
        model TEXT NOT NULL,
        judged_at TEXT NOT NULL,
        PRIMARY KEY (a, b)
    );
    """,
    # 3: podcasts, Apple Podcasts links, discovered (trial) sources
    """
    ALTER TABLE sources ADD COLUMN apple_id INTEGER;
    ALTER TABLE sources ADD COLUMN discovered_at TEXT;
    ALTER TABLE sources ADD COLUMN discovery_query TEXT;

    ALTER TABLE items ADD COLUMN media TEXT NOT NULL DEFAULT 'article';
    ALTER TABLE items ADD COLUMN duration_s INTEGER;
    ALTER TABLE items ADD COLUMN listen_url TEXT;
    ALTER TABLE items ADD COLUMN audio_url TEXT;
    ALTER TABLE items ADD COLUMN transcript_url TEXT;
    ALTER TABLE items ADD COLUMN transcript_type TEXT;
    """,
    # 4: podcast discovery verdicts, so a rejected show is not judged again every week
    """
    CREATE TABLE show_judgements (
        apple_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        fit INTEGER NOT NULL,
        reason TEXT NOT NULL,
        judged_at TEXT NOT NULL
    );
    """,
    # 5: picks published to Raindrop. One row per item; status says whether the curator created the
    # raindrop or found the URL already saved.
    """
    CREATE TABLE published (
        item_id INTEGER PRIMARY KEY REFERENCES items(id),
        raindrop_id INTEGER NOT NULL,
        collection_id INTEGER NOT NULL,
        link TEXT NOT NULL,
        status TEXT NOT NULL,
        run_id INTEGER REFERENCES runs(id),
        published_at TEXT NOT NULL
    );
    CREATE INDEX published_raindrop ON published(raindrop_id);
    """,
]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN")
        try:
            for stmt in script.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={i}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


class Run:
    """Records one job execution in the runs table."""

    def __init__(self, conn: sqlite3.Connection, kind: str):
        self.conn = conn
        self.kind = kind
        self.stats: dict = {}
        self.id = conn.execute(
            "INSERT INTO runs(kind, started_at, status) VALUES (?, ?, 'running')", (kind, now())
        ).lastrowid

    def finish(self, status: str = "ok", error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
            (now(), status, json.dumps(self.stats), error, self.id),
        )
