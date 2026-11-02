"""Publishing to Raindrop.io against a fake Raindrop API."""

from __future__ import annotations

import json

import httpx
import pytest

from content_curator import db, publish
from content_curator.config import Publish
from content_curator.select import Candidate, Decision


class FakeRaindrop:
    """Just enough of the Raindrop API: collections, exists, parse, batch create."""

    def __init__(self, saved: dict[str, int] | None = None):
        self.collections: dict[str, int] = {}
        self.collection_bodies: dict[str, dict] = {}
        self.inbox: list[dict] = []
        self.updates: dict[int, dict] = {}
        self.saved = dict(saved or {})
        self.created: list[dict] = []
        self.next_id = 1000
        self.fail_create = False
        self.auth: set[str] = set()
        self.tagged: list[dict] = []
        self.searches: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("Authorization", ""))
        path = request.url.path.removeprefix("/rest/v1")
        body = json.loads(request.content) if request.content else {}
        if path == "/collections":
            return httpx.Response(200, json={"items": [{"_id": i, "title": t} for t, i in self.collections.items()]})
        if path == "/collection":
            cid = 77 + len(self.collections)
            self.collections[body["title"]] = cid
            self.collection_bodies[body["title"]] = body
            return httpx.Response(200, json={"result": True, "item": {"_id": cid, "title": body["title"]}})
        if path.startswith("/collections/covers/"):
            return httpx.Response(200, json={"items": [{"title": "Icons", "icons": [{"png": "https://icon.test/x.png"}]}]})
        if path == "/raindrops/0" and request.url.params.get("search"):
            query = request.url.params["search"]
            self.searches.append(query)
            tag = query.removeprefix("#")
            hits = [r for r in self.tagged if any(t.startswith(tag) for t in r["tags"])]
            return httpx.Response(200, json={"items": hits if int(request.url.params.get("page", 0)) == 0 else []})
        if path.startswith("/raindrops/") and request.method == "GET":
            page = int(request.url.params.get("page", 0))
            return httpx.Response(200, json={"items": self.inbox if page == 0 else []})
        if path.startswith("/raindrop/") and request.method == "PUT":
            self.updates[int(path.rsplit("/", 1)[1])] = body
            return httpx.Response(200, json={"result": True, "item": body})
        if path == "/import/url/exists":
            dupes = [{"link": u, "_id": self.saved[u]} for u in body["urls"] if u in self.saved]
            return httpx.Response(200, json={"result": bool(dupes), "ids": [d["_id"] for d in dupes],
                                             "duplicates": dupes})
        if path == "/import/url/parse":
            url = request.url.params["url"]
            kind = "video" if "youtube" in url else "article"
            return httpx.Response(200, json={"item": {"type": kind, "cover": f"{url}/cover.jpg",
                                                      "excerpt": "page description"}})
        if path == "/raindrops" and request.method == "POST":
            if self.fail_create:
                return httpx.Response(500, text="boom")
            out = []
            for item in body["items"]:
                self.next_id += 1
                self.saved[item["link"]] = self.next_id
                self.created.append(item)
                out.append({**item, "_id": self.next_id})
            return httpx.Response(200, json={"result": True, "items": out})
        return httpx.Response(404)


def client_for(fake: FakeRaindrop) -> publish.Raindrop:
    return publish.Raindrop("secret", httpx.Client(transport=httpx.MockTransport(fake.handler)))


def pick(conn, i: int, bucket: str = "core", media: str = "article", url: str | None = None,
         listen_url: str | None = None) -> Decision:
    conn.execute("INSERT OR IGNORE INTO sources(id, name, url, type) VALUES ('s', 'Source', 'https://s.test/feed', 'rss')")
    url = url or f"https://s.test/{i}"
    conn.execute(
        "INSERT INTO items(id, source_id, url, normalized_url, title, fetched_at) VALUES (?, 's', ?, ?, ?, ?)",
        (i, url, url, f"Item {i}", db.now()),
    )
    c = Candidate(
        item_id=i, source_id="s", source_name="Source", title=f"Item {i}", url=url, published_at=None,
        word_count=1000, confidence="high", explanation=f"Why item {i}.", topics=["topic-ai"], relevance=8,
        novelty=8, depth=8, source_quality=8, actionability=8, discovery_value=3, outside_interests=False,
        media=media, listen_url=listen_url,
    )
    return Decision(c, True, bucket, "test", rank=i)


def run_id(conn) -> int:
    return db.Run(conn, "curate").id


def test_publishes_new_picks_into_collection(conn):
    fake = FakeRaindrop()
    picks = [pick(conn, 1), pick(conn, 2, bucket="discovery", url="https://youtube.com/watch?v=x", media="video"),
             pick(conn, 3, bucket="listen", media="podcast", listen_url="https://podcasts.apple.com/ep?i=3")]
    stats = publish.publish(conn, run_id(conn), picks, Publish(collection="AI picks"), client_for(fake))

    assert stats == {"published": 3, "already_published": 0, "already_saved": 0, "submitted": 0}
    assert fake.collections == {"AI picks": 77}
    assert fake.auth == {"Bearer secret"}
    by_link = {r["link"]: r for r in fake.created}
    article = by_link["https://s.test/1"]
    assert article["title"] == "Item 1" and article["note"] == "Why item 1."
    assert article["collection"] == {"$id": 77}
    assert article["tags"] == ["ai-curated", "media-article", "topic-ai"]
    assert article["type"] == "article" and article["excerpt"] == "page description"
    video = by_link["https://youtube.com/watch?v=x"]
    assert "excerpt" not in video and "ai-discovery" in video["tags"]
    # A podcast is saved as its Apple Podcasts link, tagged as the daily listen.
    assert "ai-listen" in by_link["https://podcasts.apple.com/ep?i=3"]["tags"]
    rows = conn.execute("SELECT item_id, status, collection_id FROM published ORDER BY item_id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "published", 77), (2, "published", 77), (3, "published", 77)]


def test_rerun_never_double_publishes(conn):
    fake = FakeRaindrop()
    picks = [pick(conn, 1)]
    publish.publish(conn, run_id(conn), picks, Publish(), client_for(fake))
    stats = publish.publish(conn, run_id(conn), picks, Publish(), client_for(fake))
    assert stats["already_published"] == 1 and stats["published"] == 0
    assert len(fake.created) == 1


def test_url_already_in_account_is_mapped_not_saved_again(conn):
    fake = FakeRaindrop(saved={"https://s.test/1": 555})
    stats = publish.publish(conn, run_id(conn), [pick(conn, 1)], Publish(), client_for(fake))
    assert stats["already_saved"] == 1 and fake.created == []
    row = conn.execute("SELECT raindrop_id, status FROM published WHERE item_id=1").fetchone()
    assert tuple(row) == (555, "already_saved")


def test_failed_create_records_nothing(conn):
    fake = FakeRaindrop()
    fake.fail_create = True
    with pytest.raises(publish.PublishError):
        publish.publish(conn, run_id(conn), [pick(conn, 1)], Publish(), client_for(fake))
    assert conn.execute("SELECT COUNT(*) FROM published").fetchone()[0] == 0


def test_missing_token_is_a_publish_error():
    with pytest.raises(publish.PublishError):
        publish.Raindrop("")


def test_note_carries_trial_and_other_coverage(conn):
    d = pick(conn, 1)
    d.candidate.trial = True
    d.candidate.also = [("Other", "Same story", "https://o.test/a")]
    note = publish.note_for(d.candidate)
    assert "a source on trial" in note and "follow" in note and "Other (https://o.test/a)" in note
    assert "ai-trial-source" in publish.tags_for(d)
    d.candidate.media = "podcast"
    assert "ai-trial-show" in publish.tags_for(d) and "a show on trial" in publish.note_for(d.candidate)


def test_follow_tags_follow_or_drop_the_source_then_come_off(conn):
    from content_curator import ingest
    from content_curator.config import Source

    conn.execute("INSERT INTO sources(id, name, url, type, approved, discovery, discovered_at) "
                 "VALUES ('t', 'Trial', 'https://t.test/feed', 'rss', 0, 'trial', ?)", (db.now(),))
    conn.execute("INSERT INTO sources(id, name, url, type) VALUES ('c', 'Configured', 'https://c.test/feed', 'rss')")
    for item, source, rid in ((1, "t", 501), (2, "c", 502)):
        url = f"https://{source}.test/{item}"
        conn.execute("INSERT INTO items(id, source_id, url, normalized_url, title, fetched_at) VALUES (?, ?, ?, ?, 'x', ?)",
                     (item, source, url, url, db.now()))
        conn.execute("INSERT INTO published(item_id, raindrop_id, collection_id, link, status, published_at) "
                     "VALUES (?, ?, 77, 'l', 'published', ?)", (item, rid, db.now()))
    fake = FakeRaindrop()
    fake.tagged = [{"_id": 501, "tags": ["ai-curated", "follow"]},
                   {"_id": 502, "tags": ["unfollow"]},
                   {"_id": 999, "tags": ["follow"]},          # not a curator pick: left alone
                   {"_id": 503, "tags": ["followup"]}]        # prefix match from search: ignored

    stats = publish.apply_follow_tags(conn, Publish(), client_for(fake))

    assert stats == {"followed": 1, "unfollowed": 1, "follow_tag_unknown": 1}
    trial = conn.execute("SELECT discovery, approved, enabled FROM sources WHERE id='t'").fetchone()
    assert tuple(trial) == ("followed", 1, 1)
    assert fake.updates == {501: {"tags": ["ai-curated"]}, 502: {"tags": []}}
    assert fake.searches == ["#follow", "#unfollow"]

    # The followed trial survives a sync without it in sources.yaml; the unfollowed source stays off
    # even though sources.yaml still lists it.
    ingest.sync_sources(conn, [Source(id="c", name="Configured", url="https://c.test/feed")])
    assert dict(conn.execute("SELECT id, enabled FROM sources").fetchall()) == {"t": 1, "c": 0}


def test_saved_lowest_rank_first_so_best_is_on_top(conn):
    fake = FakeRaindrop()
    publish.publish(conn, run_id(conn), [pick(conn, 1), pick(conn, 2), pick(conn, 3)], Publish(), client_for(fake))
    assert [r["title"] for r in fake.created] == ["Item 3", "Item 2", "Item 1"]


def test_excerpt_ends_at_a_sentence():
    text = "First sentence here. " * 30
    out = publish.sentences(text)
    assert len(out) <= 300 and out.endswith(".")
    assert publish.sentences("word " * 100).endswith("…")


def test_collection_created_with_view_description_and_icon(conn):
    fake = FakeRaindrop()
    publish.publish(conn, run_id(conn), [pick(conn, 1)], Publish(), client_for(fake))
    body = fake.collection_bodies["AI picks"]
    assert body["view"] == "list" and body["description"] and body["cover"] == ["https://icon.test/x.png"]


def test_inbox_link_is_pulled_then_moved_into_picks(conn, settings, monkeypatch):
    from content_curator import ingest

    def fake_ingest(conn, settings, url, title=None):
        conn.execute("INSERT OR IGNORE INTO sources(id, name, url, type) VALUES ('manual', 'Manual', '', 'manual')")
        return conn.execute(
            "INSERT INTO items(source_id, url, normalized_url, title, fetched_at) VALUES ('manual', ?, ?, ?, ?)",
            (url, url, title or url, db.now())).lastrowid

    monkeypatch.setattr(ingest, "ingest_url", fake_ingest)
    fake = FakeRaindrop()
    fake.inbox = [{"_id": 9001, "link": "https://mine.test/a", "title": "Mine", "note": "read this", "tags": ["k8s"]}]
    cfg = Publish()
    assert publish.pull_inbox(conn, settings, cfg, client_for(fake)) == {"inbox_new": 1}
    # A second pull does not add it again.
    assert publish.pull_inbox(conn, settings, cfg, client_for(fake)) == {"inbox_new": 0}
    assert fake.collection_bodies["To curate"]["description"]

    item_id = conn.execute("SELECT item_id FROM inbox WHERE raindrop_id=9001").fetchone()[0]
    d = Decision(Candidate(
        item_id=item_id, source_id="manual", source_name="Manual", title="Mine", url="https://mine.test/a",
        published_at=None, word_count=500, confidence="high", explanation="Curator's take.", topics=["topic-kubernetes"],
        relevance=5, novelty=5, depth=5, source_quality=5, actionability=5, discovery_value=5,
        outside_interests=False), True, "submitted", "you saved it to the inbox", rank=1)
    stats = publish.publish(conn, run_id(conn), [d], cfg, client_for(fake))

    assert stats["submitted"] == 1 and fake.created == []
    moved = fake.updates[9001]
    assert moved["collection"] == {"$id": fake.collections["AI picks"]}
    assert moved["note"].startswith("Curator's take.") and "Your note: read this" in moved["note"]
    assert moved["tags"][0] == "k8s" and "ai-submitted" in moved["tags"]
    assert conn.execute("SELECT status FROM inbox WHERE raindrop_id=9001").fetchone()[0] == "published"
