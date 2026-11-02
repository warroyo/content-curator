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
| `publish.py` | Raindrop.io client and publishing: one collection, the explanation as the note, tags, a mapping per item so a pick is never saved twice. |
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
| `POST /api/jobs/{collect,score,curate,run,discover,candidates}` | start a job; 202, or 409 if one is running. Body: `{"force": true}`, `{"limit": n}` |
| `GET /api/picks/latest[?near_misses=true]` | picks of the last curate run, with scores and explanations |
| `GET /api/items/{id}` | one item, its score and every selection decision about it ("why was this picked") |
| `POST /api/urls` | submit a URL by hand: `{"url": ..., "title": ...}` |
| `POST /api/candidates[?by=name]` | suggest sources to try: JSON or YAML, a list of `{url, name, topics, why}` or `{"submitted_by": ..., "candidates": [...]}`. Known sources and recent verdicts are skipped |
| `GET /api/candidates[?status=pending\|trial\|rejected]` | suggestions and their verdicts |
| `GET /api/sources` | every source with its status (`configured`, `trial`, `followed`, `unfollowed`, `expired`, `removed`), pick count and fetch state |
| `POST /api/sources/{id}/follow`, `/unfollow` | follow or drop a source, as the Raindrop tags do |
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
| `published` | one row per item saved to Raindrop: raindrop id, collection, link, `published`, `already_saved` or `submitted` |
| `inbox` | links pulled from the Raindrop inbox collection, with your note and tags, until moved into the picks |
| `candidates` | suggested feeds: who suggested them and why, the feed found, the fit verdict, and `pending`, `trial` or `rejected` |
| `follows` | follow and unfollow decisions per source, which outlive `sources.yaml` |

## Same-story grouping

The nightly run embeds each fresh item (title plus the first 300 words) and stores the unit vector in `embeddings`. Pairs from different sources with cosine similarity of `same_story` or more are grouped; pairs between `borderline` and `same_story`, and same-source pairs, are sent to the chat model, which answers "same specific event, or only the same topic". Its answers are cached in `story_judgements`. Groups are complete-linkage: every pair in a story must clear `borderline`, so A~B and B~C cannot pull in an unrelated C. A story keeps its id across nights.

Selection takes one candidate per story, the best-scored item, and the report lists the rest as "Also covered by". A story with a published pick is never picked again.

If the embedding model is down, grouping is skipped for that run and the rest of the run goes on.

## Publishing

With `publish.enabled` (or `--publish`), `curate` saves the picks to one Raindrop.io collection, "AI picks" by default, created on the first publish. Each raindrop gets the curator's title, the explanation as its `note` (shown on the card under the title), and tags: `ai-curated`, `media-<media>`, the item's `topic-*` tags, plus `ai-discovery`, `ai-listen`, or `ai-trial-source` (`ai-trial-show` for a podcast) for a trial source's pick. A podcast is saved as its Apple Podcasts episode link. Raindrop's `import/url/parse` supplies the type and cover; `pleaseParse` does nothing through the API.

Raindrop accepts the same URL twice, so double-publishing is guarded twice: the `published` table, and `import/url/exists`, which maps a URL already in the account to that raindrop instead of saving it again. If Raindrop fails, the report is still written and the job fails; the picks stay unpublished and can be picked again.

The curator creates its collections on first use, in list view, with a description and an icon from Raindrop's icon library.

### Inbox

Links you save to the inbox collection ("To curate"), from the phone's share sheet or the browser extension, are pulled in by `collect` as items of the `manual` source, with full text extracted like any page. The nightly run scores them, and `curate` adds every scored inbox link to the picks as `submitted`, on top of the daily count and whatever the score: the point is the curator's take on something you chose. Publishing then moves that same raindrop into the picks collection, sets the title, prepends the explanation to the note (keeping yours as "Your note: …") and adds the curator's tags to yours. Moving rather than copying keeps your highlights. In a dry run the report lists them under "You submitted" and they stay in the inbox.

## Podcasts

A `podcast` source is an RSS feed. Each episode keeps its duration, show notes, audio URL and, when the feed has a Podcasting 2.0 `podcast:transcript` tag, the transcript (preferring text, VTT or SRT over HTML). No audio is downloaded or transcribed. An episode is scored on its show notes plus the transcript; with notes only it is low confidence. An item whose link is the audio file or the show's home page is keyed by its GUID.

Each new episode gets an Apple Podcasts link: the show is found once by searching its name and matching the feed URL, then the show's recent episodes are looked up and matched by `episodeGuid`, which is the RSS GUID. The report links the episode to Apple Podcasts and to its page. Selection counts a podcast's length in minutes of audio and allows at most `max_per_media.podcast` episodes a day. One reserved slot, on top of the daily reads, goes to the best episode at 6.0 or more: the day's listen.

### Discovery

Weekly, `discover` runs podcast discovery and then the feed check (below). Podcast discovery builds candidates from a search per `podcast_discovery.queries` term and from Apple's US Technology top chart. It drops shows already known or judged in the last 90 days, shows with fewer than `min_episodes` episodes, shows silent for `active_within_days`, and shows publishing more than 10 episodes a week (back-catalogue dumps, generated shows). The rest, up to `max_judged`, go to the chat model with their last 8 episode titles, which returns a 0-10 fit against the interest profile. The best, at `min_fit` or more, become **trial sources**: collected and scored nightly (5 newest episodes at most), marked in the report, never written to `sources.yaml`. See [Trials and following](#trials-and-following).

First live run (2026-10-31): 197 candidates, 104 after filters, 30 judged in 7 minutes. Rejected included an agency-marketing feed, an AI-agent product directory, a formulaic paper-summary show and consumer gadget shows; added Kubernetes Bytes, The New Stack Podcast, Ship It Weekly and cloud2030.

## Feed discovery

Feeds come in as suggestions through `POST /api/candidates`, from anyone: a person with curl, a script, an agent that searched the web. The curator does not search for blogs itself. Suggestions are queued in `candidates`; known sources, suggestions already queued, and ones rejected in the last 90 days are skipped.

The weekly `discover` job (or `curator candidates` / `POST /api/jobs/candidates` on demand) checks up to `feed_discovery.max_checked` pending suggestions, oldest first:

1. A GitHub release, tag or commit feed is rejected without a fetch: a version number and a commit list is never worth a read.
2. The URL is fetched. If it is a page, its `<link rel="alternate">` feed is followed.
3. The feed must parse, have a dated entry within `active_within_days`, and have at most `max_per_week` entries in the last 7 days. Mostly audio enclosures make it a podcast.
4. A feed already in `sources` is rejected. The rest go to the chat model with the 8 newest titles and summaries and the suggester's reason, for a 0-10 fit.
5. Feeds at `min_fit` or more become trial sources, best first, up to `max_new_per_run` and while fewer than `max_active_trials` trials are running. One that passed but found no free slot stays pending with its verdict, and starts on a later run without being judged again.

A fetch error leaves a suggestion pending; after 3 failed runs it is rejected.

## Trials and following

A trial source, from either kind of discovery, is collected and scored nightly like any other, at most its 5 newest entries a night. Its picks are tagged `ai-trial-source` (`ai-trial-show` for a podcast) in Raindrop, and the note says it is on trial. The report lists running trials, those with a pick first as "ready to follow".

Following is a tag. Put `follow` on a pick in Raindrop and the next `collect` makes its source a permanent one (`discovery = followed`); `unfollow` disables the source, whatever it is, including one listed in `sources.yaml`. The curator then removes the tag. A tag on a raindrop the curator did not publish, or on a link you submitted yourself (which has no source), is left alone. `POST /api/sources/{id}/follow` and `/unfollow` do the same without Raindrop.

A trial that is not followed is disabled after `trial_days`, whether it was picked or not. Following it later, from an old pick, brings it back. Follow decisions live in the `follows` table and the `sources` row, never in `sources.yaml`: config sync leaves `trial` and `followed` sources alone, keeps unfollowed sources off even if the file lists them, and lets the file take over a discovered source when it lists the same id or URL.

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
