"""Turn saved live chat bursts into pending clips once the VOD covers the window."""

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
from .discovery import Twitch, _duration, _timestamp
from .download import download_vod
from .editing import probe_duration, render
from .highlights import Moment
from .metadata import generate

MAX_ARCHIVE_WAIT = 48 * 3600
HALF_WINDOW = 30


def archive_window(archives: list[dict], stream_id: str, anchor: int) -> tuple[str, int] | None:
    """Return a VOD ID and its offset for the full 30s before and after a burst."""
    for vod in archives:
        if vod.get("stream_id") != stream_id or not vod["id"].isdecimal():
            continue
        start = anchor - HALF_WINDOW - round(_timestamp(vod["created_at"]).timestamp())
        if start >= 0 and start + 2 * HALF_WINDOW <= _duration(vod["duration"]):
            return vod["id"], start
    return None


def _waiting(config: Config, burst, detail: str, delay: int = 60):
    with connect(config.paths.database) as db:
        db.execute(
            "UPDATE live_bursts SET detail=?, retry_at=? WHERE stream_id=? AND anchor=?",
            (detail, int(time.time()) + delay, burst["stream_id"], burst["anchor"]),
        )


def _render(config: Config, transcriber: Transcriber, burst, vod_id: str, start: int):
    stream_id, anchor = burst["stream_id"], burst["anchor"]
    clip_id = f"live-{stream_id}-{anchor}"
    relative = Path("clips") / f"{clip_id}.mp4"
    staging = config.paths.data_dir / "work"
    staging.mkdir(parents=True, exist_ok=True)
    length = 2 * HALF_WINDOW
    with TemporaryDirectory(prefix=f"{clip_id}-", dir=staging) as directory:
        working = Path(directory)
        section = download_vod(f"https://www.twitch.tv/videos/{vod_id}", working,
                               section=(start, start + length))
        # A live HLS playlist can lag the duration from Helix or end during a fetch.
        if abs(probe_duration(section) - length) > 0.35:
            raise ValueError("The Twitch archive has not made the full 60-second section available")
        transcript = render(section, working, config.paths.data_dir / relative,
                            Moment(0, length, burst["messages"]), burst["streamer"],
                            config.editing, transcriber)
        metadata = generate(transcript, burst["game_name"], burst["streamer"], vod_id, start)
        with connect(config.paths.database) as db:
            db.execute("INSERT OR IGNORE INTO vods(id, streamer, status) VALUES (?, ?, 'live-window')",
                       (vod_id, burst["streamer"]))
            db.execute(INSERT_CLIP, (clip_id, vod_id, burst["streamer"], relative.as_posix(), start,
                                    length, transcript, metadata.title, metadata.description, metadata.hashtags))
            db.execute(
                """UPDATE live_bursts SET state='queued', clip_id=?, detail=?
                   WHERE stream_id=? AND anchor=?""",
                (clip_id, f"VOD {vod_id} at {start}s", stream_id, anchor),
            )
    logging.info("Queued live clip %s from %s for review", clip_id, burst["streamer"])


def render_due(config: Config, stop: Event | None = None):
    """Process a short batch. A separate lock lets a standalone worker resume saved jobs."""
    try:
        with worker_lock(config.paths.data_dir, "render-live"):
            _process_due(config, stop)
    except WorkerBusy:
        return


def _process_due(config: Config, stop: Event | None):
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
    twitch = Twitch()
    transcriber = Transcriber(config.editing.whisper_model)
    archives = {}
    for burst in due:
        if stop is not None and stop.is_set():
            break
        broadcaster = burst["broadcaster_id"]
        try:
            if broadcaster not in archives:
                archives[broadcaster] = twitch.get("videos", {
                    "user_id": broadcaster, "type": "archive", "sort": "time", "first": 30,
                })["data"]
            window = archive_window(archives[broadcaster], burst["stream_id"], burst["anchor"])
            if window is None:
                _waiting(config, burst, "Waiting for the Twitch archive to cover the full 60 seconds")
                continue
            _render(config, transcriber, burst, *window)
        except requests.RequestException:
            _waiting(config, burst, "Twitch archive lookup failed. Retrying.", 120)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            _waiting(config, burst, f"Clip download or render will retry ({type(error).__name__})", 120)
            logging.warning("Live clip from %s delayed (%s)", burst["streamer"], type(error).__name__)


def live_status(config: Config):
    with connect(config.paths.database) as db:
        recent = db.execute("SELECT * FROM live_bursts ORDER BY anchor DESC LIMIT 30").fetchall()
    if not recent:
        print("No chat bursts recorded.")
        return
    for burst in recent:
        timestamp = datetime.fromtimestamp(burst["anchor"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        detail = burst["clip_id"] or burst["detail"] or ""
        print(f'{timestamp}  @{burst["streamer"]}  {burst["messages"]} msgs/10s  {burst["state"]}  {detail}')
