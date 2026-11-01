from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from content_curator import db
from content_curator.config import Settings

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = tmp_path / "config"
    shutil.copytree(REPO / "config", config)
    # The repo's app.yaml is the live config and may publish; tests never reach Raindrop.
    app = config / "app.yaml"
    app.write_text(app.read_text().replace("enabled: true", "enabled: false"))
    return Settings(
        data_dir=tmp_path / "data",
        config_dir=config,
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_timeout=5,
        user_agent="curator-test",
    )


@pytest.fixture
def conn(settings: Settings):
    c = db.connect(settings.db_path)
    yield c
    c.close()
