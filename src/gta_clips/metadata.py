"""Generate editable metadata from the actual transcript."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Metadata:
    title: str
    description: str
    hashtags: str


NON_GAME_CATEGORIES = {"just chatting", "irl", "music", "art", "talk shows & podcasts", "asmr",
                       "sports", "travel & outdoors", "food & drink", "pools, hot tubs, and beaches"}
GAME_THEMES = {"car": "#Cars", "race": "#Racing", "chase": "#Chase", "mission": "#Missions"}
# Conservative public-copy filter; this is not a content-safety assessment of the video.
EXPLICIT = re.compile(r"\b(?:fuck\w*|shit\w*|bitch\w*|nigg\w*|cunt\w*|cock|dick|pussy|sex|sexual|rape\w*)\b", re.I)
FILLERS = {'okay', 'ok', 'yeah', 'yes', 'no', 'hey', 'oh', 'wow', 'man', 'bro', 'guys', 'chat', 'just', 'like', 'well', 'uh', 'um'}


def headline_from_transcript(words: str, fallback: str) -> str:
    """Extract a substantive sentence; never invent an event from uncertain ASR."""
    for sentence in re.split(r'(?<=[.!?])\s+', words):
        tokens = re.findall(r"[a-z]+(?:'[a-z]+)?", sentence.casefold())
        content = [word for word in tokens if word not in FILLERS]
        if 5 <= len(tokens) <= 24 and len(set(content)) >= 4 and not EXPLICIT.search(sentence):
            return sentence
    return fallback


def mask_explicit(text: str) -> str:
    """Keep each explicit word's first letter and star out the rest, for text shown on screen."""
    return EXPLICIT.sub(lambda match: match.group(0)[0] + "*" * (len(match.group(0)) - 1), text)


def attribution(streamer: str) -> str:
    return f"@{streamer} on Twitch\nhttps://www.twitch.tv/{streamer}"


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (text[:limit - 1].rsplit(" ", 1)[0] or text[:limit - 1]) + "…"


def generate(transcript: str, game: str, streamer: str, vod_id: str, start: int) -> Metadata:
    words = " ".join(transcript.replace("<", "‹").replace(">", "›").split())
    suffix = f" | @{streamer}"
    headline = headline_from_transcript(words, f"{game} highlight")
    title = _excerpt(headline, 100 - len(suffix)) + suffix
    description = f"{attribution(streamer)}\nSource VOD: https://www.twitch.tv/videos/{vod_id}?t={start}s"
    # Keep the full transcript in the source database, not in public descriptions.
    if headline != f"{game} highlight":
        description = f"{_excerpt(headline, 240)}\n\n{description}"
    category = game.casefold()
    tag = "GTA6" if category == "grand theft auto vi" else re.sub(r"\W", "", game)
    tokens = set(re.findall(r"\w+", words.casefold()))
    tags = ([f"#{tag}"] if tag else []) + ["#TwitchClips"]
    if category not in NON_GAME_CATEGORIES:
        # Driving themes describe a game; in Just Chatting "car" or "race" means something else.
        tags.append("#Gaming")
        tags += [theme for keyword, theme in GAME_THEMES.items() if keyword in tokens][:2]
    tags = " ".join(tags)
    return Metadata(title, description, tags)


def clean_title(title: str, description: str, transcript: str, game: str) -> tuple[str, str] | None:
    """A new (title, description) for a clip whose title has an explicit word, or None if it has none.

    Titles made before the explicit-word filter can quote a sentence with swearing. This picks the
    headline again the way `generate` does now; the rest of the description stays as it was.
    """
    if not EXPLICIT.search(title):
        return None
    headline_part, _, streamer = title.rpartition(" | @")
    suffix = f" | @{streamer}" if headline_part else ""
    words = " ".join(transcript.replace("<", "‹").replace(">", "›").split())
    fallback = f"{game} highlight"
    headline = headline_from_transcript(words, fallback)
    new_title = _excerpt(headline, 100 - len(suffix)) + suffix
    # `generate` puts the headline as the first paragraph unless the description starts with credit.
    first, separator, rest = description.partition("\n\n")
    body = rest if separator and not first.startswith("@") else description
    new_description = f"{_excerpt(headline, 240)}\n\n{body}" if headline != fallback else body
    return new_title, new_description
