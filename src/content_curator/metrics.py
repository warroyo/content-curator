"""Prometheus metrics, served at /metrics."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

JOB_RUNS = Counter("curator_job_runs_total", "Job runs by outcome", ["job", "status"])
JOB_DURATION = Histogram(
    "curator_job_duration_seconds", "Job duration", ["job"], buckets=(1, 10, 60, 300, 900, 1800, 3600, 7200)
)
JOB_LAST_SUCCESS = Gauge("curator_job_last_success_timestamp_seconds", "Unix time of the last successful run", ["job"])
JOB_RUNNING = Gauge("curator_job_running", "1 while the job is running", ["job"])

ITEMS_FETCHED = Counter("curator_items_fetched_total", "New items stored")
FETCH_FAILURES = Counter("curator_fetch_failures_total", "Source fetches that failed")
DUPLICATES = Counter("curator_duplicates_total", "Items marked as exact duplicates")
ITEMS_SCORED = Counter("curator_items_scored_total", "Items scored by the model")
SCORE_FAILURES = Counter("curator_score_failures_total", "Items whose scoring failed")
MODEL_UNAVAILABLE = Counter("curator_model_unavailable_total", "Score runs stopped because the model was unreachable")
MODEL_TOKENS = Counter("curator_model_tokens_total", "Model tokens", ["kind"])
ITEMS_EMBEDDED = Counter("curator_items_embedded_total", "Items embedded")
STORIES = Gauge("curator_stories", "Same-story groups among fresh items at the last grouping")
TRIAL_SHOWS_ADDED = Counter("curator_trial_shows_added_total", "Podcast shows added as trial sources")
PICKS = Counter("curator_picks_total", "Items picked", ["bucket"])
PUBLISHED = Counter("curator_published_total", "Picks sent to Raindrop", ["status"])
PUBLISH_FAILURES = Counter("curator_publish_failures_total", "Curate runs whose publish to Raindrop failed")


def record(job: str, stats: dict) -> None:
    """Fold a job's stats into the counters."""
    if job == "collect":
        ITEMS_FETCHED.inc(stats.get("new_items", 0))
        FETCH_FAILURES.inc(stats.get("fetch_failures", 0))
        DUPLICATES.inc(stats.get("duplicates", 0))
    elif job == "score":
        ITEMS_SCORED.inc(stats.get("scored", 0))
        SCORE_FAILURES.inc(stats.get("failures", 0))
        MODEL_TOKENS.labels("prompt").inc(stats.get("prompt_tokens", 0))
        MODEL_TOKENS.labels("completion").inc(stats.get("completion_tokens", 0))
    elif job == "stories":
        ITEMS_EMBEDDED.inc(stats.get("embedded", 0))
        if "stories" in stats:
            STORIES.set(stats["stories"])
    elif job == "discover":
        TRIAL_SHOWS_ADDED.inc(stats.get("added", 0))
    elif job == "curate":
        for bucket, n in stats.get("buckets", {}).items():
            PICKS.labels(bucket).inc(n)
        for status in ("published", "already_saved"):
            if stats.get(status):
                PUBLISHED.labels(status).inc(stats[status])
