# Configuration

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `CURATOR_LLM_BASE_URL` | `http://127.0.0.1:4000/v1` | OpenAI-compatible base URL |
| `CURATOR_LLM_API_KEY` | empty | bearer key for that endpoint |
| `CURATOR_LLM_MODEL` | `local-qwen` | model name sent in each request |
| `CURATOR_EMBED_MODEL` | `local-embed` | embedding model for same-story grouping |
| `CURATOR_LLM_TIMEOUT` | `300` | seconds per request |
| `CURATOR_LOG_LEVEL` | `INFO` | log level; logs are JSON on stderr |
| `CURATOR_DATA_DIR` | `/data` in the image | database, lock and reports |
| `CURATOR_CONFIG_DIR` | `/config` in the image | `sources.yaml` and `interests.yaml` |
| `CURATOR_API_HOST` | `127.0.0.1` | API bind address |
| `CURATOR_API_PORT` | `8090` | API port |
| `CURATOR_API_TOKEN` | empty | bearer token for POST endpoints; empty means none |
| `CURATOR_RAINDROP_TOKEN` | empty | Raindrop.io test token; publishing fails without it |
| `CURATOR_USER_AGENT` | `content-curator/0.1 (+repo URL)` | HTTP user agent, also used for `robots.txt` |

Compose-only: `CURATOR_DATA_PATH` and `CURATOR_CONFIG_PATH` (host paths to mount), `CURATOR_UID`/`CURATOR_GID` (user to run as), `CURATOR_TAG` (image tag).

## app.yaml

```yaml
schedule:
  timezone: America/Denver       # cron expressions are in this zone
  misfire_grace_seconds: 3600    # a run missed while the service was down may start this late
  jobs:
    - {job: run, cron: "30 1 * * *"}       # collect, score, curate
    # - {job: collect, cron: "0 13 * * *"}
publish:
  enabled: false                 # off: every run is a dry run that only writes the report
  collection: AI picks           # Raindrop collection, created on the first publish
  inbox_collection: To curate    # links you save here are scored and moved into the picks; empty = off
  icon: sparkle                  # icon for the collections the curator creates
```

Jobs: `collect`, `score`, `curate`, `run`. Restart the service after changing the schedule. `curator curate --publish` or `--dry-run` (and the same on `run`) overrides `publish.enabled` for one run; the scheduler and the API follow it.

## sources.yaml

```yaml
sources:
  - id: kubernetes             # unique slug
    name: Kubernetes blog
    url: https://kubernetes.io/feed.xml
    type: rss                  # rss or podcast
    topics: [topic-kubernetes]
    enabled: true
    discovery: manual          # manual | opml | discovered
    approved: true             # only approved, enabled sources are fetched
    priority: 0
    fetch_interval_hours: 4
    apple_id: 1406537385       # podcasts only, optional: looked up from the feed URL when missing
```

Removing a source from the file disables it in the database; its items stay. `import-opml` adds feeds not already present (by normalized URL), uses the OPML folder as the topic, and rewrites the file, which drops comments.

## interests.yaml

- `groups`: interest groups, each with a `tag`, a `weight` and weighted `topics`. Tags come back from the model as the item's topics.
- `exclusions`: things to score low on relevance.
- `preferred_sources`, `discovery_notes`: free text passed to the model.
- `scoring.weights`: weights of the six dimensions in the overall score; only ratios matter.
- `scoring.max_words`: text cap per item. `scoring.full_text_min_words`: below this the score is low confidence.
- `selection`:

| Key | Default | Meaning |
|---|---|---|
| `daily_count` | 8 | picks per run |
| `discovery_share` | 0.25 | share of picks reserved for strong items outside the stated interests |
| `max_per_source` | 2 | picks per source per run |
| `min_score` | 5.5 | minimum overall score for a core pick |
| `max_age_days` | 7 | freshness window for scoring and selection |
| `max_reading_minutes` | null | optional reading-time budget |
| `near_misses` | 10 | near misses listed in the report |
| `words_per_minute` | 230 | reading speed for time estimates |

- `stories`: same-story grouping.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | turn grouping off |
| `same_story` | 0.92 | cosine similarity at or above which two items from different sources are the same story without asking |
| `borderline` | 0.62 | pairs from here up to `same_story`, and any same-source pair above it, are decided by the chat model; every pair in a story must clear it |
| `max_judgements` | 80 | chat-model decisions per run |
| `embed_words` | 300 | words after the title that are embedded |

- `selection.max_per_media`: most picks per media type per run, default `{podcast: 2}`.
- `selection.reserved`: slots per media type on top of `daily_count` and outside the reading budget, filled by the best item at `min_score` or more and left empty otherwise. Default `{podcast: {slots: 1, min_score: 6.0}}`: the day's listen, shown under "Today's listen" in the report.
- `podcast_discovery`: weekly Apple Podcasts discovery.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | turn discovery off |
| `queries` | [] | search terms, one Apple search each |
| `use_top_chart` | true | also consider Apple's US Technology top 100 |
| `min_episodes` | 20 | drop shows with fewer episodes |
| `active_within_days` | 30 | drop shows that have not published lately |
| `max_judged` | 30 | candidate shows sent to the chat model per run |
| `min_fit` | 7 | fit (0-10) needed for a trial |
| `max_new_per_run` | 5 | trials added per run |
| `trial_days` | 30 | a trial with no pick in this many days is dropped |

A discovery pick needs `outside_interests`, discovery value 7+, and depth and source quality 5+.
