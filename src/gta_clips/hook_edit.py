"""Rebuild an unapproved clip's hook from its original VOD, never over burned captions."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .captions import Transcriber
from .context_text import manual_hook, suggest_hook
from .db import connect, worker_lock
from .download import download_vod
from .editing import render
from .highlights import Moment
from .review import get_clip


def edit_hook(config, clip_id: str, text: str | None = None, *, suggest_only=False):
    with connect(config.paths.database) as db:
        clip = dict(get_clip(db, clip_id))
        has_uploads = db.execute('SELECT 1 FROM uploads WHERE clip_id=?', (clip_id,)).fetchone()
    hook = suggest_hook(clip['transcript']) if text is None else manual_hook(text)
    if suggest_only:
        print(hook.text or '(No reliable short quote found)')
        return hook
    if clip['state'] != 'pending' or has_uploads:
        raise ValueError('Only pending clips with no upload records can be re-edited')
    base = config.paths.data_dir
    staging = base / 'work'
    staging.mkdir(parents=True, exist_ok=True)
    # A generated filename avoids interpreting a clip ID as a path.
    relative = Path('clips') / f'edited-{uuid4().hex}.mp4'
    target = base / relative
    with worker_lock(base, 'hook-edit'), TemporaryDirectory(dir=staging, ignore_cleanup_errors=True) as folder:
        work = Path(folder)
        start, duration = clip['start'], clip['duration']
        padding = min(3, start)
        source = download_vod(f"https://www.twitch.tv/videos/{clip['vod_id']}", work,
                              section=(start - padding, start + duration + 3), max_height=720)
        try:
            render(source, work, target, Moment(padding, duration, 0), clip['streamer'],
                   replace(config.editing, renderer='remotion', layout='fit'),
                   Transcriber(config.editing.whisper_model), hook_text=text)
            with connect(config.paths.database) as db:
                updated = db.execute("""UPDATE clips SET path=? WHERE id=? AND path=? AND state='pending'
                    AND NOT EXISTS (SELECT 1 FROM uploads WHERE clip_id=clips.id)""",
                    (relative.as_posix(), clip_id, clip['path']))
                if updated.rowcount != 1:
                    raise ValueError('Clip changed or was approved while rendering; original kept')
        except BaseException:
            target.unlink(missing_ok=True)
            target.with_suffix('.edit.json').unlink(missing_ok=True)
            raise
    print(f'Updated hook for {clip_id}. Preview it again before approval. Original video retained.')
    return hook
