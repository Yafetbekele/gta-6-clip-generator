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


def ffmpeg_binary() -> str:
    """Prefer a configured binary and fall back to the wheel's FFmpeg."""
    configured = os.getenv("FFMPEG")
    if configured:
        return shutil.which(configured) or str(Path(configured).expanduser().resolve())
    return get_ffmpeg_exe()


def download_vod(url: str, directory: Path, *, section: tuple[int, int] | None = None) -> Path:
    options = [] if section is None else [
        "--download-sections", f"*{section[0]}-{section[1]}", "--force-keyframes-at-cuts",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
         "--ffmpeg-location", ffmpeg_binary(),
         "--no-progress", "--no-warnings", "--retries", "3", "--fragment-retries", "5",
         *options, "-f", "best[ext=mp4]/best", "-o", str(directory / "source.%(ext)s"),
         "--print", "after_move:filepath", "--", url],
        timeout=4 * 3600 if section is None else 180, check=True, capture_output=True, text=True,
    )
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
            subprocess.run([binary, "chatdownload", "-u", vod_id, "-o", str(path)],
                           check=True, timeout=600, capture_output=True)
            return read_chat(path)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        logging.warning("Chat replay unavailable for %s (%s)", vod_id, type(exc).__name__)
        return []
    logging.info("Set TWITCH_DOWNLOADER or import chat to enable chat scoring")
    return []
