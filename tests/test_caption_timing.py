"""Caption accuracy and timing: language choice, GPU fallback, word starts and on-screen holds."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips import captions
from gta_clips.captions import HOLD, Transcriber, Word, choose_language, longest_word, tidy, write_ass
from gta_clips.config import ConfigError, load_config


def _events(words, duration=10.0):
    with TemporaryDirectory() as folder:
        path = Path(folder) / "c.ass"
        write_ass(path, words, "s", duration)
        lines = path.read_text(encoding="utf-8").splitlines()
    spans = []
    for line in lines:
        if line.startswith("Dialogue:") and ",Caption," in line:
            _, start, end, *_ = line.split(",", 3)
            spans.append((_seconds(start), _seconds(end)))
    return spans


def _seconds(stamp: str) -> float:
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _wav(path: Path, seconds: float = 1.0):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * int(16000 * seconds))


class LanguageTest(unittest.TestCase):
    def test_the_accounts_language_wins_unless_whisper_is_sure(self):
        # A Portuguese stream once came out as Catalan nonsense at the base model's guess.
        self.assertEqual(choose_language("ca", 0.55, "pt"), "pt")
        self.assertEqual(choose_language("es", 0.95, "en"), "es")
        self.assertEqual(choose_language("en", 0.4, "en"), "en")
        self.assertEqual(choose_language("de", 0.3, None), "de")
        self.assertEqual(choose_language(None, 0.0, "en"), "en")


class FakeModel:
    def __init__(self, detected=("ca", 0.55), fail=False):
        self.detected, self.fail, self.calls = detected, fail, []

    def detect_language(self, samples, vad_filter=False):
        if self.fail:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return (*self.detected, [])

    def transcribe(self, samples, **options):
        self.calls.append(options)
        word = SimpleNamespace(start=0.1, end=0.4, word=" olá")
        return iter([SimpleNamespace(words=[word])]), None


class TranscriberTest(unittest.TestCase):
    def test_transcribes_in_the_accounts_language_the_same_way_every_time(self):
        model = FakeModel()
        with TemporaryDirectory() as folder, patch.object(captions, "_load", return_value=model):
            _wav(Path(folder) / "a.wav")
            words = Transcriber("large-v3-turbo", "cpu").transcribe(Path(folder) / "a.wav", "pt")
        self.assertEqual([w.text for w in words], ["olá"])
        self.assertEqual(model.calls[0]["language"], "pt")
        # No random sampling and no VAD: both dropped real speech from clips with loud game audio.
        self.assertEqual(model.calls[0]["temperature"], 0.0)
        self.assertFalse(model.calls[0]["vad_filter"])
        self.assertTrue(model.calls[0]["word_timestamps"])

    def test_a_broken_gpu_falls_back_to_the_cpu_for_good(self):
        gpu, cpu = FakeModel(fail=True), FakeModel(detected=("en", 0.99))
        loads = []

        def load(name, device):
            loads.append(device)
            return gpu if device == "cuda" else cpu

        with TemporaryDirectory() as folder, patch.object(captions, "_load", side_effect=load), \
                patch.object(Transcriber, "_devices", lambda self: ["cuda", "cpu"] if self.device == "auto" else ["cpu"]), \
                self.assertLogs(level="WARNING"):
            _wav(Path(folder) / "a.wav")
            transcriber = Transcriber("large-v3-turbo")
            transcriber.transcribe(Path(folder) / "a.wav", "en")
            transcriber.transcribe(Path(folder) / "a.wav", "en")
        self.assertEqual(loads, ["cuda", "cpu", "cpu"])
        self.assertEqual(transcriber.language, "en")

    def test_cuda_alone_raises_instead_of_hiding_the_failure(self):
        with TemporaryDirectory() as folder, patch.object(captions, "_load", return_value=FakeModel(fail=True)):
            _wav(Path(folder) / "a.wav")
            with self.assertRaises(RuntimeError):
                Transcriber("base", "cuda").transcribe(Path(folder) / "a.wav")

    def test_silence_gives_no_words(self):
        with TemporaryDirectory() as folder, patch.object(captions, "_load", return_value=FakeModel()):
            _wav(Path(folder) / "a.wav", 0)
            self.assertEqual(Transcriber("base", "cpu").transcribe(Path(folder) / "a.wav"), [])


class TidyTest(unittest.TestCase):
    def test_a_word_stretched_over_the_pause_before_it_starts_when_it_is_said(self):
        # From a real base-model clip: "might" from 4.0 to 5.4, said at about 5.3.
        words = tidy([Word(3.5, 4.0, "cars"), Word(4.0, 5.4, "might"), Word(5.4, 5.5, "be")])
        self.assertEqual(words[0], Word(3.5, 4.0, "cars"))
        self.assertAlmostEqual(words[1].start, 5.4 - longest_word("might"))
        self.assertEqual((words[1].end, words[2].start), (5.4, 5.4))

    def test_words_come_out_in_time_order(self):
        words = tidy([Word(2.0, 2.2, "b"), Word(1.0, 1.2, "a")])
        self.assertEqual([w.text for w in words], ["a", "b"])


class HoldTest(unittest.TestCase):
    def test_a_phrase_stays_up_between_its_words(self):
        words = [Word(1.0, 1.2, "one"), Word(1.6, 1.8, "two"), Word(2.2, 2.4, "three")]
        spans = _events(words)
        self.assertEqual(spans, [(1.0, 1.6), (1.6, 2.2), (2.2, 2.4 + HOLD)])

    def test_the_last_word_holds_only_until_the_next_phrase(self):
        # A pause of over 0.6 s starts a new phrase; the hold may not overlap it.
        words = [Word(1.0, 1.2, "one"), Word(1.5, 1.7, "next")]
        self.assertEqual(_events(words)[0], (1.0, 1.5))
        words = [Word(1.0, 1.2, "one"), Word(2.0, 2.2, "later")]
        self.assertEqual(_events(words), [(1.0, 1.2 + HOLD), (2.0, 2.2 + HOLD)])

    def test_nothing_runs_past_the_end_of_the_clip(self):
        self.assertEqual(_events([Word(9.8, 9.9, "end")], duration=10.0), [(9.8, 10.0)])


class BurnedInSyncTest(unittest.TestCase):
    """Captions are drawn at clip time, not source time, whether the clip is trimmed or not."""

    def _caption_rows(self, trim: bool) -> list[int]:
        import subprocess
        from unittest.mock import Mock
        import av
        from gta_clips.config import EditingConfig
        from gta_clips.download import ffmpeg_binary
        from gta_clips.editing import render
        from gta_clips.highlights import Moment
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            source = work / "source.mp4"
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=320x180:r=30:d=6",
                            "-f", "lavfi", "-i", "sine=f=440:r=16000", "-t", "6", "-c:v", "libx264", "-c:a", "aac",
                            str(source)], check=True, capture_output=True)
            transcriber = Mock(language="en", language_probability=1.0)
            # Said 0.0-0.5 s into the clip; the source window starts 3 s in, like a padded live download.
            transcriber.transcribe.return_value = [Word(0.0, 0.5, "HELLO")]
            target = work / "out.mp4"
            render(source, work, target, Moment(3, 2, 1), "s", EditingConfig("fit", "base", {}), transcriber,
                   hook_text="", trim=trim)
            with av.open(str(target)) as media:
                # Brightest pixel in the caption band of each frame.
                return [int(frame.to_ndarray(format="gray")[1350:1650].max()) for frame in media.decode(video=0)]

    def test_untrimmed_captions_show_when_the_word_is_said(self):
        rows = self._caption_rows(trim=False)
        self.assertGreater(rows[3], 200)   # 0.1 s: on screen
        self.assertLess(rows[45], 50)      # 1.5 s: gone (0.5 s + HOLD)


class ConfigTest(unittest.TestCase):
    def _load(self, editing: str):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(f"[editing]\n{editing}\n", encoding="utf-8")
            return load_config(path)

    def test_whisper_device_and_chat_talk_settings(self):
        config = self._load("")
        self.assertEqual((config.editing.whisper_device, config.editing.skip_chat_talk), ("auto", True))
        config = self._load('whisper_device = "cpu"\nskip_chat_talk = false')
        self.assertEqual((config.editing.whisper_device, config.editing.skip_chat_talk), ("cpu", False))
        with self.assertRaises(ConfigError):
            self._load('whisper_device = "gpu"')


if __name__ == "__main__":
    unittest.main()
