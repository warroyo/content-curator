"""Preference learning: score adjustments from what you did with earlier picks in Raindrop.

Nothing learned is stored. Every curate run reads the `feedback` table and works out, for each
candidate:

- a source adjustment: the decayed mean signal on that source's picks, shrunk toward 0 by
  `source_prior` neutral signals, scaled to at most `source_cap`;
- a similarity adjustment: how close the candidate's embedding is to the nearest pick you
  favourited, minus the nearest you tagged meh, counted from `similar_from` up, at most `similar_cap`.

Deletions and reads only feed the source adjustment: a deletion says "not this one", not "less like
this". The interest profile stays the authority: the adjustments are small next to the 0-10 score,
and `since` resets them without losing the history.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from content_curator.config import Learning
from content_curator.select import Candidate


@dataclass
class Signal:
    item_id: int
    source_id: str
    title: str
    kind: str
    age_days: float


def load_signals(conn: sqlite3.Connection, cfg: Learning) -> list[Signal]:
    rows = conn.execute(
        """SELECT f.item_id, i.source_id, i.title, f.kind, f.recorded_at
           FROM feedback f JOIN items i ON i.id = f.item_id
           WHERE (? IS NULL OR date(f.recorded_at) >= date(?))""",
        (cfg.since, cfg.since),
    ).fetchall()
    now = datetime.now(UTC)
    return [Signal(r["item_id"], r["source_id"], r["title"], r["kind"],
                   (now - datetime.fromisoformat(r["recorded_at"])).total_seconds() / 86400) for r in rows]


def source_adjustments(signals: list[Signal], cfg: Learning) -> dict[str, tuple[float, str]]:
    """Adjustment per source, with a short account of the signals behind it."""
    scale = max((abs(w) for w in cfg.weights.values()), default=0) or 1.0
    total: dict[str, float] = defaultdict(float)
    weight: dict[str, float] = defaultdict(float)
    kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in signals:
        w = cfg.weights.get(s.kind, 0.0)
        if not w:
            continue
        decay = 0.5 ** (s.age_days / cfg.half_life_days)
        total[s.source_id] += w * decay
        weight[s.source_id] += decay
        kinds[s.source_id][s.kind] += 1
    out = {}
    for source, t in total.items():
        adj = round(cfg.source_cap * t / (weight[source] + cfg.source_prior) / scale, 2)
        if adj:
            counts = ", ".join(f"{n} {k}" for k, n in sorted(kinds[source].items()))
            out[source] = (adj, counts)
    return out


def _vectors(conn: sqlite3.Connection, ids: list[int]) -> dict[int, np.ndarray]:
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT item_id, vector FROM embeddings WHERE item_id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    return {r["item_id"]: np.frombuffer(r["vector"], dtype=np.float32) for r in rows}


def _nearest(v: np.ndarray, refs: dict[int, np.ndarray]) -> tuple[float, int | None]:
    best, best_id = -1.0, None
    for item_id, r in refs.items():
        if r.shape != v.shape:
            continue
        sim = float(np.dot(v, r))
        if sim > best:
            best, best_id = sim, item_id
    return best, best_id


def apply(conn: sqlite3.Connection, cfg: Learning, candidates: list[Candidate]) -> dict:
    """Set `learned` and `learned_why` on each candidate. Returns counts for the run stats."""
    if cfg.mode == "off":
        return {}
    signals = load_signals(conn, cfg)
    by_source = source_adjustments(signals, cfg)

    recent = [s for s in signals if s.age_days <= cfg.similar_days]
    liked = {s.item_id: s.title for s in recent if s.kind == "favourite"}
    meh = {s.item_id: s.title for s in recent if s.kind == "meh"}
    refs = _vectors(conn, list(liked | meh))
    liked_v = {i: v for i, v in refs.items() if i in liked}
    meh_v = {i: v for i, v in refs.items() if i in meh}
    cand_v = _vectors(conn, [c.item_id for c in candidates]) if refs else {}

    def strength(sim: float) -> float:
        return max(0.0, (sim - cfg.similar_from) / (1 - cfg.similar_from))

    adjusted = 0
    for c in candidates:
        parts, why = 0.0, []
        if c.source_id in by_source:
            adj, counts = by_source[c.source_id]
            parts += adj
            why.append(f"source {adj:+g} ({counts})")
        v = cand_v.get(c.item_id)
        if v is not None:
            pos, pos_id = _nearest(v, liked_v)
            neg, neg_id = _nearest(v, meh_v)
            sim_adj = round(cfg.similar_cap * max(-1.0, min(1.0, strength(pos) - strength(neg))), 2)
            if sim_adj:
                parts += sim_adj
                near = []
                if strength(pos) and pos_id is not None:
                    near.append(f'like "{liked[pos_id]}" you favourited ({pos:.2f})')
                if strength(neg) and neg_id is not None:
                    near.append(f'like "{meh[neg_id]}" you tagged meh ({neg:.2f})')
                why.append(f"similar {sim_adj:+g}: " + "; ".join(near))
        c.learned = round(parts, 2)
        c.learned_why = ", ".join(why)
        adjusted += bool(c.learned)
    return {"learning_signals": len(signals), "learning_adjusted": adjusted}
