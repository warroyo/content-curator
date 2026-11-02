"""The source registry beyond sources.yaml: suggested feeds waiting for a check, and follow decisions.

Kept free of feed parsing and HTTP so the long-running service can import it for the API without
loading the heavy modules that jobs use (see jobs.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from content_curator.db import now
from content_curator.normalize import normalize_url

log = logging.getLogger(__name__)

# A show or feed judged this recently is not sent to the chat model again.
REJUDGE_DAYS = 90


def set_follow(conn: sqlite3.Connection, source_id: str, state: str, via: str) -> bool:
    """Follow or unfollow a source. Returns False if there is no such source.

    Following a trial makes it a permanent source; following an unfollowed one turns it back on.
    Unfollowing disables any source, and the decision outlives sources.yaml (see ingest.sync_sources).
    """
    if state not in ("followed", "unfollowed"):
        raise ValueError(f"unknown follow state {state!r}")
    row = conn.execute("SELECT discovery FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None or source_id == "manual":
        return False
    conn.execute(
        """INSERT INTO follows(source_id, state, via, changed_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(source_id) DO UPDATE SET state=excluded.state, via=excluded.via,
                                                changed_at=excluded.changed_at""",
        (source_id, state, via, now()),
    )
    if state == "unfollowed":
        conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (source_id,))
    elif row["discovery"] in ("trial", "followed"):
        conn.execute("UPDATE sources SET discovery = 'followed', approved = 1, enabled = 1 WHERE id = ?",
                     (source_id,))
    else:
        conn.execute("UPDATE sources SET enabled = 1 WHERE id = ?", (source_id,))
    log.info("source follow changed", extra={"source": source_id, "state": state, "via": via})
    return True


class CandidateIn(BaseModel):
    """One suggested source: a feed URL or a page that links to its feed."""

    url: str
    name: str = ""
    topics: list[str] = Field(default_factory=list)
    why: str = ""


def add_candidates(conn: sqlite3.Connection, candidates: list[CandidateIn], submitted_by: str = "") -> dict:
    """Queue suggestions for the next feed check. Known sources and recent verdicts are skipped."""
    stats: dict = {"added": 0, "skipped": []}
    known = {normalize_url(r["url"]) for r in conn.execute("SELECT url FROM sources WHERE url != ''")}
    rejudge_after = (datetime.now(UTC) - timedelta(days=REJUDGE_DAYS)).isoformat(timespec="seconds")
    for c in candidates:
        key = normalize_url(c.url)
        row = conn.execute("SELECT status, checked_at FROM candidates WHERE url = ?", (key,)).fetchone()
        why = None
        if key in known:
            why = "already a source"
        elif row and row["status"] in ("pending", "trial"):
            why = f"already {row['status']}"
        elif row and row["status"] == "rejected" and (row["checked_at"] or "") >= rejudge_after:
            why = "rejected in the last 90 days"
        if why:
            stats["skipped"].append({"url": c.url, "why": why})
            continue
        conn.execute(
            """INSERT INTO candidates(url, submitted_url, name, topics, why, submitted_by, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET submitted_url=excluded.submitted_url, name=excluded.name,
                   topics=excluded.topics, why=excluded.why, submitted_by=excluded.submitted_by,
                   submitted_at=excluded.submitted_at, status='pending', attempts=0, fit=NULL, reason=NULL""",
            (key, c.url, c.name, json.dumps(c.topics), c.why, submitted_by, now()),
        )
        stats["added"] += 1
    return stats


def list_candidates(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM candidates"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    out = []
    for r in conn.execute(sql + " ORDER BY submitted_at DESC", args):
        d = dict(r)
        d["topics"] = json.loads(d["topics"])
        out.append(d)
    return out
