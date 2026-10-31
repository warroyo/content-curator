from content_curator.config import Selection, Weights
from content_curator.select import Candidate, select


def cand(i, source="s1", score=7, outside=False, disc=0, words=1000, confidence="high"):
    return Candidate(
        item_id=i, source_id=source, source_name=source, title=f"item {i}", url=f"https://x.test/{i}",
        published_at=None, word_count=words, confidence=confidence, explanation="", topics=[],
        relevance=score, novelty=score, depth=score, source_quality=score, actionability=score,
        discovery_value=disc, outside_interests=outside,
    )


W = Weights(relevance=1, novelty=0, depth=0, source_quality=0, actionability=0, discovery_value=0)


def test_ranks_by_score_and_respects_budget():
    cands = [cand(i, source=f"s{i}", score=s) for i, s in enumerate([6, 9, 7, 8])]
    r = select(cands, Selection(daily_count=2, discovery_share=0, min_score=5), W)
    assert [d.candidate.item_id for d in r.picks] == [1, 3]
    assert [d.rank for d in r.picks] == [1, 2]
    assert {d.reason for d in r.near_misses} == {"daily budget full"}


def test_source_cap():
    cands = [cand(i, source="same", score=9 - i * 0.1) for i in range(5)] + [cand(9, source="other", score=6)]
    r = select(cands, Selection(daily_count=4, discovery_share=0, max_per_source=2, min_score=5), W)
    assert [d.candidate.item_id for d in r.picks] == [0, 1, 9]
    assert any("source cap" in d.reason for d in r.near_misses)


def test_threshold():
    r = select([cand(1, score=4), cand(2, source="b", score=6)], Selection(daily_count=5, discovery_share=0, min_score=5), W)
    assert [d.candidate.item_id for d in r.picks] == [2]
    assert r.near_misses[0].reason == "below threshold"


def test_discovery_slots_filled_first():
    core = [cand(i, source=f"c{i}", score=9) for i in range(10)]
    disc = cand(99, source="d", score=5, outside=True, disc=9)
    r = select(core + [disc], Selection(daily_count=4, discovery_share=0.25, min_score=5), W)
    buckets = {d.candidate.item_id: d.bucket for d in r.picks}
    assert buckets[99] == "discovery"
    assert list(buckets.values()).count("core") == 3


def test_low_confidence_loses_ties():
    r = select(
        [cand(1, source="a", score=8, confidence="low"), cand(2, source="b", score=8)],
        Selection(daily_count=1, discovery_share=0, min_score=5), W,
    )
    assert r.picks[0].candidate.item_id == 2


def test_reading_time_budget():
    cands = [cand(1, source="a", score=9, words=230 * 30), cand(2, source="b", score=8, words=230 * 10)]
    r = select(cands, Selection(daily_count=5, discovery_share=0, min_score=5, max_reading_minutes=20), W)
    assert [d.candidate.item_id for d in r.picks] == [2]
