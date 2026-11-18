"""Bridge exact, uncaptioned footage and Whisper timing to the local Remotion project."""

import json
import math
import os
from pathlib import Path
import shutil

from .captions import Word
from .diagnostics import run
from .context_text import manual_hook, suggest_hook


def caption_data(words: list[Word], duration: float) -> list[dict]:
    result = []
    for word in sorted(words, key=lambda item: item.start):
        if not math.isfinite(word.start) or not math.isfinite(word.end):
            continue
        start, end = max(0, word.start), min(duration, word.end)
        if end <= start or not word.text.strip():
            continue
        result.append(dict(text=word.text.strip(), startMs=round(start * 1000),
                           endMs=round(end * 1000), timestampMs=None, confidence=None))
    return result


def render_polished(source: Path, working: Path, target: Path, moment, streamer: str,
                    words: list[Word], ffmpeg: str, hook_text: str | None = None):
    hook = suggest_hook(' '.join(word.text for word in words)) if hook_text is None else manual_hook(hook_text)
    project = Path(os.environ.get("REMOTION_PROJECT", Path(__file__).resolve().parents[2] / "remotion")).resolve()
    node = shutil.which("node")
    if not node or not (project / "node_modules" / "@remotion" / "renderer").exists():
        raise RuntimeError("Remotion is not installed. Install Node.js, then run npm ci in remotion/.")
    assets = working.resolve() / "remotion-assets"
    assets.mkdir(exist_ok=True)
    # Trim before composition so audio, footage, and captions all share time zero.
    # This intermediate has no burned-in captions or credit.
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source.resolve()),
         "-ss", str(moment.start), "-t", str(moment.duration), "-map", "0:v:0", "-map", "0:a:0?",
         "-vf", "fps=30,setsar=1", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(assets / "source.mp4")],
        timeout=600, low_priority=True)
    # Remotion can hold a final video frame; reject incomplete VOD windows first.
    from .editing import FRAME_TOLERANCE, video_duration
    if abs(video_duration(assets / "source.mp4") - moment.duration) > FRAME_TOLERANCE:
        raise ValueError("The source does not cover the full Remotion window yet")
    job = working.resolve() / "remotion-job.json"
    job.write_text(json.dumps({"source": "source.mp4", "streamer": streamer,
                              "durationSeconds": moment.duration,
                              "hook": hook.to_dict(),
                              "captions": caption_data(words, moment.duration)}, ensure_ascii=False), encoding="utf-8")
    run([node, str(project / "render.mjs"), str(job), str(assets), str(target.resolve())],
        cwd=project, timeout=1800, low_priority=True)
