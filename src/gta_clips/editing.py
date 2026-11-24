"""Render vertical video, Twitch credit, and Whisper captions with FFmpeg."""

from dataclasses import replace
from pathlib import Path
import json
import logging
import time

import av

from .captions import Transcriber, write_ass
from .chat_talk import reason as chat_talk_reason
from .config import EditingConfig, Crop
from .context_text import debate_hook, manual_hook
from .diagnostics import CommandFailed, run
from .download import ffmpeg_binary
from .filler import Filler, STREAM_TOP
from .highlights import Moment
from .trim import Plan as TrimPlan, plan as trim_plan


class SkippedMoment(Exception):
    """The moment is not worth a clip (see chat_talk.py); nothing was encoded."""


# A section downloaded while the VOD is still live can start at the 10 s HLS segment boundary before
# the requested second; its end is right, so the extra length is how early it starts.
MAX_LEAD = 10.5
MIN_LEAD = 0.25


def source_lead(source: Path, section_length: float) -> float:
    """Seconds the downloaded section starts before the requested start (0 when it starts on time)."""
    lead = probe_duration(source) - section_length
    if lead < MIN_LEAD:
        return 0.0
    if lead > MAX_LEAD:
        logging.warning("%s is %.1fs longer than the %.1fs section; not shifting the cut",
                        source.name, lead, section_length)
        return 0.0
    return lead


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


def video_duration(path: Path) -> float:
    """Length of the first video stream, which (unlike the container) ignores longer audio."""
    with av.open(str(path)) as media:
        stream = media.streams.video[0]
        if stream.duration is not None and stream.time_base is not None:
            return float(stream.duration * stream.time_base)
        if stream.frames and stream.average_rate:
            return float(stream.frames / stream.average_rate)
        return media.duration / av.time_base


def max_source_height(settings: EditingConfig) -> int | None:
    """The fit layout shrinks a landscape frame to 1080 px wide, so 720p sources are enough."""
    return 720 if settings.layout == "fit" else None


FRAME_TOLERANCE = 1.5 / 30


def _video_top(source: Path) -> int:
    """Where the fit layout places the top edge of the full, uncropped frame."""
    with av.open(str(source)) as media:
        stream = media.streams.video[0]
        width, height = stream.width, stream.height
    if not width or not height:
        return 0
    scaled = min(1920, 1080 * height / width)
    return int((1920 - scaled) // 2)


WIDTH = 1080
# With filler the stream fits a 1080 x 608 box (a full 16:9 frame) starting at STREAM_TOP.
FILLER_STREAM_HEIGHT = 608


def _stream_box(source: Path) -> tuple[int, int]:
    """Height of the stream inside the filler layout's box, and the row where the filler starts."""
    with av.open(str(source)) as media:
        stream = media.streams.video[0]
        width, height = stream.width, stream.height
    scaled = FILLER_STREAM_HEIGHT if not width or not height else min(FILLER_STREAM_HEIGHT, WIDTH * height / width)
    box = int(scaled) // 2 * 2
    return box, (STREAM_TOP + (FILLER_STREAM_HEIGHT + box) // 2) // 2 * 2


def render(
    source: Path, working: Path, target: Path, moment: Moment, streamer: str,
    settings: EditingConfig, transcriber: Transcriber, details: dict | None = None,
    *, hook_text: str | None = None, topic: str = "", hook_key: str = "", language: str = "en",
    hook_style: str = "quote", filler: Filler | None = None,
    trim: bool = False, protect: tuple[float, float] | None = None,
    caption_style: str = "standard",
    skip_chat_talk: bool = False, burst: float | None = None,
    section_length: float | None = None,
) -> str:
    """Render ``moment`` exactly. ``details`` (if given) receives timing and speech facts.

    With ``trim`` dead time is cut out (see trim.py), never within ``protect`` (seconds into the
    clip); ``details["trimmed_s"]`` says how much went, and the video is that much shorter.

    Without ``hook_text`` a debate prompt is generated from the speech and ``topic``
    (the Twitch category) in ``hook_style`` (see context_text.debate_hook); ``hook_text=""``
    renders no headline. ``details["hook_method"]`` says which kind of hook was shown.
    With ``filler`` (see filler.py) the whole stream sits near the top and the muted filler video
    fills the bottom; only the FFmpeg renderer supports it.
    With ``skip_chat_talk`` a moment where the streamer mostly talks to chat raises SkippedMoment
    before anything is encoded; ``burst`` is when its chat burst peaked, in seconds into the clip.
    ``section_length`` is how long the downloaded section should be; a longer file started early
    (see source_lead), so the cut moves later by the difference and still shows the chosen window.
    ``details["source_lead_s"]`` says by how much.
    """
    ffmpeg = ffmpeg_binary()
    lead = source_lead(source, section_length) if section_length else 0.0
    if lead:
        logging.info("%s starts %.2fs early; shifting the cut to match", source.name, lead)
        moment = replace(moment, start=moment.start + lead)
    audio = working / "speech.wav"
    # Output-side -ss/-t decode from the previous keyframe and cut on the exact frame.
    input_args = ["-i", str(source), "-ss", str(moment.start), "-t", str(moment.duration)]
    began = time.monotonic()
    has_audio = True
    try:
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *input_args,
             "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)],
            timeout=180, low_priority=True)
        words = transcriber.transcribe(audio, language or None)
    except CommandFailed as error:
        if "matches no streams" not in (error.stderr or ""):
            raise
        words, has_audio = [], False
    transcribed = time.monotonic()
    if skip_chat_talk:
        why = chat_talk_reason(words, language or "en", burst)
        if why:
            raise SkippedMoment(why)
    if trim and settings.renderer == "remotion":
        logging.warning("The Remotion renderer does not support trimming; rendering %s untrimmed", target.name)
        trim = False
    cut = (trim_plan(words, audio if has_audio else None, moment.duration, protect) if trim
           else TrimPlan([], moment.duration))
    words = cut.words(words)
    duration = moment.duration - cut.removed
    # Quote only speech confidently in the headline's language; garbled quotes look broken.
    language = language or "en"
    matches = (getattr(transcriber, "language", None) == language
               and (getattr(transcriber, "language_probability", None) or 0) >= 0.7)
    hook = (debate_hook(" ".join(w.text for w in words), topic, hook_key or streamer, quote=matches,
                        language=language, style=hook_style)
            if hook_text is None else manual_hook(hook_text))
    if filler is not None and settings.renderer == "remotion":
        logging.warning("The Remotion renderer does not support filler; rendering %s without it", target.name)
        filler = None
    if settings.renderer == "remotion":
        from .remotion_render import render_polished
        temporary = working / "rendered.mp4"
        render_polished(source, working, temporary, moment, streamer, words, ffmpeg, hook=hook)
    else:
        extra_inputs = []
        if filler is not None:
            box, seam = _stream_box(source)
            hook_bottom = STREAM_TOP + (FILLER_STREAM_HEIGHT - box) // 2 - 30
            # Captions straddle the seam between the stream and the filler.
            write_ass(working / "captions.ass", words, streamer, duration, hook, hook_bottom,
                      caption_margin=1920 - seam - 130, style=caption_style)
        else:
            hook_bottom = _video_top(source) - 30 if settings.layout == "fit" else None
            write_ass(working / "captions.ass", words, streamer, duration, hook,
                      hook_bottom if hook_bottom and hook_bottom >= 420 else None, style=caption_style)
        split = settings.streamers.get(streamer) if settings.layout == "split" else None
        # Dropping to 30 fps first halves the scaling work for 60 fps sources.
        if filler is not None:
            # The filler loops if it is short. The clip is cut inside the filter graph, so its
            # filler starts at zero.
            extra_inputs = ["-stream_loop", "-1", "-ss", str(filler.offset), "-t", str(moment.duration + 2),
                            "-i", str(filler.path)]
            layout = (f"[0:v]fps=30,scale={WIDTH}:{FILLER_STREAM_HEIGHT}:force_original_aspect_ratio=decrease:"
                      f"force_divisible_by=2,pad={WIDTH}:1920:(ow-iw)/2:{STREAM_TOP}+({FILLER_STREAM_HEIGHT}-ih)/2:"
                      f"color=black,setsar=1[top];"
                      f"[1:v]fps=30,{_fill(1920 - seam)}[bottom];"
                      f"[top][bottom]overlay=0:{seam}:eof_action=repeat:shortest=1")
        elif split:
            layout = (f"[0:v]fps=30,split[face_src][game_src];"
                      f"[face_src]{_crop(split.facecam)}{_fill(720)}[face];"
                      f"[game_src]{_crop(split.gameplay)}{_fill(1200)}[game];[face][game]vstack")
        elif settings.layout == "fit":
            # Preserve every edge, with spare vertical space for captions and credit.
            layout = ("[0:v]fps=30,scale=1080:1920:force_original_aspect_ratio=decrease:"
                      "force_divisible_by=2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
        else:
            layout = f"[0:v]fps=30,{_fill(1920)}"
        filters = f"{layout},ass=captions.ass[video]"
        # The window is always cut inside the filter graph, so the captions see clip time. An
        # output-side -ss cuts only after filtering: the captions saw source time and ran
        # moment.start (3 s on live clips) ahead of the speech.
        # Each kept stretch is cut from the source and joined; the layout then reads the result.
        parts, labels = [], []
        for index, (start, end) in enumerate(cut.keep):
            begin, finish = moment.start + start, moment.start + end
            parts.append(f"[0:v]trim=start={begin:.3f}:end={finish:.3f},setpts=PTS-STARTPTS[v{index}]")
            labels.append(f"[v{index}]")
            if has_audio:
                parts.append(f"[0:a:0]atrim=start={begin:.3f}:end={finish:.3f},asetpts=PTS-STARTPTS[a{index}]")
                labels.append(f"[a{index}]")
        joined = "[joined][joined_audio]" if has_audio else "[joined]"
        parts.append(f"{''.join(labels)}concat=n={len(cut.keep)}:v=1:a={int(has_audio)}{joined}")
        filters = ";".join(parts) + ";" + filters.replace("[0:v]", "[joined]", 1)
        audio_map = "[joined_audio]" if has_audio else "0:a:0?"
        temporary = working / "rendered.mp4"
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), *extra_inputs,
             "-filter_complex_threads", "2", "-filter_complex", filters, "-map", "[video]",
             "-map", audio_map, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
             str(temporary)],
            cwd=working, timeout=600, low_priority=True)
    encoded = time.monotonic()
    rendered = video_duration(temporary)
    # Each joined stretch can end up to a frame off.
    if abs(rendered - duration) > FRAME_TOLERANCE * max(1, len(cut.keep)):
        raise ValueError(f"Rendered clip is {rendered:.2f}s, expected {duration:.2f}s; "
                         "the source does not cover the full window yet")
    target.parent.mkdir(parents=True, exist_ok=True)
    if settings.renderer == "remotion":
        # Keep editable text and timing, not the raw video that is cleaned after rendering.
        manifest = json.loads((working / "remotion-job.json").read_text(encoding="utf-8"))
        manifest.pop("source", None)
        manifest["schema_version"] = 1
    else:
        manifest = {"schema_version": 1, "renderer": "ffmpeg", "streamer": streamer,
                    "durationSeconds": duration, "hook": hook.to_dict()}
        if filler is not None:
            manifest["filler"] = {"kind": filler.kind, "file": filler.path.name, "offset": filler.offset}
        if cut.cuts:
            manifest["trimmed"] = [[round(start, 2), round(end, 2)] for start, end in cut.cuts]
    # The review page reads the hook from here.
    target.with_suffix('.edit.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)
    if details is not None:
        details.update(
            transcribe_seconds=round(transcribed - began, 2), encode_seconds=round(encoded - transcribed, 2),
            word_count=len(words), speech_seconds=round(sum(w.end - w.start for w in words), 2),
            # Seconds into the clip when the first and last words are spoken.
            first_word_s=round(words[0].start, 2) if words else None,
            last_word_s=round(words[-1].end, 2) if words else None,
            language=getattr(transcriber, "language", None) if words else None,
            language_probability=getattr(transcriber, "language_probability", None) if words else None,
            hook_method=hook.method,
            source_lead_s=round(lead, 2),
            trimmed_s=round(cut.removed, 2),
            # Seconds removed before the protected stretch (a live clip's burst), so its new position is known.
            trimmed_before_s=round(sum(end - start for start, end in cut.cuts
                                       if protect is not None and end <= protect[0]), 2),
        )
    logging.info("Rendered %s: transcribe %.1fs, encode %.1fs", target.name,
                 transcribed - began, encoded - transcribed)
    return " ".join(word.text for word in words)
