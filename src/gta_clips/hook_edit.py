"""Rebuild an unapproved clip's hook from its original VOD, never over burned captions."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .captions import Transcriber
from .context_text import debate_hook, manual_hook
from .db import connect, worker_lock
from .download import download_vod
from .editing import render
from .filler import pick
from .highlights import Moment
from .review import get_clip


def edit_hook(config, clip_id: str, text: str | None = None, *, suggest_only=False):
    with connect(config.paths.database) as db:
        clip = dict(get_clip(db, clip_id))
        has_uploads = db.execute('SELECT 1 FROM uploads WHERE clip_id=?', (clip_id,)).fetchone()
        # Live clips remember the category they were watched in; VOD clips use the current one.
        burst = db.execute("SELECT game_name FROM live_bursts WHERE 'live-' || stream_id || '-' || anchor = ?",
                           (clip_id,)).fetchone()
    topic = burst[0] if burst else config.twitch.game_name
    account = config.account(clip.get('account'))
    language = (account.language if account else '') or 'en'
    # A clip in the hook test keeps the style it was given (see hook_styles.py).
    style = clip.get('hook_style') or 'quote'
    hook = (debate_hook(clip['transcript'], topic, clip_id, language=language, style=style)
            if text is None else manual_hook(text))
    if suggest_only:
        print(' '.join(filter(None, (hook.label, hook.text))) or '(No hook)')
        return hook
    if clip['state'] != 'pending' or has_uploads:
        raise ValueError('Only pending clips with no upload records can be re-edited')
    base = config.paths.data_dir
    staging = base / 'work'
    staging.mkdir(parents=True, exist_ok=True)
    # A generated filename avoids interpreting a clip ID as a path.
    relative = Path('clips') / (account.name if account else '') / f'edited-{uuid4().hex}.mp4'
    target = base / relative
    with worker_lock(base, 'hook-edit'), TemporaryDirectory(dir=staging, ignore_cleanup_errors=True) as folder:
        work = Path(folder)
        start, duration = clip['start'], clip['duration']
        padding = min(3, start)
        source = download_vod(f"https://www.twitch.tv/videos/{clip['vod_id']}", work,
                              section=(start - padding, start + duration + 3), max_height=720)
        try:
            details = {}
            render(source, work, target, Moment(padding, duration, 0), clip['streamer'],
                   replace(config.editing, layout='fit'),
                   Transcriber(config.editing.whisper_model), details, hook_text=text, topic=topic,
                   hook_key=clip_id, language=language, hook_style=style,
                   filler=pick(clip_id, clip.get('filler'), base, duration))
            with connect(config.paths.database) as db:
                updated = db.execute("""UPDATE clips SET path=?, hook_method=? WHERE id=? AND path=? AND state='pending'
                    AND NOT EXISTS (SELECT 1 FROM uploads WHERE clip_id=clips.id)""",
                    (relative.as_posix(), details.get('hook_method', hook.method), clip_id, clip['path']))
                if updated.rowcount != 1:
                    raise ValueError('Clip changed or was approved while rendering; original kept')
        except BaseException:
            target.unlink(missing_ok=True)
            target.with_suffix('.edit.json').unlink(missing_ok=True)
            raise
    print(f'Updated hook for {clip_id}. Preview it again before approval. Original video retained.')
    return hook
