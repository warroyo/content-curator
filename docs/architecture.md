# Architecture

## Components

| Module | Role |
|---|---|
| `collectors/` | One class per source type behind the `Collector` interface. Only `rss` exists; YouTube, podcasts, Hacker News and GitHub releases are later phases. |
| `ingest.py` | Syncs `sources.yaml` into the database, runs collectors, extracts article text, marks exact duplicates. |
| `normalize.py` | URL normalization and content hashing. |
| `scoring.py` | Builds the prompt from the interest profile, calls the model with a JSON schema response format, validates with Pydantic, caches by input hash. |
| `select.py` | Deterministic selection: overall score, discovery slots, per-source cap, reading-time budget, near misses. |
| `report.py` | Markdown report of picks and near misses. |
| `service.py` | `curator serve`: FastAPI app, APScheduler cron jobs from `config/app.yaml`, `/metrics`, `/healthz`. |
| `jobs.py` | The jobs (`collect`, `score`, `curate`, `run`) and the runner that executes one at a time, records state and metrics. |
| `status.py` | Read-only queries behind `/api/status`, picks and item detail. |
| `cli.py` | `serve`, plus one-shot `collect`, `score`, `curate`, `run`, `status`, `import-opml`, `add-url`. |

Model calls are the only non-deterministic step. Fetching, extraction, deduplication, the overall score and selection are plain code.

## Service

The container runs `curator serve`: one Python process with uvicorn, the scheduler and the job runner. Jobs run in a worker thread; only one job runs at a time, and an exclusive file lock on `/data` also keeps a one-shot CLI run (`docker compose exec curator curator run`) from overlapping the service.

### API

| Method and path | Does |
|---|---|
| `GET /healthz` | liveness; checks the database opens |
| `GET /metrics` | Prometheus metrics (`curator_*`) |
| `GET /api/status` | counts, failing sources, last runs, job state and next scheduled run |
| `GET /api/jobs` | job state |
| `POST /api/jobs/{collect,score,curate,run}` | start a job; 202, or 409 if one is running. Body: `{"force": true}`, `{"limit": n}` |
| `GET /api/picks/latest[?near_misses=true]` | picks of the last curate run, with scores and explanations |
| `GET /api/items/{id}` | one item, its score and every selection decision about it ("why was this picked") |
| `POST /api/urls` | submit a URL by hand: `{"url": ..., "title": ...}` |
| `GET /api/reports/latest` | the latest markdown report |

POST endpoints need `Authorization: Bearer $CURATOR_API_TOKEN` when the token is set. The API binds `127.0.0.1:8090` by default.

### Resource use

Measured on `ubuntu-home` on 2026-10-31: idle, the service used 18 ms of CPU in 120 s (about 0.015% of one core) and 50 MiB of memory. Three things keep it idle:

- APScheduler sleeps until the next fire time instead of polling.
- uvicorn's main loop normally wakes every 100 ms; `QuietServer` makes it 2 s.
- The Docker healthcheck starts a Python process, so it runs every 5 minutes.

During the nightly run it is network-bound and waits on the model; the container has `cpu_shares: 256` and a one-core limit so the agents on the host win under contention.

## Data model

SQLite in WAL mode at `/data/curator.db`, migrated through `PRAGMA user_version`.

| Table | Holds |
|---|---|
| `sources` | registry copy plus fetch state: ETag, Last-Modified, last success, consecutive failures |
| `items` | one row per unique normalized URL; text, word count, extraction status, `duplicate_of` |
| `scores` | one row per item: six dimensions, topics, explanation, confidence, model, prompt version, input hash, tokens, latency |
| `runs` | each job run with its stats |
| `selections` | every pick and near miss per curate run, with bucket, score and reason |

## Deduplication

An item is not inserted when its normalized URL, or its GUID within the same source, already exists. After text extraction, an item points at the earliest item that shares its canonical URL or its content hash (text of 100+ words, case and whitespace normalized). Semantic clustering is Phase 2.

## Scoring

- Text comes from the feed when it has 250+ words, otherwise from the page via trafilatura. Pages disallowed by `robots.txt` are not fetched.
- Items without full text are scored on their summary and marked `low` confidence; selection subtracts 0.5 from their overall score.
- Text is capped at `scoring.max_words` words.
- The overall score is a weighted mean computed at selection time, so changing weights needs no rescoring. Changing the profile or prompt changes the input hash and rescores.

Measured on 2026-10-31 against `local-qwen` (Qwen3.8 Flash-Next on the EVO-X2 via LiteLLM): about 12 s per item, most of it decoding about 480 output tokens.

## Failure handling

- A failing source increments `consecutive_failures` and the run continues.
- A model transport error, 401/403, or repeated 429/5xx stops the score run with exit code 69. Unscored items stay queued for the next run.
- Invalid model output is retried up to three times, then that item is skipped and counted.
- Jobs take an exclusive lock on `/data/curator.lock`; a second job exits with code 75.
- Every job is safe to rerun.
