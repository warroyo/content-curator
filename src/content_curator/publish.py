"""Publishing picks to Raindrop.io, into one collection ("AI picks" by default).

Raindrop's create endpoint accepts the same URL twice, so the `published` table is the guard
against double-publishing, and `import/url/exists` is a second one: a URL already in the account
(saved by hand, or by a run that died before recording it) is mapped to that raindrop, not saved
again. `pleaseParse` does nothing through the API (smoke test, 2026-11-01), so each pick is parsed
with `import/url/parse` for its type and cover; the title and note come from the curator.

The inbox collection ("To curate") works the other way: links saved there are pulled in by
`collect`, scored overnight like any item, and the same raindrop is moved into the picks collection
with the explanation added to its note. Moving, not copying, keeps your own note and highlights.

Follow and unfollow are tags too: `follow` or `unfollow` on a pick, anywhere in the account, follows
or drops the source it came from on the next `collect`, and the curator removes the tag.

Raindrop has no read state, so feedback is tags and deletions as well: `read` or `meh` on a pick is
recorded, the tag comes off, and a pick still in the picks collection moves to "AI read" or
"AI skipped", leaving the picks collection as the unread queue. A deleted pick is recorded from the
Trash. Only picks you tagged or deleted are touched.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta

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
TRASH = -99
# Favourites are looked for on picks published this recently.
FAVOURITE_DAYS = 90

DESCRIPTIONS = {
    "picks": "Nightly picks from content-curator. The note on each says why it was picked.",
    "inbox": "Save links here. content-curator scores them overnight and moves them to the picks with its take.",
    "read": "Picks you tagged read. content-curator moves them here from the picks.",
    "meh": "Picks you tagged meh: less like this. content-curator moves them here from the picks.",
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

    def raindrops(self, collection: int, newer_than: str | None = None) -> list[dict]:
        """A collection's raindrops; with `newer_than` (ISO time), newest first down to that time."""
        out: list[dict] = []
        page = 0
        params: dict = {"perpage": PAGE}
        if newer_than:
            params["sort"] = "-created"
        while True:
            items = self._call("GET", f"/raindrops/{collection}", params={**params, "page": page}).get("items", [])
            if newer_than:
                fresh = [r for r in items if (r.get("created") or "") >= newer_than]
                out += fresh
                if len(fresh) < len(items):
                    return out
            else:
                out += items
            if len(items) < PAGE:
                return out
            page += 1

    def find_collection(self, title: str) -> int | None:
        """Id of the root collection with this title, or None; never creates one."""
        for c in self._call("GET", "/collections").get("items", []):
            if c.get("title") == title:
                return int(c["_id"])
        return None

    def tagged(self, tag: str) -> list[dict]:
        """Every raindrop in the account carrying this tag (collection 0 is all of them)."""
        out: list[dict] = []
        page = 0
        while True:
            items = self._call("GET", "/raindrops/0",
                               params={"search": f"#{tag}", "perpage": PAGE, "page": page}).get("items", [])
            # Raindrop's search matches tag prefixes too; keep exact matches only.
            out += [r for r in items if tag in (r.get("tags") or [])]
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
        tags.append("ai-trial-show" if c.media == "podcast" else "ai-trial-source")
    return list(dict.fromkeys(tags))


def note_for(c: Candidate) -> str:
    """The why-this-item explanation, shown on the card under the title."""
    parts = [c.explanation.strip()]
    if c.trial:
        kind = "a show" if c.media == "podcast" else "a source"
        parts.append(f"From {c.source_name}, {kind} on trial that you do not follow yet. "
                     f"Tag this follow to keep it, or unfollow to drop it.")
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


def apply_follow_tags(conn: sqlite3.Connection, cfg: Publish, client: Raindrop) -> dict:
    """Follow or unfollow the source of every pick tagged `follow_tag` / `unfollow_tag`, then drop the tag.

    A tagged raindrop the curator did not publish (or one from a hand-submitted link, which has no
    source) keeps its tag and is counted as unknown.
    """
    from content_curator.registry import set_follow

    stats = {"followed": 0, "unfollowed": 0, "follow_tag_unknown": 0}
    for tag, state in ((cfg.follow_tag, "followed"), (cfg.unfollow_tag, "unfollowed")):
        if not tag:
            continue
        for r in client.tagged(tag):
            rid = int(r["_id"])
            row = conn.execute(
                "SELECT i.source_id FROM published p JOIN items i ON i.id = p.item_id WHERE p.raindrop_id = ?",
                (rid,),
            ).fetchone()
            if row is None or not set_follow(conn, row["source_id"], state, via="raindrop"):
                stats["follow_tag_unknown"] += 1
                continue
            client.update(rid, {"tags": [t for t in r.get("tags") or [] if t != tag]})
            stats[state] += 1
    if stats["followed"] or stats["unfollowed"]:
        log.info("follow tags applied", extra=stats)
    return stats


def apply_feedback(conn: sqlite3.Connection, cfg: Publish, client: Raindrop) -> dict:
    """Record picks tagged `read_tag` or `meh_tag`, and picks deleted to the Trash.

    A tagged pick loses the tag and, if it is still in the collection it was published to, moves to
    `read_collection` or `skipped_collection`; one you moved elsewhere stays where you put it. A
    tagged raindrop the curator did not publish keeps its tag and is counted as unknown. A deletion
    is seen only while the pick is in the Trash, so emptying the Trash before the next run loses it.
    """
    stats = {"read": 0, "meh": 0, "deleted": 0, "feedback_unknown": 0}
    for tag, kind, collection in ((cfg.read_tag, "read", cfg.read_collection),
                                  (cfg.meh_tag, "meh", cfg.skipped_collection)):
        if not tag:
            continue
        target: int | None = None
        for r in client.tagged(tag):
            rid = int(r["_id"])
            row = conn.execute("SELECT item_id, collection_id FROM published WHERE raindrop_id = ?", (rid,)).fetchone()
            if row is None:
                stats["feedback_unknown"] += 1
                continue
            fields: dict = {"tags": [t for t in r.get("tags") or [] if t != tag]}
            if collection and (r.get("collection") or {}).get("$id") == row["collection_id"]:
                if target is None:
                    target = client.collection_id(collection, DESCRIPTIONS[kind], cfg.icon)
                fields["collection"] = {"$id": target}
            client.update(rid, fields)
            _record_feedback(conn, row["item_id"], kind, rid)
            stats[kind] += 1

    published = dict(conn.execute("SELECT raindrop_id, item_id FROM published").fetchall())
    if published:
        for r in client.raindrops(TRASH):
            rid = int(r["_id"])
            if rid in published and _record_feedback(conn, published[rid], "deleted", rid):
                stats["deleted"] += 1
    if any(stats.values()):
        log.info("feedback applied", extra=stats)
    return stats


def sync_favourites(conn: sqlite3.Connection, cfg: Publish, client: Raindrop) -> dict:
    """Record picks you favourited (Raindrop's `important`), and forget ones you unfavourited.

    Reads the curator's own collections (picks, read, skipped), newest first, back to
    FAVOURITE_DAYS: a favourite on a pick you moved to a collection of your own is not seen.
    """
    published = dict(conn.execute("SELECT raindrop_id, item_id FROM published").fetchall())
    stats = {"favourite": 0, "unfavourited": 0}
    if not published:
        return stats
    since = (datetime.now(UTC) - timedelta(days=FAVOURITE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    for title in dict.fromkeys(t for t in (cfg.collection, cfg.read_collection, cfg.skipped_collection) if t):
        collection = client.find_collection(title)
        if collection is None:
            continue
        for r in client.raindrops(collection, newer_than=since):
            rid = int(r["_id"])
            if rid not in published:
                continue
            if r.get("important"):
                stats["favourite"] += _record_feedback(conn, published[rid], "favourite", rid)
            else:
                stats["unfavourited"] += conn.execute(
                    "DELETE FROM feedback WHERE item_id = ? AND kind = 'favourite'", (published[rid],)).rowcount
    if any(stats.values()):
        log.info("favourites synced", extra=stats)
    return stats


def _record_feedback(conn: sqlite3.Connection, item_id: int, kind: str, raindrop_id: int) -> bool:
    """Store one piece of feedback; False if it was already recorded."""
    return conn.execute(
        "INSERT OR IGNORE INTO feedback(item_id, kind, raindrop_id, recorded_at) VALUES (?, ?, ?, ?)",
        (item_id, kind, raindrop_id, now()),
    ).rowcount == 1


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
