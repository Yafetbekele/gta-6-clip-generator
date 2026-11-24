"""Cutting dead time out of a clip: a test of tighter editing.

Dead time is a stretch where nobody speaks for at least MIN_GAP seconds and the sound stays at
least QUIET_DB below the clip's talking, so loud gameplay without talking is kept. Each such
stretch is cut down to EDGE seconds on each side. Nothing is cut within PROTECT seconds of a live
clip's chat burst, so the build-up and the payoff stay whole, and at most MAX_SHARE of a clip is
removed. The layout, captions and hook are otherwise the same as an untrimmed clip.

With ``editing.trim_dead_time = "test"`` half the clips are trimmed and half are not, chosen from a
hash of the clip ID (not the clock or anything about the clip), so a retried render gets the same
choice and views can show whether trimming helps. "on" trims every clip, "off" none.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import wave

import numpy as np

from .captions import Word

TRIM = "trim"
NONE = "none"
MIN_GAP = 1.2
EDGE = 0.25
MIN_CUT = 0.5
PROTECT = 5.0
MAX_SHARE = 0.35
FRAME = 0.1
# A gap counts as quiet when at least QUIET_SHARE of it is QUIET_DB or more below the clip's talking.
QUIET_SHARE = 0.8
QUIET_DB = 10.0


def choose(clip_id: str, mode: str) -> str | None:
    """TRIM or NONE for this clip, or None when the test is off (the clip is not trimmed)."""
    if mode == "off":
        return None
    if mode == "on":
        return TRIM
    return TRIM if sha256(f"trim/{clip_id}".encode()).digest()[0] % 2 == 0 else NONE


@dataclass(frozen=True)
class Plan:
    cuts: list[tuple[float, float]]  # stretches removed, in seconds into the untrimmed clip
    duration: float  # the untrimmed clip's length

    @property
    def removed(self) -> float:
        return sum(end - start for start, end in self.cuts)

    @property
    def keep(self) -> list[tuple[float, float]]:
        """The stretches kept, in order."""
        found, at = [], 0.0
        for start, end in self.cuts:
            if start > at:
                found.append((at, start))
            at = end
        if at < self.duration:
            found.append((at, self.duration))
        return found

    def shift(self, moment: float) -> float:
        """Where ``moment`` of the untrimmed clip lands in the trimmed one."""
        return moment - sum(min(end, moment) - start for start, end in self.cuts if start < moment)

    def words(self, words: list[Word]) -> list[Word]:
        return [replace(word, start=self.shift(word.start), end=self.shift(word.end)) for word in words]


def _loudness(path: Path) -> np.ndarray:
    """Loudness in dB of each FRAME-second piece of a 16-bit mono WAV file."""
    with wave.open(str(path), "rb") as audio:
        rate = audio.getframerate()
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16).astype(float)
    size = max(1, int(rate * FRAME))
    count = len(samples) // size
    if not count:
        return np.array([])
    pieces = samples[:count * size].reshape(count, size)
    return 20 * np.log10(np.sqrt((pieces ** 2).mean(axis=1)) + 1.0)


def _frames(levels: np.ndarray, start: float, end: float) -> np.ndarray:
    return levels[int(start / FRAME):max(int(start / FRAME) + 1, int(end / FRAME))]


def _quiet(levels: np.ndarray, start: float, end: float, limit: float) -> bool:
    piece = _frames(levels, start, end)
    return bool(len(piece)) and float((piece < limit).mean()) >= QUIET_SHARE


def plan(words: list[Word], audio: Path | None, duration: float,
         protect: tuple[float, float] | None = None) -> Plan:
    """The dead time to cut from a clip of ``duration`` seconds with these spoken ``words``.

    ``protect`` is a stretch (seconds into the clip) that is never cut. Without audio to check,
    nothing is cut: silence in speech alone can't tell a pause from loud action.
    """
    levels = _loudness(audio) if audio is not None and audio.is_file() else np.array([])
    if not len(levels):
        return Plan([], duration)
    # How loud the talking is; a clip with no words falls back to its overall median.
    spoken = [_frames(levels, word.start, word.end) for word in words]
    spoken = np.concatenate(spoken) if spoken else np.array([])
    limit = float(np.median(spoken if len(spoken) else levels)) - QUIET_DB
    edges = [0.0, *[value for word in sorted(words, key=lambda w: w.start) for value in (word.start, word.end)],
             duration]
    gaps = [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2) if edges[i + 1] - edges[i] >= MIN_GAP]
    pieces = []
    for start, end in gaps:
        if not _quiet(levels, start, end, limit):
            continue
        # Keep a moment of breath on each side, except at the very start and end of the clip.
        start, end = (start + EDGE if start > 0 else 0.0), (end - EDGE if end < duration else duration)
        spans = [(start, end)]
        if protect is not None:
            low, high = protect
            spans = [(a, b) for a, b in ((start, min(end, low)), (max(start, high), end)) if b > a]
        pieces.extend((a, b) for a, b in spans if b - a >= MIN_CUT)
    # The longest dead stretches go first until the cap is reached.
    chosen, total = [], 0.0
    for start, end in sorted(pieces, key=lambda piece: piece[0] - piece[1]):
        if total + end - start <= MAX_SHARE * duration:
            chosen.append((start, end))
            total += end - start
    return Plan(sorted(chosen), duration)
