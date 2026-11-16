"""Render vertical video, Twitch credit, and Whisper captions with FFmpeg."""

from pathlib import Path
import subprocess

import av

from .captions import Transcriber, write_ass
from .config import EditingConfig, Crop
from .download import ffmpeg_binary
from .highlights import Moment


def probe_duration(path: Path) -> float:
    with av.open(str(path)) as media:
        if media.duration is None:
            raise RuntimeError("Could not read the downloaded video's duration")
        return media.duration / av.time_base


def _crop(rectangle: Crop) -> str:
    return (f"crop=w=trunc(iw*{rectangle.width}/2)*2:h=trunc(ih*{rectangle.height}/2)*2:"
            f"x=iw*{rectangle.x}:y=ih*{rectangle.y},")


def _fill(height: int) -> str:
    return (f"scale=1080:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop=1080:{height},setsar=1")


def render(
    source: Path, working: Path, target: Path, moment: Moment, streamer: str,
    settings: EditingConfig, transcriber: Transcriber,
) -> str:
    ffmpeg = ffmpeg_binary()
    audio = working / "speech.wav"
    input_args = ["-ss", str(moment.start), "-i", str(source), "-t", str(moment.duration)]
    probe_audio = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *input_args,
         "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)],
        capture_output=True, text=True, timeout=180,
    )
    if probe_audio.returncode and "matches no streams" in probe_audio.stderr:
        words = []
    else:
        probe_audio.check_returncode()
        words = transcriber.transcribe(audio)
    write_ass(working / "captions.ass", words, streamer, moment.duration)
    split = settings.streamers.get(streamer) if settings.layout == "split" else None
    if split:
        layout = (f"[0:v]split[face_src][game_src];[face_src]{_crop(split.facecam)}{_fill(720)}[face];"
                  f"[game_src]{_crop(split.gameplay)}{_fill(1200)}[game];[face][game]vstack")
    else:
        layout = f"[0:v]{_fill(1920)}"
    filters = f"{layout},fps=30,ass=captions.ass[video]"
    temporary = working / "rendered.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *input_args,
         "-filter_complex_threads", "2", "-filter_complex", filters, "-map", "[video]",
         "-map", "0:a:0?", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
         str(temporary)],
        cwd=working, check=True, timeout=600,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary.replace(target)
    return " ".join(word.text for word in words)
