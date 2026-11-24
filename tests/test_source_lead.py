"""A VOD section downloaded while the stream is live can start at the segment boundary, early."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips import editing
from gta_clips.config import EditingConfig
from gta_clips.download import ffmpeg_binary
from gta_clips.editing import render, source_lead
from gta_clips.highlights import Moment


class SourceLeadTest(unittest.TestCase):
    def _lead(self, file_length, section_length):
        with patch.object(editing, "probe_duration", return_value=file_length):
            return source_lead(Path("source.mp4"), section_length)

    def test_the_extra_length_is_how_early_the_file_starts(self):
        # Live repro: 66 s asked for, 68.87 s came back, and it started 2.84 s early.
        self.assertAlmostEqual(self._lead(68.87, 66), 2.87)

    def test_a_file_on_time_or_short_is_not_shifted(self):
        self.assertEqual(self._lead(66.05, 66), 0.0)
        self.assertEqual(self._lead(60.0, 66), 0.0)

    def test_more_than_a_segment_extra_is_not_trusted(self):
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self._lead(90.0, 66), 0.0)


class RenderShiftTest(unittest.TestCase):
    """The window in the VOD turns white; the clip must start on white, wherever the file starts."""

    def _first_and_last(self, early: float, section_length: float | None) -> tuple[int, int, dict]:
        import av
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            source = work / "source.mp4"
            # Requested: 1 s padding + 2 s window + 1 s padding. The window starts 1 s + early in.
            black, white = 1 + early, 3
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y",
                            "-f", "lavfi", "-i", f"color=black:s=320x180:r=30:d={black}",
                            "-f", "lavfi", "-i", f"color=white:s=320x180:r=30:d={white}",
                            "-f", "lavfi", "-i", "sine=f=440:r=16000",
                            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
                            "-t", str(black + white), "-c:v", "libx264", "-g", "30", "-c:a", "aac",
                            str(source)], check=True, capture_output=True)
            transcriber = Mock(language="en", language_probability=1.0)
            transcriber.transcribe.return_value = []
            target, details = work / "out.mp4", {}
            render(source, work, target, Moment(1, 2, 1), "s", EditingConfig("fit", "base", {}), transcriber,
                   details, hook_text="", section_length=section_length)
            with av.open(str(target)) as media:
                frames = [int(frame.to_ndarray(format="gray")[960, 540]) for frame in media.decode(video=0)]
        return frames[0], frames[-1], details

    def test_an_early_file_is_cut_at_the_chosen_window(self):
        first, last, details = self._first_and_last(early=2.8, section_length=4)
        self.assertGreater(first, 200)
        self.assertGreater(last, 200)
        self.assertAlmostEqual(details["source_lead_s"], 2.8, delta=0.1)

    def test_a_file_on_time_is_cut_where_asked(self):
        first, _, details = self._first_and_last(early=0, section_length=4)
        self.assertGreater(first, 200)
        self.assertEqual(details["source_lead_s"], 0.0)

    def test_without_the_section_length_nothing_moves(self):
        # Old behaviour, which showed 2.8 s of the wrong (black) footage first.
        first, _, details = self._first_and_last(early=2.8, section_length=None)
        self.assertLess(first, 50)
        self.assertEqual(details["source_lead_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
