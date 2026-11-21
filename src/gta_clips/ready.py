"""Copy recent clips of each account set into a folder for posting by hand.

Nothing here uploads anything. A person opens the folder, watches each clip and posts it
(or not) through the platform's own app, which keeps human approval before publication.
"""

from datetime import datetime, timedelta
from pathlib import Path
import logging
import random
import re
import shutil
import sqlite3
import time

from .config import Config
from .db import connect
from .metadata import clean_title
from .view_model import Model, clip_features, train

FOLDER = "ready-to-post"
# Clips older than this are not offered any more; the moment has passed.
MAX_AGE = 3 * 86400

# Which clips may be offered. A rejected clip, or one you rated 1 or 2, is never offered.
# The order among the rest comes from `_order`: stats tied to real views, or random until then.
CANDIDATES = """
SELECT c.id, c.path, c.streamer, c.title, c.description, c.hashtags, c.created
FROM clips c
LEFT JOIN clip_ratings r ON r.clip_id = c.id
WHERE c.account = ? AND c.state != 'rejected' AND c.created >= ?
  AND COALESCE(r.score, 3) >= 3
  AND NOT EXISTS (SELECT 1 FROM ready_exports e WHERE e.clip_id = c.id)
"""
# With a views model, this share of picks is still random, so the model keeps seeing clips
# it would not have chosen and can correct itself.
EXPLORE = 0.2


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
            + (f"Post to:\n{post_to}" if post_to else "")
            + (f"Sign-in email: {account.email}\n" if account.email else "") + "\n"
            f"Title:\n{clip['title']}\n\n"
            f"Description:\n{clip['description']}\n\n"
            f"Hashtags:\n{clip['hashtags']}\n\n"
            f"Clip ID: {clip['id']}\n")


def _order(db, candidates: list, model: Model | None, rng: random.Random) -> tuple[list, dict]:
    """Highest predicted views first, with some random picks; fully random without a model.

    Also returns each clip's predicted log views, kept so they can be checked against real views.
    """
    remaining = list(candidates)
    rng.shuffle(remaining)
    if model is None or not remaining:
        return remaining, {}
    found = clip_features(db, [clip["id"] for clip in remaining])
    predicted = {clip_id: float(value) for clip_id, value in zip(found, model.predict(list(found.values())))}
    remaining.sort(key=lambda clip: -predicted.get(clip["id"], float("-inf")))
    ordered = []
    while remaining:
        ordered.append(remaining.pop(rng.randrange(len(remaining)) if rng.random() < EXPLORE else 0))
    return ordered, predicted


def export_due(config: Config, now: float | None = None, rng: random.Random | None = None) -> list[Path]:
    """Add the next clips for each account set whose share for today is not used up yet."""
    per_day = config.runtime.ready_per_day
    if not config.accounts or per_day <= 0:
        return []
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now)
    day = moment.strftime("%Y-%m-%d")
    base = config.paths.data_dir
    added = []
    rng = rng or random.Random()
    model = train(config, now)
    for account in config.accounts:
        with connect(config.paths.database) as db:
            done = db.execute("SELECT COUNT(*) FROM ready_exports WHERE account=? AND day=?",
                              (account.name, day)).fetchone()[0]
            wanted = allowed_by(per_day, moment) - done
            if wanted <= 0:
                continue
            candidates, predicted = _order(
                db, db.execute(CANDIDATES, (account.name, int(now - MAX_AGE))).fetchall(), model, rng)
        for clip in candidates:
            if wanted <= 0:
                break
            source = base / clip["path"]
            if not source.is_file():
                continue
            folder = base / FOLDER / day / account.name
            folder.mkdir(parents=True, exist_ok=True)
            # The number after the time is the clip's place in today's order, so clips added in
            # the same minute still sort best first by file name.
            done += 1
            stem = (f"{moment:%H%M}-{done:02d} {_safe(clip['streamer'], 25)} - "
                    f"{_safe(clip['title'].split(' | @')[0])}")
            target = folder / f"{stem}.mp4"
            shutil.copy2(source, target)
            target.with_suffix(".txt").write_text(_note(account, clip), encoding="utf-8")
            with connect(config.paths.database) as db:
                db.execute("INSERT INTO ready_exports(clip_id, account, day, path, predicted) VALUES (?, ?, ?, ?, ?)",
                           (clip["id"], account.name, day, target.relative_to(base).as_posix(),
                            predicted.get(clip["id"])))
                queue_instagram(config, db, account.name, clip["id"])
            logging.info("Ready to post on %s: %s", account.name, target.name)
            added.append(target)
            wanted -= 1
    return added


def queue_instagram(config: Config, db, account: str, clip_id: str) -> bool:
    """Approve a clip the ready step just picked for Instagram, if ``account`` posts there by API.

    The ready step's pick stands in for a manual approval (the browser uploader posts the same
    clips to YouTube). Only one clip waits per account at a time, so a daily limit lower than
    ready_per_day posts the newest picks instead of building a backlog of stale ones.
    """
    if config.schedule["instagram"].daily_limit <= 0:
        return False
    if not db.execute("SELECT 1 FROM oauth WHERE platform=?", (f"instagram@{account}",)).fetchone():
        return False
    waiting = db.execute("""SELECT 1 FROM uploads u JOIN clips c ON c.id = u.clip_id
                            WHERE u.platform='instagram' AND u.state='queued' AND c.account=?""",
                         (account,)).fetchone()
    if waiting:
        return False
    clip = db.execute("SELECT state, title, description, hashtags FROM clips WHERE id=?", (clip_id,)).fetchone()
    if clip is None or clip["state"] != "pending":
        return False
    # Instagram's caption limit, as review.decide checks for manual approvals.
    if len(f'{clip["title"]}\n\n{clip["description"]}\n\n{clip["hashtags"]}') > 2200:
        return False
    db.execute("UPDATE clips SET state='approved' WHERE id=?", (clip_id,))
    db.execute("INSERT OR IGNORE INTO uploads(clip_id, platform, checkpoint) VALUES (?, 'instagram', '{}')",
               (clip_id,))
    logging.info("Queued %s for Instagram on %s", clip_id, account)
    return True


def queue_ready_for_instagram(config: Config) -> int:
    """Queue the newest of today's ready clips for each API Instagram account with nothing waiting."""
    queued = 0
    with connect(config.paths.database) as db:
        rows = db.execute("""SELECT clip_id, account FROM ready_exports WHERE day=?
                             ORDER BY rowid DESC""", (datetime.now().strftime("%Y-%m-%d"),)).fetchall()
        for row in rows:
            queued += queue_instagram(config, db, row["account"], row["clip_id"])
    return queued


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


def _not_retitled(config: Config) -> set[str]:
    """Clips whose title must stay: already on YouTube, or mid-upload or held anywhere."""
    keep = set()
    ledger = config.paths.data_dir / "browser-upload" / "posts.sqlite3"
    if ledger.is_file():
        source = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True)
        try:
            keep |= {row[0] for row in source.execute(
                "SELECT clip_id FROM posts WHERE platform='youtube' OR state NOT IN ('posted', 'under_review')")}
        finally:
            source.close()
    with connect(config.paths.database) as db:
        keep |= {row[0] for row in db.execute(
            """SELECT clip_id FROM posts WHERE platform='youtube' AND clip_id IS NOT NULL
               UNION SELECT clip_id FROM uploads WHERE platform='youtube' OR state != 'queued'""")}
    return keep


def retitle_explicit(config: Config) -> int:
    """Pick a clean title for clips made before titles skipped swearing, unless already on YouTube.

    YouTube age-restricted some clips whose titles swore. Clips already posted there keep their
    title so the record matches what is public; the .txt notes pick up the change in `rewrite_notes`.
    """
    keep = _not_retitled(config)
    changed = 0
    with connect(config.paths.database) as db:
        rows = db.execute("""SELECT c.id, c.title, c.description, c.transcript,
                                    COALESCE(b.game_name, 'Gameplay') AS game
                             FROM clips c LEFT JOIN live_bursts b ON b.clip_id = c.id""").fetchall()
        for row in rows:
            if row["id"] in keep:
                continue
            cleaned = clean_title(row["title"], row["description"], row["transcript"], row["game"])
            if cleaned and cleaned != (row["title"], row["description"]):
                db.execute("UPDATE clips SET title=?, description=? WHERE id=?", (*cleaned, row["id"]))
                changed += 1
    if changed:
        logging.info("Gave %d clips a title without swearing", changed)
    return changed
