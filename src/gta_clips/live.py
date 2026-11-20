"""Watch live Twitch chat through EventSub and record windows around bursts."""

from bisect import insort
from collections import deque
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit
import json
import logging
import os
import random
import time

import requests
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect as websocket

from .config import Config, Feed, LiveConfig
from .db import connect, worker_lock
from .diagnostics import describe, redact
from .discovery import Twitch, _timestamp
from .live_render import HALF_WINDOW, RenderContext, render_due
from .oauth import access_token

EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
SUBSCRIPTIONS = "https://api.twitch.tv/helix/eventsub/subscriptions"
REFRESH_SECONDS = 30
HISTORY_SECONDS = 300
# A channel unwatched for longer than this starts its warmup again.
MAX_GAP_SECONDS = 60
# A channel's rate is kept this long after it leaves the watched set, so brief dips keep their baseline.
RATE_RETENTION = 600
# Watched channels stay until they fall below max_channels + RANK_SLACK, or for MIN_DWELL_SECONDS.
RANK_SLACK = 5
MIN_DWELL_SECONDS = 600
CANDIDATE_EXTRA = 10
PEAK_SECONDS = HALF_WINDOW
# Frames buffered by the socket's reader thread, so Twitch's pings are answered during HTTP work.
RECEIVE_QUEUE = 10_000
NETWORK_ERRORS = (WebSocketException, OSError, requests.RequestException)
STATUS_FILE = "watch-status.json"
# Creating this file asks a running watcher to stop cleanly (used by `gta-clips run`).
STOP_FILE = "watch-live.stop"
SNAPSHOT_SECONDS = 300


class ChatAuthError(RuntimeError):
    """Twitch chat authorization is missing, wrong or revoked. Retrying cannot fix it."""


@dataclass(frozen=True)
class Broadcast:
    stream_id: str
    broadcaster_id: str
    login: str
    started_at: float
    viewer_count: int = 0
    rank: int = 0
    language: str = ""
    game_name: str = ""
    # Feed.key of the category and language this channel was found in.
    feed: str = ""
    title: str = ""
    tags: str = ""  # Comma-separated stream tags
    is_mature: bool = False


@dataclass
class Peak:
    """The largest 10-second count seen during the post-roll after an anchor."""
    anchor: int
    until: float
    messages: int
    unique: int
    at: float


@dataclass
class ChatRate:
    first_seen: float | None = None
    recent: deque = field(default_factory=deque)  # (timestamp, chatter) for the last 10 s, in order
    history: deque[float] = field(default_factory=deque)
    latest: float = 0
    last_burst: int = 0
    gaps: list[tuple[float, float]] = field(default_factory=list)
    paused_at: float | None = None
    peaks: list[Peak] = field(default_factory=list)
    near_miss: Peak | None = None
    next_control: float | None = None
    next_near_miss: float = 0
    _anonymous: int = 0

    def start_coverage(self, now: float):
        """The channel's subscription is active. Time since the last pause becomes a gap."""
        if self.first_seen is None:
            self.first_seen = now
        elif self.paused_at is not None and now > self.paused_at:
            self.gaps.append((self.paused_at, now))
        self.paused_at = None

    def pause(self, now: float):
        if self.paused_at is None and self.first_seen is not None:
            self.paused_at = now

    def gap_since_pause(self, now: float) -> float:
        return now - self.paused_at if self.paused_at is not None else 0

    def _uncovered(self, start: float, end: float) -> float:
        return sum(max(0, min(gap_end, end) - max(gap_start, start)) for gap_start, gap_end in self.gaps)

    @property
    def covered_seconds(self) -> float:
        if self.first_seen is None:
            return 0
        return max(0.0, self.latest - self.first_seen - self._uncovered(self.first_seen, self.latest))

    def warm(self, config: LiveConfig) -> bool:
        return self.first_seen is not None and self.covered_seconds >= config.warmup_seconds

    @property
    def unique_chatters(self) -> int:
        return len({chatter for _, chatter in self.recent})

    def observe(self, timestamp: float, config: LiveConfig, chatter: str | None = None) -> int:
        if self.first_seen is None:
            self.first_seen = timestamp
        if timestamp < self.latest - 10:
            return 0
        if chatter is None:
            self._anonymous += 1
            chatter = f"\0anonymous-{self._anonymous}"
        if timestamp >= self.latest:
            self.latest = timestamp
            self.recent.append((timestamp, chatter))
        else:
            # A late message belongs at its own time, not at the newest one.
            insort(self.recent, (timestamp, chatter), key=lambda item: item[0])
        while self.recent and self.recent[0][0] <= self.latest - 10:
            self.history.append(self.recent.popleft()[0])
        while self.history and self.history[0] <= self.latest - 10 - HISTORY_SECONDS:
            self.history.popleft()
        count, unique = len(self.recent), self.unique_chatters
        for peak in [*self.peaks, *([self.near_miss] if self.near_miss else [])]:
            if timestamp <= peak.until:
                if count > peak.messages:
                    peak.messages, peak.at = count, timestamp
                peak.unique = max(peak.unique, unique)
        if not self.warm(config):
            return 0
        threshold = max(config.messages_per_10_seconds, self.baseline * config.spike_multiplier)
        return count if count >= threshold and unique >= required_unique(config) else 0

    def finished_peaks(self) -> list[Peak]:
        done = [peak for peak in self.peaks if self.latest > peak.until]
        self.peaks = [peak for peak in self.peaks if self.latest <= peak.until]
        return done

    @property
    def baseline(self) -> float:
        end = self.latest - 10
        start = max(self.first_seen or 0, end - HISTORY_SECONDS)
        seconds = min(HISTORY_SECONDS, max(10, end - start - self._uncovered(start, end)))
        return len(self.history) * 10 / seconds


def required_unique(config: LiveConfig) -> int:
    # With a message floor set, the chatter rule cannot ask for more people than messages.
    if config.messages_per_10_seconds:
        return min(config.min_unique_chatters, config.messages_per_10_seconds)
    return config.min_unique_chatters


class ChatBuckets:
    """Per-second message and chatter counts, kept for offline re-detection and coverage checks."""

    def __init__(self):
        self.data: dict[tuple[str, int], list] = {}

    def add(self, stream_id: str, timestamp: float, chatter: str | None, emotes: int = 0, bits: int = 0):
        bucket = self.data.setdefault((stream_id, int(timestamp)), [0, set(), 0, 0, 0])
        bucket[0] += 1
        if chatter:
            bucket[1].add(chatter)
        bucket[2] += emotes
        bucket[3] += 1 if bits else 0
        bucket[4] += bits

    def flush(self, config: Config, everything: bool = False):
        # Messages more than 10 s late are ignored, so older seconds are final.
        cutoff = float("inf") if everything else time.time() - 15
        ready = [key for key in self.data if key[1] < cutoff]
        if not ready:
            return
        rows = []
        for stream_id, second in ready:
            messages, chatters, emotes, cheers, bits = self.data[(stream_id, second)]
            rows.append((stream_id, second, messages, len(chatters), emotes, cheers, bits))
        with connect(config.paths.database) as db:
            db.executemany(
                """INSERT INTO chat_seconds(stream_id, ts, messages, unique_chatters, emotes, cheers, bits)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(stream_id, ts) DO UPDATE SET messages=messages+excluded.messages,
                   unique_chatters=MAX(unique_chatters, excluded.unique_chatters),
                   emotes=COALESCE(emotes, 0)+excluded.emotes, cheers=COALESCE(cheers, 0)+excluded.cheers,
                   bits=COALESCE(bits, 0)+excluded.bits""", rows,
            )
        for key in ready:
            del self.data[key]


def record_burst(config: Config, stream: Broadcast, timestamp: float, messages: int,
                 baseline: float | None = None, observed_seconds: float | None = None, *,
                 kind: str = "burst", features: dict | None = None) -> bool:
    """Commit a window before the post-roll is available. Cooldown survives restarts.

    Bursts only cool down against earlier bursts, so a control or near-miss window never hides a
    real burst. Controls and near misses stay clear of every recorded window.
    """
    anchor = int(timestamp)
    if anchor < stream.started_at + HALF_WINDOW:
        return False
    features = features or {}
    with connect(config.paths.database) as db:
        db.execute("BEGIN IMMEDIATE")
        if kind == "burst":
            latest = db.execute("""SELECT MAX(anchor) FROM live_bursts
                                   WHERE stream_id=? AND trigger_kind='burst'""",
                                (stream.stream_id,)).fetchone()[0]
            if latest is not None and anchor - latest < config.live.cooldown_seconds:
                return False
        else:
            nearest = db.execute("SELECT MIN(ABS(anchor - ?)) FROM live_bursts WHERE stream_id=?",
                                 (anchor, stream.stream_id)).fetchone()[0]
            if nearest is not None and nearest < config.live.cooldown_seconds:
                return False
        if db.execute("SELECT 1 FROM live_bursts WHERE stream_id=? AND anchor=?",
                      (stream.stream_id, anchor)).fetchone():
            return False
        db.execute(
            """INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name,
                                       messages, retry_at, detail, trigger_kind, account)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Waiting for 30 seconds of post-roll', ?, ?)""",
            (stream.stream_id, anchor, stream.broadcaster_id, stream.login,
             stream.game_name or config.twitch.game_name, messages, anchor + HALF_WINDOW, kind,
             pick_account(db, config, stream.feed)),
        )
        db.execute(
            """INSERT INTO burst_features(stream_id, anchor, baseline_per_10s, observed_seconds,
                   multiplier, minimum_messages, warmup_seconds, unique_chatters, viewer_count,
                   viewer_rank, language, stream_age_s, covered_seconds, stream_title, stream_tags, is_mature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stream.stream_id, anchor, baseline, observed_seconds, config.live.spike_multiplier,
             config.live.messages_per_10_seconds, config.live.warmup_seconds,
             features.get("unique_chatters"), stream.viewer_count or None, stream.rank or None,
             stream.language or None, timestamp - stream.started_at, features.get("covered_seconds"),
             stream.title or None, stream.tags or None, int(stream.is_mature)),
        )
    return True


def pick_account(db, config: Config, feed_key: str) -> str | None:
    """The feed's account set with the fewest windows, so accounts sharing a category stay even.

    Each window goes to exactly one account set, so no clip is posted on two accounts.
    """
    feed = next((feed for feed in config.feeds if feed.key == feed_key), None)
    if feed is None or not feed.accounts:
        return None
    marks = ",".join("?" * len(feed.accounts))
    counts = dict(db.execute(
        f"""SELECT account, COUNT(*) FROM live_bursts WHERE account IN ({marks})
            AND state != 'unavailable' GROUP BY account""", feed.accounts).fetchall())
    return min(feed.accounts, key=lambda name: (counts.get(name, 0), feed.accounts.index(name)))


def _save_peak(config: Config, stream_id: str, peak: Peak):
    with connect(config.paths.database) as db:
        db.execute(
            """UPDATE burst_features SET peak_messages=?, peak_unique_chatters=?, peak_offset_s=?
               WHERE stream_id=? AND anchor=?""",
            (peak.messages, peak.unique, round(peak.at - peak.anchor, 2), stream_id, peak.anchor),
        )


def _active_streams(twitch: Twitch, config: Config, game_id: str, pool: int | None = None,
                    feed: Feed | None = None) -> list[Broadcast]:
    """Live channels in the category (and language, for a feed), most viewers first, with their rank."""
    pool = pool or config.live.max_channels
    language = feed.language if feed else ""
    if config.accounts or config.twitch.all_streamers:
        query = {"game_id": game_id, "first": min(100, pool)}
        if language:
            query["language"] = language
        rows = list(islice(twitch.pages("streams", query), pool))
    else:
        rows = []
        for index in range(0, len(config.twitch.streamers), 100):
            rows.extend(twitch.get("streams", {
                "user_login": config.twitch.streamers[index:index + 100], "first": 100,
            })["data"])
        rows.sort(key=lambda row: row["viewer_count"], reverse=True)
    active = []
    for row in rows:
        if (row["game_id"] == game_id and row["type"] == "live" and row["id"].isdecimal()
                and (not language or row.get("language") == language)
                and int(row.get("viewer_count") or 0) >= config.live.min_viewers):
            active.append(Broadcast(row["id"], row["user_id"], row["user_login"],
                                    _timestamp(row["started_at"]).timestamp(),
                                    int(row.get("viewer_count") or 0), len(active) + 1,
                                    row.get("language") or "",
                                    row.get("game_name") or (feed.category if feed else ""),
                                    feed.key if feed else "", row.get("title") or "",
                                    ",".join(row.get("tags") or []), bool(row.get("is_mature"))))
    return active


def choose_channels(candidates: list[Broadcast], watching: dict[str, float], denied: dict[str, float],
                    unclippable: set[str], max_channels: int, now: float) -> dict[str, Broadcast]:
    """Pick channels to watch, keeping current ones through small rank changes.

    Without this hysteresis, channels near the cutoff are dropped and re-added every refresh and
    lose their baseline each time.
    """
    usable = [stream for stream in candidates
              if denied.get(stream.broadcaster_id, 0) <= now and stream.stream_id not in unclippable]
    chosen = {}
    for stream in usable:
        since = watching.get(stream.broadcaster_id)
        if since is not None and (stream.rank <= max_channels + RANK_SLACK
                                  or now - since < MIN_DWELL_SECONDS):
            chosen[stream.broadcaster_id] = stream
    for stream in usable:
        if len(chosen) >= max_channels:
            break
        chosen.setdefault(stream.broadcaster_id, stream)
    if len(chosen) > max_channels:
        kept = sorted(chosen.values(), key=lambda stream: stream.rank)[:max_channels]
        chosen = {stream.broadcaster_id: stream for stream in kept}
    return chosen


def choose_feeds(config: Config, candidates: dict[str, list[Broadcast]], watching: dict[str, float],
                 denied: dict[str, float], unclippable: set[str], now: float) -> dict[str, Broadcast]:
    """Each feed fills its own channel quota, so a huge category never crowds out a small one."""
    chosen: dict[str, Broadcast] = {}
    for feed in config.feeds:
        streams = [stream for stream in candidates.get(feed.key, []) if stream.broadcaster_id not in chosen]
        chosen.update(choose_channels(streams, watching, denied, unclippable, feed.channels, now))
    return chosen


def _unclippable(config: Config) -> set[str]:
    with connect(config.paths.database) as db:
        return {row[0] for row in db.execute("SELECT stream_id FROM unclippable_streams")}


class StreamRefresher(Thread):
    """Fetches live channels in the background so the socket reader never waits on Helix."""

    def __init__(self, config: Config, games: dict[str, str], stop: Event):
        """``games`` maps each Feed.key to its Twitch category ID."""
        super().__init__(name="stream-refresh", daemon=True)
        self.config, self.games, self.stop = config, games, stop
        self.twitch = Twitch()
        self.lock = Lock()
        self.candidates: dict[str, list[Broadcast]] | None = None

    def refresh_once(self):
        found, errors = {}, []
        for feed in self.config.feeds:
            try:
                pool = min(100, feed.channels + CANDIDATE_EXTRA)
                found[feed.key] = _active_streams(self.twitch, self.config, self.games[feed.key], pool, feed)
            except Exception as error:  # One failing category keeps its last list.
                errors.append(error)
        with self.lock:
            previous = self.candidates or {}
            self.candidates = {feed.key: found.get(feed.key, previous.get(feed.key, []))
                               for feed in self.config.feeds}
        if errors and not found:
            raise errors[0]
        for error in errors:
            logging.warning("Live channel refresh failed for one category: %s", describe(error))

    def snapshot(self) -> dict[str, list[Broadcast]] | None:
        with self.lock:
            return dict(self.candidates) if self.candidates is not None else None

    def has_candidates(self) -> bool:
        return any((self.snapshot() or {}).values())

    def run(self):
        while not self.stop.wait(REFRESH_SECONDS):
            try:
                self.refresh_once()
            except Exception as error:  # Keep the last list; the next refresh may work.
                logging.warning("Live channel refresh failed: %s", describe(error))


def _chat_user(token: str, client_id: str) -> str:
    response = requests.get("https://id.twitch.tv/oauth2/validate",
                            headers={"Authorization": f"OAuth {token}"}, timeout=15)
    if response.status_code == 401:
        raise ChatAuthError("Run gta-clips auth twitch to authorize Twitch chat again")
    response.raise_for_status()
    details = response.json()
    if (details.get("client_id") != client_id or "user:read:chat" not in details.get("scopes", [])
            or not details.get("user_id")):
        raise ChatAuthError("Run gta-clips auth twitch to authorize chat for this Twitch app")
    return details["user_id"]


def _error_message(response) -> str:
    try:
        return redact(str(response.json().get("message", "")))[:200]
    except ValueError:
        return ""


def _sync_subscriptions(
    session: str, token: str, client_id: str, reader: str, desired: dict[str, Broadcast],
    subscriptions: dict[str, str], denied: dict[str, float], joins: deque[float],
) -> set[str]:
    """Match subscriptions to ``desired``, one channel at a time. Returns newly watched channels."""
    headers = {"Authorization": f"Bearer {token}", "Client-Id": client_id}
    added = set()
    for channel in list(subscriptions.keys() - desired.keys()):
        try:
            response = requests.delete(SUBSCRIPTIONS, params={"id": subscriptions[channel]},
                                       headers=headers, timeout=8)
        except requests.RequestException as error:
            logging.warning("Could not remove a chat subscription: %s", describe(error))
            break
        if response.status_code in (204, 404):
            del subscriptions[channel]
        else:
            logging.warning("Removing a chat subscription failed (HTTP %s %s)",
                            response.status_code, _error_message(response))
    for channel, stream in desired.items():
        now = time.monotonic()
        if channel in subscriptions or denied.get(channel, 0) > now:
            continue
        while joins and joins[0] < now - 10:
            joins.popleft()
        if len(joins) >= 20:
            break
        joins.append(now)
        try:
            response = requests.post(SUBSCRIPTIONS, headers=headers, timeout=8, json={
                "type": "channel.chat.message", "version": "1",
                "condition": {"broadcaster_user_id": channel, "user_id": reader},
                "transport": {"method": "websocket", "session_id": session},
            })
        except requests.RequestException as error:
            logging.warning("Chat subscription request failed: %s", describe(error))
            break
        status = response.status_code
        if status == 429:
            logging.info("Twitch rate-limited chat subscriptions; continuing next refresh")
            break
        if status in (200, 202):
            subscriptions[channel] = response.json()["data"][0]["id"]
            added.add(channel)
            logging.info("Watching Twitch chat for %s (rank %s)", stream.login, stream.rank or "?")
            continue
        # Every other answer affects this channel only.
        hold = 60 if status == 409 or status >= 500 else 600
        denied[channel] = now + hold
        logging.info("Chat subscription for %s deferred %ss (HTTP %s %s)",
                     stream.login, hold, status, _error_message(response))
    return added


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
    return websocket(url, open_timeout=10, close_timeout=2, ping_interval=None, max_queue=RECEIVE_QUEUE)


def _features(rate: ChatRate, count_unique: int) -> dict:
    return {"unique_chatters": count_unique, "covered_seconds": rate.covered_seconds}


def _chat_event(config: Config, packet: dict, desired: dict[str, Broadcast],
                rates: dict[str, ChatRate], seen: dict[str, None], buckets: ChatBuckets | None = None):
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
    if (event.get("chatter_user_login") or "").lower() in config.live.ignored_chatters:
        return
    live = config.live
    chatter = event.get("chatter_user_id")
    timestamp = _timestamp(metadata["message_timestamp"]).timestamp()
    if buckets is not None:
        fragments = (event.get("message") or {}).get("fragments") or []
        emotes = sum(1 for fragment in fragments if fragment.get("type") == "emote")
        bits = int((event.get("cheer") or {}).get("bits") or 0)
        buckets.add(stream.stream_id, timestamp, chatter, emotes, bits)
    rate = rates.setdefault(stream.stream_id, ChatRate())
    count = rate.observe(timestamp, live, chatter)
    for peak in rate.finished_peaks():
        _save_peak(config, stream.stream_id, peak)
    unique = rate.unique_chatters
    if count and timestamp >= rate.last_burst + live.cooldown_seconds:
        # Throttle on delivery too, even if a previous process already recorded the window.
        if int(timestamp) < stream.started_at + HALF_WINDOW:
            return
        rate.last_burst = int(timestamp)
        rate.near_miss = None  # The candidate grew into a real burst.
        if record_burst(config, stream, timestamp, count, rate.baseline, rate.latest - rate.first_seen,
                        features=_features(rate, unique)):
            rate.peaks.append(Peak(int(timestamp), timestamp + PEAK_SECONDS, count, unique, timestamp))
            logging.info("Chat burst at %s: %s messages from %s chatters in 10s. Waiting for post-roll.",
                         stream.login, count, unique)
        return
    if not rate.warm(live):
        return
    _sample_comparisons(config, stream, rate, timestamp, unique)


def _sample_comparisons(config: Config, stream: Broadcast, rate: ChatRate, timestamp: float, unique: int):
    """Record near-miss and random control windows, rendered and rated like bursts."""
    live = config.live
    count = len(rate.recent)
    if rate.near_miss is not None and timestamp > rate.near_miss.until:
        candidate, rate.near_miss = rate.near_miss, None
        if record_burst(config, stream, candidate.anchor, candidate.messages, rate.baseline,
                        rate.latest - rate.first_seen, kind="near_miss",
                        features=_features(rate, candidate.unique)):
            _save_peak(config, stream.stream_id, candidate)
            rate.next_near_miss = timestamp + live.near_miss_interval_seconds
            logging.info("Near-miss window at %s (%s messages in 10s)", stream.login, candidate.messages)
    elif (live.near_miss_interval_seconds and rate.near_miss is None and timestamp >= rate.next_near_miss
          and count >= max(live.messages_per_10_seconds, rate.baseline * live.near_miss_multiplier)
          and unique >= required_unique(live)):
        rate.near_miss = Peak(int(timestamp), timestamp + PEAK_SECONDS, count, unique, timestamp)
    if not live.control_interval_seconds:
        return
    if rate.next_control is None:
        rate.next_control = timestamp + random.uniform(0.5, 1.5) * live.control_interval_seconds
    elif timestamp >= rate.next_control:
        rate.next_control = timestamp + random.uniform(0.5, 1.5) * live.control_interval_seconds
        if record_burst(config, stream, timestamp, count, rate.baseline, rate.latest - rate.first_seen,
                        kind="control", features=_features(rate, unique)):
            rate.peaks.append(Peak(int(timestamp), timestamp + PEAK_SECONDS, count, unique, timestamp))
            logging.info("Random control window at %s", stream.login)


def _listen(config: Config, twitch: Twitch, refresher: StreamRefresher, stop: Event,
            joins: deque[float], seen: dict[str, None], rates: dict[str, ChatRate], buckets: ChatBuckets,
            status: "WatchStatus | None" = None):
    subscriptions: dict[str, str] = {}
    watching: dict[str, float] = {}
    denied: dict[str, float] = {}
    desired: dict[str, Broadcast] = {}
    token, _ = access_token(config.paths.database, "twitch")
    user = _chat_user(token, twitch.client_id)
    socket = _open_socket(EVENTSUB_URL)
    event_errors = 0
    try:
        session = _welcome(socket)
        refresh_at = 0
        flush_at = time.monotonic() + 10
        validate_at = time.monotonic() + 3600
        deadline = time.monotonic() + session["keepalive_timeout_seconds"] + 2
        while not stop.is_set():
            now = time.monotonic()
            if now >= refresh_at:
                desired = choose_feeds(config, refresher.snapshot() or {}, watching, denied,
                                       _unclippable(config), now)
                if not desired:
                    return
                token, _ = access_token(config.paths.database, "twitch")
                if now >= validate_at:
                    user = _chat_user(token, twitch.client_id)
                    validate_at = now + 3600
                added = _sync_subscriptions(session["id"], token, twitch.client_id, user, desired,
                                            subscriptions, denied, joins)
                _update_coverage(config, desired, subscriptions, watching, rates, added, now)
                denied = {channel: until for channel, until in denied.items() if until > now}
                refresh_at = now + REFRESH_SECONDS
            if now >= flush_at:
                buckets.flush(config)
                if status is not None:
                    status.write("watching", desired, subscriptions, watching, denied, rates)
                flush_at = now + 10
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
                try:
                    _chat_event(config, packet, desired, rates, seen, buckets)
                except Exception as error:
                    # One bad event or a locked database must not end an hour-long session.
                    event_errors += 1
                    if event_errors <= 5 or event_errors % 100 == 0:
                        logging.warning("Skipped a chat event (%s so far): %s", event_errors, describe(error))
            elif kind == "revocation":
                subscription = packet["payload"]["subscription"]
                channel = subscription["condition"]["broadcaster_user_id"]
                subscriptions.pop(channel, None)
                if subscription["status"] == "authorization_revoked":
                    raise ChatAuthError("Twitch chat authorization was revoked. Run gta-clips auth twitch.")
                denied[channel] = time.monotonic() + 600
                logging.info("Twitch revoked the chat subscription for %s (%s)",
                             channel, subscription["status"])
            deadline = time.monotonic() + session["keepalive_timeout_seconds"] + 2
    except ConnectionClosed as error:
        received = error.rcvd
        logging.warning("EventSub connection closed (code %s, reason %r)",
                        received.code if received else "none", received.reason if received else "")
        raise
    finally:
        wall = time.time()
        for rate in rates.values():
            rate.pause(wall)
        socket.close()


def _update_coverage(config: Config, desired: dict[str, Broadcast], subscriptions: dict[str, str],
                     watching: dict[str, float], rates: dict[str, ChatRate], added: set[str], now: float):
    wall = time.time()
    for channel in subscriptions:
        watching.setdefault(channel, now)
    for channel in list(watching):
        if channel not in subscriptions:
            del watching[channel]
    watched_streams = {desired[channel].stream_id for channel in subscriptions if channel in desired}
    for channel in added:
        stream_id = desired[channel].stream_id
        rate = rates.get(stream_id)
        if rate is not None and rate.gap_since_pause(wall) > MAX_GAP_SECONDS:
            logging.info("%s was unwatched for %.0fs; restarting its warmup",
                         desired[channel].login, rate.gap_since_pause(wall))
            rate = None
        if rate is None:
            rate = rates[stream_id] = ChatRate()
        rate.start_coverage(wall)
    for stream_id, rate in list(rates.items()):
        if stream_id not in watched_streams:
            rate.pause(wall)
            if rate.gap_since_pause(wall) > RATE_RETENTION:
                del rates[stream_id]
    logging.info("Coverage: watched=%s/desired=%s/max=%s", len(watched_streams), len(desired),
                 config.total_channels)


def write_json(path: Path, value: dict):
    """Replace a small status file atomically, so a reader never sees half of it."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=1), encoding="utf-8")
    os.replace(temporary, path)


class WatchStatus:
    """What the watcher is doing right now, for the dashboard. Failures here never stop watching."""

    def __init__(self, config: Config, session_id: int | None):
        self.config, self.session_id = config, session_id
        self.started = time.time()
        self.snapshot_at = 0.0
        self.path = config.paths.data_dir / STATUS_FILE

    def write(self, state: str, desired: dict[str, Broadcast] | None = None,
              subscriptions: dict[str, str] | None = None, watching: dict[str, float] | None = None,
              denied: dict[str, float] | None = None, rates: dict[str, ChatRate] | None = None):
        desired, subscriptions = desired or {}, subscriptions or {}
        watching, denied, rates = watching or {}, denied or {}, rates or {}
        wall, mono = time.time(), time.monotonic()
        feeds = {feed.key: feed for feed in self.config.feeds}
        channels = []
        for channel, stream in desired.items():
            rate = rates.get(stream.stream_id)
            since = watching.get(channel)
            channels.append({
                "login": stream.login, "broadcaster_id": channel, "stream_id": stream.stream_id,
                "viewers": stream.viewer_count, "rank": stream.rank, "language": stream.language,
                "category": stream.game_name, "feed": stream.feed,
                "accounts": list(feeds[stream.feed].accounts) if stream.feed in feeds else [],
                "live_since": stream.started_at,
                "watching": channel in subscriptions,
                "watching_since": wall - (mono - since) if since is not None else None,
                "held_for_seconds": round(denied[channel] - mono) if denied.get(channel, 0) > mono else None,
                "warm": bool(rate and rate.warm(self.config.live)),
                "covered_seconds": round(rate.covered_seconds) if rate else 0,
                "baseline_per_10s": round(rate.baseline, 1) if rate and rate.first_seen else None,
                "messages_last_10s": len(rate.recent) if rate else 0,
                "unique_last_10s": rate.unique_chatters if rate else 0,
            })
        try:
            write_json(self.path, {
                "state": state, "pid": os.getpid(), "session_id": self.session_id,
                "started": self.started, "updated": wall, "game": describe_feeds(self.config),
                "max_channels": self.config.total_channels, "channels": channels,
            })
        except OSError as error:
            logging.debug("Could not write watcher status: %s", error)
        if channels and wall - self.snapshot_at >= SNAPSHOT_SECONDS:
            self.snapshot_at = wall
            try:
                with connect(self.config.paths.database) as db:
                    db.executemany(
                        """INSERT OR IGNORE INTO channel_snapshots(ts, stream_id, streamer, viewers,
                               viewer_rank, watched) VALUES (?, ?, ?, ?, ?, ?)""",
                        [(int(wall), item["stream_id"], item["login"], item["viewers"], item["rank"],
                          int(item["watching"])) for item in channels],
                    )
            except Exception as error:
                logging.warning("Could not save a channel snapshot: %s", describe(error))


def describe_feeds(config: Config) -> str:
    if not config.accounts:
        return config.twitch.game_name
    names = dict.fromkeys(feed.category for feed in config.feeds)
    return ", ".join(names)


def _watch_stop_file(config: Config, stop: Event):
    path = config.paths.data_dir / STOP_FILE
    while not stop.wait(2):
        if path.exists():
            logging.info("Stop requested by %s", path.name)
            stop.set()


def _render_loop(config: Config, stop: Event):
    context = RenderContext(config.editing.whisper_model)
    while not stop.is_set():
        try:
            render_due(config, stop, context)
        except Exception:
            logging.exception("Live render worker will retry")
        stop.wait(15)


def _start_session(config: Config) -> int | None:
    try:
        with connect(config.paths.database) as db:
            return db.execute("INSERT INTO watch_sessions DEFAULT VALUES").lastrowid
    except Exception as error:
        logging.warning("Could not record the watch session: %s", describe(error))
        return None


def _end_session(config: Config, session_id: int | None, reason: str):
    logging.info("watch-live exiting: reason=%s", reason)
    if session_id is None:
        return
    try:
        with connect(config.paths.database) as db:
            db.execute("UPDATE watch_sessions SET ended=unixepoch(), exit_reason=? WHERE id=?",
                       (reason[:500], session_id))
    except Exception as error:
        logging.warning("Could not record the end of the watch session: %s", describe(error))


def watch(config: Config, stop: Event | None = None):
    if not config.twitch.streamers and not config.accounts:
        raise ValueError('Set twitch.streamers to channel logins or ["*"] before watching')
    with worker_lock(config.paths.data_dir, "watch-live"):
        twitch = Twitch()
        token, _ = access_token(config.paths.database, "twitch")
        _chat_user(token, twitch.client_id)
        games = {feed.key: twitch.game_id(feed.category) for feed in config.feeds}
        stop = stop if stop is not None else Event()
        session_id = _start_session(config)
        stop_file = config.paths.data_dir / STOP_FILE
        stop_file.unlink(missing_ok=True)
        status = WatchStatus(config, session_id)
        status.write("starting")
        Thread(target=_watch_stop_file, args=(config, stop), name="stop-file", daemon=True).start()
        refresher = StreamRefresher(config, games, stop)
        renderer = Thread(target=_render_loop, args=(config, stop), name="live-render", daemon=True)
        joins, seen, rates, buckets = deque(), {}, {}, ChatBuckets()
        failures = 0
        reason = "stopped"
        logging.info("Watching %s, up to %s channels. Ctrl+C stops the watcher.",
                     describe_feeds(config), config.total_channels)
        try:
            try:
                refresher.refresh_once()
            except NETWORK_ERRORS as error:
                logging.warning("First live channel lookup failed: %s", describe(error))
            refresher.start()
            renderer.start()
            while not stop.is_set():
                began = time.monotonic()
                try:
                    if refresher.has_candidates():
                        _listen(config, twitch, refresher, stop, joins, seen, rates, buckets, status)
                        stop.wait(5)
                    else:
                        status.write("no live channels")
                        stop.wait(REFRESH_SECONDS)
                    failures = 0
                except ChatAuthError:
                    raise
                except Exception as error:
                    if time.monotonic() - began > 300:
                        failures = 0
                    failures += 1
                    delay = min(300, 10 * 2 ** (failures - 1)) * random.uniform(0.8, 1.2)
                    if isinstance(error, NETWORK_ERRORS):
                        logging.warning("Twitch connection will retry in %.0fs: %s", delay, describe(error))
                    else:
                        logging.exception("Watcher error; retrying in %.0fs", delay)
                    status.write(f"reconnecting in {delay:.0f}s")
                    stop.wait(delay)
        except BaseException as error:
            reason = describe(error) or type(error).__name__
            raise
        finally:
            stop.set()
            try:
                buckets.flush(config, everything=True)
            except Exception as error:
                logging.warning("Could not save the last chat counts: %s", describe(error))
            if renderer.is_alive():
                logging.info("Waiting for the current clip render to finish")
                renderer.join()
            _end_session(config, session_id, reason)
            status.write("stopped")
            stop_file.unlink(missing_ok=True)
