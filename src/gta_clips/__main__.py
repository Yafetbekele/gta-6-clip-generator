"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging
import subprocess

from .config import ConfigError, PLATFORMS, load_config
from .db import WorkerBusy, worker_lock
from . import review


def _queue_parser(commands):
    queue = commands.add_parser("queue", help="Preview and review clips locally")
    actions = queue.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--state", choices=("all", "pending", "approved", "rejected"), default="pending")
    for action in ("show", "preview", "edit", "approve", "reject", "export"):
        subcommand = actions.add_parser(action)
        subcommand.add_argument("clip_id")
        if action == "edit":
            subcommand.add_argument("--title")
            subcommand.add_argument("--description")
            subcommand.add_argument("--hashtags")
        if action == "approve":
            subcommand.add_argument("--platform", action="append", required=True, choices=PLATFORMS,
                                    help="Add an upload destination. TikTok sends a draft to your inbox.")
            subcommand.add_argument("--youtube-visibility", choices=review.VISIBILITIES)
            subcommand.add_argument("--youtube-made-for-kids", action=argparse.BooleanOptionalAction)
        if action == "export":
            subcommand.add_argument("destination", type=Path)


def _review(config, args):
    if args.action == "list":
        review.list_clips(config, args.state)
    elif args.action in ("show", "preview"):
        review.show(config, args.clip_id, preview=args.action == "preview")
    elif args.action == "edit":
        review.edit(config, args.clip_id, args.title, args.description, args.hashtags)
    elif args.action == "approve":
        review.show(config, args.clip_id)
        review.decide(config, args.clip_id, args.platform, args.youtube_visibility, args.youtube_made_for_kids)
    elif args.action == "reject":
        review.decide(config, args.clip_id, [])
    else:
        review.export_clip(config, args.clip_id, args.destination)


def _logging(config, command: str):
    if command not in ("ingest", "publish"):
        return
    directory = config.paths.data_dir
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), RotatingFileHandler(
            directory / f"{command}.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8",
        )],
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gta-clips")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="TOML settings file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="Validate settings and show the configured targets")
    commands.add_parser("ingest", help="Create clips from recent completed Twitch VODs")
    commands.add_parser("publish", help="Send approved clips when their schedule permits")
    auth = commands.add_parser("auth", help="Authorize a social account through its official OAuth flow")
    auth.add_argument("platform", choices=PLATFORMS)
    retry = commands.add_parser("retry", help="Return a failed upload to its platform queue")
    retry.add_argument("platform", choices=PLATFORMS)
    retry.add_argument("clip_id")
    _queue_parser(commands)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")

    if args.command == "check-config":
        destinations = [platform for platform, settings in config.schedule.items() if settings.daily_limit]
        print(f"Configuration is valid for {config.twitch.game_name}.")
        streamers = ("all channels in this category" if config.twitch.all_streamers
                     else ", ".join(config.twitch.streamers) or "none configured")
        print(f"Twitch discovery: {streamers}")
        print(f"Storage: {config.paths.data_dir}")
        print(f"Enabled posting destinations: {', '.join(destinations) or 'none'}")
        return 0

    from dotenv import load_dotenv
    from requests import RequestException

    load_dotenv(args.config.expanduser().resolve().parent / ".env")
    try:
        _logging(config, args.command)
        if args.command == "ingest":
            from .pipeline import ingest
            return ingest(config)
        if args.command == "publish":
            from .scheduler import publish_due
            return publish_due(config)
        if args.command == "auth":
            from .oauth import authorize
            with worker_lock(config.paths.data_dir, "publish"):
                authorize(config.paths.database, args.platform)
        elif args.command == "retry":
            from .scheduler import retry_failed
            retry_failed(config, args.platform, args.clip_id)
        else:
            _review(config, args)
    except WorkerBusy as exc:
        parser.exit(0 if args.command in ("ingest", "publish") else 1, f"{exc}\n")
    except RequestException as exc:
        parser.exit(1, f"API request failed ({type(exc).__name__}). Check credentials and connectivity.\n")
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
