"""HTTP API and job runner, with fake jobs."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from content_curator import jobs
from content_curator.service import build_scheduler, create_app


@pytest.fixture
def fake_jobs(monkeypatch):
    gate = threading.Event()
    calls = []

    def slow(settings, **kw):
        calls.append(("run", kw))
        gate.wait(5)
        return {"ok": True}

    def boom(settings, **kw):
        raise RuntimeError("feed exploded")

    monkeypatch.setattr(jobs, "JOBS", {"run": slow, "collect": boom, "score": slow, "curate": slow})
    return gate, calls


def _wait(cond, timeout=5):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_health_and_metrics(settings):
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert "curator_job_runs_total" in client.get("/metrics/").text


def test_trigger_runs_job_and_rejects_overlap(settings, fake_jobs):
    gate, calls = fake_jobs
    runner = jobs.JobRunner(settings)
    with TestClient(create_app(settings, runner=runner, scheduler_enabled=False)) as client:
        r = client.post("/api/jobs/run", json={"force": True})
        assert r.status_code == 202
        _wait(lambda: runner.busy() == "run")
        assert client.post("/api/jobs/score").status_code == 409
        gate.set()
        _wait(lambda: runner.busy() is None)
        state = {j["name"]: j for j in client.get("/api/jobs").json()}
        assert state["run"]["last_status"] == "ok"
        assert calls == [("run", {"force": True})]


def test_failed_job_is_recorded(settings, fake_jobs):
    runner = jobs.JobRunner(settings)
    with TestClient(create_app(settings, runner=runner, scheduler_enabled=False)) as client:
        client.post("/api/jobs/collect")
        _wait(lambda: runner.state["collect"].last_status == "failed")
        assert runner.state["collect"].last_error == "feed exploded"


def test_unknown_job_404(settings):
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        assert client.post("/api/jobs/nope").status_code == 404


def test_token_required_when_set(settings, fake_jobs):
    gate, _ = fake_jobs
    gate.set()
    settings.api_token = "s3cret"
    with TestClient(create_app(settings, runner=jobs.JobRunner(settings), scheduler_enabled=False)) as client:
        assert client.post("/api/jobs/run").status_code == 401
        assert client.post("/api/jobs/run", headers={"Authorization": "Bearer s3cret"}).status_code == 202
        assert client.get("/api/status").status_code == 200  # reads stay open


def test_picks_404_before_first_run(settings):
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        assert client.get("/api/picks/latest").status_code == 404
        assert client.get("/api/reports/latest").status_code == 404


def test_cross_process_lock(settings, fake_jobs):
    gate, _ = fake_jobs
    gate.set()
    with jobs.data_lock(settings):
        with pytest.raises(jobs.JobBusy):
            jobs.JobRunner(settings).run("run")


def test_schedule_from_config(settings):
    (settings.config_dir / "app.yaml").write_text(
        "schedule:\n  timezone: America/Denver\n  jobs:\n    - {job: run, cron: '30 1 * * *'}\n"
    )
    scheduler = build_scheduler(settings, jobs.JobRunner(settings))
    [job] = scheduler.get_jobs()
    assert job.name == "run" and str(job.trigger.timezone) == "America/Denver"


def test_isolated_job_runs_in_child_process(settings):
    runner = jobs.JobRunner(settings, isolate=True)
    stats = runner.run("curate")
    assert stats["candidates"] == 0 and stats["report"].endswith(".md")
    assert runner.state["curate"].last_status == "ok"


def test_isolated_job_busy_when_locked(settings):
    runner = jobs.JobRunner(settings, isolate=True)
    with jobs.data_lock(settings):
        with pytest.raises(jobs.JobBusy):
            runner.run("curate")
