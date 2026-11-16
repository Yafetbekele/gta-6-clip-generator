"""Run Whisper locally and generate ASS subtitles with active-word highlighting."""

from dataclasses import dataclass
from pathlib import Path
import wave

import numpy as np


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


class Transcriber:
    def __init__(self, model: str):
        self.name = model
        self.model = None

    def transcribe(self, audio: Path) -> list[Word]:
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.name, device="cpu", compute_type="int8")
        with wave.open(str(audio), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError("Whisper input must be 16 kHz mono PCM16")
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32)
        segments, _ = self.model.transcribe(samples / 32768.0, word_timestamps=True, vad_filter=True)
        return [Word(word.start, word.end, word.word.strip())
                for segment in segments for word in segment.words or []
                if word.word.strip() and word.start is not None and word.end is not None]


def _time(centiseconds: int) -> str:
    seconds, hundredths = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}.{hundredths:02}"


def _escape(text: str) -> str:
    return " ".join(text.translate(str.maketrans({"\\": "＼", "{": "｛", "}": "｝"})).split())


def write_ass(path: Path, words: list[Word], streamer: str, duration: float):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,64,&H00FFFFFF,&H00FFFFFF,&H00131313,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,85,120,340,1
Style: Credit,Arial,37,&H00FFFFFF,&H00FFFFFF,&H00131313,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,7,75,110,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    total = round(duration * 100)
    events = [f"Dialogue: 0,0:00:00.00,{_time(total)},Credit,,0,0,0,,@{_escape(streamer)} on Twitch"]
    groups = []
    group = []
    for word in words:
        if group and (len(group) >= 5 or word.start - group[-1].end > 0.6
                      or sum(len(item.text) + 1 for item in group) + len(word.text) > 28):
            groups.append(group)
            group = []
        group.append(word)
    if group:
        groups.append(group)
    for group in groups:
        for index, word in enumerate(group):
            start = max(0, round(word.start * 100))
            next_start = round(group[index + 1].start * 100) if index + 1 < len(group) else total
            end = min(total, next_start, max(start + 1, round(word.end * 100)))
            if end <= start:
                continue
            text = " ".join(r"{\c&H004DDFFF&}" + _escape(item.text) + r"{\r}"
                            if position == index else _escape(item.text)
                            for position, item in enumerate(group))
            events.append(f"Dialogue: 0,{_time(start)},{_time(end)},Caption,,0,0,0,,{text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
