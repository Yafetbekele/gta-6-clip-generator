"""Process completed Twitch VODs into pending local clips."""

from pathlib import Path
from tempfile import TemporaryDirectory
import logging
import time

from .captions import Transcriber
from .config import Config
from .db import INSERT_CLIP, connect, worker_lock
from .diagnostics import describe
from .discovery import Twitch, Vod
from .download import VodUnavailable, download_vod, replay_chat
from .editing import max_source_height, probe_duration, render
from .highlights import audio_levels, rank_moments, video_activity
from .metadata import generate

MAX_ATTEMPTS = 5
RETRY_SECONDS = 900


def _process(config: Config, twitch: Twitch, vod: Vod, transcriber: Transcriber):
    base = config.paths.data_dir
    popular = twitch.popular(vod, config.twitch.clips_per_vod)
    staging = base / "work"
    staging.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f"{vod.id}-", dir=staging, ignore_cleanup_errors=True) as folder:
        working = Path(folder)
        source = download_vod(vod.url, working, max_height=max_source_height(config.editing))
        duration = min(vod.duration, int(probe_duration(source)))
        chat = replay_chat(vod.id, working, base / "chat-imports")
        audio = audio_levels(source)
        activity = video_activity(source, duration) if config.highlights.require_motion else None
        moments = rank_moments(duration, chat, audio, popular, config.highlights,
                               active_seconds=activity)
        if not moments:
            logging.info("VOD %s has no highlight that meets the settings", vod.id)
        with connect(config.paths.database) as db:
            existing = {row[0] for row in db.execute("SELECT id FROM clips WHERE vod_id=?", (vod.id,))}
        saved = 0
        for index, moment in enumerate(moments, start=1):
            clip_id = f"{vod.id}-{index}"
            if clip_id in existing:  # Rendered by an earlier attempt; keep it and its rating.
                saved += 1
                continue
            clip_dir = working / str(index)
            clip_dir.mkdir()
            relative = Path("clips") / f"{clip_id}.mp4"
            details = {}
            transcript = render(source, clip_dir, base / relative, moment, vod.streamer,
                                config.editing, transcriber, details)
            metadata = generate(transcript, config.twitch.game_name, vod.streamer, vod.id, moment.start)
            # Save each clip as soon as it exists, so a later failure does not discard it.
            with connect(config.paths.database) as db:
                db.execute(INSERT_CLIP, (clip_id, vod.id, vod.streamer, relative.as_posix(), moment.start,
                                         moment.duration, transcript, metadata.title, metadata.description,
                                         metadata.hashtags))
                db.execute(
                    """INSERT OR REPLACE INTO clip_features(clip_id, source, chat_score, audio_score,
                           twitch_clip_score, motion_seconds, total_score, speech_seconds, word_count,
                           language, language_probability) VALUES (?, 'vod', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (clip_id, moment.chat_score, moment.audio_score, moment.twitch_clip_score,
                     moment.motion_seconds, moment.score, details.get("speech_seconds"),
                     details.get("word_count"), details.get("language"), details.get("language_probability")),
                )
            saved += 1
        with connect(config.paths.database) as db:
            db.execute("UPDATE vods SET status='done', detail=?, updated=unixepoch() WHERE id=?",
                       (f"{saved} clips", vod.id))
    logging.info("Processed VOD %s and queued %s clips", vod.id, saved)


def _finish(config: Config, vod_id: str, status: str, detail: str):
    with connect(config.paths.database) as db:
        db.execute("""UPDATE vods SET status=?, detail=?, updated=unixepoch()
                      WHERE id=? AND status='processing'""", (status, detail[:1000], vod_id))


def ingest(config: Config) -> int:
    if not config.twitch.streamers:
        logging.info('Set twitch.streamers to channel logins or ["*"] to discover archives')
        return 0
    failures = 0
    with worker_lock(config.paths.data_dir, "ingest"):
        twitch = Twitch()
        transcriber = Transcriber(config.editing.whisper_model)
        for vod in twitch.discover(config.twitch):
            with connect(config.paths.database) as db:
                existing = db.execute("SELECT status, updated, attempts FROM vods WHERE id=?",
                                  (vod.id,)).fetchone()
            if existing and (existing["status"] in ("done", "unavailable", "abandoned") or
                             (existing["status"] == "failed"
                              and existing["updated"] > time.time() - RETRY_SECONDS)):
                continue
            if twitch.is_live(vod):
                logging.info("VOD %s is still recording", vod.id)
                continue
            with connect(config.paths.database) as db:
                db.execute(
                    """INSERT INTO vods(id, streamer, status, attempts) VALUES (?, ?, 'processing', 1)
                       ON CONFLICT(id) DO UPDATE SET status='processing', updated=unixepoch(), detail=NULL,
                                                     attempts=attempts+1""",
                    (vod.id, vod.streamer),
                )
            try:
                _process(config, twitch, vod, transcriber)
            except VodUnavailable as exc:
                # Subscriber-only or removed. Never retried and never worked around.
                logging.warning("VOD %s is unavailable: %s", vod.id, describe(exc))
                _finish(config, vod.id, "unavailable", describe(exc))
            except Exception as exc:
                failures += 1
                attempts = (existing["attempts"] if existing else 0) + 1
                status = "abandoned" if attempts >= MAX_ATTEMPTS else "failed"
                logging.error("VOD %s failed on attempt %s/%s", vod.id, attempts, MAX_ATTEMPTS, exc_info=exc)
                _finish(config, vod.id, status, describe(exc))
    return int(failures > 0)
