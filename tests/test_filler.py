"""Background gameplay under clips: layout, the per-clip choice, and normalizing new filler."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import json
import subprocess
import unittest

import av

from gta_clips import filler
from gta_clips.config import EditingConfig
from gta_clips.download import ffmpeg_binary
from gta_clips.editing import render
from gta_clips.highlights import Moment


def _video(path: Path, spec: str):
    subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", spec,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)


class FillerLayoutTest(unittest.TestCase):
    def test_stream_on_top_and_filler_below(self):
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            source = work / "source.mp4"
            # Red stream with a blue right edge; a portrait green filler.
            _video(source, "color=red:s=640x360:r=30:d=3,drawbox=x=560:y=0:w=80:h=360:color=blue:t=fill")
            kind = work / "data" / "filler" / "test_kind"
            kind.mkdir(parents=True)
            _video(kind / "green.mp4", "color=green:s=540x960:r=30:d=0.5")
            pick = filler.pick("clip", "test_kind", work / "data", 2)
            self.assertEqual(pick.offset, 0.0)  # Shorter than the clip, so it loops from the start.
            target = work / "out.mp4"
            render(source, work, target, Moment(1, 2, 1), "test", EditingConfig("fit", "base", {}), Mock(),
                   hook_text="", filler=pick)
            with av.open(str(target)) as media:
                frames = [frame.to_ndarray(format="rgb24") for frame in media.decode(video=0)]
            self.assertEqual(len(frames), 60)
            for frame in (frames[0], frames[-1]):
                self.assertEqual(frame.shape, (1920, 1080, 3))
                self.assertLess(int(frame[200, 540].max()), 10)        # Black above, for the hook.
                self.assertGreater(int(frame[760, 300, 0]), 200)       # Stream...
                self.assertGreater(int(frame[760, 1050, 2]), 200)      # ...with both edges kept.
                self.assertGreater(int(frame[1600, 540, 1]), 100)      # Filler below.
                self.assertLess(int(frame[1600, 540, 0]), 60)
            manifest = json.loads(target.with_suffix(".edit.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["filler"]["kind"], "test_kind")


class ChoiceTest(unittest.TestCase):
    def test_choice_is_stable_and_includes_a_control(self):
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.assertIsNone(filler.choose("a", "test", data))  # No filler yet.
            for name in ("minecraft_parkour", "subway_surfers"):
                (data / "filler" / name).mkdir(parents=True)
                (data / "filler" / name / "x.mp4").write_bytes(b"")
            (data / "filler" / "empty").mkdir()
            self.assertIsNone(filler.choose("a", "off", data))
            picks = {filler.choose(f"clip-{n}", "test", data) for n in range(60)}
            self.assertEqual(picks, {"none", "minecraft_parkour", "subway_surfers"})
            self.assertEqual(filler.choose("clip-7", "test", data), filler.choose("clip-7", "test", data))
            self.assertNotIn("none", {filler.choose(f"clip-{n}", "on", data) for n in range(30)})
            self.assertIsNone(filler.pick("clip", "none", data, 30))


class AddTest(unittest.TestCase):
    def test_add_saves_a_silent_panel_sized_copy(self):
        with TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            raw = work / "raw.mp4"
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=60:d=2",
                            "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264", "-shortest", str(raw)],
                           check=True, capture_output=True)
            saved = filler.add(work / "data", "Subway Surfers", str(raw), minutes=1)
            self.assertEqual(saved.parent.name, "subway_surfers")
            with av.open(str(saved)) as media:
                self.assertEqual(len(media.streams.audio), 0)
                self.assertEqual(media.streams.video[0].height, 864)
            self.assertEqual(filler.kinds(work / "data"), ["subway_surfers"])


if __name__ == "__main__":
    unittest.main()
