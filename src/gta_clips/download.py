"""Acquire a temporary VOD and convert available replay chat to timestamps."""

import json
import logging
from math import isfinite
from pathlib import Path
import os
import shutil
import subprocess
import sys

from imageio_ffmpeg import get_ffmpeg_exe

from .diagnostics import CommandFailed, describe, run


def ffmpeg_binary() -> str:
    """Prefer a configured binary and fall back to the wheel's FFmpeg."""
    configured = os.getenv("FFMPEG")
    if configured:
        return shutil.which(configured) or str(Path(configured).expanduser().resolve())
    return get_ffmpeg_exe()


class VodUnavailable(RuntimeError):
    """The VOD cannot be downloaded without access this tool does not have. Retrying will not help."""


# yt-dlp messages that mean the video is restricted or gone, not that the network hiccupped.
UNAVAILABLE_MARKERS = (
    "subscriber-only", "only available to subscribers", "must be logged into an account",
    "does not exist", "has been removed", "video is unavailable", "video unavailable",
    "this video is not available", "http error 404",
)


def format_selector(max_height: int | None) -> str:
    if not max_height:
        return "best[ext=mp4]/best"
    return (f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/"
            "best[ext=mp4]/best")


def download_vod(url: str, directory: Path, *, section: tuple[float, float] | None = None,
                 max_height: int | None = None) -> Path:
    """Fetch a VOD or one section of it.

    A section is copied without re-encoding. FFmpeg's input seek makes time 0 of the file match
    ``section[0]`` on the VOD, so callers should request a few seconds of padding and trim
    exactly while rendering, which re-encodes anyway.
    """
    options = [] if section is None else ["--download-sections", f"*{section[0]}-{section[1]}"]
    command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
               "--ffmpeg-location", ffmpeg_binary(),
               "--no-progress", "--no-warnings", "--retries", "3", "--fragment-retries", "5",
               *options, "-f", format_selector(max_height), "-o", str(directory / "source.%(ext)s"),
               "--print", "after_move:filepath", "--", url]
    try:
        result = run(command, timeout=4 * 3600 if section is None else 300, low_priority=True)
    except CommandFailed as error:
        if not error.timed_out and any(marker in (error.stderr or "").lower()
                                       for marker in UNAVAILABLE_MARKERS):
            raise VodUnavailable(error.summary) from None
        raise
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise RuntimeError("yt-dlp did not report a VOD file")
    path = Path(lines[-1])
    if not path.is_file():
        raise RuntimeError("yt-dlp did not produce a VOD file")
    return path


def read_chat(path: Path) -> list[float]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    offsets = (item.get("content_offset_seconds") for item in payload["comments"])
    return [float(value) for value in offsets
            if isinstance(value, (int, float)) and isfinite(value) and value >= 0]


def replay_chat(vod_id: str, directory: Path, imports: Path) -> list[float]:
    existing = imports / f"{vod_id}.json"
    path = directory / "chat.json"
    binary = os.environ.get("TWITCH_DOWNLOADER") or shutil.which("TwitchDownloaderCLI")
    try:
        if existing.is_file():
            return read_chat(existing)
        if binary:
            run([binary, "chatdownload", "-u", vod_id, "-o", str(path)], timeout=600)
            return read_chat(path)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        logging.warning("Chat replay unavailable for %s (%s)", vod_id, describe(exc))
        return []
    logging.info("Set TWITCH_DOWNLOADER or import chat to enable chat scoring")
    return []
