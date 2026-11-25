"""Render on demand: render a saved window only when an account set needs clips to post.

With live.render_on_demand on, the watcher still saves every chat burst and viewer clip as a
window, but render-live leaves them alone until the ready step asks. Each ready run tops every
account set up to live.render_buffer rendered clips that it could post; it asks for the windows
the running tests need most (recipe.need, the same balancing posting uses), the strongest first
within each source. A window rejected while rendering (chat talk, too short) frees its place, so
the next run asks for another. Windows are only asked for while their broadcast is likely still
online; the rest expire unrendered after 48 hours as before.

Without it every window is rendered, about ten for each clip posted.
"""

from pathlib import Path
import logging
import re
import time

from . import caption_styles, hook_styles
from . import filler as fillers
from . import trim as trims
from .config import Config
from .cuts import choose as choose_cut
from .db import connect
from .recipe import add, arms, misfits, need
from .ready import CANDIDATES, NEED_SLACK, picked_arms

# Windows whose moment is older than this are not asked for: the archive may be gone by the time
# the render runs, and the 48-hour limit in render-live would drop them anyway.
FRESH_SECONDS = 36 * 3600
# A request still not rendered after this long (archive late or gone) stops holding a buffer place.
REQUEST_SECONDS = 6 * 3600
# Only clips made this recently count toward an account set's buffer.
BUFFER_SECONDS = 24 * 3600
VIEWS = re.compile(r"\((\d+) views\)")


def planned(config: Config, window) -> dict:
    """The test settings a window's clip will get, known before rendering (all chosen by clip ID)."""
    clip_id = f"live-{window['stream_id']}-{window['anchor']}"
    cut = choose_cut(window["stream_id"], window["anchor"], window["trigger_kind"], config.live.test_cuts)
    return {"id": clip_id, "kind": window["trigger_kind"], "cut_before": cut[0], "cut_after": cut[1],
            "hook_style": hook_styles.choose(clip_id, config.editing.test_hooks),
            "filler": fillers.choose(clip_id, config.editing.filler, config.paths.data_dir),
            "trim": trims.choose(clip_id, config.editing.trim_dead_time),
            "caption_style": caption_styles.choose(clip_id, config.editing.test_captions)}


def strength(window) -> float:
    """How strong a window looked when saved: Twitch views for a viewer clip, the chat spike otherwise."""
    if window["trigger_kind"] == "viewer_clip":
        found = VIEWS.search(window["detail"] or "")
        return float(found.group(1)) if found else 0.0
    baseline = window["baseline"] or 0
    return window["messages"] / baseline if baseline else float(window["messages"])


def ready_clips(config: Config, db, account: str, now: float) -> list:
    """Rendered clips of ``account`` from the last day that the ready step could still post.

    Older clips can still be posted (up to ready.MAX_AGE) but don't count, so each day's posts can
    come from that day's moments.
    """
    base = config.paths.data_dir
    return [clip for clip in db.execute(CANDIDATES, (account, int(now - BUFFER_SECONDS))).fetchall()
            if not misfits(config, dict(clip)) and (base / clip["path"]).is_file()]


def request_renders(config: Config, now: float | None = None) -> int:
    """Ask render-live for the windows that keep each account set's buffer full. Returns how many."""
    if not config.live.render_on_demand:
        return 0
    now = time.time() if now is None else now
    requested = 0
    with connect(config.paths.database) as db:
        counts = picked_arms(db, config)
        for account in config.accounts:
            available = ready_clips(config, db, account.name, now)
            waiting = db.execute(
                """SELECT * FROM live_bursts WHERE account=? AND state='waiting'
                     AND render_requested > ?""", (account.name, int(now - REQUEST_SECONDS))).fetchall()
            short = config.live.render_buffer - len(available) - len(waiting)
            if short <= 0:
                continue
            windows = db.execute(
                """SELECT b.*, f.baseline_per_10s AS baseline FROM live_bursts b
                   LEFT JOIN burst_features f USING (stream_id, anchor)
                   WHERE b.account=? AND b.state='waiting' AND b.render_requested IS NULL
                     AND b.retry_at <= ? AND b.anchor > ?""",
                (account.name, int(now), int(now - FRESH_SECONDS))).fetchall()
            chosen = pick(config, windows, short, counts,
                          [dict(clip) for clip in available] + [planned(config, row) for row in waiting])
            for window in chosen:
                db.execute("UPDATE live_bursts SET render_requested=? WHERE stream_id=? AND anchor=?",
                           (int(now), window["stream_id"], window["anchor"]))
            if chosen:
                logging.info("%s: asked to render %d windows (%d clips ready, %d renders waiting)",
                             account.name, len(chosen), len(available), len(waiting))
            requested += len(chosen)
    return requested


def pick(config: Config, windows: list, wanted: int, counts: dict, upcoming: list) -> list:
    """Up to ``wanted`` windows: what the running tests need most, the strongest first among those.

    ``counts`` are the arms already picked for posting and ``upcoming`` the clips ready or being
    rendered; both count as posted, so the buffer fills with the arms that are behind.
    """
    counts = {name: dict(found) for name, found in counts.items()}
    for clip in upcoming:
        add(counts, arms(config, clip))
    # Strength is only comparable within a source, so rank within each and compare the ranks.
    by_kind: dict[str, list] = {}
    for window in windows:
        by_kind.setdefault(window["trigger_kind"], []).append(window)
    score = {}
    for group in by_kind.values():
        group.sort(key=strength, reverse=True)
        for rank, window in enumerate(group):
            score[(window["stream_id"], window["anchor"])] = rank / len(group)
    remaining = sorted(windows, key=lambda window: score[(window["stream_id"], window["anchor"])])
    window_arms = {(w["stream_id"], w["anchor"]): arms(config, planned(config, w)) for w in remaining}
    known: dict[str, set[str]] = {}
    for found in list(window_arms.values()) + [arms(config, clip) for clip in upcoming]:
        for name, arm in found.items():
            known.setdefault(name, set()).add(arm)
    chosen = []
    while remaining and len(chosen) < wanted:
        needs = [need(counts, window_arms[(w["stream_id"], w["anchor"])], known) for w in remaining]
        top = max(needs)
        window = remaining.pop(next(i for i, value in enumerate(needs) if value >= top - NEED_SLACK))
        add(counts, window_arms[(window["stream_id"], window["anchor"])])
        chosen.append(window)
    return chosen
