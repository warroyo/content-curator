"""Deterministic selection of the day's picks from scored candidates."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from content_curator.config import Interests, Selection, Weights
from content_curator.scoring import overall

# Items scored on a summary alone lose this much, so full-text items win ties.
LOW_CONFIDENCE_PENALTY = 0.5


@dataclass
class Candidate:
    item_id: int
    source_id: str
    source_name: str
    title: str
    url: str
    published_at: str | None
    word_count: int
    confidence: str
    explanation: str
    topics: list[str]
    relevance: float
    novelty: float
    depth: float
    source_quality: float
    actionability: float
    discovery_value: float
    outside_interests: bool
    overall: float = 0.0
    minutes: int = 0

    @property
    def discovery_score(self) -> float:
        return round((self.discovery_value * 2 + self.depth + self.source_quality + self.novelty) / 5, 2)


@dataclass
class Decision:
    candidate: Candidate
    selected: bool
    bucket: str  # "core", "discovery", or "skipped"
    reason: str
    rank: int = 0


@dataclass
class Result:
    picks: list[Decision] = field(default_factory=list)
    near_misses: list[Decision] = field(default_factory=list)


def is_discovery(c: Candidate) -> bool:
    return c.outside_interests and c.discovery_value >= 7 and c.depth >= 5 and c.source_quality >= 5


def select(candidates: list[Candidate], sel: Selection, weights: Weights) -> Result:
    for c in candidates:
        c.overall = overall(c, weights)  # type: ignore[arg-type]
        if c.confidence == "low":
            c.overall = round(c.overall - LOW_CONFIDENCE_PENALTY, 2)
        c.minutes = max(1, round(c.word_count / sel.words_per_minute)) if c.word_count else 0

    per_source: Counter[str] = Counter()
    minutes_left = sel.max_reading_minutes
    picks: list[Decision] = []
    skipped: list[Decision] = []
    taken: set[int] = set()

    def try_take(c: Candidate, bucket: str, reason: str) -> bool:
        nonlocal minutes_left
        if per_source[c.source_id] >= sel.max_per_source:
            skipped.append(Decision(c, False, "skipped", f"source cap ({sel.max_per_source}/day) reached"))
            return False
        if minutes_left is not None and c.minutes > minutes_left:
            skipped.append(Decision(c, False, "skipped", f"{c.minutes} min exceeds remaining reading budget"))
            return False
        per_source[c.source_id] += 1
        if minutes_left is not None:
            minutes_left -= c.minutes
        taken.add(c.item_id)
        picks.append(Decision(c, True, bucket, reason))
        return True

    n_discovery = round(sel.daily_count * sel.discovery_share)
    discovery = sorted((c for c in candidates if is_discovery(c)), key=lambda c: -c.discovery_score)
    for c in discovery:
        if sum(p.bucket == "discovery" for p in picks) >= n_discovery:
            break
        try_take(c, "discovery", f"discovery score {c.discovery_score} outside stated interests")

    core = sorted(candidates, key=lambda c: (-c.overall, -c.depth, -c.relevance))
    for c in core:
        if len(picks) >= sel.daily_count:
            break
        if c.item_id in taken:
            continue
        if c.overall < sel.min_score:
            break
        try_take(c, "core", f"overall {c.overall}")

    # Discovery slots are filled first so core picks cannot crowd them out, but they read last.
    picks.sort(key=lambda d: (d.bucket == "discovery", -d.candidate.overall))
    for rank, d in enumerate(picks, 1):
        d.rank = rank

    skipped_ids = {d.candidate.item_id for d in skipped}
    rest = [
        Decision(c, False, "skipped", "below threshold" if c.overall < sel.min_score else "daily budget full")
        for c in core
        if c.item_id not in taken and c.item_id not in skipped_ids
    ]
    near = sorted(skipped + rest, key=lambda d: -d.candidate.overall)
    seen: set[int] = set()
    near_misses = []
    for d in near:
        if d.candidate.item_id in seen:
            continue
        seen.add(d.candidate.item_id)
        near_misses.append(d)
    return Result(picks=picks, near_misses=near_misses[: sel.near_misses])


def load_candidates(conn: sqlite3.Connection, interests: Interests) -> list[Candidate]:
    """Scored, non-duplicate items in the freshness window that were never picked before."""
    rows = conn.execute(
        f"""
        SELECT i.id, i.source_id, s.name AS source_name, i.title, i.url, i.published_at, i.word_count,
               sc.*
        FROM items i
        JOIN scores sc ON sc.item_id = i.id
        JOIN sources s ON s.id = i.source_id
        WHERE i.duplicate_of IS NULL
          AND datetime(COALESCE(i.published_at, i.fetched_at)) >= datetime('now', '-{int(interests.selection.max_age_days)} days')
          AND i.id NOT IN (SELECT item_id FROM selections WHERE selected = 1 AND dry_run = 0)
        """
    ).fetchall()
    return [
        Candidate(
            item_id=r["id"], source_id=r["source_id"], source_name=r["source_name"], title=r["title"], url=r["url"],
            published_at=r["published_at"], word_count=r["word_count"], confidence=r["confidence"],
            explanation=r["explanation"], topics=json.loads(r["topics"]), relevance=r["relevance"],
            novelty=r["novelty"], depth=r["depth"], source_quality=r["source_quality"],
            actionability=r["actionability"], discovery_value=r["discovery_value"],
            outside_interests=bool(r["outside_interests"]),
        )
        for r in rows
    ]


def record(conn: sqlite3.Connection, run_id: int, result: Result, dry_run: bool) -> None:
    conn.execute("BEGIN")
    for d in result.picks + result.near_misses:
        conn.execute(
            "INSERT INTO selections(run_id, item_id, rank, selected, bucket, overall, reason, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, d.candidate.item_id, d.rank, int(d.selected), d.bucket, d.candidate.overall, d.reason,
             int(dry_run)),
        )
    conn.execute("COMMIT")
