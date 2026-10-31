"""Turn a published podcast transcript (VTT, SRT, Podcasting 2.0 JSON, HTML, text) into plain text."""

from __future__ import annotations

import json
import re

import httpx

from content_curator.collectors.rss import html_to_text

_CUE_TIME = re.compile(r"^\s*(\d+:)?\d{1,2}:\d{2}[.,]\d{3}\s+-->\s+")
_VOICE = re.compile(r"</?v[^>]*>|</?c[^>]*>")


def _cues_to_text(body: str) -> str:
    """VTT and SRT: drop headers, cue numbers, timings and voice tags; keep spoken lines."""
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.isdigit() or _CUE_TIME.match(s) or s.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        s = _VOICE.sub("", s).strip()
        if s and (not lines or lines[-1] != s):
            lines.append(s)
    return " ".join(lines)


def to_text(body: str, mime: str) -> str:
    if mime in ("text/vtt", "application/x-subrip", "application/srt"):
        return _cues_to_text(body)
    if mime == "application/json":
        data = json.loads(body)
        segments = data.get("segments", []) if isinstance(data, dict) else data
        return " ".join(str(s.get("body", "")).strip() for s in segments if isinstance(s, dict))
    if mime == "text/html":
        return html_to_text(body) or ""
    return " ".join(body.split())


def fetch(client: httpx.Client, url: str, mime: str) -> str | None:
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    try:
        text = to_text(resp.text, mime)
    except (ValueError, AttributeError):
        return None
    return text.strip() or None
