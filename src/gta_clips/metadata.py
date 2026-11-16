"""Generate editable metadata from the actual transcript."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Metadata:
    title: str
    description: str
    hashtags: str


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
    tag = "GTA6" if game.casefold() == "grand theft auto vi" else re.sub(r"\W", "", game)
    themes = {"car": "#Cars", "race": "#Racing", "chase": "#Chase", "mission": "#Missions"}
    tokens = set(re.findall(r"\w+", words.casefold()))
    tags = " ".join(([f"#{tag}"] if tag else []) + ["#TwitchClips", "#Gaming"] +
                    [tag for keyword, tag in themes.items() if keyword in tokens][:2])
    return Metadata(title, description, tags)
