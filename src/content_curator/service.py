"""The long-running service: HTTP API, built-in scheduler, metrics and health."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from content_curator import __version__, db, ingest
from content_curator import status as queries
from content_curator.config import Settings, load_app_config, load_interests
from content_curator.jobs import JOBS, JobBusy, JobRunner

log = logging.getLogger(__name__)

SHUTDOWN_WAIT_SECONDS = 25


class JobRequest(BaseModel):
    force: bool = False
    limit: int | None = None


class UrlRequest(BaseModel):
    url: str
    title: str | None = None


def build_scheduler(settings: Settings, runner: JobRunner) -> BackgroundScheduler:
    cfg = load_app_config(settings.config_dir).schedule
    scheduler = BackgroundScheduler(timezone=cfg.timezone)
    for entry in cfg.jobs:
        if not entry.enabled:
            continue
        if entry.job not in JOBS:
            raise ValueError(f"unknown job in app.yaml schedule: {entry.job!r}")
        scheduler.add_job(
            runner._run_quietly, CronTrigger.from_crontab(entry.cron, timezone=cfg.timezone),
            args=[entry.job], id=f"{entry.job}:{entry.cron}", name=entry.job,
            max_instances=1, coalesce=True, misfire_grace_time=cfg.misfire_grace_seconds,
        )
    return scheduler


def create_app(settings: Settings, runner: JobRunner | None = None, scheduler_enabled: bool = True) -> FastAPI:
    runner = runner or JobRunner(settings)
    scheduler = build_scheduler(settings, runner) if scheduler_enabled else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.connect(settings.db_path).close()  # create or migrate before anything runs
        if scheduler:
            scheduler.start()
            for j in scheduler.get_jobs():
                log.info("scheduled", extra={"job": j.name, "next_run": str(j.next_run_time)})
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        deadline = time.monotonic() + SHUTDOWN_WAIT_SECONDS
        while runner.busy() and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        if runner.busy():
            log.warning("stopping with a job still running; it reruns cleanly next time", extra={"job": runner.busy()})

    app = FastAPI(title="content-curator", version=__version__, lifespan=lifespan)
    app.mount("/metrics", make_asgi_app())

    def conn():
        c = db.connect(settings.db_path)
        try:
            yield c
        finally:
            c.close()

    def authorized(authorization: str | None = Header(default=None)) -> None:
        if settings.api_token and authorization != f"Bearer {settings.api_token}":
            raise HTTPException(401, "missing or wrong bearer token")

    @app.get("/healthz")
    def healthz(c=Depends(conn)):
        c.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    def get_status(c=Depends(conn)):
        max_age = load_interests(settings.config_dir).selection.max_age_days
        return {**queries.status(c, max_age), "jobs": jobs_view()}

    def jobs_view() -> list[dict]:
        next_runs: dict[str, str] = {}
        if scheduler and scheduler.running:
            for j in scheduler.get_jobs():
                t = j.next_run_time.isoformat() if j.next_run_time else None
                if t and (j.name not in next_runs or t < next_runs[j.name]):
                    next_runs[j.name] = t
        return [{**vars(s), "next_run": next_runs.get(s.name)} for s in runner.state.values()]

    @app.get("/api/jobs")
    def list_jobs():
        return jobs_view()

    @app.post("/api/jobs/{name}", status_code=202, dependencies=[Depends(authorized)])
    def trigger(name: str, req: JobRequest | None = None):
        if name not in JOBS:
            raise HTTPException(404, f"unknown job {name!r}; one of {sorted(JOBS)}")
        req = req or JobRequest()
        kwargs = {}
        if name in ("collect", "run") and req.force:
            kwargs["force"] = True
        if name == "score" and req.limit:
            kwargs["limit"] = req.limit
        try:
            runner.start(name, **kwargs)
        except JobBusy as e:
            raise HTTPException(409, str(e)) from None
        return {"job": name, "status": "started"}

    @app.get("/api/picks/latest")
    def picks(near_misses: bool = Query(False), c=Depends(conn)):
        result = queries.latest_picks(c, include_near_misses=near_misses)
        if result is None:
            raise HTTPException(404, "no curate run yet")
        return result

    @app.get("/api/items/{item_id}")
    def item(item_id: int, c=Depends(conn)):
        result = queries.item_detail(c, item_id)
        if result is None:
            raise HTTPException(404, "no such item")
        return result

    @app.post("/api/urls", status_code=201, dependencies=[Depends(authorized)])
    def add_url(req: UrlRequest, c=Depends(conn)):
        item_id = ingest.ingest_url(c, settings, req.url, req.title)
        if item_id is None:
            raise HTTPException(409, "URL already known")
        return {"item_id": item_id}

    @app.get("/api/reports/latest", response_class=PlainTextResponse)
    def latest_report():
        reports = sorted(settings.reports_dir.glob("*.md")) if settings.reports_dir.exists() else []
        if not reports:
            raise HTTPException(404, "no report yet")
        return PlainTextResponse(reports[-1].read_text(), media_type="text/markdown")

    return app


def serve(settings: Settings) -> None:
    import uvicorn

    class QuietServer(uvicorn.Server):
        """uvicorn wakes every 100 ms to refresh the Date header and check for shutdown.
        A 2 s tick keeps an idle service near zero CPU; shutdown is at most 2 s slower."""

        async def main_loop(self) -> None:
            while not await self.on_tick(0):
                await asyncio.sleep(2)

    config = uvicorn.Config(
        create_app(settings), host=settings.api_host, port=settings.api_port,
        log_config=None, access_log=False, timeout_graceful_shutdown=SHUTDOWN_WAIT_SECONDS + 5,
    )
    QuietServer(config).run()
