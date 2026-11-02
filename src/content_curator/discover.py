"""Discovery: new sources run as trials, and following or dropping them.

Two ways in. Podcast discovery finds shows on Apple Podcasts (topic searches and the Technology top
chart): shows that are new, active and established go to the chat model with their recent episode
titles, which scores how well each fits the interest profile. Feed discovery checks feeds suggested
through `POST /api/candidates`, by anyone: a person, a script, an agent. Each suggestion's feed is
found and checked (it parses, posts recently, is not a firehose or a GitHub release feed) and then
judged by the chat model on its recent titles.

The best become trial sources, collected and scored like any other and marked in the report and in
Raindrop as coming from a source you do not follow. A trial ends after `trial_days`: following it
(a `follow` tag on one of its picks, or `POST /api/sources/{id}/follow`) keeps it, otherwise it is
disabled. `unfollow` drops any source, including one in sources.yaml. Follow decisions live in the
database; nothing is written to sources.yaml.
"""

from __future__ import annotations

import calendar
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import feedparser
import httpx
import lxml.etree
import lxml.html
from pydantic import BaseModel, Field

from content_curator.apple import Apple
from content_curator.config import Interests, Settings
from content_curator.db import Run, now
from content_curator.normalize import normalize_url
from content_curator.registry import REJUDGE_DAYS
from content_curator.scoring import ModelUnavailable, _strip_fences, profile_text

log = logging.getLogger(__name__)

# A suggestion whose feed cannot be fetched is tried on this many runs before it is rejected.
MAX_ATTEMPTS = 3


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "show"


def expire_trials(conn: sqlite3.Connection, trial_days: int, source_type: str | None = None) -> int:
    """Disable trial sources older than trial_days, picked or not. A followed source is no longer a trial."""
    cutoff = (datetime.now(UTC) - timedelta(days=trial_days)).isoformat(timespec="seconds")
    sql = "UPDATE sources SET enabled = 0 WHERE discovery = 'trial' AND enabled = 1 AND discovered_at < ?"
    args: list = [cutoff]
    if source_type:
        sql += " AND type = ?"
        args.append(source_type)
    return conn.execute(sql, args).rowcount


class ShowFit(BaseModel):
    fit: int = Field(ge=0, le=10, description="How well the source fits the reader, 0-10")
    reason: str


FIT_SCHEMA = ShowFit.model_json_schema()
FIT_SCHEMA["additionalProperties"] = False


def _ask_fit(client: httpx.Client, settings: Settings, system: str, user: str) -> ShowFit:
    try:
        resp = client.post(
            f"{settings.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "source_fit", "strict": True, "schema": FIT_SCHEMA}},
            },
        )
    except httpx.TransportError as e:
        raise ModelUnavailable(str(e)) from e
    resp.raise_for_status()
    return ShowFit.model_validate_json(_strip_fences(resp.json()["choices"][0]["message"]["content"] or ""))

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
        return _ask_fit(self.client, self.settings, self.system, user)


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

    stats["expired"] = expire_trials(conn, cfg.trial_days, "podcast")
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
    """Trial sources, newest first, with how many of their items were picked."""
    return [
        dict(r) for r in conn.execute(
            """SELECT s.id, s.name, s.url, s.type, s.discovery_query, s.discovered_at, s.enabled,
                      (SELECT COUNT(DISTINCT sel.item_id) FROM selections sel JOIN items i ON i.id = sel.item_id
                       WHERE i.source_id = s.id AND sel.selected = 1) AS picks
               FROM sources s WHERE s.discovery = 'trial' ORDER BY s.discovered_at DESC"""
        )
    ]


# ---- Feed discovery: suggestions in, trials out ----

# Release and changelog feeds: a version number and a commit list, never worth a read.
RELEASE_FEED = re.compile(r"github\.com/[^/]+/[^/]+/(releases|tags|commits)([./?]|$)", re.IGNORECASE)
COMMON_FEED_PATHS = ("/feed", "/rss", "/atom.xml", "/feed.xml", "/rss.xml", "/index.xml")
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/xml",
              "text/xml")


class Rejected(Exception):
    """A suggestion that fails a check; the message says why."""


@dataclass
class FeedCheck:
    feed_url: str
    title: str
    type: str  # rss or podcast
    newest: str
    per_week: int
    recent: list[tuple[str, str, str]]  # (date, title, summary), newest first


def _entry_date(e: dict) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), UTC).isoformat(timespec="seconds")
    return None


def _feed_link(html: str, base: str) -> str | None:
    """The first RSS/Atom feed a page advertises with <link rel="alternate">."""
    try:
        root = lxml.html.fromstring(html)
    except (lxml.etree.ParserError, ValueError):
        return None
    for link in root.xpath("//link[@href]"):
        rel = (link.get("rel") or "").lower().split()
        if "alternate" in rel and (link.get("type") or "").lower() in FEED_TYPES:
            return urljoin(base, link.get("href"))
    return None


def check_feed(client: httpx.Client, url: str, active_within_days: int, max_per_week: int) -> FeedCheck:
    """Find the feed behind a URL and check it is a live, readable source. Raises Rejected."""
    if RELEASE_FEED.search(url):
        raise Rejected("GitHub release or changelog feed")
    resp = client.get(url)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    feed_url = str(resp.url)
    if not feed.entries:
        found = _feed_link(resp.text, feed_url)
        if found:
            if RELEASE_FEED.search(found):
                raise Rejected("GitHub release or changelog feed")
            resp = client.get(found)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            feed_url = str(resp.url)
            if not feed.entries:
                raise Rejected("the linked feed has no entries")
        else:
            # Some sites never advertise their feed; try where feeds usually live.
            for path in COMMON_FEED_PATHS:
                guess = client.get(urljoin(feed_url, path))
                if guess.status_code == 200 and (parsed := feedparser.parse(guess.content)).entries:
                    feed, feed_url = parsed, str(guess.url)
                    break
            else:
                raise Rejected("no feed found at the URL, linked from the page, or at a usual path")

    dated = sorted(((d, e) for e in feed.entries if (d := _entry_date(e))), key=lambda x: x[0], reverse=True)
    if not dated:
        raise Rejected("no dated entries")
    newest = dated[0][0]
    if newest < (datetime.now(UTC) - timedelta(days=active_within_days)).isoformat():
        raise Rejected(f"last post {newest[:10]}, over {active_within_days} days ago")
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    per_week = sum(1 for d, _ in dated if d >= week_ago)
    if per_week > max_per_week:
        raise Rejected(f"{per_week} entries in the last week, over {max_per_week}")

    audio = sum(1 for e in feed.entries
                if any((enc.get("type") or "").startswith("audio/") for enc in e.get("enclosures", [])))
    from content_curator.collectors.rss import html_to_text

    recent = [(d[:10], (e.get("title") or "").strip(), (html_to_text(e.get("summary")) or "")[:200])
              for d, e in dated[:8]]
    return FeedCheck(feed_url=feed_url, title=(feed.feed.get("title") or "").strip(),
                     type="podcast" if audio * 2 > len(feed.entries) else "rss",
                     newest=newest, per_week=per_week, recent=recent)


FEED_FIT_PROMPT = """You decide whether a blog, newsletter or podcast feed is worth a trial in one engineer's \
reading queue.

{profile}

Score fit 0-10 from the feed's recent posts. High: engineers writing about their own work, project \
announcements with substance, deep technical writing on these interests, and excellent writing on \
engineering, science or computing history in general. Low: marketing, press releases, webinar and event \
promotion, product launches with no technical content, link roundups with no commentary, SEO content, \
raw release or changelog feeds, feeds that only touch the topics in passing. Respond as JSON."""


class FeedJudge:
    def __init__(self, settings: Settings, interests: Interests, client: httpx.Client | None = None):
        self.settings = settings
        self.system = FEED_FIT_PROMPT.format(profile=profile_text(interests))
        self.client = client or httpx.Client(timeout=settings.llm_timeout)

    def fit(self, name: str, check: FeedCheck, why: str = "") -> ShowFit:
        posts = "\n".join(f"- {d} {title}: {summary}" for d, title, summary in check.recent)
        user = (f"Feed: {check.title or name}\nURL: {check.feed_url}\n"
                f"{'Podcast' if check.type == 'podcast' else 'Blog or newsletter'}, "
                f"{check.per_week} posts in the last week.\n"
                + (f"Suggested because: {why}\n" if why else "")
                + f"Recent posts:\n{posts}")
        return _ask_fit(self.client, self.settings, self.system, user)


def _unique_id(conn: sqlite3.Connection, base: str) -> str:
    sid, n = base, 2
    while conn.execute("SELECT 1 FROM sources WHERE id = ?", (sid,)).fetchone():
        sid, n = f"{base}-{n}", n + 1
    return sid


def discover_feeds(conn: sqlite3.Connection, settings: Settings, interests: Interests,
                   client: httpx.Client | None = None, judge: FeedJudge | None = None) -> dict:
    """Check pending suggestions; the best that pass start as trial sources, within the trial cap."""
    cfg = interests.feed_discovery
    run = Run(conn, "discover_feeds")
    stats = run.stats
    stats.update(checked=0, judged=0, added=0, expired=0, waiting=0, added_feeds=[], rejected=[])
    if not cfg.enabled:
        stats["skipped"] = "feed_discovery disabled"
        run.finish()
        return stats

    stats["expired"] = expire_trials(conn, cfg.trial_days, "rss")
    judge = judge or FeedJudge(settings, interests)
    rejudge_after = (datetime.now(UTC) - timedelta(days=REJUDGE_DAYS)).isoformat(timespec="seconds")
    pending = conn.execute(
        "SELECT * FROM candidates WHERE status = 'pending' ORDER BY submitted_at LIMIT ?", (cfg.max_checked,)
    ).fetchall()

    def reject(row, reason: str, check: FeedCheck | None = None, fit: int | None = None) -> None:
        conn.execute(
            "UPDATE candidates SET status='rejected', reason=?, feed_url=?, type=?, fit=?, checked_at=? WHERE url=?",
            (reason, check.feed_url if check else None, check.type if check else None, fit, now(), row["url"]),
        )
        stats["rejected"].append({"url": row["submitted_url"], "why": reason[:160]})

    passed = []
    own = httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=20, follow_redirects=True)
    http = client or own
    try:
        for row in pending:
            stats["checked"] += 1
            try:
                check = check_feed(http, row["submitted_url"], cfg.active_within_days, cfg.max_per_week)
            except Rejected as e:
                reject(row, str(e))
                continue
            except httpx.HTTPError as e:
                attempts = row["attempts"] + 1
                if attempts >= MAX_ATTEMPTS:
                    reject(row, f"fetch failed {attempts} times: {e}")
                else:
                    conn.execute("UPDATE candidates SET attempts=?, reason=? WHERE url=?",
                                 (attempts, f"fetch failed: {str(e)[:200]}", row["url"]))
                continue
            known = conn.execute("SELECT id, url FROM sources WHERE url != ''").fetchall()
            if any(normalize_url(k["url"]) == normalize_url(check.feed_url) for k in known):
                reject(row, "already a source", check)
                continue
            if row["fit"] is not None and (row["checked_at"] or "") >= rejudge_after:
                verdict = ShowFit(fit=row["fit"], reason=row["reason"] or "")
            else:
                try:
                    verdict = judge.fit(row["name"], check, row["why"])
                except ModelUnavailable as e:
                    stats["error"] = str(e)
                    log.warning("feed discovery stopped: model unavailable", extra={"error": str(e)})
                    break
                stats["judged"] += 1
            log.info("feed judged", extra={"feed": check.feed_url, "fit": verdict.fit})
            if verdict.fit < cfg.min_fit:
                reject(row, f"fit {verdict.fit}: {verdict.reason}", check, verdict.fit)
                continue
            conn.execute(
                "UPDATE candidates SET feed_url=?, type=?, fit=?, reason=?, checked_at=? WHERE url=?",
                (check.feed_url, check.type, verdict.fit, verdict.reason, now(), row["url"]),
            )
            passed.append((verdict.fit, row, check, verdict.reason))
    finally:
        own.close()

    active = conn.execute("SELECT COUNT(*) FROM sources WHERE discovery = 'trial' AND enabled = 1").fetchone()[0]
    slots = max(0, min(cfg.max_new_per_run, cfg.max_active_trials - active))
    passed.sort(key=lambda x: -x[0])
    for fit, row, check, reason in passed[:slots]:
        name = row["name"] or check.title or check.feed_url
        sid = _unique_id(conn, f"trial-{_slug(name)}")
        conn.execute(
            """INSERT INTO sources(id, name, url, type, topics, enabled, approved, discovery,
                                   fetch_interval_hours, discovered_at, discovery_query)
               VALUES (?, ?, ?, ?, ?, 1, 0, 'trial', ?, ?, ?)""",
            (sid, name, check.feed_url, check.type, row["topics"], 24 if check.type == "podcast" else 4, now(),
             f"suggested by {row['submitted_by'] or 'API'}"),
        )
        conn.execute("UPDATE candidates SET status='trial', source_id=? WHERE url=?", (sid, row["url"]))
        stats["added"] += 1
        stats["added_feeds"].append({"id": sid, "name": name, "fit": fit, "type": check.type,
                                     "why": reason[:200]})
    # Passed but no trial slot free: they stay pending with their verdict, for a later run.
    stats["waiting"] = len(passed) - min(len(passed), slots)
    run.finish()
    return stats
