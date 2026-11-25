"""Turn saved live chat bursts into pending clips once the VOD covers the window."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Lock
import logging
import subprocess
import time

import requests

from .captions import Transcriber
from .config import Config
from .cuts import STANDARD, choose
from .db import INSERT_CLIP, WorkerBusy, connect, worker_lock
from .diagnostics import describe
from .discovery import Twitch, _duration, _timestamp
from .download import VodUnavailable, download_vod
from .editing import SkippedMoment, max_source_height, render
from . import filler as fillers
from . import hook_styles
from . import trim as trims
from . import caption_styles
from .highlights import Moment
from .metadata import generate

MAX_ARCHIVE_WAIT = 48 * 3600
# Twitch lists a stream's archive while it is still live. None after this long means VODs are off.
NO_ARCHIVE_GRACE = 2 * 3600
# Extra seconds fetched on each side so the render can cut the exact window from a stream copy.
PAD = 3
MAX_RETRY_DELAY = 3600


def archive_window(archives: list[dict], stream_id: str, anchor: int,
                   cut: tuple[int, int] = STANDARD) -> tuple[str, int] | None:
    """Return a VOD ID and its offset for the full window: ``cut`` seconds before and after a burst."""
    before, after = cut
    for vod in archives:
        if vod.get("stream_id") != stream_id or not vod["id"].isdecimal():
            continue
        start = anchor - before - round(_timestamp(vod["created_at"]).timestamp())
        if start >= 0 and start + before + after <= _duration(vod["duration"]):
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
    """Expensive objects kept between batches in one worker: Whisper models and an app token."""
    model: str
    device: str = "auto"
    twitch: Twitch | None = None
    # One Transcriber per clip rendering at the same time; each keeps its own language result.
    transcribers: SimpleQueue = field(default_factory=SimpleQueue)
    _lock: Lock = field(default_factory=Lock)

    def client(self) -> Twitch:
        with self._lock:
            if self.twitch is None:
                self.twitch = Twitch()
            return self.twitch

    @contextmanager
    def whisper(self):
        try:
            transcriber = self.transcribers.get_nowait()
        except Empty:
            transcriber = Transcriber(self.model, self.device)
        try:
            yield transcriber
        finally:
            self.transcribers.put(transcriber)


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


def _skip(config: Config, burst, reason: str):
    """Record that a burst's moment was not worth a clip (see chat_talk.py); it is never retried."""
    with connect(config.paths.database) as db:
        db.execute("UPDATE live_bursts SET state='skipped', detail=? WHERE stream_id=? AND anchor=?",
                   (reason[:300], burst["stream_id"], burst["anchor"]))
    logging.info("Skipped a live clip from %s: %s", burst["streamer"], reason)


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


def _cut(config: Config, burst) -> tuple[int, int]:
    return choose(burst["stream_id"], burst["anchor"], burst["trigger_kind"], config.live.test_cuts)


def _render(config: Config, transcriber: Transcriber, burst, vod_id: str, start: int,
            vod_duration: int, muted: float = 0.0):
    stream_id, anchor = burst["stream_id"], burst["anchor"]
    clip_id = f"live-{stream_id}-{anchor}"
    # Each account set has its own folder; clips from before account sets stay at the top.
    account = config.account(burst["account"])
    relative = Path("clips") / (account.name if account else "") / f"{clip_id}.mp4"
    staging = config.paths.data_dir / "work"
    staging.mkdir(parents=True, exist_ok=True)
    cut_before, cut_after = _cut(config, burst)
    hook_style = hook_styles.choose(clip_id, config.editing.test_hooks)
    filler_kind = fillers.choose(clip_id, config.editing.filler, config.paths.data_dir)
    trim_choice = trims.choose(clip_id, config.editing.trim_dead_time)
    caption_style = caption_styles.choose(clip_id, config.editing.test_captions)
    length = cut_before + cut_after
    before = min(PAD, start)
    section = (start - before, min(vod_duration, start + length + PAD))
    with TemporaryDirectory(prefix=f"{clip_id}-", dir=staging, ignore_cleanup_errors=True) as directory:
        working = Path(directory)
        began = time.monotonic()
        source = download_vod(f"https://www.twitch.tv/videos/{vod_id}", working, section=section,
                              max_height=max_source_height(config.editing))
        downloaded = time.monotonic()
        details = {}
        # render() cuts the exact window and rejects a section that is still too short.
        try:
            transcript = render(source, working, config.paths.data_dir / relative,
                                Moment(before, length, burst["messages"]), burst["streamer"],
                                config.editing, transcriber, details, topic=burst["game_name"], hook_key=clip_id,
                                language=(account.language if account else "") or "en",
                                hook_style=hook_style or hook_styles.STANDARD,
                                filler=fillers.pick(clip_id, filler_kind, config.paths.data_dir, length),
                                trim=trim_choice == trims.TRIM,
                                protect=(cut_before - trims.PROTECT, cut_before + trims.PROTECT),
                                caption_style=caption_style or caption_styles.STANDARD,
                                skip_chat_talk=config.editing.skip_chat_talk, burst=float(cut_before),
                                section_length=section[1] - section[0])
        except SkippedMoment as skipped:
            _skip(config, burst, str(skipped))
            return
        metadata = generate(transcript, burst["game_name"], burst["streamer"], vod_id, start)
        with connect(config.paths.database) as db:
            db.execute("INSERT OR IGNORE INTO vods(id, streamer, status) VALUES (?, ?, 'live-window')",
                       (vod_id, burst["streamer"]))
            db.execute(INSERT_CLIP, (clip_id, vod_id, burst["streamer"], relative.as_posix(), start,
                                    length, transcript, metadata.title, metadata.description, metadata.hashtags))
            db.execute("""UPDATE clips SET account=?, cut_before=?, cut_after=?, hook_style=?, hook_method=?,
                              filler=?, trim=?, trimmed_s=?, trimmed_before_s=?, caption_style=? WHERE id=?""",
                       (account.name if account else None, cut_before, cut_after, hook_style,
                        details.get("hook_method"), filler_kind, trim_choice, details.get("trimmed_s"),
                        details.get("trimmed_before_s"), caption_style, clip_id))
            db.execute(
                """INSERT OR REPLACE INTO clip_features(clip_id, source, speech_seconds, word_count,
                       language, language_probability, first_word_s, last_word_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (clip_id, burst["trigger_kind"], details.get("speech_seconds"), details.get("word_count"),
                 details.get("language"), details.get("language_probability"),
                 details.get("first_word_s"), details.get("last_word_s")),
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
    # Measure now: the video may be removed once every destination has it.
    try:
        from .clip_stats import measure
        measure(config, budget_seconds=120, clip_ids=[clip_id])
    except Exception:
        logging.exception("Could not measure %s; `gta-clips stats` will retry", clip_id)


def render_due(config: Config, stop: Event | None = None, context: RenderContext | None = None):
    """Process a short batch. A separate lock lets a standalone worker resume saved jobs."""
    try:
        with worker_lock(config.paths.data_dir, "render-live"):
            _process_due(config, stop, context or RenderContext(config.editing.whisper_model, config.editing.whisper_device))
    except WorkerBusy:
        return


def _process_due(config: Config, stop: Event | None, context: RenderContext):
    with connect(config.paths.database) as db:
        db.execute(
            """UPDATE live_bursts SET state='unavailable', detail='No complete readable VOD window in 48 hours'
               WHERE state='waiting' AND anchor < unixepoch() - ?""", (MAX_ARCHIVE_WAIT,),
        )
        workers = config.live.render_workers
        # On demand, only windows the ready step asked for are rendered (see on_demand.py).
        due = db.execute(
            """SELECT * FROM live_bursts WHERE state='waiting' AND retry_at<=unixepoch()
                 AND (? = 0 OR render_requested IS NOT NULL)
               ORDER BY retry_at, anchor LIMIT ?""", (int(config.live.render_on_demand), max(5, 3 * workers)),
        ).fetchall()
    if not due:
        return
    archives = {}  # Fresh per batch: archive durations grow while a stream is live.
    jobs = []
    for burst in due:
        if stop is not None and stop.is_set():
            break
        job = _attempt(config, burst, lambda burst=burst: _find_window(config, context, burst, archives))
        if job is not None:
            jobs.append(job)

    def run(job):
        burst, vod_id, start, duration, muted = job
        if stop is not None and stop.is_set():
            return
        def work():
            with context.whisper() as transcriber:
                _render(config, transcriber, burst, vod_id, start, duration, muted)
        _attempt(config, burst, work)

    if workers == 1 or len(jobs) <= 1:
        for job in jobs:
            run(job)
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as pool:
        futures = [pool.submit(run, job) for job in jobs]
    for future in futures:
        future.result()  # Errors _attempt does not handle still reach the render loop's log.


def _find_window(config: Config, context: RenderContext, burst, archives: dict):
    """The VOD window for a burst, or None after recording why it has to wait or cannot be clipped."""
    broadcaster = burst["broadcaster_id"]
    if broadcaster not in archives:
        archives[broadcaster] = context.client().get("videos", {
            "user_id": broadcaster, "type": "archive", "sort": "time", "first": 30,
        })["data"]
    listed = archives[broadcaster]
    cut = _cut(config, burst)
    window = archive_window(listed, burst["stream_id"], burst["anchor"], cut)
    if window is None:
        has_archive = any(vod.get("stream_id") == burst["stream_id"] for vod in listed)
        if not has_archive and time.time() - burst["anchor"] > NO_ARCHIVE_GRACE:
            mark_unclippable(config, burst, "No Twitch archive for this stream (VODs may be off)")
        else:
            _waiting(config, burst, f"Waiting for the Twitch archive to cover the full {sum(cut)} seconds")
        return None
    vod_id, start = window
    if any(vod["id"] == vod_id and vod.get("viewable") == "private" for vod in listed):
        mark_unclippable(config, burst, "The Twitch archive is not public")
        return None
    duration = next(_duration(vod["duration"]) for vod in listed if vod["id"] == vod_id)
    return burst, vod_id, start, duration, muted_overlap(listed, vod_id, start, sum(cut))


def _attempt(config: Config, burst, action):
    """Run one step for a burst, recording expected failures on that burst only."""
    try:
        return action()
    except VodUnavailable as error:
        # Subscriber-only or removed. This tool never works around access restrictions.
        mark_unclippable(config, burst, f"VOD unavailable: {describe(error)[:300]}")
    except requests.RequestException as error:
        _failed_attempt(config, burst, error)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        _failed_attempt(config, burst, error)
    return None


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
