"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging
import sqlite3
import subprocess
import sys

from .config import ConfigError, PLATFORMS, load_config
from .db import WorkerBusy, worker_lock
from .diagnostics import describe, install_thread_logging, log_versions
from . import review


def _queue_parser(commands):
    queue = commands.add_parser("queue", help="Preview and review clips locally")
    actions = queue.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--state", choices=("all", "pending", "approved", "rejected"), default="pending")
    ratings = actions.add_parser("ratings-export")
    ratings.add_argument("destination", type=Path, help="A .json or .csv file")
    actions.add_parser("ratings-summary", help="Describe how ratings relate to recorded features")
    for action in ("show", "preview", "edit", "approve", "reject", "export", "rate"):
        subcommand = actions.add_parser(action)
        subcommand.add_argument("clip_id")
        if action == "rate":
            subcommand.add_argument("score", type=int, choices=range(1, 6))
            subcommand.add_argument("--notes", default="")
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
    if args.action == "rate":
        review.rate(config, args.clip_id, args.score, args.notes)
    elif args.action == "ratings-export":
        review.export_ratings(config, args.destination)
    elif args.action == "ratings-summary":
        review.print_ratings_summary(config)
    elif args.action == "list":
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


LOGGED_COMMANDS = ("ingest", "publish", "watch-live", "render-live", "review-web")


def _logging(config, command: str) -> bool:
    if command not in LOGGED_COMMANDS:
        return False
    directory = config.paths.data_dir
    directory.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(
        directory / f"{command}.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8",
    )]
    if sys.stderr is not None:  # pythonw.exe (the Desktop shortcut) has no console.
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)
    install_thread_logging()
    if command != "review-web":
        log_versions()
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gta-clips")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="TOML settings file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="Validate settings and show the configured targets")
    commands.add_parser("ingest", help="Create clips from recent completed Twitch VODs")
    commands.add_parser("watch-live", help="Watch live Twitch chats and clip bursts for review")
    commands.add_parser("render-live", help="Process saved chat bursts that now have a VOD")
    commands.add_parser("live-status", help="List recent chat bursts and their clip status")
    commands.add_parser("publish", help="Send approved clips when their schedule permits")
    web = commands.add_parser("review-web", help="Watch clips and save ratings in a local browser")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", dest="open_browser")
    auth = commands.add_parser("auth", help="Authorize Twitch chat or a publishing account")
    auth.add_argument("platform", choices=("twitch", *PLATFORMS))
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
        print(f"Live chat watcher: up to {config.live.max_channels} channels, "
              f"{config.live.messages_per_10_seconds} messages/10s minimum (run watch-live to start)")
        print(f"Storage: {config.paths.data_dir}")
        print(f"Enabled posting destinations: {', '.join(destinations) or 'none'}")
        return 0

    from dotenv import load_dotenv
    from requests import RequestException

    load_dotenv(args.config.expanduser().resolve().parent / ".env")
    logged = False

    def fail(message: str, error: BaseException, code: int = 1):
        # parser.exit only reaches the console; keep the reason and traceback in the log file too.
        if logged:
            logging.error("%s exiting: %s", args.command, describe(error), exc_info=error)
        parser.exit(code, message)

    try:
        logged = _logging(config, args.command)
        if args.command == "review-web":
            from .review_web import serve
            serve(config, args.port, args.open_browser)
            return 0
        if args.command == "ingest":
            from .pipeline import ingest
            return ingest(config)
        if args.command == "watch-live":
            from .live import watch
            watch(config)
            return 0
        if args.command in ("render-live", "live-status"):
            from .live_render import live_status, render_due
            if args.command == "render-live":
                render_due(config)
            else:
                live_status(config)
            return 0
        if args.command == "publish":
            from .scheduler import publish_due
            return publish_due(config)
        if args.command == "auth":
            from .oauth import authorize
            name = "watch-live" if args.platform == "twitch" else "publish"
            with worker_lock(config.paths.data_dir, name):
                authorize(config.paths.database, args.platform)
        elif args.command == "retry":
            from .scheduler import retry_failed
            retry_failed(config, args.platform, args.clip_id)
        else:
            _review(config, args)
    except WorkerBusy as exc:
        parser.exit(0 if args.command in ("ingest", "publish", "watch-live") else 1, f"{exc}\n")
    except RequestException as exc:
        fail(f"API request failed ({type(exc).__name__}). Check credentials and connectivity.\n", exc)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
        fail(f"{describe(exc)}\n", exc)
    except KeyboardInterrupt:
        if logged:
            logging.info("%s stopped by Ctrl+C", args.command)
        return 130
    except Exception as exc:
        fail(f"Unexpected error: {describe(exc)}\n", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
