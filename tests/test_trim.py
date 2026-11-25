"""Dead-time trimming: the per-clip choice, which gaps get cut, rendering, and the reports."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import json
import subprocess
import sys
import unittest
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips import trim
from gta_clips.captions import Word
from gta_clips.config import EditingConfig
from gta_clips.db import connect
from gta_clips.download import ffmpeg_binary
from gta_clips.editing import render, video_duration
from gta_clips.highlights import Moment
from gta_clips.upload.instagram import window
from gta_clips.view_model import TARGET_AGE, cut_results, retention_results, trim_results


def _wav(path: Path, loud: list[tuple[float, float]], seconds: float, rate: int = 16000):
    """A tone during each ``loud`` stretch and near-silence elsewhere."""
    t = np.arange(int(seconds * rate)) / rate
    signal = np.full(len(t), 0.001)
    for start, end in loud:
        inside = (t >= start) & (t < end)
        signal[inside] = 0.5 * np.sin(2 * np.pi * 440 * t[inside])
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((signal * 32767).astype(np.int16).tobytes())


class ChoiceTest(unittest.TestCase):
    def test_off_on_and_an_even_stable_test(self):
        self.assertIsNone(trim.choose("a", "off"))
        self.assertEqual(trim.choose("a", "on"), trim.TRIM)
        picks = [trim.choose(f"clip-{n}", "test") for n in range(200)]
        self.assertEqual(set(picks), {trim.TRIM, trim.NONE})
        self.assertTrue(70 < picks.count(trim.TRIM) < 130)
        self.assertEqual(trim.choose("clip-7", "test"), trim.choose("clip-7", "test"))


class PlanTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.audio = Path(folder.name) / "speech.wav"

    def test_a_quiet_pause_is_cut_down_to_its_edges(self):
        _wav(self.audio, [(0, 3), (6, 10)], 10)
        found = trim.plan([Word(0.5, 3.0, "hi"), Word(6.0, 9.5, "there")], self.audio, 10)
        self.assertEqual(len(found.cuts), 1)
        start, end = found.cuts[0]
        self.assertAlmostEqual(start, 3.25)
        self.assertAlmostEqual(end, 5.75)
        self.assertAlmostEqual(found.removed, 2.5)
        self.assertEqual(found.keep, [(0.0, 3.25), (5.75, 10)])

    def test_loud_action_without_speech_is_kept(self):
        _wav(self.audio, [(0, 10)], 10)
        found = trim.plan([Word(0.5, 1.0, "go"), Word(9.0, 9.5, "yes")], self.audio, 10)
        self.assertEqual(found.cuts, [])

    def test_the_protected_burst_is_never_cut(self):
        _wav(self.audio, [(0, 2), (18, 20)], 20)
        found = trim.plan([Word(0.5, 2.0, "a"), Word(18.0, 19.5, "b")], self.audio, 20, protect=(8, 12))
        for start, end in found.cuts:
            self.assertTrue(end <= 8 or start >= 12, (start, end))
        self.assertGreater(found.removed, 0)

    def test_at_most_a_share_of_the_clip_is_removed(self):
        _wav(self.audio, [(0, 1), (19, 20)], 20)
        found = trim.plan([Word(0.2, 1.0, "a"), Word(19.0, 19.8, "b")], self.audio, 20)
        self.assertLessEqual(found.removed, trim.MAX_SHARE * 20)

    def test_nothing_is_cut_without_audio(self):
        self.assertEqual(trim.plan([Word(0, 1, "a"), Word(8, 9, "b")], None, 10).cuts, [])

    def test_words_move_up_by_what_was_cut_before_them(self):
        found = trim.Plan([(2.0, 4.0), (6.0, 7.0)], 10)
        self.assertEqual(found.shift(1.0), 1.0)
        self.assertEqual(found.shift(5.0), 3.0)
        self.assertEqual(found.shift(9.0), 6.0)
        moved = found.words([Word(4.5, 5.5, "x"), Word(8.0, 8.5, "y")])
        self.assertEqual([(w.start, w.end) for w in moved], [(2.5, 3.5), (5.0, 5.5)])


class RenderTest(unittest.TestCase):
    def test_a_trimmed_render_is_shorter_with_captions_moved(self):
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            source = work / "source.mp4"
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=10.5",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=10.5,volume=enable='between(t,3,6)':volume=0",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
                           check=True, capture_output=True)
            transcriber = Mock(language="en", language_probability=0.9)
            transcriber.transcribe.return_value = [Word(0.5, 3.0, "hello"), Word(6.0, 9.5, "again")]
            target, details = work / "out.mp4", {}
            render(source, work, target, Moment(0, 10, 1), "test", EditingConfig("fit", "base", {}), transcriber,
                   details, hook_text="", trim=True)
            self.assertGreater(details["trimmed_s"], 2.0)
            self.assertAlmostEqual(video_duration(target), 10 - details["trimmed_s"], delta=0.1)
            self.assertLessEqual(details["first_word_s"], 0.5)
            self.assertAlmostEqual(details["last_word_s"], 9.5 - details["trimmed_s"], delta=0.01)
            manifest = json.loads(target.with_suffix(".edit.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["trimmed"]), 1)
            self.assertAlmostEqual(manifest["durationSeconds"], 10 - details["trimmed_s"])
            # Untrimmed, the same clip keeps its full length.
            plain = work / "plain.mp4"
            render(source, work, plain, Moment(0, 10, 1), "test", EditingConfig("fit", "base", {}), transcriber,
                   {}, hook_text="")
            self.assertAlmostEqual(video_duration(plain), 10, delta=0.1)


class InstagramCutTest(unittest.TestCase):
    def test_the_30_second_cut_follows_the_burst_in_a_trimmed_clip(self):
        # A 60 s window, burst 30 s in, with 8 s cut before the burst: the video is 52 s long.
        self.assertEqual(window(60 - 8, 30 - 8), (2.0, 30.0))


class ReportTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "state.sqlite3"
        with connect(self.database) as db:
            db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'done')")
        self.count = 0

    def _post(self, cut=(30, 30), created=2000, views=100, arm=None, trimmed=0.0, watched=None):
        self.count += 1
        clip, video = f"c{self.count}", f"p{self.count}"
        with connect(self.database) as db:
            db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                          description, hashtags, created, cut_before, cut_after, trim, trimmed_s)
                          VALUES (?, 'v', 's', 'x.mp4', 0, ?, '', 't', 'd', '', ?, ?, ?, ?, ?)""",
                       (clip, sum(cut), created, cut[0], cut[1], arm, trimmed))
            db.execute("""INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name, messages,
                          state, retry_at, clip_id, trigger_kind) VALUES ('st', ?, 'b', 's', 'g', 9, 'clipped', 0, ?, 'burst')""",
                       (self.count, clip))
            db.execute("INSERT INTO posts(platform, video_id, clip_id, source) VALUES ('youtube', ?, ?, 'api')",
                       (video, clip))
            db.execute("""INSERT INTO post_stats(platform, video_id, fetched, published, views, avg_view_percent)
                          VALUES ('youtube', ?, ?, 0, ?, ?)""", (video, TARGET_AGE, views, watched))

    def test_each_post_counts_once_under_its_whole_cut_and_old_clips_are_left_out(self):
        self._post(created=1000, views=5)   # Before the test: always 30 + 30, not part of the draw.
        self._post(cut=(15, 5), created=2000, views=50)
        self._post(cut=(30, 30), created=2100, views=70)
        self._post(cut=(15, 5), created=2200, views=90)
        with connect(self.database) as db:
            found = cut_results(db)["youtube"]
        self.assertEqual(found, {"15s before + 5s after": {"posts": 2, "median_views": 70},
                                 "30s before + 30s after": {"posts": 1, "median_views": 70}})

    def test_trim_report_compares_views_and_percent_watched(self):
        self._post(arm="trim", trimmed=6.0, views=200, watched=60.0)
        self._post(arm="trim", trimmed=4.0, views=100, watched=50.0)
        self._post(arm="none", views=80, watched=40.0)
        self._post(views=999)  # Not in the test.
        with connect(self.database) as db:
            found = trim_results(db)["youtube"]
        self.assertEqual(found["trim"], {"posts": 2, "posts_with_24h_views": 2, "median_views": 150,
                                         "avg_percent_watched": 55.0, "avg_seconds_cut": 5.0})
        self.assertEqual(found["none"]["median_views"], 80)
        self.assertEqual(found["none"]["avg_seconds_cut"], 0.0)

    def test_cut_report_includes_percent_watched_where_youtube_reports_it(self):
        self._post(created=1000, views=5)
        self._post(cut=(15, 5), created=2000, views=50, watched=70.0)
        self._post(cut=(15, 5), created=2200, views=90, watched=60.0)
        self._post(cut=(30, 30), created=2100, views=70)
        with connect(self.database) as db:
            found = cut_results(db)["youtube"]
        self.assertEqual(found["15s before + 5s after"], {"posts": 2, "median_views": 70, "posts_with_watched": 2,
                                                         "avg_percent_watched": 65.0})
        self.assertNotIn("avg_percent_watched", found["30s before + 30s after"])

    def test_retention_splits_posts_past_the_first_thousand_views(self):
        self._post(views=1200, watched=80.0)
        self._post(views=300, watched=50.0)
        self._post(views=100, watched=40.0)
        self._post(views=5000)  # No analytics yet.
        with connect(self.database) as db:
            found = retention_results(db)["youtube"]
        self.assertEqual(found["all"]["posts"], 3)
        self.assertEqual(found["1,000+ views"]["avg_percent_watched"], 80.0)
        self.assertEqual(found["under 1,000 views"]["avg_percent_watched"], 45.0)


if __name__ == "__main__":
    unittest.main()
