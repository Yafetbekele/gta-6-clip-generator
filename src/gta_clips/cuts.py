"""Which part of the stream around a chat burst a live clip shows: a test of different cuts.

Without the test every live clip is the 30 seconds before the burst and the 30 seconds after it.
With it, each burst is given a lead-in and a follow-through at random, independently of each
other, so clips from 20 to 75 seconds get made and their views can show which cut works.

The choice comes from a hash of the stream and the moment, not from the clock: a render that is
retried gets the same cut, and nothing about the clip itself (streamer, chat, game) decides it.
Comparison windows (controls and near misses) keep the standard cut so ratings stay comparable.
"""

from hashlib import sha256

STANDARD = (30, 30)
# Seconds of stream shown before the burst, and after it.
BEFORE = (15, 30, 45)
AFTER = (5, 15, 30)
# The most any cut needs: a burst this early in a stream, or this recent, can't be cut yet.
MOST_BEFORE = max(BEFORE + (STANDARD[0],))
MOST_AFTER = max(AFTER + (STANDARD[1],))


def choose(stream_id: str, anchor: int, kind: str, testing: bool) -> tuple[int, int]:
    """(seconds before, seconds after) the burst for this clip."""
    if not testing or kind != "burst":
        return STANDARD
    digest = sha256(f"{stream_id}/{anchor}".encode()).digest()
    return BEFORE[digest[0] % len(BEFORE)], AFTER[digest[1] % len(AFTER)]
