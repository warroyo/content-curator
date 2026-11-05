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
    story_id: int | None = None
    media: str = "article"
    duration_s: int | None = None
    listen_url: str | None = None
    # From a trial source found by discovery, not one you follow.
    trial: bool = False
    overall: float = 0.0
    minutes: int = 0
    # Other items covering the same story, best first: (source_name, title, url).
    also: list[tuple[str, str, str]] = field(default_factory=list)
    # Adjustment learned from feedback on earlier picks (learning.py), and why.
    learned: float = 0.0
    learned_why: str = ""

    @property
    def adjusted(self) -> float:
        return round(self.overall + self.learned, 2)

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


def prepare(c: Candidate, sel: Selection, weights: Weights) -> Candidate:
    """Fill in the overall score and the reading or listening minutes."""
    c.overall = overall(c, weights)  # type: ignore[arg-type]
    if c.confidence == "low":
        c.overall = round(c.overall - LOW_CONFIDENCE_PENALTY, 2)
    if c.media == "podcast":
        c.minutes = round(c.duration_s / 60) if c.duration_s else 0
    else:
        c.minutes = max(1, round(c.word_count / sel.words_per_minute)) if c.word_count else 0
    return c


def select(candidates: list[Candidate], sel: Selection, weights: Weights, learn: bool = False) -> Result:
    """With `learn`, core picks rank by overall plus the learned adjustment. A positive adjustment
    never lifts an item over `min_score`; a negative one can drop it below. Discovery and reserved
    slots ignore it, so what lies outside the stated interests is not narrowed by past feedback."""
    for c in candidates:
        prepare(c, sel, weights)
    candidates = collapse_stories(candidates)

    per_source: Counter[str] = Counter()
    per_media: Counter[str] = Counter()
    minutes_left = sel.max_reading_minutes
    picks: list[Decision] = []
    skipped: list[Decision] = []
    taken: set[int] = set()

    def try_take(c: Candidate, bucket: str, reason: str, use_reading_budget: bool = True) -> bool:
        nonlocal minutes_left
        if per_source[c.source_id] >= sel.max_per_source:
            skipped.append(Decision(c, False, "skipped", f"source cap ({sel.max_per_source}/day) reached"))
            return False
        media_cap = sel.max_per_media.get(c.media)
        if media_cap is not None and per_media[c.media] >= media_cap:
            skipped.append(Decision(c, False, "skipped", f"{c.media} cap ({media_cap}/day) reached"))
            return False
        if use_reading_budget and minutes_left is not None and c.minutes > minutes_left:
            skipped.append(Decision(c, False, "skipped", f"{c.minutes} min exceeds remaining reading budget"))
            return False
        per_source[c.source_id] += 1
        per_media[c.media] += 1
        if use_reading_budget and minutes_left is not None:
            minutes_left -= c.minutes
        taken.add(c.item_id)
        picks.append(Decision(c, True, bucket, reason))
        return True

    # Reserved slots (a daily listen) come on top of daily_count and the reading budget: they are for
    # a different time of day, and a good episode should not have to beat the day's best articles.
    for media, slot in sel.reserved.items():
        best = sorted((c for c in candidates if c.media == media and c.overall >= slot.min_score),
                      key=lambda c: (-c.overall, -c.depth))
        filled = 0
        for c in best:
            if filled >= slot.slots:
                break
            if try_take(c, "listen" if media == "podcast" else media,
                        f"reserved {media} slot, overall {c.overall}", use_reading_budget=False):
                filled += 1
    reserved_buckets = {"listen" if m == "podcast" else m for m in sel.reserved}

    def counted() -> int:
        return sum(p.bucket not in reserved_buckets for p in picks)

    n_discovery = round(sel.daily_count * sel.discovery_share)
    discovery = sorted((c for c in candidates if is_discovery(c)), key=lambda c: -c.discovery_score)
    for c in discovery:
        if sum(p.bucket == "discovery" for p in picks) >= n_discovery:
            break
        try_take(c, "discovery", f"discovery score {c.discovery_score} outside stated interests")

    def rank_score(c: Candidate) -> float:
        return c.adjusted if learn else c.overall

    def clears(c: Candidate) -> bool:
        return min(c.overall, rank_score(c)) >= sel.min_score

    core = sorted(candidates, key=lambda c: (-rank_score(c), -c.depth, -c.relevance))
    for c in core:
        if counted() >= sel.daily_count:
            break
        if c.item_id in taken or not clears(c):
            continue
        reason = f"overall {c.overall}"
        if learn and c.learned:
            reason += f", learned {c.learned:+g}"
        try_take(c, "core", reason)

    # Reserved and discovery slots are filled first so core picks cannot crowd them out, but they
    # read last: core, then discovery, then the listen.
    order = {"core": 0, "discovery": 1}
    picks.sort(key=lambda d: (order.get(d.bucket, 2), -d.candidate.overall))
    for rank, d in enumerate(picks, 1):
        d.rank = rank

    skipped_ids = {d.candidate.item_id for d in skipped}
    rest = [
        Decision(c, False, "skipped", "daily budget full" if clears(c) else "below threshold")
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


def collapse_stories(candidates: list[Candidate]) -> list[Candidate]:
    """Keep one candidate per story: the best-scored item, with the others listed as other coverage."""
    stories: dict[int, list[Candidate]] = {}
    for c in candidates:
        stories.setdefault(c.story_id if c.story_id is not None else -c.item_id, []).append(c)
    out = []
    for group in stories.values():
        group.sort(key=lambda c: (-c.overall, -c.depth, c.item_id))
        primary = group[0]
        primary.also = [(c.source_name, c.title, c.url) for c in group[1:]]
        out.append(primary)
    return out


CANDIDATE_SELECT = """
        SELECT i.id, i.source_id, s.name AS source_name, i.title, i.url, i.published_at, i.word_count,
               i.story_id, i.media, i.duration_s, i.listen_url, s.discovery AS source_discovery, sc.*
        FROM items i
        JOIN scores sc ON sc.item_id = i.id
        JOIN sources s ON s.id = i.source_id
"""


def load_candidates(conn: sqlite3.Connection, interests: Interests) -> list[Candidate]:
    """Scored, non-duplicate items in the freshness window that were never picked before."""
    rows = conn.execute(
        CANDIDATE_SELECT + f"""
        WHERE i.duplicate_of IS NULL
          -- links saved to the Raindrop inbox are always shown, as submitted picks
          AND i.id NOT IN (SELECT item_id FROM inbox WHERE status = 'pending')
          AND datetime(COALESCE(i.published_at, i.fetched_at)) >= datetime('now', '-{int(interests.selection.max_age_days)} days')
          -- never pick an item, or another article about its story, that was already published
          AND COALESCE(i.story_id, i.id) NOT IN (
              SELECT COALESCE(p.story_id, p.id) FROM published pub JOIN items p ON p.id = pub.item_id)
        """
    ).fetchall()
    return [_candidate(r) for r in rows]


def load_submitted(conn: sqlite3.Connection) -> list[Decision]:
    """Scored links from the Raindrop inbox, not yet moved into the picks. No age limit or threshold."""
    rows = conn.execute(
        CANDIDATE_SELECT + "WHERE i.id IN (SELECT item_id FROM inbox WHERE status = 'pending') ORDER BY i.id"
    ).fetchall()
    return [Decision(_candidate(r), True, "submitted", "you saved it to the inbox") for r in rows]


def _candidate(r: sqlite3.Row) -> Candidate:
    return Candidate(
        item_id=r["id"], source_id=r["source_id"], source_name=r["source_name"], title=r["title"], url=r["url"],
        published_at=r["published_at"], word_count=r["word_count"], confidence=r["confidence"],
        explanation=r["explanation"], topics=json.loads(r["topics"]), relevance=r["relevance"],
        novelty=r["novelty"], depth=r["depth"], source_quality=r["source_quality"],
        actionability=r["actionability"], discovery_value=r["discovery_value"],
        outside_interests=bool(r["outside_interests"]), story_id=r["story_id"], media=r["media"],
        duration_s=r["duration_s"], listen_url=r["listen_url"], trial=r["source_discovery"] == "trial",
    )


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
