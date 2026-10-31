"""Command line entry point: `curator serve` runs the service; the other commands run one job and exit."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from content_curator import db, ingest, logs
from content_curator import status as queries
from content_curator.config import Settings, load_interests
from content_curator.jobs import JobBusy, JobRunner
from content_curator.opml import merge_into_sources
from content_curator.scoring import ModelUnavailable

log = logging.getLogger("curator")

# sysexits.h codes, so callers can tell outcomes apart.
EX_USAGE, EX_UNAVAILABLE, EX_TEMPFAIL = 64, 69, 75


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _job(settings: Settings, name: str, **kwargs) -> int:
    try:
        _print(JobRunner(settings).run(name, **kwargs))
    except JobBusy as e:
        log.error(str(e))
        return EX_TEMPFAIL
    except ModelUnavailable:
        log.error("model unavailable; items stay queued for the next run")
        return EX_UNAVAILABLE
    except NotImplementedError as e:
        log.error(str(e))
        return EX_USAGE
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="curator", description="Self-hosted content curator")
    p.add_argument("--log-level", default=os.environ.get("CURATOR_LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the service: API, scheduler, metrics")

    s = sub.add_parser("collect", help="fetch due sources and extract new items")
    s.add_argument("--force", action="store_true", help="fetch every source, ignoring its interval")

    s = sub.add_parser("score", help="score new items with the model")
    s.add_argument("--limit", type=int, help="score at most this many items")

    for name, help_ in (("curate", "select picks and write the report"), ("run", "collect, score and curate")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--dry-run", action="store_true", default=True,
                       help="write the report without publishing (the only mode until Phase 1b)")
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
            added, skipped = merge_into_sources(Path(args.file), settings.config_dir / "sources.yaml")
            _print({"added": added, "already_present": skipped})
            return 0
        case "add-url":
            conn = db.connect(settings.db_path)
            item_id = ingest.ingest_url(conn, settings, args.url, args.title)
            _print({"item_id": item_id, "status": "added" if item_id else "already known"})
            return 0
    return EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
