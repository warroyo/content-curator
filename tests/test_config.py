"""Config files: plain YAML, or a markdown note with ```yaml blocks."""

from __future__ import annotations

from content_curator.config import load_interests, load_sources

NOTE = """---
type: note
---

Feeds the curator follows.

```yaml
sources:
  - id: a
    name: A
    url: https://a.test/feed
```

Podcasts:

```yaml
  - id: p
    name: P
    url: https://p.test/feed
    type: podcast
```
"""


def test_sources_from_markdown_note(tmp_path, monkeypatch):
    note = tmp_path / "content-curator-sources.md"
    note.write_text(NOTE)
    monkeypatch.setenv("CURATOR_SOURCES_FILE", str(note))
    assert [s.id for s in load_sources(tmp_path)] == ["a", "p"]


def test_interests_default_to_config_dir(settings, monkeypatch):
    monkeypatch.delenv("CURATOR_INTERESTS_FILE", raising=False)
    assert load_interests(settings.config_dir).groups
