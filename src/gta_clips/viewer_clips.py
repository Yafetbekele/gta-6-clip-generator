"""Viewer clips: the moments Twitch viewers clipped most, as windows for render-live to cut.

A test of a second source next to chat bursts (live.test_viewer_clips). Each run asks Twitch for
the most-viewed clips of the last day in every feed's category and language (or of a streamer
feed's channels), and saves the best ones as windows of kind "viewer_clip". render-live then cuts
them from the broadcast exactly like a chat burst: only the window is downloaded, and the same
hooks, captions, background, trimming and chat-talk check apply. Viewer clips go to the feed's
account sets in turn, like bursts, and posting balances them against chat-burst clips (recipe.py),
so the views of the two can be compared.

The window ends where the viewer's clip ends (Twitch clips usually end on the moment the viewer
reacted to) and starts cuts.VIEWER[0] seconds before it, with cuts.VIEWER[1] seconds after.
We cut our own copy from the archive; the viewer's clip itself is never downloaded or reposted.
"""

from datetime import datetime, timezone
import logging
import time

from .config import Config, Feed
from .cuts import VIEWER
from .db import connect, worker_lock
from .discovery import Twitch, _timestamp

KIND = "viewer_clip"
LOOKBACK_SECONDS = 24 * 3600
# Viewer-clip windows saved per account set per day, so they can't crowd out chat bursts.
PER_ACCOUNT_PER_DAY = 3
# Clips with fewer views than this are not a sign of anything.
MIN_VIEWS = 100
# A viewer clip this close to a window already saved for the stream is the same moment.
SAME_MOMENT_SECONDS = 60


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def candidates(twitch: Twitch, feed: Feed, game_ids: dict[str, str], now: float) -> list[dict]:
    """The feed's Twitch clips from the last day that point into a broadcast, most viewed first."""
    window = {"started_at": _iso(now - LOOKBACK_SECONDS), "ended_at": _iso(now), "first": 100}
    rows: list[dict] = []
    if feed.streamers:
        for user in twitch.get("users", {"login": list(feed.streamers)})["data"]:
            rows.extend(twitch.get("clips", {**window, "broadcaster_id": user["id"]})["data"])
    else:
        if feed.category not in game_ids:
            game_ids[feed.category] = twitch.game_id(feed.category)
        rows = twitch.get("clips", {**window, "game_id": game_ids[feed.category]})["data"]
        rows = [row for row in rows if not feed.language or row.get("language") == feed.language]
    rows = [row for row in rows if row.get("video_id") and row.get("vod_offset") is not None
            and int(row.get("view_count") or 0) >= MIN_VIEWS]
    rows.sort(key=lambda row: -int(row["view_count"]))
    return rows


def window_for(clip: dict, video: dict) -> tuple[str, int] | None:
    """(stream ID, anchor) for a viewer clip: the anchor is where the clip ends in the stream."""
    stream_id = video.get("stream_id")
    if not stream_id or not str(stream_id).isdecimal() or video.get("viewable") == "private":
        return None
    created = _timestamp(video["created_at"]).timestamp()
    anchor = int(created + float(clip["vod_offset"]) + float(clip.get("duration") or 0))
    if anchor - VIEWER[0] < created:
        return None
    return str(stream_id), anchor


def _room(db, feed: Feed, now: float) -> int:
    marks = ",".join("?" * len(feed.accounts))
    saved = db.execute(f"""SELECT COUNT(*) FROM live_bursts WHERE trigger_kind=? AND account IN ({marks})
                           AND anchor > ?""", (KIND, *feed.accounts, now - LOOKBACK_SECONDS)).fetchone()[0]
    return PER_ACCOUNT_PER_DAY * len(feed.accounts) - saved


def save_window(config: Config, feed: Feed, clip: dict, video: dict, game_name: str, now: float) -> bool:
    """Save one viewer clip as a window render-live will cut. False if it is unusable or a repeat."""
    from .live import pick_account
    found = window_for(clip, video)
    if found is None:
        return False
    stream_id, anchor = found
    # A clip made today of an older moment: skipped, so the daily cap (counted by moment) holds.
    if anchor <= now - LOOKBACK_SECONDS:
        return False
    with connect(config.paths.database) as db:
        db.execute("BEGIN IMMEDIATE")
        nearest = db.execute("SELECT MIN(ABS(anchor - ?)) FROM live_bursts WHERE stream_id=?",
                             (anchor, stream_id)).fetchone()[0]
        if nearest is not None and nearest < SAME_MOMENT_SECONDS:
            return False
        db.execute(
            """INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name, messages,
                                       retry_at, detail, trigger_kind, account)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (stream_id, anchor, clip["broadcaster_id"], (video.get("user_login") or "").lower(),
             game_name or feed.category, int(now),
             f"Twitch clip {clip.get('id')} ({int(clip['view_count'])} views)", KIND,
             pick_account(db, config, feed.key, anchor)),
        )
    return True


def collect(config: Config, twitch: Twitch | None = None, now: float | None = None) -> int:
    """Save the day's most-viewed viewer clips of every feed. Returns how many windows were saved."""
    if not config.live.test_viewer_clips:
        logging.info("live.test_viewer_clips is off; no viewer clips collected")
        return 0
    now = time.time() if now is None else now
    twitch = twitch or Twitch()
    game_ids: dict[str, str] = {}
    saved = 0
    with worker_lock(config.paths.data_dir, "viewer-clips"):
        for feed in config.feeds:
            if not feed.accounts:
                continue
            try:
                saved += _collect_feed(config, twitch, feed, game_ids, now)
            except Exception as exc:  # One feed's failure shouldn't stop the others.
                logging.warning("Viewer clips for %s failed: %s", feed.label, exc)
    logging.info("Saved %d viewer-clip windows", saved)
    return saved


def _collect_feed(config: Config, twitch: Twitch, feed: Feed, game_ids: dict[str, str], now: float) -> int:
    with connect(config.paths.database) as db:
        room = _room(db, feed, now)
    if room <= 0:
        return 0
    rows = candidates(twitch, feed, game_ids, now)[:100]
    if not rows:
        return 0
    videos = {video["id"]: video for video in twitch.get(
        "videos", {"id": sorted({row["video_id"] for row in rows})})["data"]}
    names = {}
    if feed.streamers:
        ids = sorted({row["game_id"] for row in rows if row.get("game_id")})
        names = {game["id"]: game["name"] for game in (twitch.get("games", {"id": ids})["data"] if ids else [])}
    saved = 0
    for row in rows:
        video = videos.get(row["video_id"])
        if video is None:
            continue
        if save_window(config, feed, row, video, names.get(row.get("game_id"), feed.category), now):
            saved += 1
            if saved >= room:
                break
    if saved:
        logging.info("%s: saved %d viewer clips (room for %d)", feed.label, saved, room)
    return saved
