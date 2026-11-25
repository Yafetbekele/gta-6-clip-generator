"""Which part of the stream around a chat burst a live clip shows: a test of different cuts.

Without the test every live clip is the 30 seconds before the burst and the 30 seconds after it.
With it, a quarter of bursts keep that 60 s cut as the comparison, and the rest get a short cut
(20 to 35 s): a lead-in and a follow-through picked at random, independently of each other.
YouTube showed every 60 s Short to its ~1,000-viewer test audience, which watched about 40 % of
it, and none went further; short clips are the test of whether watching more of them changes that.
(The first run of this test, 21-23 Nov 2026, tried 15-45 s before and 5-30 s after.)

The choice comes from a hash of the stream and the moment, not from the clock: a render that is
retried gets the same cut, and nothing about the clip itself (streamer, chat, game) decides it.
Comparison windows (controls and near misses) keep the standard cut so ratings stay comparable.
"""

from hashlib import sha256

STANDARD = (30, 30)
# Seconds of stream shown before the burst, and after it.
BEFORE = (15, 20, 25)
AFTER = (5, 10)
# One burst in this many keeps STANDARD, so the short cuts have 60 s clips to beat.
CONTROL_EVERY = 4
# The most any cut needs: a burst this early in a stream, or this recent, can't be cut yet.
# A viewer clip's window ends where the viewer's Twitch clip ends (viewer_clips.py), so it shows
# the build-up and a few seconds of aftermath.
VIEWER = (30, 5)
MOST_BEFORE = max(BEFORE + (STANDARD[0], VIEWER[0]))
MOST_AFTER = max(AFTER + (STANDARD[1], VIEWER[1]))


def choose(stream_id: str, anchor: int, kind: str, testing: bool) -> tuple[int, int]:
    """(seconds before, seconds after) the burst for this clip."""
    if kind == "viewer_clip":
        return VIEWER
    if not testing or kind != "burst":
        return STANDARD
    digest = sha256(f"{stream_id}/{anchor}".encode()).digest()
    if digest[2] % CONTROL_EVERY == 0:
        return STANDARD
    return BEFORE[digest[0] % len(BEFORE)], AFTER[digest[1] % len(AFTER)]
