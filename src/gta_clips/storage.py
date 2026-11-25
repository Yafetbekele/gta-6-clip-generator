"""Keep the clips folder from filling the disk.

Each account set keeps its newest ``runtime.max_clips_per_account`` clip videos. Older videos are
removed, and every video goes once it is past the age at which the ready step stops offering it
(three days), even an approved one still waiting to be sent: its waiting uploads are marked failed.
Database rows, ratings and measured stats are left untouched; only the video file and its edit
manifest go.
"""
import logging
import time
from collections import defaultdict
from pathlib import Path

from .config import Config
from .db import connect
from .ready import MAX_AGE

QUERY = """SELECT c.id, c.account, c.path, c.created, c.state,
    EXISTS(SELECT 1 FROM clip_stats s WHERE s.clip_id = c.id) AS measured,
    (EXISTS(SELECT 1 FROM uploads u WHERE u.clip_id = c.id)
     AND NOT EXISTS(SELECT 1 FROM uploads u WHERE u.clip_id = c.id AND u.state NOT IN ('posted', 'draft'))) AS sent
FROM clips c WHERE COALESCE(c.path, '') != ''
ORDER BY c.created DESC, c.id DESC"""


def plan_trim(config: Config, now: float | None = None) -> list[tuple[Path, int]]:
    """Videos the cap would remove, with their sizes, oldest last. Removes nothing."""
    keep = config.runtime.max_clips_per_account
    if keep <= 0:
        return []
    now = time.time() if now is None else now
    root = (config.paths.data_dir / "clips").resolve()
    with connect(config.paths.database) as db:
        rows = db.execute(QUERY).fetchall()
    # Several clip rows can point at one file: it goes only if every row lets it go.
    verdicts: dict[Path, list[bool]] = defaultdict(list)
    kept_per_account: dict[str, int] = defaultdict(int)
    for row in rows:
        path = (config.paths.data_dir / row["path"]).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".mp4" or not path.is_file():
            continue
        # Within three days, an approved clip is kept until every upload of it has gone out.
        protected = (row["state"] == "approved" and not row["sent"]) or not row["measured"]
        account = row["account"] or ""
        within_cap = kept_per_account[account] < keep
        fresh = now - row["created"] < MAX_AGE
        if fresh and (protected or within_cap):
            kept_per_account[account] += 1
            verdicts[path].append(False)
        else:
            verdicts[path].append(True)
    return [(path, path.stat().st_size) for path, votes in verdicts.items() if all(votes)]


def trim_clips(config: Config, now: float | None = None) -> tuple[int, int]:
    """Remove the videos ``plan_trim`` picks. Returns (files removed, bytes freed)."""
    removed = freed = 0
    gone: list[str] = []
    # The clips folder may be a link to another drive, so name files from it, not from data_dir.
    root = (config.paths.data_dir / "clips").resolve()
    for path, size in plan_trim(config, now):
        try:
            path.unlink()
        except OSError as error:
            logging.warning("Could not remove %s: %s", path.name, error)
            continue
        path.with_suffix(".edit.json").unlink(missing_ok=True)
        gone.append("clips/" + path.relative_to(root).as_posix())
        removed += 1
        freed += size
    if gone:
        # Uploads still waiting for a removed video can never go out; say why instead of retrying.
        marks = ",".join("?" * len(gone))
        with connect(config.paths.database) as db:
            db.execute(f"""UPDATE uploads SET state='failed', detail='Clip was over 3 days old; its video was removed'
                           WHERE state='queued' AND clip_id IN (SELECT id FROM clips WHERE path IN ({marks}))""", gone)
    if removed:
        logging.info("Removed %d old clip videos (%.1f GB) to stay under %d per account",
                     removed, freed / 1e9, config.runtime.max_clips_per_account)
    return removed, freed
