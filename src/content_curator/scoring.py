"""AI relevance scoring through an OpenAI-compatible endpoint (LiteLLM in the lab)."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time

import httpx
from pydantic import BaseModel, Field, ValidationError

from content_curator.config import Interests, Settings, Weights
from content_curator.db import Run, now

log = logging.getLogger(__name__)

# Bump when the prompt or schema changes, so cached scores are redone.
PROMPT_VERSION = "1"


class ItemScore(BaseModel):
    relevance: int = Field(ge=0, le=10, description="Alignment with the reader's interests")
    novelty: int = Field(ge=0, le=10, description="Meaningful new information, not a rehash")
    depth: int = Field(ge=0, le=10, description="Technical or analytical substance")
    source_quality: int = Field(ge=0, le=10, description="Credibility and originality of the source")
    actionability: int = Field(ge=0, le=10, description="Practical usefulness")
    discovery_value: int = Field(ge=0, le=10, description="Worth reading even though outside stated interests")
    outside_interests: bool = Field(description="True when the item is not covered by any listed interest")
    topics: list[str] = Field(description="Tags of the matching interest groups; empty if none")
    explanation: str = Field(description="One or two sentences on why this is or is not worth the reader's time")


SCORE_SCHEMA = ItemScore.model_json_schema()
SCORE_SCHEMA["additionalProperties"] = False


class ModelUnavailable(Exception):
    """The model endpoint cannot be reached; the run should stop and retry later."""


def overall(score: ItemScore | sqlite3.Row, weights: Weights) -> float:
    """Weighted 0-10 score, computed in code so weights change without rescoring."""
    w = weights.model_dump()
    total = sum(w.values()) or 1.0
    get = score.__getitem__ if isinstance(score, sqlite3.Row) else lambda k: getattr(score, k)
    return round(sum(get(k) * v for k, v in w.items()) / total, 2)


def profile_text(interests: Interests) -> str:
    lines = ["Interest groups (tag: topics, with weights; higher weight = more important):"]
    for g in interests.groups:
        topics = "; ".join(f"{t.name} ({t.weight})" for t in g.topics)
        lines.append(f"- {g.tag} [{g.name}, weight {g.weight}]: {topics}")
    if interests.exclusions:
        lines.append("Not interested in (score relevance 0-2): " + "; ".join(interests.exclusions))
    if interests.preferred_sources:
        lines.append("Trusted sources: " + "; ".join(interests.preferred_sources))
    if interests.discovery_notes:
        lines.append("Discovery: " + interests.discovery_notes)
    return "\n".join(lines)


SYSTEM_PROMPT = """You are a strict content curator for one reader, an engineer. You judge whether an item \
deserves a place in a reading queue of 5-10 items a day out of hundreds. Most items do not.

{profile}

Scoring guide, every dimension 0-10:
- relevance: 9-10 central to a high-weight interest; 5-6 adjacent; 0-2 unrelated or excluded.
- novelty: is there real new information (a release, result, original analysis), or is it a rehash, listicle, or PR?
- depth: technical substance. Announcements without detail score low; deep dives score high.
- source_quality: original reporting, primary documentation, or a known expert score high; aggregators, SEO \
content and marketing score low.
- actionability: could the reader use this in their work or lab?
- discovery_value: high only for excellent items outside the listed interests that a curious engineer would enjoy.
Be calibrated: a typical item scores 3-6. Reserve 8+ for items you would personally insist the reader see.
Judge the content itself, not the title. The explanation must be specific to this item, not generic praise.
Respond only with JSON matching the schema."""


def item_prompt(row: sqlite3.Row, source_name: str, max_words: int) -> tuple[str, bool]:
    """Build the user message. Returns (prompt, full_text_available)."""
    text = row["content_text"] or row["summary"] or ""
    words = text.split()
    truncated = len(words) > max_words
    body = " ".join(words[:max_words]) + (" [truncated]" if truncated else "")
    full = row["extraction"] in ("feed", "page")
    parts = [
        f"Title: {row['title']}",
        f"Source: {source_name}",
        f"URL: {row['url']}",
        f"Published: {row['published_at'] or 'unknown'}",
        f"Length: {row['word_count']} words",
        "Content:" if full else "Only a summary is available:",
        body or "(no text available)",
    ]
    return "\n".join(parts), full


class Scorer:
    def __init__(self, settings: Settings, interests: Interests, client: httpx.Client | None = None):
        self.settings = settings
        self.interests = interests
        self.system = SYSTEM_PROMPT.format(profile=profile_text(interests))
        self.client = client or httpx.Client(timeout=settings.llm_timeout)

    def score(self, user: str) -> tuple[ItemScore, dict]:
        """Call the model with a JSON schema response format. Retries once on invalid output."""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.client.post(
                    f"{self.settings.llm_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                    json={
                        "model": self.settings.llm_model,
                        "temperature": 0.2,
                        "messages": [
                            {"role": "system", "content": self.system},
                            {"role": "user", "content": user},
                        ],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {"name": "item_score", "strict": True, "schema": SCORE_SCHEMA},
                        },
                    },
                )
            except httpx.TransportError as e:
                raise ModelUnavailable(str(e)) from e
            if resp.status_code in (502, 503, 504) or resp.status_code == 429:
                last_error = ModelUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                raise ModelUnavailable(f"HTTP {resp.status_code}: check CURATOR_LLM_API_KEY")
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
            try:
                return ItemScore.model_validate_json(_strip_fences(content)), data.get("usage") or {}
            except ValidationError as e:
                last_error = e
                log.info("invalid model output, retrying", extra={"attempt": attempt, "error": str(e)[:300]})
        assert last_error is not None
        raise last_error


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text


def input_hash(model: str, system: str, user: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\0{model}\0{system}\0{user}".encode()).hexdigest()


def score_pending(
    conn: sqlite3.Connection, settings: Settings, interests: Interests, scorer: Scorer | None = None,
    limit: int | None = None,
) -> dict:
    """Score unscored, non-duplicate items within the freshness window. Stops cleanly if the model is down."""
    scorer = scorer or Scorer(settings, interests)
    run = Run(conn, "score")
    stats = run.stats
    stats.update(scored=0, cached=0, failures=0, prompt_tokens=0, completion_tokens=0)
    max_age = interests.selection.max_age_days
    rows = conn.execute(
        f"""
        SELECT i.*, s.name AS source_name, sc.input_hash AS old_hash
        FROM items i JOIN sources s ON s.id = i.source_id
        LEFT JOIN scores sc ON sc.item_id = i.id
        WHERE i.duplicate_of IS NULL AND i.extraction != 'pending'
          AND datetime(COALESCE(i.published_at, i.fetched_at)) >= datetime('now', '-{int(max_age)} days')
        ORDER BY i.id
        {f'LIMIT {int(limit)}' if limit else ''}
        """
    ).fetchall()
    started = time.monotonic()
    for row in rows:
        user, full = item_prompt(row, row["source_name"], interests.scoring.max_words)
        h = input_hash(settings.llm_model, scorer.system, user)
        if row["old_hash"] == h:
            stats["cached"] += 1
            continue
        t0 = time.monotonic()
        try:
            score, usage = scorer.score(user)
        except ModelUnavailable as e:
            stats["model_unavailable"] = str(e)
            log.error("model unavailable, stopping scoring", extra={"error": str(e)})
            run.finish("failed", str(e))
            raise
        except Exception as e:
            stats["failures"] += 1
            log.warning("scoring failed", extra={"item": row["id"], "error": str(e)[:300]})
            continue
        ms = int((time.monotonic() - t0) * 1000)
        confidence = "high" if full and row["word_count"] >= interests.scoring.full_text_min_words else "low"
        conn.execute(
            """
            INSERT OR REPLACE INTO scores(item_id, model, prompt_version, input_hash, relevance, novelty, depth,
                source_quality, actionability, discovery_value, outside_interests, topics, explanation, confidence,
                prompt_tokens, completion_tokens, duration_ms, scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["id"], settings.llm_model, PROMPT_VERSION, h, score.relevance, score.novelty, score.depth,
             score.source_quality, score.actionability, score.discovery_value, int(score.outside_interests),
             json.dumps(score.topics), score.explanation, confidence, usage.get("prompt_tokens"),
             usage.get("completion_tokens"), ms, now()),
        )
        stats["scored"] += 1
        stats["prompt_tokens"] += usage.get("prompt_tokens") or 0
        stats["completion_tokens"] += usage.get("completion_tokens") or 0
        log.info("scored", extra={"item": row["id"], "overall": overall(score, interests.scoring.weights), "ms": ms})
    stats["duration_s"] = round(time.monotonic() - started, 1)
    run.finish()
    return stats
