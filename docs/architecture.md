# Architecture

## Components

| Module | Role |
|---|---|
| `collectors/` | One class per source type behind the `Collector` interface: `rss` and `podcast`. YouTube is on the backlog. |
| `apple.py` | Apple Podcasts through the keyless iTunes Search API: episode links by GUID, show search, the Technology top chart. |
| `transcripts.py` | Published podcast transcripts (VTT, SRT, JSON, HTML) to plain text. |
| `discover.py` | Weekly podcast discovery: candidate shows, cheap filters, a chat-model fit check, trial sources. |
| `ingest.py` | Syncs `sources.yaml` into the database, runs collectors, extracts article text, marks exact duplicates. |
| `normalize.py` | URL normalization and content hashing. |
| `stories.py` | Embeds fresh items through `/v1/embeddings` (`local-embed`) and groups same-story coverage; borderline pairs go to the chat model. |
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

## Same-story grouping

The nightly run embeds each fresh item (title plus the first 300 words) and stores the unit vector in `embeddings`. Pairs from different sources with cosine similarity of `same_story` or more are grouped; pairs between `borderline` and `same_story`, and same-source pairs, are sent to the chat model, which answers "same specific event, or only the same topic". Its answers are cached in `story_judgements`. Groups are complete-linkage: every pair in a story must clear `borderline`, so A~B and B~C cannot pull in an unrelated C. A story keeps its id across nights.

Selection takes one candidate per story, the best-scored item, and the report lists the rest as "Also covered by". Once publishing exists, a story with a published pick is never picked again.

If the embedding model is down, grouping is skipped for that run and the rest of the run goes on.

## Podcasts

A `podcast` source is an RSS feed. Each episode keeps its duration, show notes, audio URL and, when the feed has a Podcasting 2.0 `podcast:transcript` tag, the transcript (preferring text, VTT or SRT over HTML). No audio is downloaded or transcribed. An episode is scored on its show notes plus the transcript; with notes only it is low confidence. An item whose link is the audio file or the show's home page is keyed by its GUID.

Each new episode gets an Apple Podcasts link: the show is found once by searching its name and matching the feed URL, then the show's recent episodes are looked up and matched by `episodeGuid`, which is the RSS GUID. The report links the episode to Apple Podcasts and to its page. Selection counts a podcast's length in minutes of audio and allows at most `max_per_media.podcast` episodes a day.

### Discovery

Weekly, `discover` builds candidates from a search per `podcast_discovery.queries` term and from Apple's US Technology top chart. It drops shows already known or judged in the last 90 days, shows with fewer than `min_episodes` episodes, shows silent for `active_within_days`, and shows publishing more than 10 episodes a week (back-catalogue dumps, generated shows). The rest, up to `max_judged`, go to the chat model with their last 8 episode titles, which returns a 0-10 fit against the interest profile. The best, at `min_fit` or more, become **trial sources**: collected and scored nightly (5 newest episodes at most), marked in the report, never written to `sources.yaml`. Following one is adding it to `sources.yaml`. A trial with no pick in `trial_days` is disabled.

First live run (2026-10-31): 197 candidates, 104 after filters, 30 judged in 7 minutes. Rejected included an agency-marketing feed, an AI-agent product directory, a formulaic paper-summary show and consumer gadget shows; added Kubernetes Bytes, The New Stack Podcast, Ship It Weekly and cloud2030.

## Deduplication

An item is not inserted when its normalized URL, or its GUID within the same source, already exists. After text extraction, an item points at the earliest item that shares its canonical URL or its content hash (text of 100+ words, case and whitespace normalized). Same-story grouping is the step above.

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
