"""Process completed Twitch VODs into pending local clips."""

from pathlib import Path
from tempfile import TemporaryDirectory
import logging
import time

from .captions import Transcriber
from .config import Config
from .db import connect, worker_lock
from .discovery import Twitch, Vod
from .download import download_vod, replay_chat
from .editing import probe_duration, render
from .highlights import audio_levels, rank_moments
from .metadata import generate


def _process(config: Config, twitch: Twitch, vod: Vod, transcriber: Transcriber):
    base = config.paths.data_dir
    popular = twitch.popular(vod, config.twitch.clips_per_vod)
    staging = base / "work"
    staging.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f"{vod.id}-", dir=staging) as folder:
        working = Path(folder)
        source = download_vod(vod.url, working)
        duration = min(vod.duration, int(probe_duration(source)))
        chat = replay_chat(vod.id, working, base / "chat-imports")
        audio = audio_levels(source)
        moments = rank_moments(duration, chat, audio, popular, config.highlights)
        rows = []
        for index, moment in enumerate(moments, start=1):
            clip_dir = working / str(index)
            clip_dir.mkdir()
            clip_id = f"{vod.id}-{index}"
            relative = Path("clips") / f"{clip_id}.mp4"
            transcript = render(source, clip_dir, base / relative, moment, vod.streamer,
                                config.editing, transcriber)
            metadata = generate(transcript, config.twitch.game_name, vod.streamer, vod.id, moment.start)
            rows.append((clip_id, vod.id, vod.streamer, relative.as_posix(), moment.start,
                         moment.duration, transcript, metadata.title, metadata.description, metadata.hashtags))
        with connect(config.paths.database) as db:
            db.executemany(
                """INSERT INTO clips
                   (id, vod_id, streamer, path, start, duration, transcript, title, description, hashtags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows,
            )
            db.execute("UPDATE vods SET status='done', detail=?, updated=unixepoch() WHERE id=?",
                       (f"{len(rows)} clips", vod.id))
    logging.info("Processed VOD %s and queued %s clips", vod.id, len(rows))


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
                existing = db.execute("SELECT status, updated FROM vods WHERE id=?", (vod.id,)).fetchone()
            if existing and (existing["status"] == "done" or
                             (existing["status"] == "failed" and existing["updated"] > time.time() - 900)):
                continue
            if twitch.is_live(vod):
                logging.info("VOD %s is still recording", vod.id)
                continue
            with connect(config.paths.database) as db:
                db.execute(
                    """INSERT INTO vods(id, streamer, status) VALUES (?, ?, 'processing')
                       ON CONFLICT(id) DO UPDATE SET status='processing', updated=unixepoch(), detail=NULL""",
                    (vod.id, vod.streamer),
                )
            try:
                _process(config, twitch, vod, transcriber)
            except Exception as exc:
                failures += 1
                logging.error("VOD %s failed (%s)", vod.id, type(exc).__name__)
                with connect(config.paths.database) as db:
                    db.execute(
                        """UPDATE vods SET status='failed', detail=?, updated=unixepoch()
                           WHERE id=? AND status='processing'""", (type(exc).__name__, vod.id),
                    )
    return int(failures > 0)
