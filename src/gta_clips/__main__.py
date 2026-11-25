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
    for action in ("show", "preview", "edit", "approve", "reject", "export", "rate", "hook"):
        subcommand = actions.add_parser(action)
        subcommand.add_argument("clip_id")
        if action == "hook":
            choice = subcommand.add_mutually_exclusive_group()
            choice.add_argument("--text", help="Opening overlay, at most 72 characters")
            choice.add_argument("--clear", action="store_true", help="Remove the context overlay")
            subcommand.add_argument("--suggest-only", action="store_true", help="Show a suggestion without rendering")
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
    if args.action == "hook":
        from .hook_edit import edit_hook
        edit_hook(config, args.clip_id, '' if args.clear else args.text, suggest_only=args.suggest_only)
    elif args.action == "rate":
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


LOGGED_COMMANDS = ("ingest", "publish", "ready", "stats", "watch-live", "render-live", "review-web", "run", "dashboard",
                   "viewer-clips")


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
    if command not in ("review-web", "dashboard"):
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
    commands.add_parser("ready", help="Copy new clips to the ready-to-post folder for posting by hand")
    commands.add_parser("stats", help="Measure clips, read post views and refit the views model")
    commands.add_parser("viewer-clips", help="Save the day's most-viewed Twitch viewer clips for render-live")
    posted = commands.add_parser("posted", help="Record the link of a clip you posted by hand")
    posted.add_argument("clip_id", help="The Clip ID from the clip's .txt note")
    posted.add_argument("url", help="The YouTube, TikTok or Instagram link of the post")
    web = commands.add_parser("review-web", help="Watch clips and save ratings in a local browser")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", dest="open_browser")
    commands.add_parser("run", help="Start each worker on the [runtime] schedule until stopped")
    board = commands.add_parser("dashboard", help="See what the watcher is doing and change settings")
    board.add_argument("--port", type=int, default=8766)
    board.add_argument("--open", action="store_true", dest="open_browser")
    auth = commands.add_parser("auth", help="Authorize Twitch chat or a publishing account")
    auth.add_argument("platform", choices=("twitch", *PLATFORMS))
    auth.add_argument("--account", help="Account set name from [[accounts]], such as gta6-1")
    accounts = commands.add_parser("accounts", help="List account sets, or sort earlier clips into them")
    accounts.add_argument("action", nargs="?", choices=("list", "sort"), default="list")
    retry = commands.add_parser("retry", help="Return a failed upload to its platform queue")
    retry.add_argument("platform", choices=PLATFORMS)
    retry.add_argument("clip_id")
    filler = commands.add_parser("filler", help="Add or list background gameplay shown under clips")
    filler_actions = filler.add_subparsers(dest="filler_action")
    filler_actions.add_parser("list")
    filler_add = filler_actions.add_parser("add", help="Save a silent copy of a video file or URL as filler")
    filler_add.add_argument("kind", help="Kind of filler, such as subway_surfers or minecraft_parkour")
    filler_add.add_argument("source", help="A local video file or a video URL")
    filler_add.add_argument("--start", type=float, default=0, help="Seconds to skip at the beginning")
    filler_add.add_argument("--minutes", type=float, default=15, help="Minutes to keep (default 15)")
    filler_add.add_argument("--name", help="File name for the saved copy")
    _queue_parser(commands)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")

    if args.command == "check-config":
        destinations = [platform for platform, settings in config.schedule.items() if settings.daily_limit]
        categories = ", ".join(dict.fromkeys(feed.label for feed in config.feeds))
        print(f"Configuration is valid for {categories}.")
        streamers = ("all channels in each category" if config.accounts or config.twitch.all_streamers
                     else ", ".join(config.twitch.streamers) or "none configured")
        print(f"Twitch discovery: {streamers}")
        print(f"Live chat watcher: up to {config.total_channels} channels, "
              f"bursts at {config.live.spike_multiplier:g}x the normal chat rate (run watch-live to start)")
        for account in config.accounts:
            if account.streamers:
                print(f"  Account set {account.name}: {account.watches}, live in any category")
                continue
            print(f"  Account set {account.name}: {account.watches} "
                  f"({account.language or 'any language'}), {account.channels} channels{' per category' if account.more_categories else ''}")
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
        if args.command == "dashboard":
            from .dashboard import serve as serve_dashboard
            serve_dashboard(config, args.config.expanduser().resolve(), args.port, args.open_browser)
            return 0
        if args.command == "run":
            from .runner import run
            run(args.config)
            return 0
        if args.command == "accounts":
            from .accounts import print_summary, sort_existing
            if args.action == "sort":
                moved = sort_existing(config)
                print(f'Assigned {moved["windows"]} windows ({moved["clips"]} clips) to account sets and '
                      f'moved {moved["moved"]} clip files into account folders.'
                      + (f' {moved["missing"]} clip files were missing.' if moved["missing"] else ""))
            print_summary(config)
            return 0
        if args.command == "filler":
            from . import filler as fillers
            if args.filler_action == "add":
                saved = fillers.add(config.paths.data_dir, args.kind, args.source, start=args.start,
                                    minutes=args.minutes, name=args.name)
                print(f"Saved {saved}")
            found = fillers.summary(config.paths.data_dir)
            for kind, files in found.items():
                print(f"{kind}: " + ", ".join(f"{name} ({seconds // 60:.0f} min)" for name, seconds in files))
            if not found:
                print(f"No filler yet. Add some with: gta-clips filler add subway_surfers <file or URL>")
            print(f"editing.filler = {config.editing.filler!r}")
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
        if args.command == "ready":
            from .ready import cleanup, export_due, queue_ready_for_instagram, retitle_explicit, rewrite_notes
            with worker_lock(config.paths.data_dir, "ready"):
                retitle_explicit(config)
                added = export_due(config)
                queue_ready_for_instagram(config)
                rewrite_notes(config)
                cleanup(config, config.runtime.ready_keep_days)
                from .storage import trim_clips
                trim_clips(config)
            print(f"Added {len(added)} clips to {config.paths.data_dir / 'ready-to-post'}")
            return 0
        if args.command == "stats":
            from .stats_job import run_stats
            with worker_lock(config.paths.data_dir, "stats"):
                print(run_stats(config))
            return 0
        if args.command == "viewer-clips":
            from .viewer_clips import collect
            print(f"Saved {collect(config)} viewer-clip windows")
            return 0
        if args.command == "posted":
            from .post_stats import record
            try:
                platform, video_id = record(config, args.url, args.clip_id)
            except ValueError as error:
                parser.exit(2, f"{error}\n")
            print(f"Recorded {platform} post {video_id} for {args.clip_id}")
            return 0
        if args.command == "publish":
            from .scheduler import publish_due
            return publish_due(config)
        if args.command == "auth":
            from .oauth import authorize
            name = "watch-live" if args.platform == "twitch" else "publish"
            if args.account and config.account(args.account) is None:
                names = ", ".join(account.name for account in config.accounts) or "none configured"
                parser.exit(2, f"No account set named {args.account} (account sets: {names})\n")
            with worker_lock(config.paths.data_dir, name):
                chosen = config.account(args.account) if args.account else None
                handle = getattr(chosen, "instagram", "") if args.platform == "instagram" else ""
                authorize(config.paths.database, args.platform, args.account, handle or None)
        elif args.command == "retry":
            from .scheduler import retry_failed
            retry_failed(config, args.platform, args.clip_id)
        else:
            _review(config, args)
    except WorkerBusy as exc:
        parser.exit(0 if args.command in ("ingest", "publish", "ready", "stats", "watch-live", "run",
                                                "viewer-clips") else 1, f"{exc}\n")
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
