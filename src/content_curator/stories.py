"""Same-story grouping: embed each item, then group items whose embeddings are close.

Embeddings place texts with similar meaning near each other. Two articles about the same
release or incident land very close; articles that merely share a topic land somewhat close.
Pairs above `same_story` are grouped outright. Pairs in the borderline band go to the chat
model, which decides "same story or just same topic". Every pair inside a group must clear
`borderline`, so a chain A~B~C cannot pull two unrelated items together.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time

import httpx
import numpy as np
from pydantic import BaseModel

from content_curator.config import Interests, Settings
from content_curator.db import Run, now
from content_curator.scoring import ModelUnavailable, _strip_fences

log = logging.getLogger(__name__)

BATCH = 16


def embed_text(row: sqlite3.Row, words: int) -> str:
    body = (row["content_text"] or row["summary"] or "").split()
    return f"{row['title']}\n\n{' '.join(body[:words])}".strip()


class Embedder:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.llm_timeout)

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        try:
            resp = self.client.post(
                f"{self.settings.llm_base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json={"model": self.settings.embed_model, "input": texts},
            )
        except httpx.TransportError as e:
            raise ModelUnavailable(str(e)) from e
        # Any error skips grouping for this run, including LiteLLM's 400 for a model it does not know.
        if resp.status_code >= 400:
            raise ModelUnavailable(f"embeddings HTTP {resp.status_code}: {resp.text[:200]}")
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        out = []
        for d in data:
            v = np.asarray(d["embedding"], dtype=np.float32)
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return out


def embed_pending(conn: sqlite3.Connection, settings: Settings, interests: Interests,
                  embedder: Embedder | None = None) -> dict:
    """Embed fresh, non-duplicate items that have no embedding for the current model and text."""
    embedder = embedder or Embedder(settings)
    words = interests.stories.embed_words
    rows = conn.execute(
        f"""
        SELECT i.id, i.title, i.content_text, i.summary, e.input_hash AS old_hash, e.model AS old_model
        FROM items i LEFT JOIN embeddings e ON e.item_id = i.id
        WHERE i.duplicate_of IS NULL AND i.extraction != 'pending'
          AND datetime(COALESCE(i.published_at, i.fetched_at)) >= datetime('now', '-{int(interests.selection.max_age_days)} days')
        ORDER BY i.id
        """
    ).fetchall()
    todo = []
    for r in rows:
        text = embed_text(r, words)
        h = hashlib.sha256(text.encode()).hexdigest()
        if r["old_hash"] != h or r["old_model"] != settings.embed_model:
            todo.append((r["id"], text, h))
    t0 = time.monotonic()
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        vectors = embedder.embed([t for _, t, _ in batch])
        conn.execute("BEGIN")
        for (item_id, _, h), v in zip(batch, vectors):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings(item_id, model, input_hash, dim, vector, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, settings.embed_model, h, len(v), v.tobytes(), now()),
            )
        conn.execute("COMMIT")
    return {"embedded": len(todo), "embed_cached": len(rows) - len(todo), "embed_s": round(time.monotonic() - t0, 1)}


class SameStory(BaseModel):
    same_story: bool
    reason: str


JUDGE_SCHEMA = SameStory.model_json_schema()
JUDGE_SCHEMA["additionalProperties"] = False

JUDGE_PROMPT = """Two articles are the same story when they report the same specific event, release, \
announcement, incident or paper, even from different angles. They are NOT the same story when they only \
share a topic (two different Kubernetes releases, two unrelated posts about AI agents, a release and a \
separate opinion piece about the field). Answer as JSON matching the schema."""


class Judge:
    """Asks the chat model whether a borderline pair is the same story."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.llm_timeout)

    def same(self, a: sqlite3.Row, b: sqlite3.Row) -> SameStory:
        def show(r):
            return f"Title: {r['title']}\nSource: {r['source_name']}\n{embed_text(r, 150)}"

        try:
            resp = self.client.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json={
                    "model": self.settings.llm_model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": JUDGE_PROMPT},
                        {"role": "user", "content": f"Article A\n{show(a)}\n\nArticle B\n{show(b)}"},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "same_story", "strict": True, "schema": JUDGE_SCHEMA},
                    },
                },
            )
        except httpx.TransportError as e:
            raise ModelUnavailable(str(e)) from e
        resp.raise_for_status()
        return SameStory.model_validate_json(_strip_fences(resp.json()["choices"][0]["message"]["content"] or ""))


def group_stories(conn: sqlite3.Connection, settings: Settings, interests: Interests,
                  judge: Judge | None = None) -> dict:
    """Assign items.story_id for fresh items, keeping the groups found on earlier nights."""
    cfg = interests.stories
    rows = conn.execute(
        f"""
        SELECT i.id, i.source_id, i.title, i.content_text, i.summary, i.story_id, s.name AS source_name, e.vector
        FROM items i JOIN embeddings e ON e.item_id = i.id JOIN sources s ON s.id = i.source_id
        WHERE i.duplicate_of IS NULL AND e.model = ?
          AND datetime(COALESCE(i.published_at, i.fetched_at)) >= datetime('now', '-{int(interests.selection.max_age_days)} days')
        ORDER BY i.id
        """,
        (settings.embed_model,),
    ).fetchall()
    stats = {"stories": 0, "grouped_items": 0, "judged": 0, "judged_same": 0}
    if len(rows) < 2:
        return stats

    ids = [r["id"] for r in rows]
    index = {item_id: k for k, item_id in enumerate(ids)}
    m = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    sims = m @ m.T

    # Start from the groups already recorded, then merge greedily, most similar pairs first.
    cluster = list(range(len(rows)))
    members: dict[int, set[int]] = {k: {k} for k in range(len(rows))}
    by_story: dict[int, int] = {}
    for k, r in enumerate(rows):
        if r["story_id"] is not None:
            root = by_story.setdefault(r["story_id"], k)
            if root != k:
                _merge(cluster, members, root, k)

    ii, jj = np.where(np.triu(sims, 1) >= cfg.borderline)
    pairs = sorted(zip(ii.tolist(), jj.tolist()), key=lambda p: -sims[p])
    judge_cache = {
        (r["a"], r["b"]): bool(r["same"])
        for r in conn.execute("SELECT a, b, same FROM story_judgements")
    }
    for a, b in pairs:
        ca, cb = cluster[a], cluster[b]
        if ca == cb:
            continue
        cross = sims[np.ix_(sorted(members[ca]), sorted(members[cb]))]
        if cross.min() < cfg.borderline:
            continue
        sim = float(sims[a, b])
        # A source's recurring series ("Security updates for Friday") embeds as near-identical
        # without being one story, so two items from one source always go to the judge.
        same_source = rows[a]["source_id"] == rows[b]["source_id"]
        if sim < cfg.same_story or same_source:
            key = (ids[a], ids[b])
            if key not in judge_cache:
                if judge is None or stats["judged"] >= cfg.max_judgements:
                    continue
                verdict = judge.same(rows[a], rows[b])
                stats["judged"] += 1
                judge_cache[key] = verdict.same_story
                conn.execute(
                    "INSERT OR REPLACE INTO story_judgements(a, b, similarity, same, reason, model, judged_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ids[a], ids[b], sim, int(verdict.same_story), verdict.reason, settings.llm_model, now()),
                )
                log.info("story judgement", extra={"a": ids[a], "b": ids[b], "sim": round(sim, 3),
                                                   "same": verdict.same_story})
            if not judge_cache[key]:
                continue
            stats["judged_same"] += 1
        _merge(cluster, members, ca, cb)

    conn.execute("BEGIN")
    for root, ks in members.items():
        if cluster[root] != root:
            continue
        # Keep an existing story id, so a story picked earlier stays the same story when its
        # first article falls out of the freshness window.
        known = [rows[k]["story_id"] for k in ks if rows[k]["story_id"] is not None]
        story = min(known) if known else (min(ids[k] for k in ks) if len(ks) > 1 else None)
        if len(ks) > 1:
            stats["stories"] += 1
            stats["grouped_items"] += len(ks)
        conn.executemany("UPDATE items SET story_id=? WHERE id=?", [(story, ids[k]) for k in ks])
    conn.execute("COMMIT")
    return stats


def _merge(cluster: list[int], members: dict[int, set[int]], a: int, b: int) -> None:
    ra, rb = cluster[a], cluster[b]
    if ra == rb:
        return
    if len(members[ra]) < len(members[rb]):
        ra, rb = rb, ra
    for k in members[rb]:
        cluster[k] = ra
    members[ra] |= members.pop(rb)


def run_stories(conn: sqlite3.Connection, settings: Settings, interests: Interests,
                embedder: Embedder | None = None, judge: Judge | None = None) -> dict:
    """Embed and group. If the embedding model is down, report it and leave grouping for the next run."""
    run = Run(conn, "stories")
    if not interests.stories.enabled:
        run.stats["skipped"] = "disabled in interests.yaml"
        run.finish()
        return run.stats
    try:
        run.stats.update(embed_pending(conn, settings, interests, embedder))
        run.stats.update(group_stories(conn, settings, interests, judge or Judge(settings)))
    except ModelUnavailable as e:
        run.stats["model_unavailable"] = str(e)
        log.warning("stories skipped: model unavailable", extra={"error": str(e)})
        run.finish("failed", str(e))
        return run.stats
    run.finish()
    log.info("stories", extra=run.stats)
    return run.stats


def story_members(conn: sqlite3.Connection, story_id: int) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT i.id, i.title, i.url, s.name AS source FROM items i JOIN sources s ON s.id=i.source_id "
            "WHERE i.story_id = ? ORDER BY i.id",
            (story_id,),
        )
    ]

