"""Measure each rendered clip: loudness, when sound starts, silence, scene cuts, chat and streamer.

These are facts about the clip, recorded once in ``clip_stats``. Nothing here judges a clip;
`view_model` relates these numbers to how posted clips actually performed.
"""

from datetime import datetime
from pathlib import Path
import logging
import re
import time

from .config import Config
from .db import connect
from .diagnostics import CommandFailed, run
from .download import ffmpeg_binary

# A live clip without a recorded cut spans this many seconds either side of its chat anchor.
HALF_WINDOW = 30
# Chat compared before and after the clip, in seconds.
AROUND = 60
SILENCE_DB = -35
SCENE_THRESHOLD = 0.3
BROADCASTER_MAX_AGE = 7 * 86400

COLUMNS = ("loudness_lufs", "loudness_range_lu", "true_peak_dbfs", "max_momentary_lufs", "loudest_at_s",
           "first_sound_s", "silence_fraction", "scene_changes", "chat_messages", "chat_before",
           "chat_after", "chat_peak_second", "chat_peak_at_s", "chat_emotes", "chat_cheers", "chat_bits",
           "broadcaster_type", "broadcaster_age_days", "error")


def _number(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    if not match:
        return None
    value = float(match.group(1))
    return None if value in (float("inf"), float("-inf")) else value


def parse_audio(stdout: str, stderr: str, duration: float) -> dict:
    """Read ffmpeg ebur128 (summary on stderr, momentary loudness on stdout) and silencedetect output."""
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else ""
    momentary = [(float(t), float(m)) for t, m in
                 re.findall(r"pts_time:([\d.]+)\s+lavfi\.r128\.M=(-?[\d.]+)", stdout)]
    loudest = max(momentary, key=lambda item: item[1]) if momentary else None
    starts = [float(value) for value in re.findall(r"silence_start:\s*(-?[\d.]+)", stderr)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([\d.]+)", stderr)]
    silent = 0.0
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else duration
        silent += max(0.0, min(end, duration) - max(start, 0.0))
    first_sound = ends[0] if starts and starts[0] <= 0.05 and ends else 0.0
    if starts and starts[0] <= 0.05 and not ends:
        first_sound = None  # Silent throughout
    return {
        "loudness_lufs": _number(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary),
        "loudness_range_lu": _number(r"LRA:\s*(-?[\d.]+)\s*LU", summary),
        "true_peak_dbfs": _number(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary),
        "max_momentary_lufs": loudest[1] if loudest else None,
        "loudest_at_s": loudest[0] if loudest else None,
        "first_sound_s": first_sound,
        "silence_fraction": round(silent / duration, 3) if duration else None,
    }


def _audio(path: Path, duration: float) -> dict:
    try:
        result = run([ffmpeg_binary(), "-hide_banner", "-nostats", "-i", str(path), "-vn",
                      "-af", f"ebur128=peak=true:metadata=1,ametadata=print:key=lavfi.r128.M:file=-,"
                             f"silencedetect=n={SILENCE_DB}dB:d=0.3",
                      "-f", "null", "-"], timeout=300, low_priority=True)
    except CommandFailed as error:
        if "matches no streams" in (error.stderr or "") or "does not contain any stream" in (error.stderr or ""):
            return {"silence_fraction": 1.0}
        raise
    return parse_audio(result.stdout, result.stderr, duration)


def _scenes(path: Path, layout: str) -> int:
    # In the "fit" layout the stream sits in the middle band; captions and black bars would
    # hide cuts. Small frames are enough to notice a cut, and much faster to compare.
    crop = "crop=iw:ih*0.32:0:ih*0.34," if layout == "fit" else ""
    result = run([ffmpeg_binary(), "-hide_banner", "-nostats", "-i", str(path), "-an",
                  "-vf", f"{crop}scale=180:-2,select='gt(scene\\,{SCENE_THRESHOLD})',showinfo",
                  "-f", "null", "-"], timeout=300, low_priority=True)
    return sum("pts_time" in line for line in result.stderr.splitlines())


def _chat(db, stream_id: str, anchor: int, before: int | None = None, after: int | None = None) -> dict:
    """Chat during the clip's own window: ``before`` and ``after`` seconds around its anchor."""
    before = HALF_WINDOW if before is None else before
    after = HALF_WINDOW if after is None else after
    start, end = anchor - before, anchor + after
    rows = db.execute("""SELECT ts, messages, COALESCE(emotes, 0) AS emotes, COALESCE(cheers, 0) AS cheers,
                                COALESCE(bits, 0) AS bits FROM chat_seconds
                         WHERE stream_id=? AND ts >= ? AND ts < ?""",
                      (stream_id, start - AROUND, end + AROUND)).fetchall()
    if not rows:
        return {}
    inside = [row for row in rows if start <= row["ts"] < end]
    peak = max(inside, key=lambda row: row["messages"], default=None)
    return {
        "chat_messages": sum(row["messages"] for row in inside),
        "chat_before": sum(row["messages"] for row in rows if row["ts"] < start),
        "chat_after": sum(row["messages"] for row in rows if row["ts"] >= end),
        "chat_peak_second": peak["messages"] if peak else 0,
        "chat_peak_at_s": peak["ts"] - start if peak else None,
        "chat_emotes": sum(row["emotes"] for row in inside),
        "chat_cheers": sum(row["cheers"] for row in inside),
        "chat_bits": sum(row["bits"] for row in inside),
    }


def refresh_broadcasters(config: Config, twitch=None) -> int:
    """Cache partner/affiliate status and channel age for streamers with clips."""
    with connect(config.paths.database) as db:
        wanted = [row[0] for row in db.execute(
            """SELECT DISTINCT b.broadcaster_id FROM live_bursts b
               LEFT JOIN broadcasters s ON s.broadcaster_id = b.broadcaster_id
               WHERE b.clip_id IS NOT NULL AND (s.fetched IS NULL OR s.fetched < ?)""",
            (int(time.time() - BROADCASTER_MAX_AGE),))]
    if not wanted:
        return 0
    if twitch is None:
        from .discovery import Twitch
        twitch = Twitch()
    saved = 0
    for index in range(0, len(wanted), 100):
        users = twitch.get("users", {"id": wanted[index:index + 100]})["data"]
        with connect(config.paths.database) as db:
            for user in users:
                created = datetime.fromisoformat(user["created_at"].replace("Z", "+00:00")).timestamp()
                db.execute("""INSERT OR REPLACE INTO broadcasters(broadcaster_id, login, broadcaster_type,
                                  created_at, description, fetched) VALUES (?, ?, ?, ?, ?, unixepoch())""",
                           (user["id"], user["login"], user.get("broadcaster_type") or "", created,
                            user.get("description") or ""))
                saved += 1
    return saved


def measure(config: Config, budget_seconds: float = 600, limit: int | None = None,
            clip_ids: list[str] | None = None) -> int:
    """Measure clips that have no stats yet, newest first, for up to ``budget_seconds``."""
    deadline = time.monotonic() + budget_seconds
    only = f"AND c.id IN ({','.join('?' * len(clip_ids))})" if clip_ids else ""
    with connect(config.paths.database) as db:
        clips = db.execute(
            f"""SELECT c.id, c.path, c.duration, c.cut_before, c.cut_after, b.stream_id, b.anchor, b.broadcaster_id
               FROM clips c LEFT JOIN live_bursts b ON b.clip_id = c.id
               WHERE NOT EXISTS (SELECT 1 FROM clip_stats s WHERE s.clip_id = c.id) {only}
               ORDER BY c.created DESC""", clip_ids or []).fetchall()
    done = 0
    for clip in clips[:limit] if limit else clips:
        if time.monotonic() >= deadline:
            break
        path = config.paths.data_dir / clip["path"]
        if not path.is_file():
            continue
        stats: dict = {}
        try:
            stats.update(_audio(path, clip["duration"]))
            stats["scene_changes"] = _scenes(path, config.editing.layout)
        except (CommandFailed, OSError) as error:
            logging.warning("Could not measure %s: %s", clip["id"], error)
            stats["error"] = str(error)[:500]
        with connect(config.paths.database) as db:
            if clip["stream_id"]:
                stats.update(_chat(db, clip["stream_id"], clip["anchor"], clip["cut_before"], clip["cut_after"]))
                owner = db.execute("SELECT broadcaster_type, created_at FROM broadcasters WHERE broadcaster_id=?",
                                   (clip["broadcaster_id"],)).fetchone()
                if owner:
                    stats["broadcaster_type"] = owner["broadcaster_type"]
                    stats["broadcaster_age_days"] = round((time.time() - owner["created_at"]) / 86400, 1)
            db.execute(f"""INSERT OR REPLACE INTO clip_stats(clip_id, {", ".join(COLUMNS)})
                           VALUES (?, {", ".join("?" * len(COLUMNS))})""",
                       (clip["id"], *(stats.get(column) for column in COLUMNS)))
        done += 1
    return done
