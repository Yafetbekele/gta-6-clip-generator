"""Remove submitted ready copies and originals; preserve database tracking records."""
import sqlite3
from pathlib import Path

from .browser_upload import scan
from .db import worker_lock, WorkerBusy


def cleanup_originals(config, data, ledger_db):
    database = data / 'state.sqlite3'
    if not database.is_file():
        return 0
    source_db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    source_db.row_factory = sqlite3.Row
    try:
        if not source_db.execute("SELECT 1 FROM sqlite_master WHERE name='clips'").fetchone():
            return 0
        columns = {r[1] for r in source_db.execute('PRAGMA table_info(clips)')}
        if not {'id', 'account', 'path'} <= columns:
            return 0
        sources = source_db.execute('SELECT id,account,path FROM clips').fetchall()
    finally:
        source_db.close()
    candidates = {}
    for row in sources:
        path = (data / row['path']).resolve()
        # Multiple clip records can share a file: every reference must qualify.
        candidates.setdefault(path, []).append(row)
    clips_root = (data / 'clips').resolve()
    removed = 0
    for path, references in candidates.items():
        if not path.is_relative_to(clips_root) or path.suffix.lower() != '.mp4' or not path.is_file():
            continue
        complete = True
        for row in references:
            targets = config['accounts'].get(row['account'], {})
            platforms = [p for p, t in targets.items() if t.get('channel_id' if p == 'youtube' else 'handle', '').strip()]
            if not platforms:
                complete = False
                break
            for platform in platforms:
                record = ledger_db.execute('SELECT state FROM posts WHERE account=? AND platform=? AND clip_id=?',
                                           (row['account'], platform, row['id'])).fetchone()
                if not record or record[0] not in ('posted', 'under_review'):
                    complete = False
                    break
            if not complete:
                break
        if complete:
            path.unlink()
            removed += 1
    return removed


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
                originals = cleanup_originals(config, state.parent, db)
                if originals:
                    print(f'Deleted {originals} fully submitted original video(s).', flush=True)
            finally:
                db.close()
    except WorkerBusy:
        pass  # Another uploader is cleaning; the next scheduled cycle checks again.
    return removed
