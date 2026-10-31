"""Jobs: the units of work shared by the scheduler, the HTTP API and the CLI."""

from __future__ import annotations

import fcntl
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from content_curator import db, ingest, metrics, report
from content_curator import select as selection
from content_curator.config import Settings, load_interests, load_sources
from content_curator.scoring import ModelUnavailable, score_pending

log = logging.getLogger(__name__)


class JobBusy(Exception):
    """Another job holds the data lock (in this process or another container)."""


@contextmanager
def data_lock(settings: Settings) -> Iterator[None]:
    """Exclusive lock on /data, so the service and a manual CLI run never write at the same time."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.data_dir / "curator.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobBusy("another curator job is running") from None
        yield


def collect(settings: Settings, force: bool = False) -> dict:
    conn = db.connect(settings.db_path)
    try:
        return ingest.collect(conn, settings, load_sources(settings.config_dir), force=force)
    finally:
        conn.close()


def score(settings: Settings, limit: int | None = None) -> dict:
    conn = db.connect(settings.db_path)
    try:
        return score_pending(conn, settings, load_interests(settings.config_dir), limit=limit)
    except ModelUnavailable:
        metrics.MODEL_UNAVAILABLE.inc()
        raise
    finally:
        conn.close()


def curate(settings: Settings, dry_run: bool = True, extra_stats: dict | None = None) -> dict:
    if not dry_run:
        raise NotImplementedError("publishing to Readwise Reader is Phase 1b")
    conn = db.connect(settings.db_path)
    try:
        interests = load_interests(settings.config_dir)
        run = db.Run(conn, "curate")
        candidates = selection.load_candidates(conn, interests)
        result = selection.select(candidates, interests.selection, interests.scoring.weights)
        selection.record(conn, run.id, result, dry_run=dry_run)
        buckets: dict[str, int] = {}
        for d in result.picks:
            buckets[d.bucket] = buckets.get(d.bucket, 0) + 1
        run.stats.update(candidates=len(candidates), picks=len(result.picks), buckets=buckets, **(extra_stats or {}))
        path = report.write(settings.reports_dir, report.render(result, run.stats, dry_run=dry_run))
        run.stats["report"] = path.name
        run.finish()
        return run.stats
    finally:
        conn.close()


def pipeline(settings: Settings, dry_run: bool = True, force: bool = False) -> dict:
    """The nightly job: collect, score, curate. A model outage stops it before curation."""
    collected = collect(settings, force=force)
    metrics.record("collect", collected)
    scored = score(settings)
    metrics.record("score", scored)
    extra = {
        "fetched_new": collected["new_items"], "fetch_failures": collected["fetch_failures"],
        "duplicates": collected["duplicates"], "scored": scored["scored"], "score_failures": scored["failures"],
        "scoring_s": scored["duration_s"], "prompt_tokens": scored["prompt_tokens"],
        "completion_tokens": scored["completion_tokens"],
    }
    return curate(settings, dry_run=dry_run, extra_stats=extra)


JOBS: dict[str, Callable[..., dict]] = {
    "collect": collect,
    "score": score,
    "curate": curate,
    "run": pipeline,
}


@dataclass
class JobState:
    name: str
    running: bool = False
    started_at: str | None = None
    last_finished_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_stats: dict = field(default_factory=dict)


class JobRunner:
    """Runs one job at a time, records outcome and metrics. Used by the scheduler and the API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = {name: JobState(name) for name in JOBS}
        self._busy = threading.Lock()

    def busy(self) -> str | None:
        return next((s.name for s in self.state.values() if s.running), None)

    def run(self, name: str, **kwargs) -> dict:
        if name not in JOBS:
            raise KeyError(name)
        if not self._busy.acquire(blocking=False):
            raise JobBusy(f"job {self.busy()} is running")
        state = self.state[name]
        try:
            with data_lock(self.settings):
                state.running, state.started_at, state.last_error = True, _now(), None
                metrics.JOB_RUNNING.labels(name).set(1)
                t0 = time.monotonic()
                log.info("job started", extra={"job": name})
                try:
                    stats = JOBS[name](self.settings, **kwargs)
                except Exception as e:
                    state.last_status, state.last_error = "failed", str(e)
                    metrics.JOB_RUNS.labels(name, "failed").inc()
                    log.exception("job failed", extra={"job": name})
                    raise
                else:
                    state.last_status, state.last_stats = "ok", stats
                    metrics.JOB_RUNS.labels(name, "ok").inc()
                    metrics.JOB_LAST_SUCCESS.labels(name).set(time.time())
                    if name != "run":
                        metrics.record(name, stats)
                    log.info("job finished", extra={"job": name, "stats": stats})
                    return stats
                finally:
                    state.running, state.last_finished_at = False, _now()
                    metrics.JOB_RUNNING.labels(name).set(0)
                    metrics.JOB_DURATION.labels(name).observe(time.monotonic() - t0)
        finally:
            self._busy.release()

    def start(self, name: str, **kwargs) -> None:
        """Run a job in a background thread; raises JobBusy at once if one is running."""
        if self.busy():
            raise JobBusy(f"job {self.busy()} is running")
        threading.Thread(target=self._run_quietly, args=(name,), kwargs=kwargs, daemon=True, name=f"job-{name}").start()

    def _run_quietly(self, name: str, **kwargs) -> None:
        try:
            self.run(name, **kwargs)
        except JobBusy as e:
            log.warning("job skipped", extra={"job": name, "reason": str(e)})
        except Exception:
            pass  # already logged and recorded


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
