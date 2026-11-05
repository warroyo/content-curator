"""Preference learning: source and similarity adjustments from the feedback table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from content_curator import db, learning, report
from content_curator.config import Learning, Selection, Weights
from content_curator.select import Candidate, select

W = Weights(relevance=1, novelty=0, depth=0, source_quality=0, actionability=0, discovery_value=0)
SEL = Selection(daily_count=1, discovery_share=0, min_score=5, reserved={})


def unit(*xs: float) -> np.ndarray:
    v = np.asarray(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def item(conn, i: int, source: str = "s", vector: np.ndarray | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO sources(id, name, url, type) VALUES (?, ?, ?, 'rss')",
                 (source, source, f"https://{source}.test/feed"))
    conn.execute("INSERT INTO items(id, source_id, url, normalized_url, title, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (i, source, f"https://{source}.test/{i}", f"https://{source}.test/{i}", f"Item {i}", db.now()))
    if vector is not None:
        conn.execute("INSERT INTO embeddings(item_id, model, input_hash, dim, vector, created_at) "
                     "VALUES (?, 'm', 'h', ?, ?, ?)", (i, len(vector), vector.tobytes(), db.now()))


def feedback(conn, i: int, kind: str, days_ago: float = 0) -> None:
    at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO feedback(item_id, kind, raindrop_id, recorded_at) VALUES (?, ?, ?, ?)",
                 (i, kind, 1000 + i, at))


def cand(i: int, source: str = "s", score: float = 7) -> Candidate:
    return Candidate(
        item_id=i, source_id=source, source_name=source, title=f"Item {i}", url=f"https://{source}.test/{i}",
        published_at=None, word_count=1000, confidence="high", explanation="", topics=[], relevance=score,
        novelty=score, depth=score, source_quality=score, actionability=score, discovery_value=0,
        outside_interests=False,
    )


def test_source_adjustment_is_shrunk_capped_and_decays(conn):
    for i in range(1, 4):
        item(conn, i, source="bad")
        feedback(conn, i, "meh")
    item(conn, 4, source="once")
    feedback(conn, 4, "meh")
    item(conn, 5, source="old")
    feedback(conn, 5, "meh", days_ago=120)
    item(conn, 6, source="read")
    feedback(conn, 6, "read")

    adj = learning.source_adjustments(learning.load_signals(conn, Learning()), Learning())

    # Three fresh meh: 0.5 * -6 / (3 + 5) / 2 = -0.19; one meh: 0.5 * -2 / 6 / 2 = -0.08.
    assert adj["bad"] == (-0.19, "3 meh")
    assert adj["once"][0] == -0.08
    # Four half-lives old: barely counts.
    assert -0.01 <= adj["old"][0] <= 0
    # A read is neutral.
    assert "read" not in adj


def test_since_resets_without_losing_history(conn):
    item(conn, 1)
    feedback(conn, 1, "meh", days_ago=10)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    assert learning.load_signals(conn, Learning(since=tomorrow)) == []
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1


def test_similarity_moves_candidates_near_a_favourite_up_and_near_a_meh_down(conn):
    item(conn, 1, source="a", vector=unit(1, 0, 0))
    feedback(conn, 1, "favourite")
    item(conn, 2, source="b", vector=unit(0, 1, 0))
    feedback(conn, 2, "meh")
    item(conn, 10, source="c", vector=unit(1, 0.2, 0))   # close to the favourite
    item(conn, 11, source="d", vector=unit(0.1, 1, 0))   # close to the meh
    item(conn, 12, source="e", vector=unit(0, 0, 1))     # close to neither
    cands = [cand(10, "c"), cand(11, "d"), cand(12, "e")]

    stats = learning.apply(conn, Learning(source_cap=0), cands)

    up, down, neither = cands
    assert up.learned > 0.3 and 'like "Item 1" you favourited' in up.learned_why
    assert down.learned < -0.3 and "you tagged meh" in down.learned_why
    assert neither.learned == 0 and neither.learned_why == ""
    assert stats == {"learning_signals": 2, "learning_adjusted": 2}


def test_off_leaves_candidates_alone(conn):
    item(conn, 1)
    feedback(conn, 1, "meh")
    c = cand(5)
    assert learning.apply(conn, Learning(mode="off"), [c]) == {}
    assert c.learned == 0


def test_learning_reorders_core_picks_only_when_on():
    a, b = cand(1, "a", score=7.0), cand(2, "b", score=6.8)
    a.learned, b.learned = -0.3, 0.3
    assert [d.candidate.item_id for d in select([a, b], SEL, W).picks] == [1]
    picks = select([a, b], SEL, W, learn=True).picks
    assert [d.candidate.item_id for d in picks] == [2]
    assert picks[0].reason == "overall 6.8, learned +0.3"


def test_learning_never_lifts_an_item_over_the_threshold_but_can_drop_one_below():
    weak, edge = cand(1, "a", score=4.8), cand(2, "b", score=5.2)
    weak.learned, edge.learned = 0.5, -0.4
    r = select([weak, edge], Selection(daily_count=5, discovery_share=0, min_score=5, reserved={}), W, learn=True)
    assert r.picks == []
    assert {d.reason for d in r.near_misses} == {"below threshold"}


def test_report_shows_what_shadow_learning_would_change():
    a, b = cand(1, "a", score=7.0), cand(2, "b", score=6.8)
    a.learned, a.learned_why = -0.3, "source -0.3 (3 meh)"
    b.learned, b.learned_why = 0.3, 'similar +0.3: like "X" you favourited (0.80)'
    would = select([a, b], SEL, W, learn=True)
    result = select([a, b], SEL, W)
    text = report.render(result, {}, dry_run=True, learning_mode="shadow",
                         shadow={d.candidate.item_id: d for d in would.picks})
    assert "## Learning (shadow)" in text
    assert "would add [Item 2]" in text and "would drop [Item 1]" in text
    assert "Learned -0.3: source -0.3 (3 meh)" in text
