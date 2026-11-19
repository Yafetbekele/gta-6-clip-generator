"""Turn saved live chat bursts into pending clips once the VOD covers the window."""

from dataclasses import dataclass, field
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Event
import logging
import subprocess
import time

import requests

from .captions import Transcriber
from .config import Config
from .db import INSERT_CLIP, WorkerBusy, connect, worker_lock
from .diagnostics import describe
from .discovery import Twitch, _duration, _timestamp
from .download import VodUnavailable, download_vod
from .editing import max_source_height, render
from .highlights import Moment
from .metadata import generate

MAX_ARCHIVE_WAIT = 48 * 3600
# Twitch lists a stream's archive while it is still live. None after this long means VODs are off.
NO_ARCHIVE_GRACE = 2 * 3600
HALF_WINDOW = 30
# Extra seconds fetched on each side so the render can cut the exact window from a stream copy.
PAD = 3
MAX_RETRY_DELAY = 3600


def archive_window(archives: list[dict], stream_id: str, anchor: int) -> tuple[str, int] | None:
    """Return a VOD ID and its offset for the full 30s before and after a burst."""
    for vod in archives:
        if vod.get("stream_id") != stream_id or not vod["id"].isdecimal():
            continue
        start = anchor - HALF_WINDOW - round(_timestamp(vod["created_at"]).timestamp())
        if start >= 0 and start + 2 * HALF_WINDOW <= _duration(vod["duration"]):
            return vod["id"], start
    return None


def muted_overlap(archives: list[dict], vod_id: str, start: int, length: int) -> float:
    """Seconds of the clip window that Twitch muted, from the Helix ``muted_segments`` field."""
    for vod in archives:
        if vod["id"] == vod_id:
            total = 0.0
            for segment in vod.get("muted_segments") or []:
                begin, end = segment.get("offset", 0), segment.get("offset", 0) + segment.get("duration", 0)
                total += max(0, min(end, start + length) - max(begin, start))
            return total
    return 0.0


def retry_delay(attempts: int) -> int:
    return min(MAX_RETRY_DELAY, 120 * 2 ** max(0, attempts - 1))


@dataclass
class RenderContext:
    """Expensive objects shared by every batch in one worker: the Whisper model and an app token."""
    model: str
    twitch: Twitch | None = None
    transcriber: Transcriber | None = None
    archives: dict = field(default_factory=dict)

    def client(self) -> Twitch:
        if self.twitch is None:
            self.twitch = Twitch()
        return self.twitch

    def whisper(self) -> Transcriber:
        if self.transcriber is None:
            self.transcriber = Transcriber(self.model)
        return self.transcriber


def _waiting(config: Config, burst, detail: str, delay: int = 60):
    with connect(config.paths.database) as db:
        db.execute(
            "UPDATE live_bursts SET detail=?, retry_at=? WHERE stream_id=? AND anchor=?",
            (detail, int(time.time()) + delay, burst["stream_id"], burst["anchor"]),
        )


def _failed_attempt(config: Config, burst, error: BaseException):
    attempts = burst["attempts"] + 1
    delay = retry_delay(attempts)
    summary = describe(error)
    with connect(config.paths.database) as db:
        db.execute(
            """UPDATE live_bursts SET attempts=?, retry_at=?, last_error=?, last_error_at=unixepoch(),
                      detail=? WHERE stream_id=? AND anchor=?""",
            (attempts, int(time.time()) + delay, summary,
             f"Attempt {attempts} failed; retrying in {delay // 60} min", burst["stream_id"], burst["anchor"]),
        )
    logging.warning("Live clip from %s delayed after attempt %s: %s", burst["streamer"], attempts, summary)


def mark_unclippable(config: Config, burst, reason: str):
    """Stop retrying every waiting burst from this stream, and let the watcher skip the stream."""
    with connect(config.paths.database) as db:
        db.execute(
            """INSERT INTO unclippable_streams(stream_id, streamer, reason) VALUES (?, ?, ?)
               ON CONFLICT(stream_id) DO UPDATE SET reason=excluded.reason""",
            (burst["stream_id"], burst["streamer"], reason),
        )
        db.execute(
            """UPDATE live_bursts SET state='unavailable', detail=?, last_error_at=unixepoch()
               WHERE stream_id=? AND state='waiting'""", (reason, burst["stream_id"]),
        )
    logging.warning("Clips from %s's current stream are unavailable: %s", burst["streamer"], reason)


def _render(config: Config, transcriber: Transcriber, burst, vod_id: str, start: int,
            vod_duration: int, muted: float = 0.0):
    stream_id, anchor = burst["stream_id"], burst["anchor"]
    clip_id = f"live-{stream_id}-{anchor}"
    relative = Path("clips") / f"{clip_id}.mp4"
    staging = config.paths.data_dir / "work"
    staging.mkdir(parents=True, exist_ok=True)
    length = 2 * HALF_WINDOW
    before = min(PAD, start)
    section = (start - before, min(vod_duration, start + length + PAD))
    with TemporaryDirectory(prefix=f"{clip_id}-", dir=staging, ignore_cleanup_errors=True) as directory:
        working = Path(directory)
        began = time.monotonic()
        source = download_vod(f"https://www.twitch.tv/videos/{vod_id}", working, section=section,
                              max_height=max_source_height(config.editing))
        downloaded = time.monotonic()
        details = {}
        # render() cuts the exact 60 seconds and rejects a section that is still too short.
        transcript = render(source, working, config.paths.data_dir / relative,
                            Moment(before, length, burst["messages"]), burst["streamer"],
                            config.editing, transcriber, details, topic=burst["game_name"], hook_key=clip_id)
        metadata = generate(transcript, burst["game_name"], burst["streamer"], vod_id, start)
        with connect(config.paths.database) as db:
            db.execute("INSERT OR IGNORE INTO vods(id, streamer, status) VALUES (?, ?, 'live-window')",
                       (vod_id, burst["streamer"]))
            db.execute(INSERT_CLIP, (clip_id, vod_id, burst["streamer"], relative.as_posix(), start,
                                    length, transcript, metadata.title, metadata.description, metadata.hashtags))
            db.execute(
                """INSERT OR REPLACE INTO clip_features(clip_id, source, speech_seconds, word_count,
                       language, language_probability) VALUES (?, ?, ?, ?, ?, ?)""",
                (clip_id, burst["trigger_kind"], details.get("speech_seconds"), details.get("word_count"),
                 details.get("language"), details.get("language_probability")),
            )
            db.execute("UPDATE burst_features SET muted_overlap_s=? WHERE stream_id=? AND anchor=?",
                       (muted, stream_id, anchor))
            db.execute(
                """UPDATE live_bursts SET state='queued', clip_id=?, detail=?
                   WHERE stream_id=? AND anchor=?""",
                (clip_id, f"VOD {vod_id} at {start}s", stream_id, anchor),
            )
    logging.info("Queued live clip %s from %s for review (download %.1fs)", clip_id,
                 burst["streamer"], downloaded - began)


def render_due(config: Config, stop: Event | None = None, context: RenderContext | None = None):
    """Process a short batch. A separate lock lets a standalone worker resume saved jobs."""
    try:
        with worker_lock(config.paths.data_dir, "render-live"):
            _process_due(config, stop, context or RenderContext(config.editing.whisper_model))
    except WorkerBusy:
        return


def _process_due(config: Config, stop: Event | None, context: RenderContext):
    with connect(config.paths.database) as db:
        db.execute(
            """UPDATE live_bursts SET state='unavailable', detail='No complete readable VOD window in 48 hours'
               WHERE state='waiting' AND anchor < unixepoch() - ?""", (MAX_ARCHIVE_WAIT,),
        )
        due = db.execute(
            """SELECT * FROM live_bursts WHERE state='waiting' AND retry_at<=unixepoch()
               ORDER BY retry_at, anchor LIMIT 5""",
        ).fetchall()
    if not due:
        return
    archives = {}  # Fresh per batch: archive durations grow while a stream is live.
    for burst in due:
        if stop is not None and stop.is_set():
            break
        broadcaster = burst["broadcaster_id"]
        try:
            if broadcaster not in archives:
                archives[broadcaster] = context.client().get("videos", {
                    "user_id": broadcaster, "type": "archive", "sort": "time", "first": 30,
                })["data"]
            listed = archives[broadcaster]
            window = archive_window(listed, burst["stream_id"], burst["anchor"])
            if window is None:
                has_archive = any(vod.get("stream_id") == burst["stream_id"] for vod in listed)
                if not has_archive and time.time() - burst["anchor"] > NO_ARCHIVE_GRACE:
                    mark_unclippable(config, burst, "No Twitch archive for this stream (VODs may be off)")
                else:
                    _waiting(config, burst, "Waiting for the Twitch archive to cover the full 60 seconds")
                continue
            vod_id, start = window
            if any(vod["id"] == vod_id and vod.get("viewable") == "private" for vod in listed):
                mark_unclippable(config, burst, "The Twitch archive is not public")
                continue
            duration = next(_duration(vod["duration"]) for vod in listed if vod["id"] == vod_id)
            _render(config, context.whisper(), burst, vod_id, start, duration,
                    muted_overlap(listed, vod_id, start, 2 * HALF_WINDOW))
        except VodUnavailable as error:
            # Subscriber-only or removed. This tool never works around access restrictions.
            mark_unclippable(config, burst, f"VOD unavailable: {describe(error)[:300]}")
        except requests.RequestException as error:
            _failed_attempt(config, burst, error)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            _failed_attempt(config, burst, error)


def live_status(config: Config):
    with connect(config.paths.database) as db:
        recent = db.execute("SELECT * FROM live_bursts ORDER BY anchor DESC LIMIT 30").fetchall()
    if not recent:
        print("No chat bursts recorded.")
        return
    for burst in recent:
        timestamp = datetime.fromtimestamp(burst["anchor"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        detail = burst["clip_id"] or burst["detail"] or ""
        kind = "" if burst["trigger_kind"] == "burst" else f' [{burst["trigger_kind"]}]'
        print(f'{timestamp}  @{burst["streamer"]}{kind}  {burst["messages"]} msgs/10s  {burst["state"]}  {detail}')
        if burst["state"] == "waiting" and burst["last_error"]:
            print(f'    last error (attempt {burst["attempts"]}): {burst["last_error"][:300]}')
