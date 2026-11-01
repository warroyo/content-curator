"""Publishing picks to Raindrop.io, into one collection ("AI picks" by default).

Raindrop's create endpoint accepts the same URL twice, so the `published` table is the guard
against double-publishing, and `import/url/exists` is a second one: a URL already in the account
(saved by hand, or by a run that died before recording it) is mapped to that raindrop, not saved
again. `pleaseParse` does nothing through the API (smoke test, 2026-11-01), so each pick is parsed
with `import/url/parse` for its type and cover; the title and note come from the curator.
"""

from __future__ import annotations

import logging
import sqlite3
import time

import httpx

from content_curator.config import Publish, Settings
from content_curator.db import now
from content_curator.select import Candidate, Decision

log = logging.getLogger(__name__)

API = "https://api.raindrop.io/rest/v1"
# Batch create takes at most this many raindrops per request.
BATCH = 100
RETRIES = 3


class PublishError(Exception):
    """Raindrop could not be reached or refused the request; the picks stay unpublished."""


class Raindrop:
    def __init__(self, token: str, client: httpx.Client | None = None, base: str = API):
        if not token:
            raise PublishError("no Raindrop token: set CURATOR_RAINDROP_TOKEN")
        self.base = base
        self.client = client or httpx.Client(timeout=30)
        self.client.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(RETRIES):
            try:
                resp = self.client.request(method, f"{self.base}{path}", **kwargs)
            except httpx.TransportError as e:
                if attempt == RETRIES - 1:
                    raise PublishError(f"Raindrop unreachable: {e}") from e
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 and attempt < RETRIES - 1:
                reset = resp.headers.get("X-RateLimit-Reset")
                wait = max(1.0, float(reset) - time.time()) if reset else 60.0
                log.warning("raindrop rate limited", extra={"wait_s": round(wait)})
                time.sleep(min(wait, 120))
                continue
            if resp.status_code >= 400:
                raise PublishError(f"Raindrop {method} {path}: {resp.status_code} {resp.text[:200]}")
            return resp.json()
        raise PublishError(f"Raindrop {method} {path}: retries exhausted")

    def collection_id(self, title: str) -> int:
        """Id of the root collection with this title, created if missing."""
        for c in self._call("GET", "/collections").get("items", []):
            if c.get("title") == title:
                return int(c["_id"])
        return int(self._call("POST", "/collection", json={"title": title})["item"]["_id"])

    def existing(self, urls: list[str]) -> dict[str, int]:
        """Raindrop id per URL for those already saved anywhere in the account."""
        if not urls:
            return {}
        r = self._call("POST", "/import/url/exists", json={"urls": urls})
        return {d["link"]: int(d["_id"]) for d in r.get("duplicates", [])}

    def parse(self, url: str) -> dict:
        """Raindrop's own reading of a page: type, cover, title, excerpt. Empty on failure."""
        try:
            return self._call("GET", "/import/url/parse", params={"url": url}).get("item", {})
        except PublishError as e:
            log.info("raindrop parse failed", extra={"url": url, "error": str(e)})
            return {}

    def create(self, raindrops: list[dict]) -> list[dict]:
        out = []
        for i in range(0, len(raindrops), BATCH):
            out += self._call("POST", "/raindrops", json={"items": raindrops[i : i + BATCH]}).get("items", [])
        return out


def link_for(c: Candidate) -> str:
    """What gets saved: the Apple Podcasts episode for a podcast, otherwise the item's URL."""
    return c.listen_url or c.url


def tags_for(d: Decision) -> list[str]:
    c = d.candidate
    tags = ["ai-curated", f"media-{c.media}", *c.topics]
    if d.bucket == "discovery":
        tags.append("ai-discovery")
    if d.bucket == "listen":
        tags.append("ai-listen")
    if c.trial:
        tags.append("ai-trial-show")
    return list(dict.fromkeys(tags))


def note_for(c: Candidate) -> str:
    """The why-this-item explanation, shown on the card under the title."""
    parts = [c.explanation.strip()]
    if c.trial:
        parts.append(f"From {c.source_name}, a show on trial that you do not follow yet.")
    if c.also:
        parts.append("Also covered by: " + ", ".join(f"{src} ({url})" for src, _, url in c.also))
    return "\n\n".join(p for p in parts if p)


def sentences(text: str, limit: int = 300) -> str:
    """Whole sentences up to about `limit` characters, so an excerpt never stops mid-word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = max(text.rfind(". ", 0, limit), text.rfind("? ", 0, limit), text.rfind("! ", 0, limit))
    if cut > 0:
        return text[: cut + 1]
    return text[: text.rfind(" ", 0, limit)].rstrip(",;:") + "…"


def raindrop_for(d: Decision, collection: int, parsed: dict) -> dict:
    c = d.candidate
    body = {
        "link": link_for(c),
        "title": c.title,
        "note": note_for(c),
        "tags": tags_for(d),
        "collection": {"$id": collection},
    }
    if parsed.get("type") in ("article", "video", "audio", "document"):
        body["type"] = parsed["type"]
    if parsed.get("cover"):
        body["cover"] = parsed["cover"]
    # A page's own description reads well under the note; YouTube's parsed excerpt is unrelated text.
    if parsed.get("excerpt") and parsed.get("type") != "video":
        body["excerpt"] = sentences(parsed["excerpt"])
    return body


def publish(conn: sqlite3.Connection, run_id: int, picks: list[Decision], cfg: Publish,
            client: Raindrop) -> dict:
    """Save picks not yet published; record every pick's raindrop id. Returns counts."""
    stats = {"published": 0, "already_published": 0, "already_saved": 0}
    done = {r[0] for r in conn.execute("SELECT item_id FROM published")}
    todo = []
    for d in picks:
        if d.candidate.item_id in done:
            stats["already_published"] += 1
        else:
            todo.append(d)
    if not todo:
        return stats

    collection = client.collection_id(cfg.collection)
    saved = client.existing([link_for(d.candidate) for d in todo])

    def record(d: Decision, raindrop_id: int, status: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO published(item_id, raindrop_id, collection_id, link, status, run_id, "
            "published_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d.candidate.item_id, raindrop_id, collection, link_for(d.candidate), status, run_id, now()),
        )

    new = []
    for d in todo:
        link = link_for(d.candidate)
        if link in saved:
            record(d, saved[link], "already_saved")
            stats["already_saved"] += 1
        else:
            new.append(d)
    # Raindrop shows the last-saved raindrop first, and ignores `order` on create: save the
    # lowest-ranked pick first so the best one ends up on top.
    new.sort(key=lambda d: -d.rank)
    bodies = [raindrop_for(d, collection, client.parse(link_for(d.candidate))) for d in new]
    created = {r["link"]: int(r["_id"]) for r in client.create(bodies)} if bodies else {}
    for d in new:
        rid = created.get(link_for(d.candidate))
        if rid is None:
            # Raindrop can normalize a link; find it by the URL we sent.
            rid = client.existing([link_for(d.candidate)]).get(link_for(d.candidate))
        if rid is None:
            raise PublishError(f"Raindrop did not return an id for {link_for(d.candidate)}")
        record(d, rid, "published")
        stats["published"] += 1
    log.info("published to raindrop", extra=stats | {"collection": cfg.collection})
    return stats


def client_from(settings: Settings) -> Raindrop:
    return Raindrop(settings.raindrop_token)
