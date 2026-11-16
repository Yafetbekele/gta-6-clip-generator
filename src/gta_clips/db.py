"""SQLite state and cross-process worker locks."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS vods (
    id TEXT PRIMARY KEY, streamer TEXT NOT NULL, status TEXT NOT NULL,
    detail TEXT, updated INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY, vod_id TEXT NOT NULL REFERENCES vods(id),
    streamer TEXT NOT NULL, path TEXT NOT NULL, start REAL NOT NULL, duration REAL NOT NULL,
    transcript TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
    hashtags TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'approved', 'rejected')),
    created INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS uploads (
    clip_id TEXT NOT NULL REFERENCES clips(id), platform TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0, sent_at INTEGER, remote_id TEXT,
    checkpoint TEXT NOT NULL DEFAULT '{}', detail TEXT, visibility TEXT, made_for_kids INTEGER,
    PRIMARY KEY (clip_id, platform)
);
CREATE TABLE IF NOT EXISTS oauth (
    platform TEXT PRIMARY KEY, token TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()


class WorkerBusy(RuntimeError):
    """Another process is running the same worker."""


@contextmanager
def worker_lock(directory: Path, name: str):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.lock").open("a+b") as file:
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkerBusy(f"{name} already running") from exc
        yield
