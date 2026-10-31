"""Podcast discovery: find new shows on Apple Podcasts and run them as trial sources.

Candidates come from topic searches and Apple's Technology top chart. Shows that are new,
active and established (enough episodes) go to the chat model with their recent episode
titles, which scores how well each fits the interest profile. The best become trials.

A trial source is collected and scored like any other, and its picks are marked in the report as
coming from a show you do not follow. Following it is adding it to sources.yaml. A trial show with
no pick within `trial_days` is disabled. Nothing is added to sources.yaml automatically.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, Field

from content_curator.apple import Apple
from content_curator.config import Interests, Settings
from content_curator.db import Run, now
from content_curator.normalize import normalize_url
from content_curator.scoring import ModelUnavailable, _strip_fences, profile_text

log = logging.getLogger(__name__)

# A show judged this recently is not sent to the chat model again.
REJUDGE_DAYS = 90


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "show"


def expire_trials(conn: sqlite3.Connection, trial_days: int) -> int:
    """Disable trial sources older than trial_days that never produced a pick."""
    cutoff = (datetime.now(UTC) - timedelta(days=trial_days)).isoformat(timespec="seconds")
    return conn.execute(
        """
        UPDATE sources SET enabled = 0
        WHERE discovery = 'trial' AND enabled = 1 AND discovered_at < ?
          AND id NOT IN (SELECT i.source_id FROM selections sel JOIN items i ON i.id = sel.item_id
                         WHERE sel.selected = 1)
        """,
        (cutoff,),
    ).rowcount


class ShowFit(BaseModel):
    fit: int = Field(ge=0, le=10, description="How well the show fits the reader, 0-10")
    reason: str


FIT_SCHEMA = ShowFit.model_json_schema()
FIT_SCHEMA["additionalProperties"] = False

FIT_PROMPT = """You decide whether a podcast is worth a trial in one engineer's listening queue.

{profile}

Score fit 0-10. High: substantive, technically literate shows on these interests, with real hosts \
and guests. Low: marketing, get-rich or agency-growth content, AI-generated or text-to-speech \
shows, news-reading filler, shows that only touch the topics in passing. Respond as JSON."""


class ShowJudge:
    def __init__(self, settings: Settings, interests: Interests, client: httpx.Client | None = None):
        self.settings = settings
        self.system = FIT_PROMPT.format(profile=profile_text(interests))
        self.client = client or httpx.Client(timeout=settings.llm_timeout)

    def fit(self, show: dict, episodes: list[dict]) -> ShowFit:
        eps = "\n".join(f"- {e.get('releaseDate', '')[:10]} {e.get('trackName', '')}: "
                         f"{(e.get('description') or '')[:200]}" for e in episodes)
        user = (f"Show: {show['collectionName']}\nBy: {show.get('artistName', '')}\n"
                f"Genres: {', '.join(show.get('genres', []))}\nEpisodes: {show.get('trackCount')}\n"
                f"Recent episodes:\n{eps or '(none listed)'}")
        try:
            resp = self.client.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json={
                    "model": self.settings.llm_model, "temperature": 0,
                    "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": user}],
                    "response_format": {"type": "json_schema",
                                        "json_schema": {"name": "show_fit", "strict": True, "schema": FIT_SCHEMA}},
                },
            )
        except httpx.TransportError as e:
            raise ModelUnavailable(str(e)) from e
        resp.raise_for_status()
        return ShowFit.model_validate_json(_strip_fences(resp.json()["choices"][0]["message"]["content"] or ""))


def _too_frequent(episodes: list[dict], days: int = 7, most: int = 10) -> bool:
    """More than `most` episodes in `days`: a back-catalogue dump or a generated show."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return sum(1 for e in episodes if e.get("releaseDate", "") >= since) > most


def discover_podcasts(conn: sqlite3.Connection, settings: Settings, interests: Interests,
                      apple: Apple | None = None, judge: ShowJudge | None = None) -> dict:
    """Find candidate shows (top chart and topic searches), filter cheaply, let the chat model judge fit."""
    cfg = interests.podcast_discovery
    run = Run(conn, "discover")
    stats = run.stats
    stats.update(candidates=0, filtered=0, judged=0, added=0, expired=0, added_shows=[], rejected=[])
    if not cfg.enabled:
        stats["skipped"] = "podcast_discovery disabled"
        run.finish()
        return stats

    stats["expired"] = expire_trials(conn, cfg.trial_days)
    known_urls = {normalize_url(r["url"]) for r in conn.execute("SELECT url FROM sources")}
    known_ids = {r["apple_id"] for r in conn.execute("SELECT apple_id FROM sources WHERE apple_id IS NOT NULL")}
    active_since = (datetime.now(UTC) - timedelta(days=cfg.active_within_days)).isoformat()
    judge = judge or ShowJudge(settings, interests)

    with httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=20, follow_redirects=True) as client:
        apple = apple or Apple(client)
        try:
            # Candidates in rank order: query results first (on-topic), then the chart (popular).
            found: dict[int, tuple[dict, str]] = {}
            for query in cfg.queries:
                for show in apple.search_shows(query):
                    found.setdefault(int(show["collectionId"]), (show, query))
            if cfg.use_top_chart:
                chart = [i for i in apple.top_chart_ids() if i not in found]
                for show in apple.lookup_shows(chart):
                    found.setdefault(int(show["collectionId"]), (show, "top Technology chart"))
            stats["candidates"] = len(found)

            rejudge_after = (datetime.now(UTC) - timedelta(days=REJUDGE_DAYS)).isoformat(timespec="seconds")
            judged_recently = {r[0] for r in conn.execute(
                "SELECT apple_id FROM show_judgements WHERE judged_at >= ?", (rejudge_after,))}
            shortlist = []
            for show_id, (show, query) in found.items():
                if (show_id in known_ids or show_id in judged_recently or normalize_url(show["feedUrl"]) in known_urls
                        or (show.get("trackCount") or 0) < cfg.min_episodes
                        or show.get("releaseDate", "") < active_since):
                    continue
                shortlist.append((show, query))
            stats["filtered"] = len(shortlist)

            scored = []
            for show, query in shortlist[: cfg.max_judged]:
                episodes = apple.recent_episodes(int(show["collectionId"]))
                if _too_frequent(episodes):
                    stats["rejected"].append({"name": show["collectionName"], "why": "too many episodes a week"})
                    continue
                verdict = judge.fit(show, episodes)
                stats["judged"] += 1
                conn.execute(
                    "INSERT OR REPLACE INTO show_judgements(apple_id, name, fit, reason, judged_at) VALUES (?, ?, ?, ?, ?)",
                    (int(show["collectionId"]), show["collectionName"], verdict.fit, verdict.reason, now()),
                )
                log.info("show judged", extra={"show": show["collectionName"], "fit": verdict.fit})
                if verdict.fit >= cfg.min_fit:
                    scored.append((verdict.fit, show, query, verdict.reason))
                else:
                    stats["rejected"].append({"name": show["collectionName"], "fit": verdict.fit,
                                              "why": verdict.reason[:160]})

            for fit, show, query, reason in sorted(scored, key=lambda x: -x[0])[: cfg.max_new_per_run]:
                show_id = int(show["collectionId"])
                sid = f"trial-{_slug(show['collectionName'])}-{show_id}"
                conn.execute(
                    """INSERT OR IGNORE INTO sources(id, name, url, type, topics, enabled, approved, discovery,
                               fetch_interval_hours, apple_id, discovered_at, discovery_query)
                       VALUES (?, ?, ?, 'podcast', '[]', 1, 0, 'trial', 24, ?, ?, ?)""",
                    (sid, show["collectionName"], show["feedUrl"], show_id, now(), query),
                )
                stats["added"] += 1
                stats["added_shows"].append({"id": sid, "name": show["collectionName"], "fit": fit,
                                             "found_by": query, "why": reason[:200]})
        except (httpx.HTTPError, ModelUnavailable) as e:
            stats["error"] = str(e)
            log.warning("podcast discovery stopped", extra={"error": str(e)})
    run.finish()
    return stats


def trial_sources(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT id, name, url, discovery_query, discovered_at, enabled FROM sources WHERE discovery='trial' "
            "ORDER BY discovered_at DESC"
        )
    ]
