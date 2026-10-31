"""Same-story grouping with fake embeddings and a fake judge."""

from __future__ import annotations

import math

import httpx
import numpy as np
import pytest

from content_curator import db
from content_curator.config import Selection, Weights, load_interests
from content_curator.scoring import ModelUnavailable
from content_curator.select import Candidate, collapse_stories, select
from content_curator.stories import Embedder, SameStory, run_stories


def angle(deg: float) -> np.ndarray:
    return np.array([math.cos(math.radians(deg)), math.sin(math.radians(deg)), 0.0], dtype=np.float32)


class FakeEmbedder(Embedder):
    def __init__(self, vectors: dict[str, np.ndarray]):
        self.vectors = vectors
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [self.vectors[t.split("\n", 1)[0]] for t in texts]


class FakeJudge:
    def __init__(self, answer: bool):
        self.answer = answer
        self.calls = []

    def same(self, a, b):
        self.calls.append((a["title"], b["title"]))
        return SameStory(same_story=self.answer, reason="test")


def seed(conn, titles):
    conn.execute("INSERT OR IGNORE INTO sources(id, name, url, type) VALUES ('s', 'S', 'https://s.test', 'rss')")
    for t in titles:
        conn.execute(
            "INSERT INTO items(source_id, url, normalized_url, title, fetched_at, published_at, content_text, extraction)"
            " VALUES ('s', ?, ?, ?, ?, ?, 'body text', 'page')",
            (f"https://s.test/{t}", f"https://s.test/{t}", t, db.now(), db.now()),
        )


def stories_by_title(conn):
    return {r["title"]: r["story_id"] for r in conn.execute("SELECT title, story_id FROM items")}


@pytest.fixture
def interests(settings):
    i = load_interests(settings.config_dir)
    i.stories.same_story, i.stories.borderline = 0.85, 0.75
    return i


def test_close_items_grouped_distant_left_alone(conn, settings, interests):
    seed(conn, ["k8s-a", "k8s-b", "other"])
    emb = FakeEmbedder({"k8s-a": angle(0), "k8s-b": angle(10), "other": angle(80)})  # cos 10° = 0.985
    stats = run_stories(conn, settings, interests, embedder=emb, judge=FakeJudge(True))
    s = stories_by_title(conn)
    assert s["k8s-a"] == s["k8s-b"] is not None and s["other"] is None
    assert stats["stories"] == 1 and stats["embedded"] == 3

    run_stories(conn, settings, interests, embedder=emb, judge=FakeJudge(True))
    assert emb.calls == 1  # second run uses stored embeddings


def test_borderline_pair_goes_to_judge_once(conn, settings, interests):
    seed(conn, ["a", "b"])
    emb = FakeEmbedder({"a": angle(0), "b": angle(37)})  # cos 37° = 0.80, borderline
    judge = FakeJudge(True)
    run_stories(conn, settings, interests, embedder=emb, judge=judge)
    assert len(judge.calls) == 1
    assert stories_by_title(conn)["a"] == stories_by_title(conn)["b"]

    run_stories(conn, settings, interests, embedder=emb, judge=judge)
    assert len(judge.calls) == 1  # verdict cached


def test_judge_can_keep_borderline_apart(conn, settings, interests):
    seed(conn, ["a", "b"])
    emb = FakeEmbedder({"a": angle(0), "b": angle(37)})
    run_stories(conn, settings, interests, embedder=emb, judge=FakeJudge(False))
    assert set(stories_by_title(conn).values()) == {None}


def test_no_chaining(conn, settings, interests):
    # a~b and b~c are 0.906, but a~c is 0.643: c must not join a and b.
    seed(conn, ["a", "b", "c"])
    emb = FakeEmbedder({"a": angle(0), "b": angle(25), "c": angle(50)})
    run_stories(conn, settings, interests, embedder=emb, judge=FakeJudge(True))
    s = stories_by_title(conn)
    assert s["a"] == s["b"] is not None and s["c"] is None


def test_embedding_model_down_is_not_fatal(conn, settings, interests):
    seed(conn, ["a"])

    class Down(Embedder):
        def embed(self, texts):
            raise ModelUnavailable("connection refused")

    stats = run_stories(conn, settings, interests, embedder=Down(settings), judge=FakeJudge(True))
    assert "model_unavailable" in stats


def test_real_embedder_normalizes(settings):
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 4]}]})

    e = Embedder(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    v = e.embed(["x", "y"])
    assert np.allclose(v[0], [0.6, 0.8]) and np.allclose(v[1], [0, 1])


def cand(i, story, score, source="s"):
    return Candidate(
        item_id=i, source_id=f"{source}{i}", source_name=f"{source}{i}", title=f"t{i}", url=f"https://x/{i}",
        published_at=None, word_count=1000, confidence="high", explanation="", topics=[], relevance=score,
        novelty=score, depth=score, source_quality=score, actionability=score, discovery_value=0,
        outside_interests=False, story_id=story, overall=score,
    )


def test_collapse_keeps_best_scored_item_per_story():
    out = collapse_stories([cand(1, 1, 6), cand(2, 1, 8), cand(3, None, 7)])
    by_id = {c.item_id: c for c in out}
    assert set(by_id) == {2, 3}
    assert [url for _, _, url in by_id[2].also] == ["https://x/1"]


def test_select_picks_one_per_story():
    w = Weights(relevance=1, novelty=0, depth=0, source_quality=0, actionability=0, discovery_value=0)
    r = select([cand(1, 1, 9), cand(2, 1, 8.5), cand(3, None, 7)], Selection(daily_count=5, discovery_share=0, min_score=5), w)
    assert [d.candidate.item_id for d in r.picks] == [1, 3]
    assert r.picks[0].candidate.also
