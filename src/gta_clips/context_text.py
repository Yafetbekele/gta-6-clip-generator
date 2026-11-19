"""Extract grounded opening text from speech without inventing visual context."""

from collections import Counter
from dataclasses import asdict, dataclass
import re
import zlib
import unicodedata

MAX_HOOK_LENGTH = 72
STOP = set('a an the and or but so to of in on at for with from is are was were be been '
           'it its this that these those i me my we our you your he she they them his her '
           'as if then than just really very like well yeah yes no okay ok uh um oh '
           'do does did have has had can could would should will know said say see '
           'get got going come look watching something anything not what how why when '
           'where which who don didn doesn'.split())
ROUTINE = re.compile(r'\b(?:hello chat|hi chat|hey chat|welcome (?:back|to)|'
                     r'thanks? (?:you )?for|thank you|prime sub|gifted subs?|'
                     r'like and subscribe|sponsored by|use (?:my )?code)\b', re.I)


@dataclass(frozen=True)
class ContextHook:
    text: str = ''
    evidence: str = ''
    method: str = 'none'
    label: str = ''

    def to_dict(self):
        return asdict(self)


def clean_text(text: str) -> str:
    # Control and bidi characters must not change overlay layout or reading direction.
    return ' '.join(''.join(c if not unicodedata.category(c).startswith('C') else ' '
                            for c in text).split())


def manual_hook(text: str) -> ContextHook:
    if not isinstance(text, str):
        raise ValueError('Hook text must be text')
    text = clean_text(text)
    if len(text) > MAX_HOOK_LENGTH:
        raise ValueError(f'Hook text must be at most {MAX_HOOK_LENGTH} characters')
    return ContextHook(text, '', 'manual' if text else 'disabled')


def _terms(text: str) -> list[str]:
    return [term for term in re.findall(r"\w+", text.casefold()) if term not in STOP and len(term) > 2]


def suggest_hook(transcript: str) -> ContextHook:
    """Rank complete short sentences/clauses across the clip; abstain on weak input.

    This is an extractive heuristic, not a visual model or an LLM. Keep negations,
    conditions and punctuation inside each candidate instead of shortening quotes.
    """
    transcript = clean_text(transcript)
    sentences = re.split(r'(?<=[.!?])\s+|\s+[—–]\s+|;\s*', transcript)
    candidates = [s.strip() for s in sentences if 12 <= len(s.strip()) <= MAX_HOOK_LENGTH
                  and 4 <= len(s.split()) <= 14 and not ROUTINE.search(s)]
    candidates = [s for s in candidates if len(set(_terms(s))) >= 2]
    if not candidates:
        return ContextHook()
    frequencies = Counter(term for s in candidates for term in set(_terms(s)))

    def score(sentence):
        terms = set(_terms(sentence))
        relevance = sum(min(frequencies[t], 3) for t in terms) / len(terms)
        substance = min(len(terms), 6) * .3
        return relevance + substance + (.3 if sentence.endswith(('?', '!')) else 0)

    selected = max(candidates, key=score)
    return ContextHook(selected, selected, 'transcript_quote_v1')


# Short names viewers actually type; other categories are shown as Twitch names them.
SHORT_GAME = {'grand theft auto vi': 'GTA 6', 'grand theft auto v': 'GTA 5'}
NOT_A_GAME = {'just chatting', 'irl', 'talk shows & podcasts'}
# Prompts frame the streamer's own words as something to argue about; they claim nothing.
QUOTE_LABELS = ('HOT TAKE OR FACTS?', 'AGREE OR DISAGREE?', 'IS THIS TAKE TOO FAR?',
                'CHAT WAS SPLIT ON THIS', 'RIGHT OR WRONG?')
# Opinions about the game itself, never about the streamer, kept as questions or "change my mind".
GAME_TAKES = (('UNPOPULAR OPINION', '{game} is overhyped. Change my mind.'),
              ('BE HONEST', 'Was {game} really worth the wait?'),
              ('DEBATE', 'Is {game} already the best game ever made?'),
              ('HOT TAKE', '{game} online will ruin {game}. Agree?'),
              ('BE HONEST', 'Is {game} actually better than {previous}?'))
GENERIC_TAKES = (('BE HONEST', 'Who was actually right here?'),
                 ('DEBATE', 'Was this out of line or fair?'),
                 ('HOT TAKE', 'Chat overreacted to this. Agree?'))


def debate_hook(transcript: str, topic: str = '', key: str = '', *, quote: bool = True) -> ContextHook:
    """A debate prompt over the clip's own words, or an opinion question about the game.

    Template-based, not an LLM. ``key`` keeps the choice stable when a clip is re-rendered.
    The text is a suggestion for human review before anything is published.
    """
    pick = zlib.crc32(key.encode('utf-8'))
    # ``quote=False`` when the speech is unreliable, such as a language Whisper was unsure of.
    spoken = suggest_hook(transcript) if quote else ContextHook()
    if spoken.text:
        return ContextHook(spoken.text, spoken.evidence, 'debate_quote_v1', QUOTE_LABELS[pick % len(QUOTE_LABELS)])
    name = clean_text(topic)
    if name and name.casefold() not in NOT_A_GAME:
        game = SHORT_GAME.get(name.casefold(), name)
        takes = GAME_TAKES if game == 'GTA 6' else GAME_TAKES[:4]
        label, text = takes[pick % len(takes)]
        text = text.format(game=game, previous='GTA 5')
        if len(text) <= MAX_HOOK_LENGTH:
            return ContextHook(text, '', 'debate_topic_v1', label)
    label, text = GENERIC_TAKES[pick % len(GENERIC_TAKES)]
    return ContextHook(text, '', 'debate_generic_v1', label)
