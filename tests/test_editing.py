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


class ExactWindowTest(unittest.TestCase):
    def test_trim_starts_on_the_requested_frame_between_keyframes(self):
        """The padded live download is cut on the output side, so the clip starts exactly on time."""
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            source = work / "source.mp4"
            # Brightness encodes the frame number; keyframes fall at 0 s and 2 s only.
            subprocess.run([
                ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
                "nullsrc=s=320x180:r=30:d=4,geq=lum='16+1.75*N':cb=128:cr=128",
                "-c:v", "libx264", "-g", "60", "-pix_fmt", "yuv420p", str(source),
            ], check=True, capture_output=True)
            target = work / "exact.mp4"
            render(source, work, target, Moment(1.5, 2, 1), "test", EditingConfig("fit", "base", {}), Mock())
            with av.open(str(source)) as media:
                original = [int(frame.to_ndarray(format="gray")[90, 160]) for frame in media.decode(video=0)]
            with av.open(str(target)) as media:
                frames = [int(frame.to_ndarray(format="gray")[960, 540]) for frame in media.decode(video=0)]
            self.assertEqual(len(frames), 60)
            # Neighbouring source frames differ by about 2 levels, so this pins the exact frame.
            self.assertAlmostEqual(frames[0], original[45], delta=1)
            self.assertAlmostEqual(frames[-1], original[104], delta=1)

    def test_short_source_is_rejected(self):
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            source = work / "short.mp4"
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
                            "color=gray:s=320x180:r=30:d=1", "-c:v", "libx264", str(source)],
                           check=True, capture_output=True)
            with self.assertRaises(ValueError):
                render(source, work, work / "out.mp4", Moment(0, 2, 1), "test",
                       EditingConfig("fit", "base", {}), Mock())


class MotionTest(unittest.TestCase):
    def test_motion_scan_finds_the_cut_from_a_still_frame(self):
        from gta_clips.highlights import video_activity
        with TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "motion.mp4"
            subprocess.run([
                ffmpeg_binary(), "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=gray:s=320x180:r=30:d=2",
                "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2",
                "-filter_complex", "[0][1]concat=n=2:v=1", "-c:v", "libx264", str(source),
            ], check=True, capture_output=True)
            active = video_activity(source, 4)
        self.assertIn(2, active)
        self.assertFalse(active & {0, 1})
