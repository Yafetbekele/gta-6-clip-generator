"""Measure and combine highlight signals on the VOD timeline."""

from dataclasses import dataclass
from math import ceil, isfinite, log1p
from pathlib import Path
import re
import statistics
import subprocess

import av
import numpy as np

from .config import HighlightsConfig
from .discovery import PopularClip
from .download import ffmpeg_binary


@dataclass(frozen=True)
class Moment:
    start: int
    duration: int
    score: float


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


def video_activity(source: Path, duration: int) -> set[int]:
    """Find seconds with motion across several parts of the video frame."""
    active = set()
    with av.open(str(source)) as media:
        if not media.streams.video:
            return active
        stream = media.streams.video[0]
        stream.thread_type = "AUTO"
        start = None
        next_sample = 0
        previous = None
        for frame in media.decode(stream):
            if frame.time is None:
                continue
            if start is None:
                start = frame.time
            elapsed = frame.time - start
            if elapsed >= duration:
                break
            if elapsed < next_sample:
                continue
            current = frame.reformat(width=96, height=54, format="gray").to_ndarray().astype(np.int16)
            if previous is not None:
                # A small animated timer or facecam alone rarely changes four of these 18 tiles.
                changes = np.abs(current - previous).reshape(3, 18, 6, 16).mean(axis=(1, 3))
                if np.count_nonzero(changes >= 4.5) >= 4:
                    active.add(int(elapsed))
            previous = current
            next_sample = (int(2 * elapsed) + 1) / 2
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
    chat_counts = [0] * (duration + 1)
    for offset in chat:
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
    scores = [
        chat_score * settings.chat_weight + audio_score * settings.audio_weight
        + clip_score / top_clip * settings.twitch_clips_weight
        for chat_score, audio_score, clip_score in zip(chatter, loudness, clip_signal)
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
            chosen.append(Moment(start, length, scores[peak]))
            if len(chosen) == settings.clips_per_vod:
                break
    return chosen
