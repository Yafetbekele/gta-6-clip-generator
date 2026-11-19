"""Copy the strongest recent clips of each account set into a folder for posting by hand.

Nothing here uploads anything. A person opens the folder, watches each clip and posts it
(or not) through the platform's own app, which keeps human approval before publication.
"""

from datetime import datetime, timedelta
from pathlib import Path
import logging
import re
import shutil
import time

from .config import Config
from .db import connect

FOLDER = "ready-to-post"
# Clips older than this are not offered any more; the moment has passed.
MAX_AGE = 3 * 86400

# Ranking, strongest first. A clip you rated 4 or 5 beats everything; a rating of 1 or 2 rules it
# out. Then chat bursts beat near misses and random comparison windows, and a bigger jump over the
# channel's normal chat rate beats a smaller one. These are recorded signals, not a trained model.
CANDIDATES = """
SELECT c.id, c.path, c.streamer, c.title, c.description, c.hashtags, c.created, r.score,
       COALESCE(b.trigger_kind, 'burst') AS kind,
       CAST(b.messages AS REAL) / MAX(COALESCE(f.baseline_per_10s, 1), 1) AS spike,
       COALESCE(f.unique_chatters, 0) AS chatters
FROM clips c
LEFT JOIN clip_ratings r ON r.clip_id = c.id
LEFT JOIN live_bursts b ON b.clip_id = c.id
LEFT JOIN burst_features f ON f.stream_id = b.stream_id AND f.anchor = b.anchor
WHERE c.account = ? AND c.state != 'rejected' AND c.created >= ?
  AND COALESCE(r.score, 3) >= 3
  AND NOT EXISTS (SELECT 1 FROM ready_exports e WHERE e.clip_id = c.id)
ORDER BY (COALESCE(r.score, 0) >= 4) DESC, (COALESCE(b.trigger_kind, 'burst') = 'burst') DESC,
         spike DESC, chatters DESC, c.created DESC
"""


def allowed_by(per_day: int, moment: datetime) -> int:
    """Spread the day's clips out: one at midnight, then one more every 24/per_day hours."""
    if per_day <= 0:
        return 0
    elapsed = (moment - moment.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    return min(per_day, int(elapsed * per_day // 86400) + 1)


def _safe(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip().rstrip(".")
    return " ".join(cleaned.split())[:limit].strip() or "clip"


def _note(account, clip) -> str:
    post_to = "".join(f"  {platform}: {handle}\n" for platform, handle in account.destinations)
    return (f"Channel: {account.title} ({account.name})\n"
            + (f"Post to:\n{post_to}" if post_to else "") + "\n"
            f"Title:\n{clip['title']}\n\n"
            f"Description:\n{clip['description']}\n\n"
            f"Hashtags:\n{clip['hashtags']}\n\n"
            f"Clip ID: {clip['id']}\n")


def export_due(config: Config, now: float | None = None) -> list[Path]:
    """Add the next best clip for each account set whose share for today is not used up yet."""
    per_day = config.runtime.ready_per_day
    if not config.accounts or per_day <= 0:
        return []
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now)
    day = moment.strftime("%Y-%m-%d")
    base = config.paths.data_dir
    added = []
    for account in config.accounts:
        with connect(config.paths.database) as db:
            done = db.execute("SELECT COUNT(*) FROM ready_exports WHERE account=? AND day=?",
                              (account.name, day)).fetchone()[0]
            wanted = allowed_by(per_day, moment) - done
            if wanted <= 0:
                continue
            candidates = db.execute(CANDIDATES, (account.name, int(now - MAX_AGE))).fetchall()
        for clip in candidates:
            if wanted <= 0:
                break
            source = base / clip["path"]
            if not source.is_file():
                continue
            folder = base / FOLDER / day / account.name
            folder.mkdir(parents=True, exist_ok=True)
            stem = f"{moment:%H%M} {_safe(clip['streamer'], 25)} - {_safe(clip['title'].split(' | @')[0])}"
            target = folder / f"{stem}.mp4"
            shutil.copy2(source, target)
            target.with_suffix(".txt").write_text(_note(account, clip), encoding="utf-8")
            with connect(config.paths.database) as db:
                db.execute("INSERT INTO ready_exports(clip_id, account, day, path) VALUES (?, ?, ?, ?)",
                           (clip["id"], account.name, day, target.relative_to(base).as_posix()))
            logging.info("Ready to post on %s: %s", account.name, target.name)
            added.append(target)
            wanted -= 1
    return added


def rewrite_notes(config: Config):
    """Refresh the .txt beside each clip still in the folder, so edited account names show up."""
    names = {account.name: account for account in config.accounts}
    base = config.paths.data_dir
    with connect(config.paths.database) as db:
        rows = db.execute("""SELECT e.account, e.path, c.id, c.title, c.description, c.hashtags
                             FROM ready_exports e JOIN clips c ON c.id = e.clip_id""").fetchall()
    for row in rows:
        note = (base / row["path"]).with_suffix(".txt")
        if row["account"] in names and note.is_file():
            text = _note(names[row["account"]], row)
            if note.read_text(encoding="utf-8") != text:
                note.write_text(text, encoding="utf-8")


def cleanup(config: Config, keep_days: int = 7):
    """Remove day folders older than ``keep_days``; the clips themselves stay in clips/."""
    root = config.paths.data_dir / FOLDER
    if not root.is_dir():
        return
    oldest = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for folder in root.iterdir():
        if folder.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", folder.name) and folder.name < oldest:
            shutil.rmtree(folder, ignore_errors=True)
