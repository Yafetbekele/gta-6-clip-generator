"""Clips longer than 30 s are cut around their chat burst before going to Instagram."""

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest

import av

from gta_clips.download import ffmpeg_binary
from gta_clips.upload.instagram import MAX_SECONDS, prepare, window


class WindowTest(unittest.TestCase):
    def test_keeps_the_burst_and_stays_inside_the_clip(self):
        self.assertIsNone(window(20, 10))
        self.assertIsNone(window(30, None))
        self.assertEqual(window(60, 30), (10, 30.0))      # 20 s run-up, 10 s reaction.
        self.assertEqual(window(60, 5), (0.0, 30.0))      # Burst near the start.
        self.assertEqual(window(60, 58), (30, 30.0))      # Burst near the end.
        self.assertEqual(window(90, None), (0.0, 30.0))   # VOD clip: keep the opening.


class PrepareTest(unittest.TestCase):
    def test_long_clip_is_cut_once_and_short_clip_is_untouched(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            clip = work / "clip.mp4"
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=108x192:r=30:d=45",
                            "-f", "lavfi", "-i", "sine=d=45", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)],
                           check=True, capture_output=True)
            job = {"id": "live-1-2", "duration": 45, "cut_before": 30}
            cut = prepare(job, clip, work / "ig")
            with av.open(str(cut)) as media:
                self.assertAlmostEqual(media.duration / av.time_base, MAX_SECONDS, delta=0.1)
            self.assertEqual(prepare(job, clip, work / "ig"), cut)
            self.assertEqual(prepare({"id": "s", "duration": 20, "cut_before": None}, clip, work / "ig"), clip)


if __name__ == "__main__":
    unittest.main()
