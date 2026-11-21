"""SQLite state and cross-process worker locks."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import time


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
CREATE TABLE IF NOT EXISTS live_bursts (
    stream_id TEXT NOT NULL, anchor INTEGER NOT NULL,
    broadcaster_id TEXT NOT NULL, streamer TEXT NOT NULL, game_name TEXT NOT NULL,
    messages INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
    retry_at INTEGER NOT NULL, detail TEXT, clip_id TEXT REFERENCES clips(id),
    PRIMARY KEY (stream_id, anchor)
);
CREATE INDEX IF NOT EXISTS live_bursts_due ON live_bursts(state, retry_at);
CREATE TABLE IF NOT EXISTS burst_features (
    stream_id TEXT NOT NULL, anchor INTEGER NOT NULL, baseline_per_10s REAL,
    observed_seconds REAL, multiplier REAL NOT NULL, minimum_messages INTEGER NOT NULL,
    warmup_seconds INTEGER NOT NULL, PRIMARY KEY(stream_id, anchor),
    FOREIGN KEY(stream_id, anchor) REFERENCES live_bursts(stream_id, anchor)
);
CREATE TABLE IF NOT EXISTS clip_ratings (
    clip_id TEXT PRIMARY KEY REFERENCES clips(id),
    score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5), notes TEXT NOT NULL,
    updated INTEGER NOT NULL DEFAULT (unixepoch())
);
"""

INSERT_CLIP = """INSERT INTO clips
    (id, vod_id, streamer, path, start, duration, transcript, title, description, hashtags)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

# Each migration only adds tables or columns, so existing clips and ratings are never rewritten.
# A migration is a list of (table, column, definition) to add, plus SQL to run afterwards.
MIGRATIONS = [
    (
        [
            ("live_bursts", "attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("live_bursts", "last_error", "TEXT"),
            ("live_bursts", "last_error_at", "INTEGER"),
            ("live_bursts", "trigger_kind", "TEXT NOT NULL DEFAULT 'burst'"),
            ("burst_features", "unique_chatters", "INTEGER"),
            ("burst_features", "peak_messages", "INTEGER"),
            ("burst_features", "peak_unique_chatters", "INTEGER"),
            ("burst_features", "peak_offset_s", "REAL"),
            ("burst_features", "viewer_count", "INTEGER"),
            ("burst_features", "viewer_rank", "INTEGER"),
            ("burst_features", "language", "TEXT"),
            ("burst_features", "stream_age_s", "REAL"),
            ("burst_features", "covered_seconds", "REAL"),
            ("burst_features", "muted_overlap_s", "REAL"),
            ("vods", "attempts", "INTEGER NOT NULL DEFAULT 0"),
        ],
        """
        CREATE TABLE IF NOT EXISTS rating_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL REFERENCES clips(id),
            score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5), notes TEXT NOT NULL,
            rated_at INTEGER NOT NULL DEFAULT (unixepoch()), watched_seconds REAL,
            rubric_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS rating_events_clip ON rating_events(clip_id, rated_at);
        INSERT INTO rating_events(clip_id, score, notes, rated_at)
            SELECT clip_id, score, notes, updated FROM clip_ratings;
        CREATE TABLE IF NOT EXISTS clip_features (
            clip_id TEXT PRIMARY KEY REFERENCES clips(id), source TEXT NOT NULL,
            chat_score REAL, audio_score REAL, twitch_clip_score REAL, motion_seconds INTEGER,
            total_score REAL, speech_seconds REAL, word_count INTEGER,
            language TEXT, language_probability REAL
        );
        CREATE TABLE IF NOT EXISTS watch_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started INTEGER NOT NULL DEFAULT (unixepoch()),
            ended INTEGER, exit_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS chat_seconds (
            stream_id TEXT NOT NULL, ts INTEGER NOT NULL, messages INTEGER NOT NULL,
            unique_chatters INTEGER NOT NULL, PRIMARY KEY(stream_id, ts)
        );
        CREATE TABLE IF NOT EXISTS unclippable_streams (
            stream_id TEXT PRIMARY KEY, streamer TEXT NOT NULL, reason TEXT NOT NULL,
            noted INTEGER NOT NULL DEFAULT (unixepoch())
        );
        """,
    ),
    (
        [],
        """
        CREATE TABLE IF NOT EXISTS channel_snapshots (
            ts INTEGER NOT NULL, stream_id TEXT NOT NULL, streamer TEXT NOT NULL,
            viewers INTEGER, viewer_rank INTEGER, watched INTEGER NOT NULL,
            PRIMARY KEY(ts, stream_id)
        );
        CREATE INDEX IF NOT EXISTS channel_snapshots_streamer ON channel_snapshots(streamer, ts)
        """,
    ),
    (
        # The account set a window or clip belongs to. NULL for clips made before account sets.
        [("live_bursts", "account", "TEXT"), ("clips", "account", "TEXT")],
        "CREATE INDEX IF NOT EXISTS clips_account ON clips(account, state)",
    ),
    (
        # Clips copied to the ready-to-post folder, so each is offered once.
        [],
        """
        CREATE TABLE IF NOT EXISTS ready_exports (
            clip_id TEXT PRIMARY KEY REFERENCES clips(id), account TEXT NOT NULL, day TEXT NOT NULL,
            exported INTEGER NOT NULL DEFAULT (unixepoch()), path TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ready_exports_day ON ready_exports(account, day)
        """,
    ),
    (
        # Everything measurable about a clip, and how each post of it performed.
        [
            ("burst_features", "stream_title", "TEXT"),
            ("burst_features", "stream_tags", "TEXT"),
            ("burst_features", "is_mature", "INTEGER"),
            ("chat_seconds", "emotes", "INTEGER"),
            ("chat_seconds", "cheers", "INTEGER"),
            ("chat_seconds", "bits", "INTEGER"),
            ("clip_features", "first_word_s", "REAL"),
            ("clip_features", "last_word_s", "REAL"),
        ],
        """
        CREATE TABLE IF NOT EXISTS clip_stats (
            clip_id TEXT PRIMARY KEY REFERENCES clips(id), measured INTEGER NOT NULL DEFAULT (unixepoch()),
            loudness_lufs REAL, loudness_range_lu REAL, true_peak_dbfs REAL, max_momentary_lufs REAL,
            loudest_at_s REAL, first_sound_s REAL, silence_fraction REAL, scene_changes INTEGER,
            chat_messages INTEGER, chat_before INTEGER, chat_after INTEGER, chat_peak_second INTEGER,
            chat_peak_at_s REAL, chat_emotes INTEGER, chat_cheers INTEGER, chat_bits INTEGER,
            broadcaster_type TEXT, broadcaster_age_days REAL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS broadcasters (
            broadcaster_id TEXT PRIMARY KEY, login TEXT, broadcaster_type TEXT, created_at REAL,
            description TEXT, fetched INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS posts (
            platform TEXT NOT NULL, video_id TEXT NOT NULL, clip_id TEXT REFERENCES clips(id),
            account TEXT, url TEXT, source TEXT NOT NULL, added INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (platform, video_id)
        );
        CREATE TABLE IF NOT EXISTS post_stats (
            platform TEXT NOT NULL, video_id TEXT NOT NULL, fetched INTEGER NOT NULL,
            published REAL, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
            avg_view_seconds REAL, avg_view_percent REAL, subscribers_gained INTEGER,
            PRIMARY KEY (platform, video_id, fetched),
            FOREIGN KEY (platform, video_id) REFERENCES posts(platform, video_id)
        );
        """,
    ),
    # The views model's guess for each clip when it was offered, to check against its real views.
    ((("ready_exports", "predicted", "REAL"),), ""),
    # Seconds of stream a live clip shows before and after its chat burst (see cuts.py).
    # Live clips before the cut test were all 30 s + 30 s.
    ((("clips", "cut_before", "INTEGER"), ("clips", "cut_after", "INTEGER")),
     "UPDATE clips SET cut_before = 30, cut_after = 30 WHERE id LIKE 'live-%' AND duration = 60"),
    # The hook style a clip was given by the hook test (see hook_styles.py; empty when the test was
    # off) and the kind of hook actually shown: a ContextHook method such as debate_quote_v1.
    ((("clips", "hook_style", "TEXT"), ("clips", "hook_method", "TEXT")), ""),
]
SCHEMA_VERSION = len(MIGRATIONS)


def _columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def backup(path: Path, label: str) -> Path:
    """Copy a live database consistently, including changes still in the WAL file."""
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}-{label}-{int(time.time())}.sqlite3"
    source = sqlite3.connect(path, timeout=20)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def _migrate(db, path: Path):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    has_data = db.execute("SELECT EXISTS(SELECT 1 FROM clips) OR EXISTS(SELECT 1 FROM clip_ratings)").fetchone()[0]
    if has_data:
        backup(path, f"pre-v{SCHEMA_VERSION}")
    db.execute("BEGIN IMMEDIATE")
    try:
        # Another process may have migrated while this one waited for the lock.
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for columns, script in MIGRATIONS[version:]:
            for table, column, definition in columns:
                if column not in _columns(db, table):
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            for statement in (part.strip() for part in script.split(";")):
                if statement:
                    db.execute(statement)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            db.executescript(SCHEMA)
            _migrate(db, path)
        db.isolation_level = ""
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
