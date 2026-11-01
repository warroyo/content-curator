"""OPML import into config/sources.yaml (from Inoreader or any feed reader's export)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from content_curator.normalize import normalize_url


def parse_opml(path: Path) -> list[dict]:
    """Return one dict per feed outline, with the enclosing folder as a topic."""
    feeds = []

    def walk(node: ET.Element, folder: str | None) -> None:
        for child in node.findall("outline"):
            url = child.get("xmlUrl")
            title = child.get("title") or child.get("text") or url
            if url:
                feeds.append({"name": title, "url": url, "folder": folder})
            else:
                walk(child, title)

    body = ET.parse(path).getroot().find("body")
    if body is not None:
        walk(body, None)
    return feeds


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "feed"


def merge_into_sources(opml_path: Path, sources_path: Path) -> tuple[int, int]:
    """Add feeds not already in sources.yaml. Returns (added, skipped)."""
    data = yaml.safe_load(sources_path.read_text()) if sources_path.exists() else None
    data = data or {}
    sources = data.setdefault("sources", [])
    known_urls = {normalize_url(s["url"]) for s in sources}
    known_ids = {s["id"] for s in sources}
    added = skipped = 0
    for feed in parse_opml(opml_path):
        if normalize_url(feed["url"]) in known_urls:
            skipped += 1
            continue
        sid = base = slug(feed["name"])
        n = 2
        while sid in known_ids:
            sid, n = f"{base}-{n}", n + 1
        entry = {"id": sid, "name": feed["name"], "url": feed["url"], "type": "rss", "discovery": "opml"}
        if feed["folder"]:
            entry["topics"] = [slug(feed["folder"])]
        sources.append(entry)
        known_ids.add(sid)
        known_urls.add(normalize_url(feed["url"]))
        added += 1
    sources_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return added, skipped
