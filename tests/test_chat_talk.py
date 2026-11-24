"""Skipping moments where the streamer is talking to chat instead of playing."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import subprocess
import time
import unittest

from gta_clips.captions import Word
from gta_clips.chat_talk import is_chat_talk, reason, sentences, talk_words
from gta_clips.config import EditingConfig, LiveConfig, PathsConfig, load_config
from gta_clips.db import connect
from gta_clips.download import ffmpeg_binary
from gta_clips.editing import SkippedMoment, render
from gta_clips.highlights import Moment
from gta_clips.live import Broadcast, record_burst
from gta_clips.live_render import render_due

ROOT = Path(__file__).resolve().parents[1]


def _words(text: str, start: float = 0.0, step: float = 0.3) -> list[Word]:
    return [Word(start + i * step, start + i * step + 0.25, w) for i, w in enumerate(text.split())]


GAMEPLAY = ("Oh my god he is right behind me. Go go go, get in the car. "
            "No way, no way that just happened. We actually made it out of there. ")


class PhraseTest(unittest.TestCase):
    def test_chat_talk_from_real_clips_is_recognised(self):
        for text in ["Do I have any Jewish people in chat, one in chat if you're Jewish",
                     "all you have to do is type in the Twitch chat",
                     "it's giveaway time",
                     "Thank you for 10 gifted subs",
                     "Yo Shade thank you for the tier 3",
                     "chat, what's the complimentary color for orange",
                     "what do you guys think",
                     "let me know in the chat",
                     "everyone in the chat spam strawberry matcha",
                     "chat is asking if this is the new update",
                     "Mods, ban that guy"]:
            self.assertTrue(is_chat_talk(text), text)

    def test_reactions_and_teammate_talk_are_not_chat_talk(self):
        for text in ["Chat this is the flank of the year",
                     "It's time, chat. Okay. Oh my God. Look at that entrance",
                     "do you guys want to go to the strip club",
                     "I buy all my mods",
                     "Did anybody die",
                     "Thank you, rock star",
                     "I'm about to get banned on TikTok"]:
            self.assertFalse(is_chat_talk(text), text)

    def test_sentences_end_at_punctuation_or_a_long_pause(self):
        words = [Word(0, 0.2, "Go."), Word(0.3, 0.5, "Now"), Word(2.0, 2.2, "wait")]
        self.assertEqual([[w.text for w in s] for s in sentences(words)], [["Go."], ["Now"], ["wait"]])

    def test_a_match_marks_only_the_words_near_it_in_a_run_on_sentence(self):
        # Whisper often leaves out punctuation; one match must not count a whole minute of speech.
        words = _words("we drive " * 30 + "thank you for the gifted subs " + "we drive " * 30)
        self.assertLess(sum(talk_words(words)), 20)


class ReasonTest(unittest.TestCase):
    def test_gameplay_is_kept(self):
        self.assertIsNone(reason(_words(GAMEPLAY * 3)))

    def test_mostly_chat_talk_is_skipped_with_a_reason(self):
        text = ("Okay chat it's giveaway time. All you have to do is type GTA in the chat. "
                "Thank you for the gifted subs bro. " + GAMEPLAY)
        why = reason(_words(text))
        self.assertIsNotNone(why)
        self.assertIn("Talking to chat", why)

    def test_a_question_right_before_the_burst_is_skipped(self):
        # Little chat talk overall, but it is what the chat burst at 30 s answered.
        words = _words(GAMEPLAY * 2, 0, 0.25) + _words("one in chat if you have ever done this", 20, 0.5) \
            + _words(GAMEPLAY * 2, 30, 0.25)
        self.assertIsNone(reason(words))
        self.assertIn("before the chat burst", reason(words, burst=30.0))
        # The same question long before the burst does not decide the clip.
        self.assertIsNone(reason(words, burst=55.0))

    def test_other_languages_and_little_speech_are_kept(self):
        self.assertIsNone(reason(_words("it's giveaway time " * 10), language="pt"))
        self.assertIsNone(reason(_words("giveaway time")))


def _source(work: Path) -> Path:
    source = work / "source.mp4"
    subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=gray:s=320x180:r=30:d=1",
                    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(source)], check=True, capture_output=True)
    return source


class RenderTest(unittest.TestCase):
    def test_render_skips_chat_talk_before_encoding(self):
        transcriber = Mock()
        transcriber.transcribe.return_value = _words("it's giveaway time, type GTA in the chat now " * 3, 0, 0.03)
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            target = work / "out.mp4"
            with self.assertRaises(SkippedMoment):
                render(_source(work), work, target, Moment(0, 1, 1), "test", EditingConfig("fit", "base", {}),
                       transcriber, skip_chat_talk=True)
            self.assertFalse(target.exists())
            # The account's language reaches Whisper.
            self.assertEqual(transcriber.transcribe.call_args.args[1], "en")
            # Without the setting the same moment renders.
            render(_source(work), work, target, Moment(0, 1, 1), "test", EditingConfig("fit", "base", {}),
                   transcriber)
            self.assertTrue(target.exists())


class LiveSkipTest(unittest.TestCase):
    def test_a_skipped_burst_is_recorded_and_not_retried(self):
        with TemporaryDirectory() as folder:
            config = load_config(ROOT / "config.example.toml")
            config = replace(config, paths=PathsConfig(Path(folder)), live=LiveConfig())
            anchor = int(time.time()) - 120
            start = anchor - 75
            record_burst(config, Broadcast("12", "42", "streamer", start), anchor, 22)
            video = {"stream_id": "12", "id": "9", "duration": "3m", "viewable": "public",
                     "created_at": datetime.fromtimestamp(start, timezone.utc).isoformat()}
            with patch("gta_clips.live_render.Twitch") as twitch, patch("gta_clips.live_render.download_vod"), \
                    patch("gta_clips.live_render.render", side_effect=SkippedMoment("Talking to chat")) as render_:
                twitch.return_value.get.return_value = {"data": [video]}
                render_due(config)
            self.assertTrue(render_.call_args.kwargs["skip_chat_talk"])
            self.assertIsNotNone(render_.call_args.kwargs["burst"])
            with connect(config.paths.database) as db:
                row = db.execute("SELECT state, detail, attempts FROM live_bursts").fetchone()
                clips = db.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
            self.assertEqual((row["state"], row["detail"], row["attempts"], clips),
                             ("skipped", "Talking to chat", 0, 0))


if __name__ == "__main__":
    unittest.main()
