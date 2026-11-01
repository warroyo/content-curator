"""Publishing picks to Raindrop.io, into one collection ("AI picks" by default).

Raindrop's create endpoint accepts the same URL twice, so the `published` table is the guard
against double-publishing, and `import/url/exists` is a second one: a URL already in the account
(saved by hand, or by a run that died before recording it) is mapped to that raindrop, not saved
again. `pleaseParse` does nothing through the API (smoke test, 2026-11-01), so each pick is parsed
with `import/url/parse` for its type and cover; the title and note come from the curator.

The inbox collection ("To curate") works the other way: links saved there are pulled in by
`collect`, scored overnight like any item, and the same raindrop is moved into the picks collection
with the explanation added to its note. Moving, not copying, keeps your own note and highlights.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

import httpx

from content_curator.config import Publish, Settings
from content_curator.db import now
from content_curator.normalize import normalize_url
from content_curator.select import Candidate, Decision

log = logging.getLogger(__name__)

API = "https://api.raindrop.io/rest/v1"
# Batch create takes at most this many raindrops per request.
BATCH = 100
RETRIES = 3
PAGE = 50

DESCRIPTIONS = {
    "picks": "Nightly picks from content-curator. The note on each says why it was picked.",
    "inbox": "Save links here. content-curator scores them overnight and moves them to the picks with its take.",
}


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

    def collection_id(self, title: str, description: str = "", icon: str = "") -> int:
        """Id of the root collection with this title, created if missing: list view, description, icon."""
        for c in self._call("GET", "/collections").get("items", []):
            if c.get("title") == title:
                return int(c["_id"])
        body: dict = {"title": title, "view": "list", "description": description}
        cover = self.icon(icon) if icon else None
        if cover:
            body["cover"] = [cover]
        log.info("raindrop collection created", extra={"collection": title})
        return int(self._call("POST", "/collection", json=body)["item"]["_id"])

    def icon(self, query: str) -> str | None:
        """First icon in Raindrop's icon library for a word, as a PNG URL."""
        try:
            for group in self._call("GET", f"/collections/covers/{query}").get("items", []):
                for icon in group.get("icons", []):
                    if icon.get("png"):
                        return icon["png"]
        except PublishError:
            pass
        return None

    def raindrops(self, collection: int) -> list[dict]:
        out: list[dict] = []
        page = 0
        while True:
            items = self._call("GET", f"/raindrops/{collection}",
                               params={"perpage": PAGE, "page": page}).get("items", [])
            out += items
            if len(items) < PAGE:
                return out
            page += 1

    def update(self, raindrop_id: int, fields: dict) -> dict:
        return self._call("PUT", f"/raindrop/{raindrop_id}", json=fields).get("item", {})

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
    if d.bucket == "submitted":
        tags.append("ai-submitted")
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


def pull_inbox(conn: sqlite3.Connection, settings: Settings, cfg: Publish, client: Raindrop) -> dict:
    """Bring links saved to the inbox collection in as items, to be scored and moved into the picks."""
    from content_curator import ingest

    stats = {"inbox_new": 0}
    if not cfg.inbox_collection:
        return stats
    inbox = client.collection_id(cfg.inbox_collection, DESCRIPTIONS["inbox"], cfg.icon)
    known = {r[0] for r in conn.execute("SELECT raindrop_id FROM inbox")}
    for r in client.raindrops(inbox):
        rid = int(r["_id"])
        if rid in known:
            continue
        link = r["link"]
        title = r.get("title") if r.get("title") and r.get("title") != link else None
        # A link saved without a title (from a share sheet that sends only the URL) gets the page's own.
        title = title or client.parse(link).get("title") or None
        item_id = ingest.ingest_url(conn, settings, link, title)
        if item_id is None:
            row = conn.execute("SELECT id FROM items WHERE normalized_url=?", (normalize_url(link),)).fetchone()
            item_id = row[0]
        conn.execute(
            "INSERT INTO inbox(raindrop_id, item_id, link, note, tags, saved_at) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, item_id, link, r.get("note") or "", json.dumps(r.get("tags") or []), now()),
        )
        stats["inbox_new"] += 1
    if stats["inbox_new"]:
        log.info("inbox pulled", extra=stats)
    return stats


def publish(conn: sqlite3.Connection, run_id: int, picks: list[Decision], cfg: Publish,
            client: Raindrop) -> dict:
    """Save picks not yet published, move submitted ones from the inbox; record every raindrop id."""
    stats = {"published": 0, "already_published": 0, "already_saved": 0, "submitted": 0}
    done = {r[0] for r in conn.execute("SELECT item_id FROM published")}
    # A submitted link is always moved, even if the curator had already picked the same item.
    todo = []
    for d in picks:
        if d.candidate.item_id in done and d.bucket != "submitted":
            stats["already_published"] += 1
        else:
            todo.append(d)
    if not todo:
        return stats

    collection = client.collection_id(cfg.collection, DESCRIPTIONS["picks"], cfg.icon)

    def record(d: Decision, raindrop_id: int, status: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO published(item_id, raindrop_id, collection_id, link, status, run_id, "
            "published_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d.candidate.item_id, raindrop_id, collection, link_for(d.candidate), status, run_id, now()),
        )

    for d in [d for d in todo if d.bucket == "submitted"]:
        row = conn.execute(
            "SELECT raindrop_id, note, tags FROM inbox WHERE item_id=? AND status='pending' ORDER BY saved_at LIMIT 1",
            (d.candidate.item_id,),
        ).fetchone()
        if row is None:
            continue
        own_note = (row["note"] or "").strip()
        note = note_for(d.candidate) + (f"\n\nYour note: {own_note}" if own_note else "")
        tags = list(dict.fromkeys([*json.loads(row["tags"]), *tags_for(d)]))
        client.update(row["raindrop_id"], {"collection": {"$id": collection}, "note": note, "tags": tags,
                                           "title": d.candidate.title})
        conn.execute("UPDATE inbox SET status='published' WHERE item_id=?", (d.candidate.item_id,))
        record(d, row["raindrop_id"], "submitted")
        stats["submitted"] += 1
    todo = [d for d in todo if d.bucket != "submitted"]
    if not todo:
        return stats

    saved = client.existing([link_for(d.candidate) for d in todo])

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
