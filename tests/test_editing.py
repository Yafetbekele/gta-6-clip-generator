"""Exercise the real fit renderer to catch accidental edge cropping."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import subprocess
import unittest

import av

from gta_clips.config import EditingConfig
from gta_clips.download import ffmpeg_binary
from gta_clips.editing import render
from gta_clips.highlights import Moment


class FitLayoutTest(unittest.TestCase):
    def test_landscape_edges_remain_visible(self):
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            source = work / "source.mp4"
            subprocess.run([
                ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=green:s=640x360:d=0.2,"
                "drawbox=x=0:y=0:w=80:h=360:color=red:t=fill,"
                "drawbox=x=560:y=0:w=80:h=360:color=blue:t=fill",
                "-c:v", "libx264", str(source),
            ], check=True, capture_output=True)
            target = work / "fit.mp4"
            render(source, work, target, Moment(0, 0.2, 1), "test",
                   EditingConfig("fit", "base", {}), Mock())
            with av.open(str(target)) as media:
                frame = next(media.decode(video=0)).to_ndarray(format="rgb24")
            self.assertEqual(frame.shape, (1920, 1080, 3))
            # Both edges survive scaling, and the unused vertical area is black.
            self.assertGreater(int(frame[960, 30, 0]), 200)
            self.assertGreater(int(frame[960, 1050, 2]), 200)
            self.assertLess(int(frame[500, 540].max()), 10)
