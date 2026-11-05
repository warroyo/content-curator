"""MCP tools for agents (Claude Code, Hermes), served at /mcp next to the HTTP API.

The same queries and actions as the HTTP API, with descriptions an agent can act on. Read tools are
open, like the API's GET routes. Write tools need the API bearer token when one is set, sent as the
`Authorization` header the client was registered with.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from content_curator import __version__, db, learning, registry
from content_curator import status as queries
from content_curator.config import Settings, load_interests
from content_curator.jobs import JOBS, JobBusy, JobRunner

INSTRUCTIONS = """\
The content curator collects feeds, scores each item 0-10 against Will's interests with a local
model, and publishes a few picks a day to Raindrop.io. Will reads them there and gives feedback:
a `read` tag, a `meh` tag (less like this), deleting a pick, or favouriting it (more like this).
Preference learning turns that feedback into small score adjustments; in shadow mode it only
reports what it would change. Use `weekly_review` for how the week went, `latest_picks` and
`item` for why something was or was not picked, `sources` for what is followed or on trial.
"""

REPORT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LEARNING_SECTION = re.compile(r"^## Learning.*?(?=^## |\Z)", re.M | re.S)


def build_mcp(settings: Settings, runner: JobRunner, jobs_view: Callable[[], list[dict]]) -> MCPServer:
    mcp = MCPServer("content-curator", version=__version__, instructions=INSTRUCTIONS)

    def authorize(ctx: Context) -> None:
        request = ctx.request_context.request
        header = request.headers.get("authorization") if request is not None else None
        if settings.api_token and header != f"Bearer {settings.api_token}":
            raise ToolError("this tool changes the curator and needs the API bearer token")

    def reports(days: int) -> list:
        if not settings.reports_dir.exists():
            return []
        since = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        return [p for p in sorted(settings.reports_dir.glob("*.md")) if p.name[:10] >= since]

    @mcp.tool()
    def status() -> dict:
        """Health of the curator: sources and failures, item and score counts, sources on trial,
        feedback counts for the last 7 days, the last run of each job and when each runs next."""
        with closing(db.connect(settings.db_path)) as c:
            max_age = load_interests(settings.config_dir).selection.max_age_days
            return {**queries.status(c, max_age), "jobs": jobs_view()}

    @mcp.tool()
    def latest_picks(near_misses: bool = False) -> dict:
        """The picks from the last curate run with their scores and reasons. With near_misses, also
        the items that just missed and why."""
        with closing(db.connect(settings.db_path)) as c:
            result = queries.latest_picks(c, include_near_misses=near_misses)
        if result is None:
            raise ToolError("no curate run yet")
        return result

    @mcp.tool()
    def item(item_id: int) -> dict:
        """One item: source, scores with the model's explanation, and every run that considered it."""
        with closing(db.connect(settings.db_path)) as c:
            result = queries.item_detail(c, item_id)
        if result is None:
            raise ToolError(f"no item {item_id}")
        return result

    @mcp.tool()
    def report(date: str | None = None) -> str:
        """The markdown report of a curate run: picks, near misses, sources on trial, learning, run
        stats. `date` is YYYY-MM-DD and gives that day's last report; without it, the latest."""
        if date is not None and not REPORT_DATE.match(date):
            raise ToolError("date is YYYY-MM-DD")
        found = [p for p in reports(3650) if date is None or p.name.startswith(date)]
        if not found:
            raise ToolError(f"no report for {date}" if date else "no report yet")
        return found[-1].read_text()

    @mcp.tool()
    def weekly_review(days: int = 7) -> dict:
        """Everything for a review of the last `days`: the feedback given in Raindrop (with titles
        and sources), the source adjustments learning works out from it now, the Learning section of
        each report in the window (in shadow mode: the picks it would have added and dropped), and
        the picks per curate run."""
        days = max(1, min(days, 90))
        cfg = load_interests(settings.config_dir).learning
        with closing(db.connect(settings.db_path)) as c:
            feedback = queries.feedback(c, days)
            adjustments = learning.source_adjustments(learning.load_signals(c, cfg), cfg)
            runs = c.execute(
                "SELECT started_at, stats FROM runs WHERE kind='curate' AND status='ok' "
                "AND datetime(started_at) >= datetime('now', ?) ORDER BY id",
                (f"-{days} days",),
            ).fetchall()
        counts: dict[str, int] = {}
        for f in feedback:
            counts[f["kind"]] = counts.get(f["kind"], 0) + 1
        return {
            "days": days,
            "learning_mode": cfg.mode,
            "feedback_counts": counts,
            "feedback": feedback,
            "source_adjustments": [
                {"source_id": s, "adjustment": adj, "from": why}
                for s, (adj, why) in sorted(adjustments.items(), key=lambda kv: kv[1][0])
            ],
            "learning_by_report": [
                {"report": p.name, "learning": m.group(0).strip()}
                for p in reports(days) if (m := LEARNING_SECTION.search(p.read_text()))
            ],
            "picks_per_run": [
                {"at": r["started_at"], **{k: json.loads(r["stats"]).get(k) for k in ("picks", "buckets")}}
                for r in runs
            ],
        }

    @mcp.tool()
    def sources(status: str | None = None) -> list[dict]:
        """Every source with its status (configured, trial, followed, unfollowed, expired, removed),
        failures and how many of its items were picked. `status` filters to one status."""
        with closing(db.connect(settings.db_path)) as c:
            rows = queries.sources(c)
        return [r for r in rows if status is None or r["status"] == status]

    @mcp.tool()
    def candidates(status: str | None = None) -> list[dict]:
        """Suggested sources waiting for, or done with, a feed check. `status` filters."""
        with closing(db.connect(settings.db_path)) as c:
            return registry.list_candidates(c, status)

    @mcp.tool()
    def add_url(url: str, ctx: Context, title: str | None = None) -> dict:
        """Add one article or episode by URL. It is fetched, scored and considered by the next run."""
        authorize(ctx)
        from content_curator import ingest

        with closing(db.connect(settings.db_path)) as c:
            item_id = ingest.ingest_url(c, settings, url, title)
        if item_id is None:
            raise ToolError("URL already known")
        return {"item_id": item_id}

    @mcp.tool()
    def follow_source(source_id: str, ctx: Context) -> dict:
        """Keep a source: a trial becomes permanent, an unfollowed source comes back."""
        return _set_follow(source_id, "followed", ctx)

    @mcp.tool()
    def unfollow_source(source_id: str, ctx: Context) -> dict:
        """Stop collecting from a source. Its history stays."""
        return _set_follow(source_id, "unfollowed", ctx)

    def _set_follow(source_id: str, state: str, ctx: Context) -> dict:
        authorize(ctx)
        with closing(db.connect(settings.db_path)) as c:
            if not registry.set_follow(c, source_id, state, via="mcp"):
                raise ToolError(f"no source {source_id!r}; `sources` lists them")
        return {"source": source_id, "state": state}

    @mcp.tool()
    def suggest_sources(candidates: list[registry.CandidateIn], ctx: Context, by: str = "") -> dict:
        """Suggest feeds or sites to try. Each needs a url; name, topics and why help. The next
        `candidates` or `discover` job checks them and puts the good ones on trial."""
        authorize(ctx)
        with closing(db.connect(settings.db_path)) as c:
            return registry.add_candidates(c, candidates, by[:64])

    @mcp.tool()
    def run_job(name: str, ctx: Context, force: bool = False) -> dict:
        """Start a job now; it runs in the background, so check `status` for the result. Jobs: run
        (the nightly pipeline), collect, stories, score, curate, discover, candidates, feedback.
        `force` refetches every source (collect and run only)."""
        authorize(ctx)
        if name not in JOBS:
            raise ToolError(f"unknown job {name!r}; one of {sorted(JOBS)}")
        try:
            runner.start(name, **({"force": True} if force and name in ("collect", "run") else {}))
        except JobBusy as e:
            raise ToolError(str(e)) from None
        return {"job": name, "status": "started"}

    return mcp
