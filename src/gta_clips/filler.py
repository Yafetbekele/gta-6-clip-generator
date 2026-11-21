"""Background gameplay (Subway Surfers, Minecraft parkour, ...) under a clip: a test of filler.

Each kind of filler is a folder under ``<data_dir>/filler`` holding silent MP4s that
``gta-clips filler add`` has normalized. With ``editing.filler = "test"`` each clip gets one of
the kinds, or none, at random, so views can show whether filler helps; ``"on"`` always uses one.
The stream keeps its full frame and audio on top; the filler is muted and fills the bottom.

Like the hook test, the choice comes from a hash of the clip's ID: a retried render gets the same
kind, file and starting point. Adding or removing a kind changes later choices, not past records.
"""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import logging
import re
import sys
from tempfile import TemporaryDirectory

import av

from .diagnostics import run
from .download import ffmpeg_binary

NONE = "none"
FOLDER = "filler"
# Canvas rows for the filler layout: the hook sits above the stream, the filler fills the rest.
STREAM_TOP = 460
WIDTH = 1080


@dataclass(frozen=True)
class Filler:
    kind: str
    path: Path
    offset: float


def folder(data_dir: Path) -> Path:
    return data_dir / FOLDER


def kinds(data_dir: Path) -> list[str]:
    """Kinds that have at least one usable video, in a stable order."""
    base = folder(data_dir)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir()
                  if entry.is_dir() and any(entry.glob("*.mp4")))


def _hash(*parts: object) -> int:
    return int.from_bytes(sha256("/".join(map(str, parts)).encode()).digest()[:8], "big")


def choose(clip_id: str, mode: str, data_dir: Path) -> str | None:
    """The filler kind for this clip, NONE for a test clip left without, or None when filler is off."""
    if mode == "off":
        return None
    available = kinds(data_dir)
    if not available:
        if mode != "off":
            logging.warning("editing.filler is %r but %s has no filler videos", mode, folder(data_dir))
        return None
    number = _hash("filler", clip_id)
    if mode == "on":
        return available[number % len(available)]
    # The test leaves half the clips without filler, so the two halves compare evenly whatever
    # the number of kinds; the other half is shared out among the kinds.
    if number % 2 == 0:
        return NONE
    return available[(number // 2) % len(available)]


def _length(path: Path) -> float:
    with av.open(str(path)) as media:
        stream = media.streams.video[0]
        if stream.duration is not None and stream.time_base is not None:
            return float(stream.duration * stream.time_base)
        return media.duration / av.time_base


def pick(clip_id: str, kind: str | None, data_dir: Path, duration: float) -> Filler | None:
    """A file of ``kind`` and a starting point in it with ``duration`` seconds left, if it is long enough."""
    if not kind or kind == NONE:
        return None
    files = sorted((folder(data_dir) / kind).glob("*.mp4"))
    if not files:
        logging.warning("No %s filler videos left; rendering without filler", kind)
        return None
    path = files[_hash("file", clip_id) % len(files)]
    spare = _length(path) - duration - 1
    # Short files loop (see editing.render), so they always start at the beginning.
    offset = (_hash("offset", clip_id) % int(spare * 10)) / 10 if spare >= 1 else 0.0
    return Filler(kind, path, offset)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not slug:
        raise ValueError("Filler kind must contain letters or digits, such as subway_surfers")
    return slug


def add(data_dir: Path, kind: str, source: str, *, start: float = 0, minutes: float = 15,
        name: str | None = None) -> Path:
    """Save a silent, 30 fps, 1080-px copy of ``source`` (a local file or a video URL) as ``kind`` filler.

    Landscape videos keep a height of 864 (the filler panel is about 1080 x 852) and portrait ones
    a width of 1080, so the renderer only crops. Frequent keyframes keep the per-clip seek fast. Only ``minutes`` from ``start`` are kept.
    """
    target_dir = folder(data_dir) / _slug(kind)
    target_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_binary()
    with TemporaryDirectory(dir=target_dir, ignore_cleanup_errors=True) as directory:
        local = Path(source)
        if not local.is_file():
            result = run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
                          "--ffmpeg-location", ffmpeg, "--no-progress", "--no-warnings",
                          "--download-sections", f"*{start}-{start + minutes * 60}",
                          "-f", "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]/b",
                          "-o", str(Path(directory) / "download.%(ext)s"),
                          "--print", "after_move:filepath", "--", source], timeout=1800)
            local = Path(result.stdout.strip().splitlines()[-1])
            start = 0
        stem = _slug(name or local.stem)[:60] or "filler"
        target = target_dir / f"{stem}.mp4"
        number = 2
        while target.exists():
            target = target_dir / f"{stem}-{number}.mp4"
            number += 1
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-i", str(local),
             "-t", str(minutes * 60), "-an", "-sn", "-dn",
             "-vf", "fps=30,scale='if(gte(iw,ih),-2,1080)':'if(gte(iw,ih),864,-2)',setsar=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-g", "60", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(target)], timeout=3600, low_priority=True)
    logging.info("Added %s filler %s (%.0f s)", kind, target.name, _length(target))
    return target


def summary(data_dir: Path) -> dict[str, list[tuple[str, float]]]:
    return {kind: [(path.name, round(_length(path))) for path in sorted((folder(data_dir) / kind).glob("*.mp4"))]
            for kind in kinds(data_dir)}
