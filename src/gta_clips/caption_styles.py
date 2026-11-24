"""How spoken captions look: a test of caption styles.

- "standard": up to 5 words at a time, white with the spoken word in yellow (the usual look).
- "big": 1 to 3 words at a time in much larger type, so each word lands as it is said.
- "box": up to 5 words at a time, white on a dark box, with no highlighted word.

Every style sits in the same place and starts with the first spoken word, so only the look
changes. With ``editing.test_captions`` each clip gets one style, chosen from a hash of its ID
(not the clock or anything about the clip), so a retried render gets the same style and views
can show which works. Without the test every clip gets STANDARD.
"""

from dataclasses import dataclass
from hashlib import sha256

STANDARD = "standard"
BIG = "big"
BOX = "box"
STYLES = (STANDARD, BIG, BOX)


@dataclass(frozen=True)
class Look:
    max_words: int
    max_chars: int
    size: int
    outline: float
    border_style: int  # 1 is an outline, 3 a box behind the text
    outline_colour: str  # the box colour with border_style 3
    highlight: bool


LOOKS = {
    STANDARD: Look(5, 28, 64, 5, 1, "&H00131313", True),
    BIG: Look(3, 16, 92, 7, 1, "&H00131313", True),
    BOX: Look(5, 28, 60, 14, 3, "&H40000000", False),
}


def choose(clip_id: str, testing: bool) -> str | None:
    """The caption style for this clip, or None when the test is off (the clip gets STANDARD)."""
    if not testing:
        return None
    return STYLES[sha256(f"captions/{clip_id}".encode()).digest()[0] % len(STYLES)]
