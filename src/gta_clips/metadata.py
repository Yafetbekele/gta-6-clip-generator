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


def attribution(streamer: str) -> str:
    return f"@{streamer} on Twitch\nhttps://www.twitch.tv/{streamer}"


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (text[:limit - 1].rsplit(" ", 1)[0] or text[:limit - 1]) + "…"


def generate(transcript: str, game: str, streamer: str, vod_id: str, start: int) -> Metadata:
    words = " ".join(transcript.replace("<", "‹").replace(">", "›").split())
    sentence = re.split(r"(?<=[.!?])\s+", words, maxsplit=1)[0].strip()
    suffix = f" | @{streamer}"
    headline = (sentence or f"{game} highlight").replace("<", "‹").replace(">", "›")
    title = _excerpt(headline, 100 - len(suffix)) + suffix
    description = f"{attribution(streamer)}\nSource VOD: https://www.twitch.tv/videos/{vod_id}?t={start}s"
    if words:
        description = f"{_excerpt(words, 240)}\n\n{description}"
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
