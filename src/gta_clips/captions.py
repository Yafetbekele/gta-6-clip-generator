"""Run Whisper locally and generate ASS subtitles with active-word highlighting."""

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
import logging
import os
import wave

import numpy as np

from .caption_styles import LOOKS, STANDARD
from .metadata import mask_explicit


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


CPU = "cpu"
CUDA = "cuda"
# A clip is spoken in the account's language unless Whisper is at least this sure it is another one.
OTHER_LANGUAGE_CONFIDENCE = 0.8
# How Whisper decodes, tested on real clips (2026-11-24):
# - No VAD: Silero VAD cut real speech out of clips with loud game audio (a 15 s stretch became
#   "Ooh." twenty times), with the base model and large-v3-turbo alike.
# - Temperature 0 only: the default fallback to random sampling made the same clip come out with
#   79 to 147 words from one run to the next, sometimes dropping whole sentences.
# - Conditioned on earlier text: without it, at temperature 0 Whisper sometimes jumped 30 s ahead
#   mid-clip and skipped everything said in between.
DECODE = dict(word_timestamps=True, vad_filter=False, temperature=0.0, condition_on_previous_text=True)

_models: dict[tuple[str, str], object] = {}
_models_lock = Lock()
# One clip at a time on the GPU, so two render workers never hold two copies of its working memory.
_gpu_lock = Lock()


def _cuda_libraries():
    """Let CTranslate2 find cuBLAS and cuDNN from the nvidia-*-cu12 pip packages on Windows."""
    if os.name != "nt":
        return
    try:
        import nvidia
    except ImportError:
        return
    for root in getattr(nvidia, "__path__", []):
        for folder in Path(root).glob("*/bin"):
            os.add_dll_directory(str(folder))
            os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"


def _load(name: str, device: str):
    from faster_whisper import WhisperModel
    with _models_lock:
        key = (name, device)
        if key not in _models:
            if device == CUDA:
                _cuda_libraries()
                _models[key] = WhisperModel(name, device=CUDA, compute_type="float16")
            else:
                _models[key] = WhisperModel(name, device=CPU, compute_type="int8")
        return _models[key]


def choose_language(detected: str | None, probability: float, wanted: str | None) -> str | None:
    """The language to transcribe in: the account's, unless the speech is clearly another one.

    Left to itself Whisper often mistakes short, noisy clips for a neighbouring language
    (Portuguese heard as Catalan, English as Welsh) and then writes nonsense in that language.
    """
    if not wanted or detected == wanted:
        return detected or wanted
    return detected if probability >= OTHER_LANGUAGE_CONFIDENCE else wanted


class Transcriber:
    """faster-whisper on the GPU (``device="auto"`` or "cuda") or the CPU.

    With "auto" a GPU that is missing or fails falls back to the CPU for the rest of the process.
    Models are shared between Transcribers, so several render workers load each one once.
    """

    def __init__(self, model: str, device: str = "auto"):
        self.name = model
        self.device = device
        # Facts about the most recent transcription, kept as rating features.
        self.language: str | None = None
        self.language_probability: float | None = None

    def _devices(self) -> list[str]:
        if self.device == CPU:
            return [CPU]
        if self.device == CUDA:
            return [CUDA]
        try:
            import ctranslate2
            has_gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            has_gpu = False
        return [CUDA, CPU] if has_gpu else [CPU]

    def transcribe(self, audio: Path, language: str | None = None) -> list[Word]:
        """Words spoken in a 16 kHz mono WAV file; ``language`` is the account's (see choose_language)."""
        with wave.open(str(audio), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError("Whisper input must be 16 kHz mono PCM16")
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        devices = self._devices()
        for device in devices:
            try:
                with _gpu_lock if device == CUDA else nullcontext():
                    return self._run(_load(self.name, device), samples, language)
            except Exception as error:
                if device == devices[-1]:
                    raise
                # cuBLAS and cuDNN load on first use, so a broken GPU setup shows up here, not at load.
                logging.warning("Whisper on the GPU failed (%s); using the CPU from now on", error)
                self.device = CPU

    def _run(self, model, samples: np.ndarray, language: str | None) -> list[Word]:
        if not len(samples):
            self.language, self.language_probability = None, None
            return []
        detected, probability, _ = model.detect_language(samples, vad_filter=True)
        self.language, self.language_probability = detected, float(probability)
        segments, _ = model.transcribe(samples, language=choose_language(detected, probability, language),
                                       **DECODE)
        return tidy([Word(word.start, word.end, word.word.strip())
                     for segment in segments for word in segment.words or []
                     if word.word.strip() and word.start is not None and word.end is not None])


def longest_word(text: str) -> float:
    """The most time one word can plausibly take to say, in seconds."""
    return 0.3 + 0.08 * len(text)


def tidy(words: list[Word]) -> list[Word]:
    """Words in time order, each starting no earlier than it can plausibly be said.

    Whisper often stretches a word back over the pause before it ("might" from 4.0 to 5.4 when it
    is said at 5.3), which lit the caption up before the streamer spoke. Such a word keeps its end
    and starts ``longest_word`` before it.
    """
    tidied = []
    for word in sorted(words, key=lambda item: item.start):
        start = max(word.start, word.end - longest_word(word.text))
        tidied.append(Word(start, max(start, word.end), word.text))
    return tidied


# The last words of a phrase stay up this long after they are said, unless the next phrase starts.
HOLD = 0.4


def _time(centiseconds: int) -> str:
    seconds, hundredths = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}.{hundredths:02}"


def _escape(text: str) -> str:
    return " ".join(text.translate(str.maketrans({"\\": "＼", "{": "｛", "}": "｝"})).split())


def _shown(text: str) -> str:
    """Spoken words and hook text as burned in: swearing is starred out (see metadata.EXPLICIT).

    YouTube age-restricted clips whose captions or titles swore; the audio itself is unchanged.
    """
    return _escape(mask_explicit(text))


def write_ass(path: Path, words: list[Word], streamer: str, duration: float, hook=None,
              hook_bottom: int | None = None, caption_margin: int = 340, style: str = STANDARD):
    """``hook`` is a ContextHook headline for the whole clip, its last line ending at ``hook_bottom``.

    Spoken captions end ``caption_margin`` pixels above the bottom edge, in ``style``
    (see caption_styles.py).
    """
    look = LOOKS[style]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{look.size},&H00FFFFFF,&H00FFFFFF,{look.outline_colour},&H80000000,-1,0,0,0,100,100,0,0,{look.border_style},{look.outline:g},2,2,85,120,{caption_margin},1
Style: Credit,Arial,37,&H00FFFFFF,&H00FFFFFF,&H00131313,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,7,75,110,110,1
Style: Hook,Arial,64,&H00FFFFFF,&H00FFFFFF,&H00131313,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,8,70,70,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    total = round(duration * 100)
    events = [f"Dialogue: 0,0:00:00.00,{_time(total)},Credit,,0,0,0,,@{_escape(streamer)} on Twitch"]
    if hook is not None and hook.text:
        text = _shown(hook.text)
        if hook.method.endswith("quote_v1"):
            text = f"“{text}”"
        if hook.label:
            # The label sits on its own line in the accent colour, above the take itself.
            text = r"{\fs46\c&H0058FFC4&}" + _shown(hook.label) + r"{\r}\N" + text
        if hook_bottom:
            text = rf"{{\an2\pos(540,{hook_bottom})}}" + text
        events.append(f"Dialogue: 1,0:00:00.00,{_time(total)},Hook,,0,0,0,,{text}")
    groups = []
    group = []
    for word in words:
        if group and (len(group) >= look.max_words or word.start - group[-1].end > 0.6
                      or sum(len(item.text) + 1 for item in group) + len(word.text) > look.max_chars):
            groups.append(group)
            group = []
        group.append(word)
    if group:
        groups.append(group)
    for number, group in enumerate(groups):
        following = round(groups[number + 1][0].start * 100) if number + 1 < len(groups) else total
        for index, word in enumerate(group):
            start = max(0, round(word.start * 100))
            if index + 1 < len(group):
                # A phrase stays on screen between its words instead of blinking off in each pause.
                end = round(group[index + 1].start * 100)
            else:
                end = min(following, round((word.end + HOLD) * 100))
            end = min(total, max(start + 1, end))
            if end <= start:
                continue
            text = " ".join(r"{\c&H004DDFFF&}" + _shown(item.text) + r"{\r}"
                            if position == index and look.highlight else _shown(item.text)
                            for position, item in enumerate(group))
            events.append(f"Dialogue: 0,{_time(start)},{_time(end)},Caption,,0,0,0,,{text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
