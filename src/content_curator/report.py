"""Markdown dry-run report: the day's picks, their scores and reasons, and the near misses."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from content_curator.select import Decision, Result


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _pick(d: Decision) -> str:
    c = d.candidate
    meta = [c.source_name, f"{c.minutes} min" if c.minutes else "length unknown"]
    if c.published_at:
        meta.append(c.published_at[:10])
    if c.confidence == "low":
        meta.append("low confidence: summary only")
    dims = (
        f"rel {c.relevance:g} · nov {c.novelty:g} · depth {c.depth:g} · src {c.source_quality:g} · "
        f"act {c.actionability:g} · disc {c.discovery_value:g}"
    )
    tags = ", ".join(f"`{t}`" for t in c.topics) or "none"
    return (
        f"### {d.rank}. [{c.title}]({c.url})\n\n"
        f"{' · '.join(meta)}  \n"
        f"**{c.overall}** overall, {d.bucket} ({d.reason}). {dims}. Topics: {tags}\n\n"
        f"> {c.explanation}\n"
    )


def render(result: Result, stats: dict, dry_run: bool) -> str:
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Curator picks, {when}",
        "",
        f"{'Dry run: nothing was published.' if dry_run else 'Published to Reader.'} "
        f"{len(result.picks)} picks from {stats.get('candidates', 0)} candidates.",
        "",
        "## Picks",
        "",
    ]
    lines += [_pick(d) for d in result.picks] or ["No item cleared the threshold.", ""]
    lines += ["## Near misses", "", "| Score | Item | Source | Why not |", "|---|---|---|---|"]
    for d in result.near_misses:
        c = d.candidate
        lines.append(f"| {c.overall} | [{_md(c.title)}]({c.url}) | {_md(c.source_name)} | {_md(d.reason)} |")
    lines += ["", "## Run stats", "", "```"]
    lines += [f"{k}: {v}" for k, v in stats.items()]
    lines += ["```", ""]
    return "\n".join(lines)


def write(reports_dir: Path, text: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{datetime.now(UTC):%Y-%m-%d-%H%M}.md"
    path.write_text(text)
    return path
