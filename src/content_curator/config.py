"""Configuration: environment settings plus the YAML source registry and interest profile."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    """Runtime settings, read from environment variables."""

    data_dir: Path
    config_dir: Path
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    user_agent: str
    api_host: str = "127.0.0.1"
    api_port: int = 8090
    # Bearer token required for POST endpoints. Empty means no auth (loopback only).
    api_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.environ.get("CURATOR_DATA_DIR", "data")),
            config_dir=Path(os.environ.get("CURATOR_CONFIG_DIR", "config")),
            llm_base_url=os.environ.get("CURATOR_LLM_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/"),
            llm_api_key=os.environ.get("CURATOR_LLM_API_KEY", ""),
            llm_model=os.environ.get("CURATOR_LLM_MODEL", "local-qwen"),
            llm_timeout=float(os.environ.get("CURATOR_LLM_TIMEOUT", "300")),
            user_agent=os.environ.get(
                "CURATOR_USER_AGENT", "content-curator/0.1 (+https://github.com/warroyo/content-curator)"
            ),
            api_host=os.environ.get("CURATOR_API_HOST", "127.0.0.1"),
            api_port=int(os.environ.get("CURATOR_API_PORT", "8090")),
            api_token=os.environ.get("CURATOR_API_TOKEN", ""),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "curator.db"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


class Source(BaseModel):
    id: str
    name: str
    url: str
    type: str = "rss"
    topics: list[str] = Field(default_factory=list)
    enabled: bool = True
    # "manual" sources are approved; "opml" and "discovered" record where a source came from.
    discovery: str = "manual"
    approved: bool = True
    priority: int = 0
    fetch_interval_hours: float = 4

    @field_validator("id")
    @classmethod
    def _id_slug(cls, v: str) -> str:
        if not v or any(c.isspace() for c in v):
            raise ValueError("source id must be a non-empty slug without spaces")
        return v


class Topic(BaseModel):
    name: str
    weight: float = 1.0
    keywords: list[str] = Field(default_factory=list)


class TopicGroup(BaseModel):
    name: str
    tag: str
    weight: float = 1.0
    topics: list[Topic]


class Weights(BaseModel):
    relevance: float = 0.35
    novelty: float = 0.15
    depth: float = 0.2
    source_quality: float = 0.15
    actionability: float = 0.1
    discovery_value: float = 0.05


class Scoring(BaseModel):
    weights: Weights = Field(default_factory=Weights)
    max_words: int = 3000
    # Below this many words of extracted text, the model only saw a title or summary.
    full_text_min_words: int = 250


class Selection(BaseModel):
    daily_count: int = 8
    discovery_share: float = 0.25
    max_per_source: int = 2
    min_score: float = 5.5
    max_age_days: int = 7
    max_reading_minutes: int | None = None
    near_misses: int = 10
    words_per_minute: int = 230


class Interests(BaseModel):
    groups: list[TopicGroup]
    exclusions: list[str] = Field(default_factory=list)
    preferred_sources: list[str] = Field(default_factory=list)
    discovery_notes: str = ""
    scoring: Scoring = Field(default_factory=Scoring)
    selection: Selection = Field(default_factory=Selection)


class ScheduledJob(BaseModel):
    job: str
    # Standard five-field cron expression, in the schedule's timezone.
    cron: str
    enabled: bool = True


class Schedule(BaseModel):
    timezone: str = "UTC"
    jobs: list[ScheduledJob] = Field(default_factory=list)
    # Seconds a missed run (for example while the service was down) may still start late.
    misfire_grace_seconds: int = 3600


class AppConfig(BaseModel):
    schedule: Schedule = Field(default_factory=Schedule)


def load_app_config(config_dir: Path) -> AppConfig:
    path = config_dir / "app.yaml"
    return AppConfig(**_load_yaml(path)) if path.exists() else AppConfig()


def _load_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_sources(config_dir: Path) -> list[Source]:
    data = _load_yaml(config_dir / "sources.yaml")
    sources = [Source(**s) for s in data.get("sources", [])]
    ids = [s.id for s in sources]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate source ids in sources.yaml: {sorted(dupes)}")
    return sources


def load_interests(config_dir: Path) -> Interests:
    return Interests(**_load_yaml(config_dir / "interests.yaml"))
