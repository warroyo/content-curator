"""Markdown dry-run report: the day's picks, their scores and reasons, and the near misses."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from content_curator.select import Decision, Result


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _pick(d: Decision) -> str:
    c = d.candidate
    length = f"{c.minutes} min" if c.minutes else "length unknown"
    meta = [c.source_name, f"podcast, {length}" if c.media == "podcast" else length]
    if c.published_at:
        meta.append(c.published_at[:10])
    if c.confidence == "low":
        meta.append("low confidence: show notes only" if c.media == "podcast" else "low confidence: summary only")
    dims = (
        f"rel {c.relevance:g} · nov {c.novelty:g} · depth {c.depth:g} · src {c.source_quality:g} · "
        f"act {c.actionability:g} · disc {c.discovery_value:g}"
    )
    tags = ", ".join(f"`{t}`" for t in c.topics) or "none"
    also = ""
    if c.media == "podcast":
        where = [f"[Apple Podcasts]({c.listen_url})"] if c.listen_url else []
        where.append(f"[episode page]({c.url})")
        also += f"\nListen: {' · '.join(where)}\n"
    if c.trial:
        also += (f"\nFrom **{c.source_name}**, a show found by discovery that you do not follow yet. "
                 f"To follow it, add it to `sources.yaml`.\n")
    if c.also:
        links = ", ".join(f"[{src}]({url})" for src, _, url in c.also)
        also += f"\nAlso covered by: {links}\n"
    link = c.listen_url or c.url
    return (
        f"### {d.rank}. [{c.title}]({link})\n\n"
        f"{' · '.join(meta)}  \n"
        f"**{c.overall}** overall, {d.bucket} ({d.reason}). {dims}. Topics: {tags}\n\n"
        f"> {c.explanation}\n{also}"
    )


def render(result: Result, stats: dict, dry_run: bool, trials: list[dict] | None = None) -> str:
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
    active = [t for t in trials or [] if t["enabled"]]
    if active:
        lines += ["", "## Shows on trial", "",
                  "Found by podcast discovery and collected for a trial period. To keep one, add it to "
                  "`sources.yaml` with `type: podcast`.", ""]
        lines += [f"- **{_md(t['name'])}** ({t['url']}), found for \"{_md(t['discovery_query'] or '')}\" "
                  f"on {(t['discovered_at'] or '')[:10]}" for t in active]
    lines += ["", "## Run stats", "", "```"]
    lines += [f"{k}: {v}" for k, v in stats.items()]
    lines += ["```", ""]
    return "\n".join(lines)


def write(reports_dir: Path, text: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{datetime.now(UTC):%Y-%m-%d-%H%M}.md"
    path.write_text(text)
    return path
