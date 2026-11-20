"""Remove ready copies only when every configured destination has accepted the clip."""
import sqlite3
from pathlib import Path

from .browser_upload import scan
from .db import worker_lock, WorkerBusy


def cleanup_used(config, root, state):
    root = Path(root).resolve()
    state = Path(state)
    database = state / 'posts.sqlite3'
    if not database.is_file():
        return 0
    removed = 0
    try:
        with worker_lock(state, 'ready-cleanup'):
            db = sqlite3.connect(database, timeout=30)
            try:
                for account, targets in config['accounts'].items():
                    platforms = [platform for platform, target in targets.items()
                                 if target.get('channel_id' if platform == 'youtube' else 'handle', '').strip()]
                    if not platforms:
                        continue
                    for clip in scan(root, account):
                        # Never follow a ready-folder link into source clips or outside the queue.
                        if not clip.file.is_relative_to(root):
                            continue
                        note = clip.file.with_suffix('.txt')
                        if not note.resolve().is_relative_to(root):
                            continue
                        complete = True
                        for platform in platforms:
                            rows = db.execute('SELECT state FROM posts WHERE account=? AND platform=? AND (clip_id=? OR digest=?)',
                                              (account, platform, clip.clip_id, clip.digest)).fetchall()
                            if not rows or any(row[0] not in ('posted', 'under_review') for row in rows):
                                complete = False
                                break
                        if complete:
                            clip.file.unlink(missing_ok=True)
                            note.unlink(missing_ok=True)
                            removed += 1
            finally:
                db.close()
    except WorkerBusy:
        pass  # Another uploader is cleaning; the next scheduled cycle checks again.
    return removed
