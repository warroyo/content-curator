"""Scoring against a mocked OpenAI-compatible endpoint."""

from __future__ import annotations

import json

import httpx
import pytest

from content_curator import db
from content_curator.config import load_interests
from content_curator.normalize import word_count
from content_curator.scoring import ModelUnavailable, Scorer, score_pending

GOOD = {
    "relevance": 8, "novelty": 7, "depth": 6, "source_quality": 8, "actionability": 5, "discovery_value": 2,
    "outside_interests": False, "topics": ["topic-kubernetes"], "explanation": "Primary release notes.",
}


def _seed(conn, n=2):
    conn.execute("INSERT INTO sources(id, name, url, type) VALUES ('s', 'S', 'https://s.test/feed', 'rss')")
    text = " ".join(f"w{i}" for i in range(400))
    for i in range(n):
        conn.execute(
            "INSERT INTO items(source_id, url, normalized_url, title, fetched_at, published_at, content_text,"
            " word_count, extraction) VALUES ('s', ?, ?, ?, ?, ?, ?, ?, 'page')",
            (f"https://s.test/{i}", f"https://s.test/{i}", f"Item {i}", db.now(), db.now(), text, word_count(text)),
        )


def _scorer(settings, responses):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        r = responses.pop(0) if len(responses) > 1 else responses[0]
        return r() if callable(r) else r

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Scorer(settings, load_interests(settings.config_dir), client=client), calls


def _ok(payload=GOOD):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 80},
    })


def test_scores_and_caches(conn, settings):
    _seed(conn)
    interests = load_interests(settings.config_dir)
    scorer, calls = _scorer(settings, [_ok()])
    stats = score_pending(conn, settings, interests, scorer=scorer)
    assert stats["scored"] == 2 and stats["prompt_tokens"] == 2000
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[0]["model"] == "test-model"
    row = conn.execute("SELECT * FROM scores LIMIT 1").fetchone()
    assert row["confidence"] == "high" and json.loads(row["topics"]) == ["topic-kubernetes"]

    stats = score_pending(conn, settings, interests, scorer=scorer)
    assert stats["scored"] == 0 and stats["cached"] == 2
    assert len(calls) == 2


def test_invalid_output_retried(conn, settings):
    _seed(conn, n=1)
    bad = httpx.Response(200, json={"choices": [{"message": {"content": '{"relevance": 99}'}}]})
    scorer, calls = _scorer(settings, [bad, _ok()])
    stats = score_pending(conn, settings, load_interests(settings.config_dir), scorer=scorer)
    assert stats["scored"] == 1 and len(calls) == 2


def test_fenced_json_accepted(conn, settings):
    _seed(conn, n=1)
    fenced = httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(GOOD) + "\n```"}}]})
    scorer, _ = _scorer(settings, [fenced])
    assert score_pending(conn, settings, load_interests(settings.config_dir), scorer=scorer)["scored"] == 1


def test_model_down_stops_run(conn, settings):
    _seed(conn)

    def down():
        raise httpx.ConnectError("connection refused")

    scorer, _ = _scorer(settings, [down])
    with pytest.raises(ModelUnavailable):
        score_pending(conn, settings, load_interests(settings.config_dir), scorer=scorer)
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM runs WHERE kind='score'").fetchone()[0] == "failed"
