"""Command line entry point: `curator serve` runs the service; the other commands run one job and exit."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from content_curator import db, logs
from content_curator import status as queries
from content_curator.config import Settings, load_interests
from content_curator.jobs import EX_TEMPFAIL, EX_UNAVAILABLE, EX_USAGE, JobBusy, JobRunner

log = logging.getLogger("curator")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _job(settings: Settings, name: str, **kwargs) -> int:
    from content_curator.scoring import ModelUnavailable

    try:
        _print(JobRunner(settings).run(name, **kwargs))
    except JobBusy as e:
        log.error(str(e))
        return EX_TEMPFAIL
    except ModelUnavailable:
        log.error("model unavailable; items stay queued for the next run")
        return EX_UNAVAILABLE
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="curator", description="Self-hosted content curator")
    p.add_argument("--log-level", default=os.environ.get("CURATOR_LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the service: API, scheduler, metrics")

    s = sub.add_parser("collect", help="fetch due sources and extract new items")
    s.add_argument("--force", action="store_true", help="fetch every source, ignoring its interval")

    sub.add_parser("stories", help="embed fresh items and group same-story coverage")
    sub.add_parser("discover", help="find new podcast shows on Apple Podcasts as trial sources")

    s = sub.add_parser("score", help="score new items with the model")
    s.add_argument("--limit", type=int, help="score at most this many items")

    for name, help_ in (("curate", "select picks, publish them and write the report"),
                        ("run", "collect, score and curate")):
        s = sub.add_parser(name, help=help_)
        mode = s.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", dest="dry_run", action="store_const", const=True,
                          help="write the report without publishing")
        mode.add_argument("--publish", dest="dry_run", action="store_const", const=False,
                          help="publish to Raindrop even if publish.enabled is off in app.yaml")
        if name == "run":
            s.add_argument("--force", action="store_true")

    sub.add_parser("status", help="show counts, failing sources and the last runs")

    s = sub.add_parser("import-opml", help="add feeds from an OPML file to sources.yaml")
    s.add_argument("file")

    s = sub.add_parser("add-url", help="submit one URL by hand")
    s.add_argument("url")
    s.add_argument("--title")

    args = p.parse_args(argv)
    logs.setup(args.log_level)
    settings = Settings.from_env()

    match args.cmd:
        case "serve":
            from content_curator.service import serve

            serve(settings)
            return 0
        case "collect":
            return _job(settings, "collect", force=args.force)
        case "discover":
            return _job(settings, "discover")
        case "stories":
            return _job(settings, "stories")
        case "score":
            return _job(settings, "score", limit=args.limit)
        case "curate":
            return _job(settings, "curate", dry_run=args.dry_run)
        case "run":
            return _job(settings, "run", dry_run=args.dry_run, force=args.force)
        case "status":
            conn = db.connect(settings.db_path)
            _print(queries.status(conn, load_interests(settings.config_dir).selection.max_age_days))
            return 0
        case "import-opml":
            from content_curator.opml import merge_into_sources

            added, skipped = merge_into_sources(Path(args.file), settings.config_dir / "sources.yaml")
            _print({"added": added, "already_present": skipped})
            return 0
        case "add-url":
            from content_curator import ingest

            conn = db.connect(settings.db_path)
            item_id = ingest.ingest_url(conn, settings, args.url, args.title)
            _print({"item_id": item_id, "status": "added" if item_id else "already known"})
            return 0
    return EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
