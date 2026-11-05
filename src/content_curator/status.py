"""Read-only queries for status, picks and item explanations."""

from __future__ import annotations

import json
import sqlite3


def status(conn: sqlite3.Connection, max_age_days: int = 7) -> dict:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "sources_enabled": one("SELECT COUNT(*) FROM sources WHERE enabled=1"),
        "sources_failing": [
            dict(r) for r in conn.execute(
                "SELECT id, consecutive_failures, last_error FROM sources WHERE consecutive_failures > 0"
            )
        ],
        "items": one("SELECT COUNT(*) FROM items"),
        "duplicates": one("SELECT COUNT(*) FROM items WHERE duplicate_of IS NOT NULL"),
        "scored": one("SELECT COUNT(*) FROM scores"),
        "unscored_fresh": one(
            "SELECT COUNT(*) FROM items i LEFT JOIN scores s ON s.item_id=i.id WHERE s.item_id IS NULL "
            "AND i.duplicate_of IS NULL AND datetime(COALESCE(i.published_at, i.fetched_at)) >= datetime('now', ?)",
            f"-{int(max_age_days)} days",
        ),
        "extraction": {r[0]: r[1] for r in conn.execute("SELECT extraction, COUNT(*) FROM items GROUP BY 1")},
        "media": {r[0]: r[1] for r in conn.execute("SELECT media, COUNT(*) FROM items GROUP BY 1")},
        "trial_shows": [dict(r) for r in conn.execute(
            "SELECT id, name, type, discovery_query, discovered_at FROM sources WHERE discovery='trial' AND enabled=1"
        )],
        "candidates": {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM candidates GROUP BY 1")},
        "feedback_7d": {r[0]: r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM feedback WHERE datetime(recorded_at) >= datetime('now', '-7 days') GROUP BY 1"
        )},
        "avg_score_ms": one("SELECT CAST(AVG(duration_ms) AS INTEGER) FROM scores"),
        "last_runs": [
            {**dict(r), "stats": json.loads(r["stats"])}
            for r in conn.execute(
                "SELECT kind, started_at, finished_at, status, stats, error FROM runs "
                "WHERE id IN (SELECT MAX(id) FROM runs GROUP BY kind) ORDER BY kind"
            )
        ],
    }


def sources(conn: sqlite3.Connection) -> list[dict]:
    """Every source the curator knows, with its status and how many of its items were picked.

    status: configured (from sources.yaml), trial, followed (a trial you kept), unfollowed,
    expired (a trial that ended), removed (no longer in sources.yaml).
    """
    rows = conn.execute(
        """SELECT s.id, s.name, s.url, s.type, s.enabled, s.discovery, s.discovered_at, s.discovery_query,
                  s.last_success_at, s.consecutive_failures, s.items_seen, f.state AS follow, f.changed_at AS follow_at,
                  (SELECT COUNT(DISTINCT sel.item_id) FROM selections sel JOIN items i ON i.id = sel.item_id
                   WHERE i.source_id = s.id AND sel.selected = 1) AS picks
           FROM sources s LEFT JOIN follows f ON f.source_id = s.id
           WHERE s.id != 'manual' ORDER BY s.discovery, s.name"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        if d["follow"] == "unfollowed":
            d["status"] = "unfollowed"
        elif d["discovery"] == "followed":
            d["status"] = "followed"
        elif d["discovery"] == "trial":
            d["status"] = "trial" if d["enabled"] else "expired"
        else:
            d["status"] = "configured" if d["enabled"] else "removed"
        out.append(d)
    return out


PICK_COLUMNS = """
    sel.rank, sel.selected, sel.bucket, sel.overall, sel.reason, sel.dry_run,
    i.id AS item_id, i.title, i.url, i.published_at, i.word_count, s.name AS source,
    sc.relevance, sc.novelty, sc.depth, sc.source_quality, sc.actionability, sc.discovery_value,
    sc.topics, sc.explanation, sc.confidence
"""


def _pick(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["topics"] = json.loads(d["topics"])
    d["selected"] = bool(d["selected"])
    d["dry_run"] = bool(d["dry_run"])
    return d


def latest_picks(conn: sqlite3.Connection, include_near_misses: bool = False) -> dict | None:
    run = conn.execute(
        "SELECT id, started_at, stats FROM runs WHERE kind='curate' AND status='ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not run:
        return None
    rows = conn.execute(
        f"""SELECT {PICK_COLUMNS} FROM selections sel
            JOIN items i ON i.id = sel.item_id JOIN sources s ON s.id = i.source_id
            JOIN scores sc ON sc.item_id = i.id
            WHERE sel.run_id = ? {'' if include_near_misses else 'AND sel.selected = 1'}
            ORDER BY sel.selected DESC, sel.rank, sel.overall DESC""",
        (run["id"],),
    ).fetchall()
    return {"run_id": run["id"], "at": run["started_at"], "stats": json.loads(run["stats"]),
            "items": [_pick(r) for r in rows]}


def feedback(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    """What you did with picks in Raindrop over the last `days`: read, meh, deleted, favourite."""
    rows = conn.execute(
        """SELECT f.kind, f.recorded_at, i.id AS item_id, i.title, i.url, s.id AS source_id, s.name AS source
           FROM feedback f JOIN items i ON i.id = f.item_id JOIN sources s ON s.id = i.source_id
           WHERE datetime(f.recorded_at) >= datetime('now', ?) ORDER BY f.recorded_at DESC""",
        (f"-{int(days)} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def item_detail(conn: sqlite3.Connection, item_id: int) -> dict | None:
    item = conn.execute(
        """SELECT i.id, i.title, i.url, i.canonical_url, i.author, i.published_at, i.fetched_at, i.word_count,
                  i.extraction, i.duplicate_of, s.id AS source_id, s.name AS source
           FROM items i JOIN sources s ON s.id = i.source_id WHERE i.id = ?""",
        (item_id,),
    ).fetchone()
    if not item:
        return None
    score = conn.execute("SELECT * FROM scores WHERE item_id = ?", (item_id,)).fetchone()
    history = conn.execute(
        """SELECT sel.run_id, r.started_at, sel.rank, sel.selected, sel.bucket, sel.overall, sel.reason, sel.dry_run
           FROM selections sel JOIN runs r ON r.id = sel.run_id WHERE sel.item_id = ? ORDER BY sel.run_id""",
        (item_id,),
    ).fetchall()
    out = dict(item)
    if score:
        out["score"] = {**dict(score), "topics": json.loads(score["topics"])}
        out["score"].pop("input_hash", None)
    out["selections"] = [dict(h) for h in history]
    return out
