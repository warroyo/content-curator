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
