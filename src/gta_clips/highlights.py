"""Measure and combine highlight signals on the VOD timeline."""

from dataclasses import dataclass
from math import ceil, isfinite, log1p
from pathlib import Path
import re
import statistics
import subprocess
import sys

import numpy as np

from .config import HighlightsConfig
from .diagnostics import CommandFailed, _kill_tree
from .discovery import PopularClip
from .download import ffmpeg_binary

MOTION_WIDTH, MOTION_HEIGHT = 96, 54
MOTION_FPS = 2


@dataclass(frozen=True)
class Moment:
    start: int
    duration: int
    score: float
    # Each signal's contribution at the chosen peak, kept so ratings can be compared per signal.
    chat_score: float = 0.0
    audio_score: float = 0.0
    twitch_clip_score: float = 0.0
    motion_seconds: int | None = None


def audio_levels(source: Path) -> dict[int, float]:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error",
         "-i", str(source), "-vn", "-map", "0:a:0", "-af",
         "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode and "matches no streams" in result.stderr:
        return {}
    result.check_returncode()
    points = {}
    for second, level in re.findall(r"pts_time:([-\d.e+]+)\s+lavfi\.astats\.Overall\.RMS_level=(\S+)",
                                    result.stdout):
        number = float(level)
        if isfinite(number):
            points[max(0, round(float(second)))] = number
    return points


def _changed_tiles(current: np.ndarray, previous: np.ndarray) -> bool:
    # A small animated timer or facecam alone rarely changes four of these 18 tiles.
    changes = np.abs(current - previous).reshape(3, 18, 6, 16).mean(axis=(1, 3))
    return np.count_nonzero(changes >= 4.5) >= 4


def video_activity(source: Path, duration: int, timeout: int = 3600) -> set[int]:
    """Find seconds with motion across several parts of the video frame.

    FFmpeg samples two tiny grayscale frames per second, so Python never handles full frames.
    """
    frame_size = MOTION_WIDTH * MOTION_HEIGHT
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", str(source),
               "-map", "0:v:0?", "-t", str(duration), "-an",
               "-vf", f"fps={MOTION_FPS},scale={MOTION_WIDTH}:{MOTION_HEIGHT},format=gray",
               "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    flags = 0x4000 | 0x08000000 if sys.platform == "win32" else 0  # below normal, no window
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=flags)
    active, previous, index = set(), None, 0
    try:
        while True:
            data = process.stdout.read(frame_size)
            if len(data) < frame_size:
                break
            current = np.frombuffer(data, dtype=np.uint8).reshape(MOTION_HEIGHT, MOTION_WIDTH).astype(np.int16)
            if previous is not None and _changed_tiles(current, previous):
                active.add(index // MOTION_FPS)
            previous = current
            index += 1
        stderr = process.stderr.read().decode("utf-8", "replace")
        code = process.wait(timeout=timeout)
    except BaseException:
        _kill_tree(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
    if code:
        raise CommandFailed(code, command, stderr=stderr)
    return active


def _spikes(values: list[float]) -> list[float]:
    baseline = statistics.median(values)
    top = max(values)
    if top <= baseline:
        return [0.0] * len(values)
    return [max(0, value - baseline) / (top - baseline) for value in values]


def rank_moments(
    duration: int, chat: list[float], audio: dict[int, float],
    popular: list[PopularClip], settings: HighlightsConfig, *, active_seconds: set[int] | None = None,
) -> list[Moment]:
    if duration < 15:
        return []
    length = min(duration, settings.duration_seconds)
    if not settings.require_motion:
        active_seconds = None
    chat_counts = [0] * (duration + 1)
    for message in chat:
        # Viewers type after they see something, so a message points slightly earlier in the video.
        offset = message - settings.chat_reaction_lag_seconds
        if 0 <= offset < duration:
            chat_counts[int(offset) + 1] += 1
    for second in range(1, duration + 1):
        chat_counts[second] += chat_counts[second - 1]
    chatter = _spikes([log1p(chat_counts[min(duration, second + 3)] -
                              chat_counts[max(0, second - 2)]) for second in range(duration)])
    # Use the audible baseline for missing/muted samples so they create no peaks.
    baseline = statistics.median(audio.values()) if audio else 0
    loudness = _spikes([audio.get(second, baseline) for second in range(duration)])
    clip_signal = [0.0] * duration
    for clip in popular:
        end = min(duration, ceil(clip.offset + max(1, clip.duration)))
        for second in range(max(0, int(clip.offset)), end):
            clip_signal[second] = max(clip_signal[second], log1p(max(0, clip.views)) + 1)
    top_clip = max(clip_signal) or 1
    clip_scores = [score / top_clip for score in clip_signal]
    scores = [
        chat_score * settings.chat_weight + audio_score * settings.audio_weight
        + clip_score * settings.twitch_clips_weight
        for chat_score, audio_score, clip_score in zip(chatter, loudness, clip_scores)
    ]
    # Score one peak per local run of equal scores and prefer the start of a tie.
    candidates = sorted(
        (i for i, score in enumerate(scores)
         if score > 0 and (i == 0 or score > scores[i - 1])
         and (i == duration - 1 or score >= scores[i + 1])),
        key=lambda i: (-scores[i], i),
    )
    chosen = []
    for peak in candidates:
        start = min(max(0, peak - settings.lead_in_seconds), duration - length)
        if active_seconds is not None:
            end = start + length
            max_distance = 4
            nearby = range(max(start, peak - max_distance), min(end, peak + max_distance + 1))
            if not any(second in active_seconds for second in nearby):
                continue
            if sum(second in active_seconds for second in range(start, end)) < max(3, ceil(length * 0.15)):
                continue
        if all(start + length <= clip.start or clip.start + clip.duration <= start
               for clip in chosen):
            motion = (sum(second in active_seconds for second in range(start, start + length))
                      if active_seconds is not None else None)
            chosen.append(Moment(start, length, scores[peak], chatter[peak], loudness[peak],
                                 clip_scores[peak], motion))
            if len(chosen) == settings.clips_per_vod:
                break
    return chosen
