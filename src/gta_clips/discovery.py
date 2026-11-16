"""Discover category archives and their popular Twitch clips via Helix."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import time

import requests

from .config import TwitchConfig


@dataclass(frozen=True)
class Vod:
    id: str
    streamer: str
    broadcaster_id: str
    duration: int
    created: datetime
    stream_id: str | None

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/videos/{self.id}"


@dataclass(frozen=True)
class PopularClip:
    offset: float
    duration: float
    views: int


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration(value: str) -> int:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if not match:
        raise ValueError(f"Invalid Twitch duration {value!r}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


class Twitch:
    def __init__(self):
        self.client_id = os.environ.get("TWITCH_CLIENT_ID")
        self.secret = os.environ.get("TWITCH_CLIENT_SECRET")
        if not self.client_id or not self.secret:
            raise RuntimeError("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET")
        self.session = requests.Session()
        self.token = ""

    def _get_token(self):
        result = self.session.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": self.client_id, "client_secret": self.secret,
                  "grant_type": "client_credentials"}, timeout=30,
        )
        result.raise_for_status()
        self.token = result.json()["access_token"]

    def get(self, endpoint: str, params):
        for attempt in range(4):
            if not self.token:
                self._get_token()
            response = self.session.get(
                f"https://api.twitch.tv/helix/{endpoint}", params=params, timeout=30,
                headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self.token}"},
            )
            if response.status_code == 401 and attempt == 0:
                self.token = ""
                continue
            if response.status_code == 429 and attempt < 3:
                reset = response.headers.get("Ratelimit-Reset", "")
                delay = max(1, int(reset) - time.time() + 1) if reset.isdecimal() else 2 ** attempt
                if delay <= 60:
                    time.sleep(delay)
                    continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Twitch request exhausted retries")

    def pages(self, endpoint: str, params: dict):
        cursor = None
        while True:
            query = {**params, "after": cursor} if cursor else params
            result = self.get(endpoint, query)
            yield from result["data"]
            next_cursor = result.get("pagination", {}).get("cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

    def discover(self, config: TwitchConfig):
        games = self.get("games", {"name": config.game_name})["data"]
        game = next((item for item in games
                     if item["name"].casefold() == config.game_name.casefold()), None)
        if game is None:
            raise RuntimeError(f"Twitch category not found: {config.game_name}")
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.lookback_days)
        allowlist = set(config.streamers)
        videos = self.pages("videos", {"game_id": game["id"], "type": "archive",
                                     "sort": "time", "first": 100})
        for item in videos:
            created = _timestamp(item["created_at"])
            if created < cutoff:
                break
            login = item["user_login"].lower()
            if login not in allowlist or not item["id"].isdecimal():
                continue
            duration = _duration(item["duration"])
            if duration:
                yield Vod(item["id"], login, item["user_id"], duration, created, item.get("stream_id"))

    def is_live(self, vod: Vod) -> bool:
        if not vod.stream_id:
            return False
        streams = self.get("streams", {"user_id": vod.broadcaster_id})["data"]
        return any(stream["id"] == vod.stream_id for stream in streams)

    def popular(self, vod: Vod, limit: int) -> list[PopularClip]:
        now = datetime.now(timezone.utc)
        # Include a short window after the broadcast for clips made from its replay.
        end = min(now, vod.created + timedelta(seconds=vod.duration, days=2))
        if end <= vod.created:
            return []
        result = []
        for item in self.pages("clips", {
            "broadcaster_id": vod.broadcaster_id, "first": 100,
            "started_at": vod.created.isoformat(), "ended_at": end.isoformat(),
        }):
            offset = item.get("vod_offset")
            if item["video_id"] == vod.id and offset is not None and 0 <= offset < vod.duration:
                result.append(PopularClip(offset, item["duration"], item["view_count"]))
                if len(result) == limit:
                    break
        logging.info("VOD %s matched %s Twitch clips", vod.id, len(result))
        return result
