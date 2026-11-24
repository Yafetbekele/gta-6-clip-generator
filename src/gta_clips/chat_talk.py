"""Spotting moments where the streamer is talking to chat instead of playing.

A chat burst often comes from the streamer asking chat something ("one in chat if…", "what do you
guys think?"), running a giveaway, thanking subs or reading messages out. Those spikes make poor
Shorts: the viewer has no chat to look at. ``reason`` reads a clip's words, sentence by sentence,
and says why to skip it, or returns None to keep it.

Only English phrases are recognised; other languages are always kept. A sentence that merely
mentions chat ("chat, this is the flank of the year") is a reaction, not chat talk, and counts
for nothing. The clip's window is never changed: a moment is either kept whole or skipped.
"""

import re

from .captions import Word

# A pause this long between two words ends a sentence even without punctuation.
SENTENCE_GAP = 0.8
# Skip when at least this share of the spoken words are in chat-talk sentences...
MAX_SHARE = 0.3
# ...or when there are at least this many chat-talk sentences and they hold MIN_SHARE of the words.
MANY_SENTENCES = 3
MIN_SHARE = 0.15
# A clip with fewer words than this has too little speech to judge.
MIN_WORDS = 12
# The stretch around a live clip's chat burst: BURST_LEAD seconds before to BURST_AFTER after.
# Skip when at least LEAD_SHARE of the (at least MIN_LEAD_WORDS) words there are chat talk.
BURST_LEAD = 12.0
BURST_AFTER = 1.0
LEAD_SHARE = 0.5
MIN_LEAD_WORDS = 6

_ASK = r"(?:type|spam|drop|put|write|send|throw)"
# Who the streamer asks. "You guys" and "y'all" are left out of yes/no questions: in GTA RP and
# squads they are as often teammates as the audience.
_AUDIENCE = r"(?:chat|everyone|everybody)"
_GROUP = r"(?:you guys|y'?all|you all|chat|everyone|everybody)"
PATTERNS = [
    # Asking chat to type something: "type 1 in chat", "spam W in the chat", "one in chat if…".
    rf"\b{_ASK}\b[^.?!]{{0,30}}\b(?:in|into|to) (?:(?:the|my|your|twitch|youtube) ){{0,2}}chat\b",
    r"\b(?:one|ones|1|1s|two|2|f|fs|w|ws|l|ls|yes|no|x)\b (?:in|into) (?:the )?chat\b",
    r"\bin (?:the )?chat if\b",
    # Asking the audience for opinions or votes.
    rf"\bwhat (?:do|did|would|should) {_GROUP} (?:think|want|say|prefer|reckon)\b",
    rf"\b(?:do|would|should|did) {_AUDIENCE} (?:think|want|wanna|like|prefer|reckon|remember|know)\b",
    r"\bchat,? (?:should|do|did|would|what|which|who|is|are|can|let me know|tell me)\b",
    r"\b(?:let me know|tell me) (?:in (?:the )?chat|what you (?:guys |all )?think|below)\b",
    r"\b(?:poll|polls|vote|voting|predictions?)\b",
    r"\bis (?:anybody|anyone) (?:in (?:the )?chat|watching)\b",
    # Giveaways, promotion and moderation.
    r"\bgiveaways?\b|\bgive away (?:time|a copy|codes?|winners?)\b",
    r"\b(?:subscribe|follow) (?:to|for|on) (?:the |my )?(?:channel|stream|twitch|youtube)\b",
    r"\bhit (?:the |that )?(?:follow|sub)\b",
    r"\b(?:my discord|the discord|merch|promo code|link in (?:the |my )?(?:bio|description|chat))\b",
    r"\b(?:mods|moderators),? (?:ban|time|timeout|clear|delete|get|can|please)\b",
    # Thanking viewers for support.
    r"\bthank(?:s| you)\b[^.?!]{0,50}\b(?:sub|subs|subbing|subscri\w*|gifted|gifts?|bits|prime|resubs?|"
    r"resubbing|follows?|donations?|donos?|raids?|raiders?|tier (?:one|two|three|1|2|3)|cheers?|hype train)\b",
    r"\b(?:gifted|gifting) (?:\w+ )?(?:subs?|memberships?)\b",
    # Reading chat out or answering it.
    r"\b(?:someone|somebody|people|everyone|everybody) in (?:the )?chat\b",
    r"\bchat (?:is|are|keeps?|was|were) (?:asking|saying|typing|spamming|telling)\b",
    r"\b(?:reading|answering|read|answer) (?:the |my )?chat\b",
    r"\b(?:to answer your question|good question|great question)\b",
]
_CHAT_TALK = re.compile("|".join(f"(?:{pattern})" for pattern in PATTERNS), re.IGNORECASE)


def sentences(words: list[Word]) -> list[list[Word]]:
    """Words split at sentence punctuation or a pause of SENTENCE_GAP seconds."""
    found, current = [], []
    for word in words:
        if current and word.start - current[-1].end >= SENTENCE_GAP:
            found.append(current)
            current = []
        current.append(word)
        if word.text.endswith((".", "?", "!")):
            found.append(current)
            current = []
    if current:
        found.append(current)
    return found


def is_chat_talk(text: str) -> bool:
    return bool(_CHAT_TALK.search(text))


# A match marks the words of its sentence within this many words before and after it, so a long
# sentence Whisper did not punctuate is not counted whole.
REACH = (4, 8)


def talk_words(words: list[Word]) -> list[bool]:
    """For each word, whether it is part of the streamer talking to chat."""
    marked = [False] * len(words)
    at = 0
    for sentence in sentences(words):
        text, offsets = "", []
        for word in sentence:
            offsets.append(len(text))
            text += word.text + " "
        for match in _CHAT_TALK.finditer(text):
            first = max(i for i, offset in enumerate(offsets) if offset <= match.start())
            last = max(i for i, offset in enumerate(offsets) if offset < match.end())
            for index in range(max(0, first - REACH[0]), min(len(sentence), last + REACH[1] + 1)):
                marked[at + index] = True
        at += len(sentence)
    return marked


def reason(words: list[Word], language: str | None = "en",
           burst: float | None = None) -> str | None:
    """Why this clip is mostly the streamer talking to chat, or None to keep it.

    ``burst`` is when (seconds into the clip) a live clip's chat burst peaked. Chat answering a
    question bursts right after it is asked, so a clip is also skipped when most of the speech in
    the BURST_LEAD seconds before the burst is chat talk.
    """
    if language not in (None, "en") or len(words) < MIN_WORDS:
        return None
    marked = talk_words(words)
    share = sum(marked) / len(words)
    # Separate stretches of chat talk.
    stretches = [index for index, flag in enumerate(marked) if flag and (index == 0 or not marked[index - 1])]
    why = None
    if share >= MAX_SHARE or (len(stretches) >= MANY_SENTENCES and share >= MIN_SHARE):
        why = f"{len(stretches)} times, {share:.0%} of the words"
    elif burst is not None:
        lead = [flag for word, flag in zip(words, marked) if burst - BURST_LEAD <= word.start <= burst + BURST_AFTER]
        if len(lead) >= MIN_LEAD_WORDS and sum(lead) / len(lead) >= LEAD_SHARE:
            why = f"{sum(lead) / len(lead):.0%} of the words just before the chat burst"
    if why is None:
        return None
    first = stretches[0]
    example = " ".join(word.text for word, flag in zip(words[first:first + 14], marked[first:first + 14]) if flag)
    return f"Talking to chat ({why}), e.g. \"{example}\""
