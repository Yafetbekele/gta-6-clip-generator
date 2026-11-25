"""Which kind of on-screen hook a clip gets: a test of different hook boxes.

Without the test every clip gets the normal hook: a quote of the clip's own words when one is
usable, else an opinion question about the game, else a generic question. With it, each clip is
given one of context_text.TEST_STYLES at random: the debate hook, the clip's own words with no
debate label, a keep-watching line, or no hook box at all, so views can show which kind works.
A style that can't apply falls back ("plain" without a usable quote shows a keep-watching line);
the clip records both the style it was given and the hook actually shown.

The choice comes from a hash of the clip's ID, not from the clock: a render that is retried gets
the same style, and nothing about the clip itself (streamer, speech, game) decides it.
"""

from hashlib import sha256

from .context_text import TEST_STYLES

STANDARD = "quote"


def choose(clip_id: str, testing: bool) -> str | None:
    """The hook style for this clip, or None when the test is off (the clip gets STANDARD)."""
    if not testing:
        return None
    return TEST_STYLES[sha256(f"hook/{clip_id}".encode()).digest()[0] % len(TEST_STYLES)]
