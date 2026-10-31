"""Podcast collection, transcripts, Apple Podcasts links and discovery, against mocked HTTP."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

from content_curator import db, ingest, transcripts
from content_curator.apple import Apple
from content_curator.collectors.podcast import parse_duration
from content_curator.config import Selection, Source, Weights, load_interests
from content_curator.discover import discover_podcasts, expire_trials
from content_curator.scoring import item_prompt
from content_curator.select import Candidate, select

NOW = format_datetime(datetime.now(UTC) - timedelta(hours=3), usegmt=True)
OLD = format_datetime(datetime.now(UTC) - timedelta(days=60), usegmt=True)
NOTES = " ".join(f"note{i}" for i in range(80))

FEED = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>
<title>Agent Talk</title><link>https://agenttalk.test/</link>
<item><title>Ep 2: MCP in production</title><guid>ep-2</guid><link>https://agenttalk.test/2</link>
  <pubDate>{NOW}</pubDate><itunes:duration>01:02:03</itunes:duration>
  <description><![CDATA[<p>{NOTES}</p>]]></description>
  <enclosure url="https://cdn.test/2.mp3" type="audio/mpeg" length="1"/>
  <podcast:transcript url="https://agenttalk.test/2.html" type="text/html"/>
  <podcast:transcript url="https://agenttalk.test/2.vtt" type="text/vtt"/>
</item>
<item><title>Ep 1: no page</title><guid>ep-1</guid><link>https://cdn.test/1.mp3</link>
  <pubDate>{NOW}</pubDate><itunes:duration>1800</itunes:duration>
  <description>Short notes.</description>
  <enclosure url="https://cdn.test/1.mp3" type="audio/mpeg" length="1"/>
</item>
<item><title>Ep 0: old</title><guid>ep-0</guid><link>https://agenttalk.test/0</link>
  <pubDate>{OLD}</pubDate><description>Old notes.</description>
  <podcast:transcript url="https://agenttalk.test/0.vtt" type="text/vtt"/>
</item>
</channel></rss>"""

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
<v Host>Welcome to Agent Talk.

00:00:04.000 --> 00:00:09.500
<v Guest>Today we run MCP servers in production.
"""

SHOW_ID = 4242
LOOKUP = {"results": [
    {"wrapperType": "track", "collectionId": SHOW_ID},
    {"wrapperType": "podcastEpisode", "episodeGuid": "ep-2",
     "trackViewUrl": "https://podcasts.apple.com/us/podcast/x/id4242?i=2&uo=4"},
    {"wrapperType": "podcastEpisode", "episodeGuid": "ep-1",
     "trackViewUrl": "https://podcasts.apple.com/us/podcast/x/id4242?i=1&uo=4"},
]}
SEARCH = {"results": [
    {"collectionId": 1, "collectionName": "Other", "feedUrl": "https://other.test/feed", "genreIds": ["1318"]},
    {"collectionId": SHOW_ID, "collectionName": "Agent Talk", "feedUrl": "http://www.agenttalk.test/feed/",
     "genreIds": ["1318", "26"]},
]}

fetched = []


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    fetched.append(url)
    if url.startswith("https://agenttalk.test/feed"):
        return httpx.Response(200, text=FEED)
    if url == "https://agenttalk.test/2.vtt":
        return httpx.Response(200, text=VTT)
    if url.startswith("https://itunes.apple.com/search"):
        return httpx.Response(200, json=SEARCH)
    if url.startswith("https://itunes.apple.com/lookup"):
        return httpx.Response(200, json=LOOKUP)
    return httpx.Response(404)


SOURCE = Source(id="agent-talk", name="Agent Talk", type="podcast", url="https://agenttalk.test/feed")


def _collect(monkeypatch, conn, settings):
    fetched.clear()
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(ingest, "http_client", lambda s: client)
    monkeypatch.setattr(ingest, "_apple", Apple(client, min_interval=0))
    return ingest.collect(conn, settings, [SOURCE])


def test_parse_duration():
    assert parse_duration("3254") == 3254
    assert parse_duration("01:02:03") == 3723
    assert parse_duration("45:10") == 2710
    assert parse_duration("") is None and parse_duration("abc") is None


def test_transcript_formats():
    assert transcripts.to_text(VTT, "text/vtt") == "Welcome to Agent Talk. Today we run MCP servers in production."
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n2\n00:00:02,000 --> 00:00:03,000\nGeneral Kenobi.\n"
    assert transcripts.to_text(srt, "application/x-subrip") == "Hello there. General Kenobi."
    js = json.dumps({"version": "1.0", "segments": [{"body": "One"}, {"body": "two."}]})
    assert transcripts.to_text(js, "application/json") == "One two."


def test_collect_podcast(monkeypatch, conn, settings):
    stats = _collect(monkeypatch, conn, settings)
    items = {r["guid"]: r for r in conn.execute("SELECT * FROM items")}

    ep2, ep1, ep0 = items["ep-2"], items["ep-1"], items["ep-0"]
    assert ep2["media"] == "podcast" and ep2["duration_s"] == 3723
    assert ep2["transcript_type"] == "text/vtt"  # preferred over HTML
    assert ep2["extraction"] == "transcript" and "MCP servers in production" in ep2["content_text"]
    assert ep2["url"] == "https://agenttalk.test/2"

    # An item whose link is the audio file is keyed by GUID and shown with the show page.
    assert ep1["url"] == "https://agenttalk.test/" and ep1["normalized_url"] == "podcast://agent-talk/ep-1"
    assert ep1["extraction"] == "notes"

    # Old episodes are stored but nothing is fetched for them.
    assert ep0["extraction"] == "stale"
    assert "https://agenttalk.test/0.vtt" not in fetched
    assert not any(u.endswith(".mp3") for u in fetched)

    # Apple: show found by feed URL, episodes linked by GUID, tracking parameter dropped.
    assert conn.execute("SELECT apple_id FROM sources WHERE id='agent-talk'").fetchone()[0] == SHOW_ID
    assert ep2["listen_url"] == "https://podcasts.apple.com/us/podcast/x/id4242?i=2"
    assert stats["apple_links"] == 2 and stats["transcripts"] == 1


def test_prompt_labels_podcast(monkeypatch, conn, settings):
    _collect(monkeypatch, conn, settings)
    rows = {r["guid"]: r for r in conn.execute("SELECT * FROM items")}
    prompt, full = item_prompt(rows["ep-2"], "Agent Talk", 3000)
    assert full and "Podcast episode, 62 minutes" in prompt and "Show notes and transcript:" in prompt
    prompt, full = item_prompt(rows["ep-1"], "Agent Talk", 3000)
    assert not full and "Only the show notes are available:" in prompt


def cand(i, media="article", score=8, duration=None, trial=False):
    return Candidate(
        item_id=i, source_id=f"s{i}", source_name=f"s{i}", title=f"t{i}", url=f"https://x/{i}",
        published_at=None, word_count=1000, confidence="high", explanation="", topics=[], relevance=score,
        novelty=score, depth=score, source_quality=score, actionability=score, discovery_value=0,
        outside_interests=False, media=media, duration_s=duration, trial=trial,
    )


def test_media_cap_and_listening_time():
    w = Weights(relevance=1, novelty=0, depth=0, source_quality=0, actionability=0, discovery_value=0)
    cands = [cand(i, "podcast", 9, 3600) for i in range(3)] + [cand(9, "article", 7)]
    r = select(cands, Selection(daily_count=5, discovery_share=0, min_score=5, max_per_media={"podcast": 2}), w)
    assert [d.candidate.media for d in r.picks] == ["podcast", "podcast", "article"]
    assert r.picks[0].candidate.minutes == 60
    assert any("podcast cap" in d.reason for d in r.near_misses)


def search_handler(results):
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": results})
    return h


class FakeShowJudge:
    def __init__(self, fits):
        self.fits = fits
        self.calls = []

    def fit(self, show, episodes):
        from content_curator.discover import ShowFit

        self.calls.append(show["collectionName"])
        return ShowFit(fit=self.fits.get(show["collectionName"], 0), reason="test")


def test_discovery_filters_then_judges(conn, settings):
    interests = load_interests(settings.config_dir)
    interests.podcast_discovery.queries = ["agents"]
    interests.podcast_discovery.use_top_chart = False
    recent = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    stale = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    conn.execute("INSERT INTO sources(id, name, url, type, apple_id) VALUES ('known', 'K', 'https://k.test/feed', 'podcast', 7)")

    def show(i, name, released=recent, count=50, genres=("1318",)):
        return {"collectionId": i, "collectionName": name, "feedUrl": f"https://{i}.test/feed",
                "genreIds": list(genres), "releaseDate": released, "trackCount": count}

    results = [show(7, "Known"), show(8, "Dormant", released=stale), show(9, "Good Show"),
               show(10, "Not Tech", genres=("1303",)), show(11, "Tiny", count=3), show(12, "Meh Show"),
               show(13, "Flood Show")]
    flood = [{"wrapperType": "podcastEpisode", "releaseDate": recent} for _ in range(12)]

    def h(request: httpx.Request) -> httpx.Response:
        if "entity=podcastEpisode" in str(request.url) and "id=13" in str(request.url):
            return httpx.Response(200, json={"results": flood})
        if "entity=podcastEpisode" in str(request.url):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"results": results})

    apple = Apple(httpx.Client(transport=httpx.MockTransport(h)), min_interval=0)
    judge = FakeShowJudge({"Good Show": 8, "Meh Show": 4})
    stats = discover_podcasts(conn, settings, interests, apple=apple, judge=judge)

    assert judge.calls == ["Good Show", "Meh Show"]  # known, dormant, non-tech, tiny and flooding never judged
    assert [s["name"] for s in stats["added_shows"]] == ["Good Show"]
    row = conn.execute("SELECT * FROM sources WHERE apple_id=9").fetchone()
    assert row["discovery"] == "trial" and row["approved"] == 0 and row["enabled"] == 1

    # Trial sources survive syncing sources.yaml.
    ingest.sync_sources(conn, [])
    assert conn.execute("SELECT enabled FROM sources WHERE apple_id=9").fetchone()[0] == 1

    # Verdicts are remembered: the rejected show is not judged again next week.
    judge.calls.clear()
    discover_podcasts(conn, settings, interests, apple=apple, judge=judge)
    assert judge.calls == []


def test_trial_without_picks_expires(conn):
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO sources(id, name, url, type, enabled, approved, discovery, discovered_at) "
        "VALUES ('t', 'T', 'https://t.test', 'podcast', 1, 0, 'trial', ?)", (old,)
    )
    assert expire_trials(conn, 30) == 1
    assert conn.execute("SELECT enabled FROM sources WHERE id='t'").fetchone()[0] == 0


def test_migration_reaches_latest(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
