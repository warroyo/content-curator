"""MCP tools at /mcp, called the way an agent calls them: JSON-RPC over HTTP."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from content_curator import db, jobs
from content_curator.service import create_app

HEADERS = {"accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, runner=jobs.JobRunner(settings), scheduler_enabled=False),
                    base_url="http://127.0.0.1:8090") as c:
        yield c


def call(client, tool: str, token: str | None = None, **arguments):
    headers = {**HEADERS, **({"authorization": f"Bearer {token}"} if token else {})}
    r = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments},
    })
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    if result["isError"]:
        return True, result["content"][0]["text"]
    if "structuredContent" in result:
        return False, result["structuredContent"]
    parsed = [json.loads(c["text"]) if c["text"][:1] in "[{" else c["text"] for c in result["content"]]
    return False, parsed[0] if len(parsed) == 1 else parsed


def test_lists_read_and_write_tools(client):
    r = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"status", "latest_picks", "item", "report", "weekly_review", "sources", "candidates",
            "add_url", "follow_source", "unfollow_source", "suggest_sources", "run_job"} <= names


def test_status_and_missing_picks(client):
    error, status = call(client, "status")
    assert not error and "feedback_7d" in status and "jobs" in status
    error, text = call(client, "latest_picks")
    assert error and "no curate run yet" in text


def test_report_by_date(client, settings):
    settings.reports_dir.mkdir(parents=True)
    (settings.reports_dir / "2026-11-04-0852.md").write_text("# old\n")
    (settings.reports_dir / "2026-11-05-0853.md").write_text("# new\n")
    assert call(client, "report") == (False, {"result": "# new\n"})
    assert call(client, "report", date="2026-11-04") == (False, {"result": "# old\n"})
    assert call(client, "report", date="nope")[0]


def test_weekly_review_gathers_feedback_and_learning(client, settings):
    with db.connect(settings.db_path) as c:
        c.execute("INSERT INTO sources(id, name, url, type) VALUES ('s', 'S', 'https://s.test/feed', 'rss')")
        c.execute("INSERT INTO items(id, source_id, url, normalized_url, title, fetched_at) "
                  "VALUES (1, 's', 'https://s.test/1', 'https://s.test/1', 'Meh one', ?)", (db.now(),))
        c.execute("INSERT INTO feedback(item_id, kind, raindrop_id, recorded_at) VALUES (1, 'meh', 9, ?)",
                  (datetime.now(UTC).isoformat(timespec="seconds"),))
    today = datetime.now(UTC).date().isoformat()
    settings.reports_dir.mkdir(parents=True)
    (settings.reports_dir / f"{today}-0100.md").write_text(
        "# Picks\n\n## Learning (shadow)\n\n- would drop [X]\n\n## Run stats\n\nfoo\n")

    error, review = call(client, "weekly_review")

    assert not error
    assert review["feedback_counts"] == {"meh": 1}
    assert review["feedback"][0]["title"] == "Meh one"
    assert review["source_adjustments"] == [{"source_id": "s", "adjustment": -0.08, "from": "1 meh"}]
    assert review["learning_by_report"] == [
        {"report": f"{today}-0100.md", "learning": "## Learning (shadow)\n\n- would drop [X]"}]


def test_writes_need_the_token_when_set(client, settings):
    settings.api_token = "s3cret"
    error, text = call(client, "unfollow_source", source_id="nope")
    assert error and "bearer token" in text
    error, text = call(client, "unfollow_source", token="s3cret", source_id="nope")
    assert error and "no source 'nope'" in text
    assert not call(client, "sources")[0]  # reads stay open


def test_run_job_rejects_unknown_job(client):
    error, text = call(client, "run_job", name="nope")
    assert error and "unknown job" in text
