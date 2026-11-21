"""The `stats` worker: measure new clips, read post numbers, refit the views model."""

import logging

from requests import RequestException

from .clip_stats import measure, refresh_broadcasters
from .config import Config
from .post_stats import fetch_instagram, fetch_youtube, match_by_time, sync
from .view_model import MIN_POSTS, train


def run_stats(config: Config, budget_seconds: float = 600) -> str:
    lines = []
    try:
        refresh_broadcasters(config)
    except (RequestException, RuntimeError) as error:
        logging.warning("Could not read Twitch channel details: %s", error)
    lines.append(f"Measured {measure(config, budget_seconds)} clips")
    lines.append(f"Found {sync(config)} new posts")
    lines.append(f"Matched {match_by_time(config)} posts to their clips by upload time")
    result = fetch_youtube(config)
    lines.append(f"Read YouTube numbers for {result['fetched']} posts")
    if result["missing_login"]:
        sets = ", ".join(sorted(name or "default" for name in result["missing_login"]))
        lines.append(f"Sign in again to read views: gta-clips auth youtube --account <set> ({sets})")
    if result["no_analytics"]:
        lines.append("No watch-time numbers for: " + ", ".join(sorted(result["no_analytics"])))
    try:
        instagram = fetch_instagram(config)
        lines.append(f"Read Instagram numbers for {instagram['fetched']} posts")
        if instagram["missing_login"]:
            sets = ", ".join(sorted(instagram["missing_login"]))
            lines.append(f"Generate a new Instagram token: gta-clips auth instagram --account <set> ({sets})")
    except RequestException as error:
        logging.warning("Could not read Instagram numbers: %s", error)
    model = train(config)
    lines.append(f"Views model: fitted on {model.posts} posts" if model
                 else f"Views model: waiting for {MIN_POSTS} posts with 24-hour views; clips are picked at random")
    settings = config.paths.data_dir.parent / "browser-upload.json"
    if settings.is_file():
        from .db import WorkerBusy
        from .view_tracking import export_tracking
        try:
            export_tracking(settings)
            lines.append("Rebuilt the view tracking page")
        except (OSError, ValueError, WorkerBusy) as error:
            logging.warning("Could not rebuild the view tracking page: %s", error)
    for line in lines:
        logging.info(line)
    return "\n".join(lines)
