"""Feed discovery: suggested feeds checked, judged and run as trials; trial expiry; the sources API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from content_curator import ingest, registry
from content_curator.config import Source, load_interests
from content_curator.discover import Rejected, ShowFit, check_feed, discover_feeds, expire_trials
from content_curator.registry import CandidateIn, add_candidates
from content_curator.service import create_app


def rss(name: str, days_ago: list[float], audio: bool = False) -> str:
    items = []
    for i, d in enumerate(days_ago):
        when = format_datetime(datetime.now(UTC) - timedelta(days=d), usegmt=True)
        enc = '<enclosure url="https://cdn.test/a.mp3" type="audio/mpeg" length="1"/>' if audio else ""
        items.append(f"<item><title>{name} post {i}</title><link>https://{name}.test/{i}</link>"
                     f"<pubDate>{when}</pubDate><description>About {name} {i}</description>{enc}</item>")
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>{name} blog</title>'
            f"<link>https://{name}.test/</link>{''.join(items)}</channel></rss>")


PAGE = """<html><head><title>Good</title>
<link rel="alternate" type="application/rss+xml" title="Feed" href="/feed.xml"></head><body>hi</body></html>"""

FEEDS = {
    "https://good.test/": (200, PAGE, "text/html"),
    "https://good.test/feed.xml": (200, rss("good", [1, 8, 20]), "application/rss+xml"),
    "https://meh.test/feed": (200, rss("meh", [2, 9]), "application/rss+xml"),
    "https://second.test/feed": (200, rss("second", [3, 10]), "application/rss+xml"),
    "https://stale.test/feed": (200, rss("stale", [200, 300]), "application/rss+xml"),
    "https://flood.test/feed": (200, rss("flood", [0.1 * i for i in range(30)]), "application/rss+xml"),
    "https://nofeed.test/": (200, "<html><head></head><body>no feed</body></html>", "text/html"),
    "https://show.test/feed": (200, rss("show", [1, 8], audio=True), "application/rss+xml"),
    # A page that never advertises its feed, which lives at a usual path.
    "https://plain.test/": (200, "<html><head></head><body>essays</body></html>", "text/html"),
    "https://plain.test/atom.xml": (200, rss("plain", [5]), "application/rss+xml"),
}
fetches: list[str] = []


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    fetches.append(url)
    if url == "https://down.test/feed":
        raise httpx.ConnectError("refused")
    if url == "https://blocked.test/":
        return httpx.Response(403)
    if url == "https://busy.test/":
        return httpx.Response(429)
    if url not in FEEDS:
        return httpx.Response(404)
    status, body, ctype = FEEDS[url]
    return httpx.Response(status, text=body, headers={"content-type": ctype})


def client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class FakeFeedJudge:
    def __init__(self, fits: dict[str, int]):
        self.fits = fits
        self.calls: list[str] = []

    def fit(self, name, check, why=""):
        self.calls.append(check.feed_url)
        return ShowFit(fit=self.fits.get(check.feed_url, 0), reason=f"judged {name}")


def test_check_feed_follows_the_page_link_and_detects_podcasts():
    check = check_feed(client(), "https://good.test/", 60, 10)
    assert check.feed_url == "https://good.test/feed.xml" and check.type == "rss" and check.title == "good blog"
    assert [t for _, t, _ in check.recent] == ["good post 0", "good post 1", "good post 2"]
    assert check_feed(client(), "https://show.test/feed", 60, 10).type == "podcast"
    assert check_feed(client(), "https://plain.test/", 60, 10).feed_url == "https://plain.test/atom.xml"


@pytest.mark.parametrize("url, why", [
    ("https://github.com/hashicorp/terraform/releases.atom", "GitHub release"),
    ("https://github.com/argoproj/argo-cd/tags.atom", "GitHub release"),
    ("https://stale.test/feed", "over 60 days ago"),
    ("https://flood.test/feed", "entries in the last week"),
    ("https://nofeed.test/", "no feed found"),
])
def test_check_feed_rejects(url, why):
    with pytest.raises(Rejected, match=why):
        check_feed(client(), url, 60, 10)


def test_release_feeds_are_rejected_without_a_fetch():
    fetches.clear()
    with pytest.raises(Rejected):
        check_feed(client(), "https://github.com/vllm-project/vllm/releases.atom", 60, 10)
    assert fetches == []


def test_add_candidates_skips_known_and_queued(conn):
    conn.execute("INSERT INTO sources(id, name, url, type) VALUES ('k', 'Known', 'https://known.test/feed', 'rss')")
    stats = add_candidates(conn, [CandidateIn(url="https://known.test/feed"), CandidateIn(url="https://good.test/"),
                                  CandidateIn(url="https://good.test")], "hermes")
    assert stats["added"] == 1
    assert [s["why"] for s in stats["skipped"]] == ["already a source", "already pending"]
    row = conn.execute("SELECT submitted_by, status FROM candidates").fetchone()
    assert tuple(row) == ("hermes", "pending")


def test_discover_feeds_end_to_end(conn, settings):
    interests = load_interests(settings.config_dir)
    cfg = interests.feed_discovery
    cfg.max_new_per_run, cfg.max_active_trials = 5, 2
    # One trial already running: room for one more.
    conn.execute("INSERT INTO sources(id, name, url, type, approved, discovery, discovered_at) "
                 "VALUES ('trial-old', 'Old', 'https://old.test/feed', 'podcast', 0, 'trial', ?)",
                 (datetime.now(UTC).isoformat(timespec="seconds"),))
    urls = ["https://good.test/", "https://meh.test/feed", "https://second.test/feed", "https://stale.test/feed",
            "https://github.com/x/y/releases.atom", "https://down.test/feed"]
    add_candidates(conn, [CandidateIn(url=u, name=u.split("/")[2].split(".")[0].title(), topics=["topic-ai"],
                                      why="test") for u in urls], "hermes")
    judge = FakeFeedJudge({"https://good.test/feed.xml": 9, "https://second.test/feed": 8, "https://meh.test/feed": 4})

    stats = discover_feeds(conn, settings, interests, client=client(), judge=judge)

    assert stats["checked"] == 6 and stats["judged"] == 3 and stats["added"] == 1 and stats["waiting"] == 1
    assert [f["id"] for f in stats["added_feeds"]] == ["trial-good"]
    src = conn.execute("SELECT * FROM sources WHERE id='trial-good'").fetchone()
    assert src["url"] == "https://good.test/feed.xml" and src["discovery"] == "trial" and src["approved"] == 0
    assert src["topics"] == '["topic-ai"]' and src["discovery_query"] == "suggested by hermes"
    status = dict(conn.execute("SELECT submitted_url, status FROM candidates").fetchall())
    assert status == {"https://good.test/": "trial", "https://second.test/feed": "pending",
                      "https://meh.test/feed": "rejected", "https://stale.test/feed": "rejected",
                      "https://github.com/x/y/releases.atom": "rejected", "https://down.test/feed": "pending"}
    assert conn.execute("SELECT attempts FROM candidates WHERE submitted_url='https://down.test/feed'").fetchone()[0] == 1

    # Next run: the waiting feed keeps its verdict (no second judgement) and starts once a slot frees up.
    conn.execute("UPDATE sources SET enabled=0 WHERE id='trial-old'")
    judge.calls.clear()
    stats = discover_feeds(conn, settings, interests, client=client(), judge=judge)
    assert judge.calls == [] and [f["id"] for f in stats["added_feeds"]] == ["trial-second"]

    # A rejected feed suggested again within 90 days is not queued.
    again = add_candidates(conn, [CandidateIn(url="https://meh.test/feed")], "hermes")
    assert again["added"] == 0 and again["skipped"][0]["why"] == "rejected in the last 90 days"


def test_unreachable_feed_is_rejected_after_three_tries(conn, settings):
    interests = load_interests(settings.config_dir)
    add_candidates(conn, [CandidateIn(url="https://down.test/feed")])
    for _ in range(3):
        discover_feeds(conn, settings, interests, client=client(), judge=FakeFeedJudge({}))
    row = conn.execute("SELECT status, reason FROM candidates").fetchone()
    assert row["status"] == "rejected" and row["reason"].startswith("fetch failed 3 times")


def test_client_errors_reject_at_once_but_rate_limits_retry(conn, settings):
    interests = load_interests(settings.config_dir)
    add_candidates(conn, [CandidateIn(url="https://blocked.test/"), CandidateIn(url="https://busy.test/")])
    discover_feeds(conn, settings, interests, client=client(), judge=FakeFeedJudge({}))
    rows = {r["submitted_url"]: (r["status"], r["reason"]) for r in conn.execute("SELECT * FROM candidates")}
    assert rows["https://blocked.test/"] == ("rejected", "HTTP 403 from https://blocked.test/")
    assert rows["https://busy.test/"] == ("pending", "fetch failed: HTTP 429")


def test_trials_end_after_trial_days_picked_or_not(conn):
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
    new = datetime.now(UTC).isoformat(timespec="seconds")
    for sid, kind, when in (("picked", "rss", old), ("unpicked", "rss", old), ("young", "rss", new),
                            ("show", "podcast", old)):
        conn.execute("INSERT INTO sources(id, name, url, type, approved, discovery, discovered_at) "
                     "VALUES (?, ?, ?, ?, 0, 'trial', ?)", (sid, sid, f"https://{sid}.test", kind, when))
    conn.execute("INSERT INTO items(id, source_id, url, normalized_url, title, fetched_at) "
                 "VALUES (1, 'picked', 'u', 'u', 't', ?)", (new,))
    run = conn.execute("INSERT INTO runs(kind, started_at, status) VALUES ('curate', ?, 'ok')", (new,)).lastrowid
    conn.execute("INSERT INTO selections VALUES (?, 1, 1, 1, 'core', 8, 'x', 0)", (run,))

    assert expire_trials(conn, 30, "rss") == 2
    enabled = dict(conn.execute("SELECT id, enabled FROM sources").fetchall())
    assert enabled == {"picked": 0, "unpicked": 0, "young": 1, "show": 1}

    # Following an ended trial brings it back as a permanent source.
    assert registry.set_follow(conn, "picked", "followed", via="test")
    row = conn.execute("SELECT discovery, enabled FROM sources WHERE id='picked'").fetchone()
    assert tuple(row) == ("followed", 1)


def test_sources_file_takes_over_a_discovered_source_by_url(conn):
    conn.execute("INSERT INTO sources(id, name, url, type, approved, discovery) "
                 "VALUES ('trial-good', 'Good', 'https://good.test/feed.xml', 'rss', 1, 'followed')")
    ingest.sync_sources(conn, [Source(id="good", name="Good", url="https://good.test/feed.xml")])
    assert dict(conn.execute("SELECT id, enabled FROM sources").fetchall()) == {"trial-good": 0, "good": 1}


def test_candidates_and_sources_api(conn, settings):
    conn.execute("INSERT INTO sources(id, name, url, type) VALUES ('c', 'Configured', 'https://c.test/feed', 'rss')")
    conn.execute("INSERT INTO sources(id, name, url, type, approved, discovery, discovered_at) "
                 "VALUES ('t', 'Trial', 'https://t.test/feed', 'rss', 0, 'trial', ?)", (datetime.now(UTC).isoformat(),))
    yaml_body = """submitted_by: hermes
candidates:
  - {url: "https://good.test/", name: Good, topics: [topic-ai], why: deep posts}
  - {url: "https://c.test/feed"}
"""
    with TestClient(create_app(settings, scheduler_enabled=False)) as api:
        r = api.post("/api/candidates", content=yaml_body, headers={"content-type": "application/yaml"})
        assert r.status_code == 200 and r.json()["added"] == 1
        r = api.post("/api/candidates?by=script", json=[{"url": "https://second.test/feed"}])
        assert r.status_code == 200 and r.json()["added"] == 1
        assert api.post("/api/candidates", content="just text").status_code == 400
        assert api.post("/api/candidates", json=[{"name": "no url"}]).status_code == 422

        pending = api.get("/api/candidates", params={"status": "pending"}).json()
        assert {(c["submitted_url"], c["submitted_by"]) for c in pending} == {
            ("https://good.test/", "hermes"), ("https://second.test/feed", "script")}

        assert api.post("/api/sources/t/follow").json() == {"source": "t", "state": "followed"}
        assert api.post("/api/sources/c/unfollow").status_code == 200
        assert api.post("/api/sources/nope/follow").status_code == 404
        assert api.post("/api/sources/c/delete").status_code == 404
        statuses = {s["id"]: s["status"] for s in api.get("/api/sources").json()}
        assert statuses == {"c": "unfollowed", "t": "followed"}
