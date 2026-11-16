"""Watch live Twitch chat through EventSub and record windows around bursts."""

from collections import deque
from dataclasses import dataclass, field
from itertools import islice
from threading import Event, Thread
from urllib.parse import urlsplit
import json
import logging
import time

import requests
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as websocket

from .config import Config, LiveConfig
from .db import connect, worker_lock
from .discovery import Twitch, _timestamp
from .live_render import HALF_WINDOW, render_due
from .oauth import access_token

EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
SUBSCRIPTIONS = "https://api.twitch.tv/helix/eventsub/subscriptions"
REFRESH_SECONDS = 30


@dataclass(frozen=True)
class Broadcast:
    stream_id: str
    broadcaster_id: str
    login: str
    started_at: float


@dataclass
class ChatRate:
    recent: deque[float] = field(default_factory=deque)
    history: deque[float] = field(default_factory=deque)
    latest: float = 0
    last_burst: int = 0

    def observe(self, timestamp: float, config: LiveConfig) -> int:
        if timestamp < self.latest - 10:
            return 0
        self.latest = max(timestamp, self.latest)
        self.recent.append(self.latest)
        while self.recent and self.recent[0] <= self.latest - 10:
            self.history.append(self.recent.popleft())
        while self.history and self.history[0] <= self.latest - 130:
            self.history.popleft()
        threshold = max(config.messages_per_10_seconds, len(self.history) / 12 * config.spike_multiplier)
        return len(self.recent) if len(self.recent) >= threshold else 0


def record_burst(config: Config, stream: Broadcast, timestamp: float, messages: int) -> bool:
    """Commit a burst before the post-roll is available. Cooldown survives restarts."""
    anchor = int(timestamp)
    if anchor < stream.started_at + HALF_WINDOW:
        return False
    with connect(config.paths.database) as db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute("SELECT MAX(anchor) FROM live_bursts WHERE stream_id=?",
                            (stream.stream_id,)).fetchone()[0]
        if latest is not None and anchor - latest < config.live.cooldown_seconds:
            return False
        db.execute(
            """INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name,
                                       messages, retry_at, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Waiting for 30 seconds of post-roll')""",
            (stream.stream_id, anchor, stream.broadcaster_id, stream.login,
             config.twitch.game_name, messages, anchor + HALF_WINDOW),
        )
    return True


def _active_streams(twitch: Twitch, config: Config, game_id: str) -> dict[str, Broadcast]:
    if config.twitch.all_streamers:
        rows = list(islice(twitch.pages("streams", {"game_id": game_id, "first": config.live.max_channels}),
                           config.live.max_channels))
    else:
        rows = []
        for index in range(0, len(config.twitch.streamers), 100):
            rows.extend(twitch.get("streams", {
                "user_login": config.twitch.streamers[index:index + 100], "first": 100,
            })["data"])
        rows.sort(key=lambda row: row["viewer_count"], reverse=True)
    active = {}
    for row in rows:
        if row["game_id"] == game_id and row["type"] == "live" and row["id"].isdecimal():
            active[row["user_id"]] = Broadcast(row["id"], row["user_id"], row["user_login"],
                                                _timestamp(row["started_at"]).timestamp())
            if len(active) >= config.live.max_channels:
                break
    return active


def _chat_user(token: str, client_id: str) -> str:
    response = requests.get("https://id.twitch.tv/oauth2/validate",
                            headers={"Authorization": f"OAuth {token}"}, timeout=15)
    if response.status_code == 401:
        raise RuntimeError("Run gta-clips auth twitch to authorize Twitch chat again")
    response.raise_for_status()
    details = response.json()
    if (details.get("client_id") != client_id or "user:read:chat" not in details.get("scopes", [])
            or not details.get("user_id")):
        raise RuntimeError("Run gta-clips auth twitch to authorize chat for this Twitch app")
    return details["user_id"]


def _sync_subscriptions(
    session: str, token: str, client_id: str, reader: str, desired: dict[str, Broadcast],
    subscriptions: dict[str, str], denied: dict[str, float], joins: deque[float],
):
    headers = {"Authorization": f"Bearer {token}", "Client-Id": client_id}
    for channel in subscriptions.keys() - desired.keys():
        response = requests.delete(SUBSCRIPTIONS, params={"id": subscriptions[channel]},
                                   headers=headers, timeout=8)
        if response.status_code in (204, 404):
            del subscriptions[channel]
        else:
            response.raise_for_status()
    for channel in desired:
        now = time.monotonic()
        if channel in subscriptions or denied.get(channel, 0) > now:
            continue
        while joins and joins[0] < now - 10:
            joins.popleft()
        if len(joins) >= 20:
            break
        joins.append(now)
        response = requests.post(SUBSCRIPTIONS, headers=headers, timeout=8, json={
            "type": "channel.chat.message", "version": "1",
            "condition": {"broadcaster_user_id": channel, "user_id": reader},
            "transport": {"method": "websocket", "session_id": session},
        })
        if response.status_code == 403:
            denied[channel] = now + 600
            logging.info("Twitch denied chat access to %s (HTTP 403)", desired[channel].login)
            continue
        if response.status_code in (409, 429):
            logging.info("Twitch deferred a chat subscription (HTTP %s)", response.status_code)
            break
        response.raise_for_status()
        subscriptions[channel] = response.json()["data"][0]["id"]
        logging.info("Watching Twitch chat for %s", desired[channel].login)


def _welcome(socket):
    packet = json.loads(socket.recv(timeout=10))
    if packet["metadata"]["message_type"] != "session_welcome":
        raise RuntimeError("Twitch EventSub did not send a welcome message")
    return packet["payload"]["session"]


def _open_socket(url: str):
    target = urlsplit(url)
    if (target.scheme != "wss" or target.netloc != "eventsub.wss.twitch.tv"
            or target.path != "/ws"):
        raise RuntimeError("Twitch EventSub returned an unexpected reconnect URL")
    return websocket(url, open_timeout=10, close_timeout=2, ping_interval=None)


def _chat_event(config: Config, packet: dict, desired: dict[str, Broadcast],
                rates: dict[str, ChatRate], seen: dict[str, None]):
    metadata = packet["metadata"]
    if metadata.get("subscription_type") != "channel.chat.message":
        return
    event = packet["payload"]["event"]
    channel = event["broadcaster_user_id"]
    stream = desired.get(channel)
    if not stream or event.get("source_broadcaster_user_id") not in (None, channel):
        return
    message_id = metadata["message_id"]
    if message_id in seen:
        return
    seen[message_id] = None
    if len(seen) > 8192:
        del seen[next(iter(seen))]
    timestamp = _timestamp(metadata["message_timestamp"]).timestamp()
    rate = rates.setdefault(stream.stream_id, ChatRate())
    count = rate.observe(timestamp, config.live)
    if count and timestamp >= rate.last_burst + config.live.cooldown_seconds:
        # Throttle on delivery too, even if a previous process already recorded the window.
        if int(timestamp) < stream.started_at + HALF_WINDOW:
            return
        rate.last_burst = int(timestamp)
        if record_burst(config, stream, timestamp, count):
            logging.info("Chat burst at %s: %s messages in 10s. Waiting for post-roll.", stream.login, count)


def _listen(config: Config, twitch: Twitch, game_id: str, initial: dict[str, Broadcast],
            stop: Event, joins: deque[float], seen: dict[str, None]):
    desired = initial
    rates = {}
    subscriptions = {}
    denied = {}
    token, _ = access_token(config.paths.database, "twitch")
    user = _chat_user(token, twitch.client_id)
    socket = _open_socket(EVENTSUB_URL)
    try:
        session = _welcome(socket)
        refresh_at = 0
        validate_at = time.monotonic() + 3600
        deadline = time.monotonic() + session["keepalive_timeout_seconds"] + 2
        while not stop.is_set():
            now = time.monotonic()
            if now >= refresh_at:
                if refresh_at:
                    desired = _active_streams(twitch, config, game_id)
                if not desired:
                    return
                token, _ = access_token(config.paths.database, "twitch")
                if now >= validate_at:
                    user = _chat_user(token, twitch.client_id)
                    validate_at = now + 3600
                _sync_subscriptions(session["id"], token, twitch.client_id, user, desired,
                                    subscriptions, denied, joins)
                current = {stream.stream_id for stream in desired.values()}
                rates = {stream_id: rate for stream_id, rate in rates.items() if stream_id in current}
                denied = {channel: until for channel, until in denied.items() if until > now}
                refresh_at = now + REFRESH_SECONDS
            try:
                packet = json.loads(socket.recv(timeout=1))
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Twitch EventSub keepalive expired") from None
                continue
            kind = packet["metadata"]["message_type"]
            if kind == "session_reconnect":
                replacement = _open_socket(packet["payload"]["session"]["reconnect_url"])
                try:
                    new_session = _welcome(replacement)
                except BaseException:
                    replacement.close()
                    raise
                socket.close()
                socket, session = replacement, new_session
            elif kind == "notification":
                _chat_event(config, packet, desired, rates, seen)
            elif kind == "revocation":
                subscription = packet["payload"]["subscription"]
                channel = subscription["condition"]["broadcaster_user_id"]
                if channel in subscriptions:
                    del subscriptions[channel]
                if subscription["status"] == "authorization_revoked":
                    raise RuntimeError("Twitch chat authorization was revoked. Run gta-clips auth twitch.")
                denied[channel] = time.monotonic() + 600
            deadline = time.monotonic() + session["keepalive_timeout_seconds"] + 2
    finally:
        socket.close()


def _render_loop(config: Config, stop: Event):
    while not stop.is_set():
        try:
            render_due(config, stop)
        except (OSError, RuntimeError, requests.RequestException) as error:
            logging.warning("Live render worker will retry (%s)", type(error).__name__)
        stop.wait(15)


def watch(config: Config):
    if not config.twitch.streamers:
        raise ValueError('Set twitch.streamers to channel logins or ["*"] before watching')
    with worker_lock(config.paths.data_dir, "watch-live"):
        twitch = Twitch()
        token, _ = access_token(config.paths.database, "twitch")
        _chat_user(token, twitch.client_id)
        game = twitch.game_id(config.twitch.game_name)
        stop = Event()
        renderer = Thread(target=_render_loop, args=(config, stop), name="live-render", daemon=True)
        renderer.start()
        joins = deque()
        seen = {}
        logging.info("Watching %s, up to %s channels. Ctrl+C stops the watcher.",
                     config.twitch.game_name, config.live.max_channels)
        try:
            while not stop.is_set():
                try:
                    desired = _active_streams(twitch, config, game)
                    if desired:
                        _listen(config, twitch, game, desired, stop, joins, seen)
                        stop.wait(5)
                    else:
                        stop.wait(REFRESH_SECONDS)
                except (WebSocketException, OSError, requests.RequestException) as error:
                    logging.warning("Twitch connection will retry (%s)", type(error).__name__)
                    stop.wait(10)
        finally:
            stop.set()
            renderer.join()
