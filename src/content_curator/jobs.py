"""Jobs: the units of work shared by the scheduler, the HTTP API and the CLI.

The heavy modules (feed parsing, extraction, scoring) are imported inside the job
functions. The service runs each job in a child process (`isolate=True`), so the
long-running process never loads them and the memory a run uses goes back to the
OS when the run ends.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from content_curator import db, metrics
from content_curator.config import Settings, load_interests, load_sources

log = logging.getLogger(__name__)

# sysexits.h codes shared with the CLI, so a parent process can tell outcomes apart.
EX_USAGE, EX_UNAVAILABLE, EX_TEMPFAIL = 64, 69, 75


class JobBusy(Exception):
    """Another job holds the data lock (in this process or another container)."""


class JobFailed(Exception):
    """A job run in a child process failed."""


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
    from content_curator import ingest

    conn = db.connect(settings.db_path)
    try:
        return ingest.collect(conn, settings, load_sources(settings.config_dir), force=force)
    finally:
        conn.close()


def stories(settings: Settings) -> dict:
    """Embed fresh items and group same-story coverage. Never fails the run: grouping is a refinement."""
    from content_curator.stories import run_stories

    conn = db.connect(settings.db_path)
    try:
        return run_stories(conn, settings, load_interests(settings.config_dir))
    finally:
        conn.close()


def discover(settings: Settings) -> dict:
    """Find new podcast shows on Apple Podcasts and add them as trial sources."""
    from content_curator.discover import discover_podcasts

    conn = db.connect(settings.db_path)
    try:
        return discover_podcasts(conn, settings, load_interests(settings.config_dir))
    finally:
        conn.close()


def score(settings: Settings, limit: int | None = None) -> dict:
    from content_curator.scoring import ModelUnavailable, score_pending

    conn = db.connect(settings.db_path)
    try:
        return score_pending(conn, settings, load_interests(settings.config_dir), limit=limit)
    except ModelUnavailable:
        metrics.MODEL_UNAVAILABLE.inc()
        raise
    finally:
        conn.close()


def curate(settings: Settings, dry_run: bool = True, extra_stats: dict | None = None) -> dict:
    from content_curator import report
    from content_curator import select as selection

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
        from content_curator.discover import trial_sources

        path = report.write(
            settings.reports_dir, report.render(result, run.stats, dry_run=dry_run, trials=trial_sources(conn))
        )
        run.stats["report"] = path.name
        run.finish()
        return run.stats
    finally:
        conn.close()


def pipeline(settings: Settings, dry_run: bool = True, force: bool = False) -> dict:
    """The nightly job: collect, group stories, score, curate. A chat model outage stops it before curation."""
    collected = collect(settings, force=force)
    grouped = stories(settings)
    scored = score(settings)
    extra = {
        "fetched_new": collected["new_items"], "fetch_failures": collected["fetch_failures"],
        "duplicates": collected["duplicates"], "scored": scored["scored"], "score_failures": scored["failures"],
        "scoring_s": scored["duration_s"], "prompt_tokens": scored["prompt_tokens"],
        "completion_tokens": scored["completion_tokens"],
        "stories": grouped.get("stories", 0), "story_items": grouped.get("grouped_items", 0),
    }
    if "model_unavailable" in grouped:
        extra["stories_skipped"] = grouped["model_unavailable"]
    stats = curate(settings, dry_run=dry_run, extra_stats=extra)
    stats["steps"] = {"collect": collected, "stories": grouped, "score": scored}
    return stats


JOBS: dict[str, Callable[..., dict]] = {
    "collect": collect,
    "stories": stories,
    "score": score,
    "curate": curate,
    "run": pipeline,
    "discover": discover,
}


def record_metrics(name: str, stats: dict) -> None:
    if name == "run":
        for step, step_stats in stats.get("steps", {}).items():
            metrics.record(step, step_stats)
        metrics.record("curate", stats)
    else:
        metrics.record(name, stats)


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
    """Runs one job at a time, records outcome and metrics. Used by the scheduler, the API and the CLI.

    isolate=True runs each job as `curator <job>` in a child process, which takes the data lock
    itself; isolate=False runs it in this process under the data lock.
    """

    def __init__(self, settings: Settings, isolate: bool = False):
        self.settings = settings
        self.isolate = isolate
        self.state = {name: JobState(name) for name in JOBS}
        self._busy = threading.Lock()

    def busy(self) -> str | None:
        return next((s.name for s in self.state.values() if s.running), None)

    def run(self, name: str, **kwargs) -> dict:
        if name not in JOBS:
            raise KeyError(name)
        if not self._busy.acquire(blocking=False):
            raise JobBusy(f"job {self.busy()} is running")
        try:
            if self.isolate:
                return self._tracked(name, lambda: self._run_child(name, **kwargs))
            with data_lock(self.settings):
                return self._tracked(name, lambda: JOBS[name](self.settings, **kwargs))
        finally:
            self._busy.release()

    def _tracked(self, name: str, fn: Callable[[], dict]) -> dict:
        state = self.state[name]
        state.running, state.started_at, state.last_error = True, _now(), None
        metrics.JOB_RUNNING.labels(name).set(1)
        t0 = time.monotonic()
        log.info("job started", extra={"job": name})
        try:
            stats = fn()
        except JobBusy:
            state.last_status = "skipped"
            raise
        except Exception as e:
            state.last_status, state.last_error = "failed", str(e)
            metrics.JOB_RUNS.labels(name, "failed").inc()
            log.error("job failed", extra={"job": name, "error": str(e)})
            raise
        else:
            state.last_status, state.last_stats = "ok", stats
            metrics.JOB_RUNS.labels(name, "ok").inc()
            metrics.JOB_LAST_SUCCESS.labels(name).set(time.time())
            record_metrics(name, stats)
            log.info("job finished", extra={"job": name, "stats": stats})
            return stats
        finally:
            state.running, state.last_finished_at = False, _now()
            metrics.JOB_RUNNING.labels(name).set(0)
            metrics.JOB_DURATION.labels(name).observe(time.monotonic() - t0)

    def _run_child(self, name: str, **kwargs) -> dict:
        cmd = [sys.executable, "-m", "content_curator.cli", name]
        if kwargs.get("force"):
            cmd.append("--force")
        if kwargs.get("limit"):
            cmd += ["--limit", str(kwargs["limit"])]
        # stderr (the child's JSON logs) goes straight to the container log; stdout is the stats.
        env = os.environ | {
            "CURATOR_DATA_DIR": str(self.settings.data_dir),
            "CURATOR_CONFIG_DIR": str(self.settings.config_dir),
        }
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=False, env=env)
        if proc.returncode == EX_TEMPFAIL:
            raise JobBusy("another curator job is running")
        if proc.returncode == EX_UNAVAILABLE:
            metrics.MODEL_UNAVAILABLE.inc()
            raise JobFailed("model unavailable; items stay queued for the next run")
        if proc.returncode != 0:
            raise JobFailed(f"curator {name} exited with {proc.returncode}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}

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
