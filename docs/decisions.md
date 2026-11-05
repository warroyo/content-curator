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

## 2026-11-01: Raindrop.io instead of Readwise Reader

**Context.** Phase 1b publishes the picks somewhere Will reads them. The plan named Readwise Reader, which is paid.

**Decision.** Publish to one "AI picks" collection in Raindrop.io, on the free plan, through its REST API with a test token. The explanation goes in the raindrop's `note`, with `ai-curated`, `topic-*` and `media-*` tags. `POST /import/url/exists` backs up the internal mapping against double-publishing.

**Alternatives rejected.** Readwise Reader: paid. Its reading progress would have given a better quality measure. Raindrop has a clean article preview with highlights, so reading in the app is not lost.

**Consequences.** Raindrop has no read state, so feedback is what Will does to a pick: favourite, highlight, tag `more-like-this` or `meh`, move it elsewhere (kept), or delete it (soft negative). An untouched pick is unknown. Raindrop reads no RSS, so `sources.yaml` stays the only feed list.

## 2026-11-01: published image, config outside it

**Context.** The curator first ran from a working copy with `make up`. The lab's rule is that dev-environment installs everything on the servers, and it should install, not build.

**Decision.** GitHub Actions tests every push and publishes the runtime image to GHCR on pushes to main. dev-environment pins an image by digest and mounts a config directory. The repo and the image are public, so the server needs no registry login; nothing secret is in either. The curator reads only YAML from `/config`. Where that YAML comes from is the deployment's business: in the home lab, dev-environment renders it from two notes in the private Obsidian vault, which are easier to read and edit than YAML files.

**Alternatives rejected.** Building the image on the server from a clone (mixes build and deploy). A private image with a read-only registry token (more moving parts for no secret). Teaching the curator to read markdown notes (ties the application to one person's note format). Config in dev-environment (public, and tuning preferences would mean a pull request).

## 2026-11-01: feed discovery from suggestions, following through Raindrop tags

**Context.** Finding good blogs was the curator's weakest point: podcast discovery had Apple's directory to search, feeds had nothing. A one-off web search by an agent found 27 candidates, and the sources it found were added by hand. That should be regular, and following a source should not mean editing a file.

**Decision.** The curator takes suggestions through `POST /api/candidates` and does the checking itself: find the feed, check it parses, is recent, is not a firehose or a GitHub release feed, then let the chat model judge fit. Passing feeds run as trials, like podcast trials. Following and dropping are `follow` and `unfollow` tags on picks in Raindrop, read on every collect; the decisions live in the database. A trial ends after `trial_days` unless followed, picked or not, so the trial list cannot grow without bound.

**Alternatives rejected.** Searching the web from the curator (needs a search API and is a different job; any agent or person can suggest instead). Letting the suggester validate feeds (a local model gets fetch-and-parse wrong; code does not). Writing followed sources back to `sources.yaml` or to the deployment's notes (ties the application to one deployment's config format; the config stays a hand-kept base list). Keeping picked trials forever (the old rule, which left trials nobody decided on).

**Consequences.** The full source list is `sources.yaml` plus the database: `GET /api/sources` shows it. Who suggests feeds, and how, is the deployment's business; in the home lab a weekly Hermes job does. Podcast trials with picks that were never followed now end at `trial_days` like the rest; following one from an old pick brings it back.

## 2026-11-04: read and meh tags, deletions recorded

**Context.** Raindrop has no read state, so "AI picks" only grows: read picks, unwanted picks and new ones sit together. The plan had `meh` and deletion as feedback signals, but nothing read them.

**Decision.** Three actions on a pick, each recorded in a `feedback` table. A `read` tag moves the pick to an "AI read" collection; a `meh` tag (less like this: its topics and source) moves it to "AI skipped"; deleting it is a light negative for that one item, read from the Trash. The curator removes the tag, as it does for `follow` and `unfollow`. "AI picks" becomes the unread queue. A new `feedback` job applies the tags every 30 minutes in the day, so a tagged pick leaves the queue the same day; `collect` applies them too.

**Alternatives rejected.** Moving a pick to "AI read" by hand (fewer API calls, but Will prefers a tag). Deletion as the only "not wanted" action (one swipe, but it cannot tell "not this one" from "less like this"). Expiring untouched picks after N days (an untouched pick is unknown, and the curator does not move what you have not touched).

**Consequences.** The rule that the curator never moves Raindrop content now has one exception: a pick you tagged `read` or `meh`, and only while it is still in the collection it was published to. A deletion is lost if the Trash is emptied before the next feedback run. `meh` and `deleted` are only recorded until preference learning uses them; `unfollow` stays the way to drop a whole source.

## 2026-11-04: preference learning as capped adjustments, in shadow first

**Context.** The `feedback` table records favourites, `meh` tags, deletions and reads. Nothing used them. About ten picks a day give at most a few dozen signals a week, only on items that were picked.

**Decision.** Two adjustments to the overall score, worked out from the feedback on every curate run and never stored: one per source (decayed, shrunk mean signal, ±0.5 at most) and one for embedding similarity to recent favourites and `meh` picks (±0.5 at most). Weights: favourite +2, meh -2, deleted -0.5, read 0. Core picks only; a positive adjustment never lifts an item over `min_score`. `learning.mode` starts at `shadow`, which only reports what would change. `learning.since` resets.

**Alternatives rejected.** Learning per topic tag (the tags are a handful of broad groups, so one meh would tilt a whole interest). Putting liked and disliked items into the scoring prompt (breaks the score cache and the explanation of why an item moved). A `like` tag for positives (the favourite is a long press in Raindrop's mobile app). Reading highlights (left for later). A read as a positive (finishing something is not liking it).

**Consequences.** Favourites are read nightly from the curator's own collections only, back 90 days. Learning cannot narrow the discovery share. One signal moves a source by under 0.1, so learning shows only after a few weeks of feedback.
