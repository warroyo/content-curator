# Decisions

The full plan and its decision log are in the vault note "Personal AI content curator plan". Decisions taken while building:

## 2026-10-31: everything builds, tests and runs in Docker

Context: the curator is lab software and should be portable. Decision: a multi-stage Dockerfile with `runtime` and `test` targets, compose for every job, a Makefile that only wraps compose. No Python is needed on the host. Host setup (Docker, compose, the deployed stack) is Ansible in dev-environment.

## 2026-10-31: Chat Completions with a JSON schema response format

Context: the plan asked whether Responses or Chat Completions gives schema-valid JSON more reliably through LiteLLM. Decision: Chat Completions with `response_format: json_schema, strict: true`. It returned valid JSON on the first tests against `local-qwen`. The client still validates with Pydantic, strips code fences and retries invalid output.

## 2026-10-31: overall score computed in code

The model returns six dimension scores; the weighted overall score is computed at selection time. Rejected: asking the model for an overall score, which would need rescoring whenever weights change and is less predictable.

## 2026-10-31: a long-running service with its own scheduler and API

Context: the first cut was one-shot CLI containers, with host timers planned to run them. That spreads the app across the host and makes it a set of scripts. Decision: `curator serve` is the app: an HTTP API, APScheduler cron jobs from `config/app.yaml`, Prometheus metrics and a healthcheck, run by compose with `restart: unless-stopped`. Deploying is `docker compose up -d`; Ansible only installs Docker and places the compose file, `.env` and config. The API is also the interface Hermes needs in Phase 4. Rejected: systemd timers or host cron calling `docker compose run` (schedule lives outside the app); supercronic in the container (a second process and still a script runner).

## 2026-10-31: near-zero CPU when idle; one run a night

Requirement: the service should use CPU only when its schedule runs. The default schedule is one nightly `run` (collect, score, curate), with an optional daytime `collect` commented out; collection every 4 hours was dropped. The collector reads up to 100 entries per feed, enough for a daily fetch. Idle CPU was measured at 2 ms/s with stock uvicorn, from its 100 ms tick; a 2 s tick brought it to 0.15 ms/s. The healthcheck runs every 5 minutes.

## 2026-10-31: host networking

The container uses `network_mode: host` to reach LiteLLM on `127.0.0.1:4000` without binding LiteLLM to the Docker bridge.

## 2026-10-31: same-story grouping with a lab-wide embedding model

The embedding model is Qwen3-Embedding-0.6B on the EVO-X2, behind LiteLLM's `local-embed` alias, set up in dev-environment as a lab-wide service (RAG will use it too). It sits in a persistent, non-exclusive llama-swap group, so an embedding call never unloads Flash-Next. Vectors live in SQLite and are compared with numpy; about 250 items a day needs no vector database. Grouping uses fixed thresholds plus the chat model for the borderline band, with complete linkage to avoid chaining. Rejected: running the embedding model on `ubuntu-home`'s CPU (the box is already the lab's bottleneck), and a vector database (no need at this scale). The thresholds are first guesses, to be tuned on real runs.

## 2026-10-31: embeddings find candidates, the chat model decides

Calibration on 177 items (15,576 pairs) with Qwen3-Embedding-0.6B: median similarity 0.27, true same-story pairs between 0.65 and 0.87, false pairs up to 0.87 (LWN's daily "Security updates for Friday/Thursday" at 0.873, two unrelated OpenAI customer stories at 0.813). No threshold separates them. So pairs from 0.62 up go to the chat model, auto-merge is only for near-duplicates at 0.92 from different sources, and two items from one source are always judged. First run: 47 judgements in 4.5 minutes, 7 stories of 17 items, every merge and every high-similarity rejection correct on inspection.

## 2026-10-31: Apple Podcasts links instead of Spotify

The plan linked episodes to Spotify. Spotify needs a developer app, matches episodes only by title and date, and has restricted its Web API for new apps. Apple's iTunes Search API needs no key and returns each episode's RSS GUID, so matching is exact. Will is fine listening in Apple Podcasts. The episode page stays the fallback. YouTube is deferred to the backlog: it needs a Data API key and Will watches little YouTube, so it has to be discovery-led.

## 2026-10-31: podcast discovery through trials judged by the chat model

Apple's keyword search ranks by relevance, not date, and its results for a topic like "AI agents" are mostly junk (generated shows, product directories, dead or one-episode shows). The top chart is better quality but broad. Discovery therefore takes both as candidates, filters on cheap facts (episodes, recency, cadence), and has the chat model judge fit from each show's recent episodes, as section 4.10 of the plan describes. Found shows run as trials rather than waiting for approval, because discovery is the point, and a trial that never produces a pick drops out. A trial contributes at most its 5 newest episodes a night, after one feed republished 30 old episodes with fresh dates.

## 2026-10-31: thinking stays on

Turning off Qwen's thinking would cut scoring time about 3x, but the run is overnight and time does not matter; quality does. Revisit only if the source list grows enough to push the run past morning.

## 2026-10-31: a reserved daily listen

On the first run with podcasts, no episode was picked: most feeds give only show notes, so episodes are scored low-confidence and lose to the day's best articles. Podcasts are listened to at other times (commute, chores), so they should not compete for reading slots. One slot a day, on top of `daily_count` and outside the reading budget, goes to the best episode at 6.0 or more; with none, it stays empty. Episodes can still win reading slots on score, up to `max_per_media.podcast`.
