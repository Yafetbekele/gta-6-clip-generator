"""Send approved clips within each destination's local daily cap and spacing."""

from collections.abc import Callable
import hashlib
import json
import logging
import time

import requests

from .config import Config
from .db import connect, worker_lock
from .oauth import access_token
from .review import path_for
from .upload import instagram, tiktok, youtube
from .upload.common import Result, Upload, UploadError

def INSTAGRAM_WORK(config: Config):
    """Where 30-second Instagram cuts wait until they are posted."""
    return config.paths.data_dir / "work" / "instagram"


SENDERS: dict[str, Callable[[Upload], Result]] = {
    "youtube": youtube.send,
    "instagram": instagram.send,
    "tiktok": tiktok.send,
}


def spacing_seconds(settings, platform: str, account: str, last_sent: int) -> int:
    """Minimum spacing plus a random extra delay of up to ``jitter_minutes``.

    The extra delay is drawn from the last post's time, so checking again every few minutes
    keeps the same due time instead of drawing a new one each check.
    """
    base = settings.min_spacing_minutes * 60
    if not settings.jitter_minutes:
        return base
    seed = hashlib.sha256(f"{platform}/{account}/{last_sent}".encode()).digest()
    return base + int.from_bytes(seed[:8], "big") % (settings.jitter_minutes * 60 + 1)


def _run_one(config: Config, platform: str, account: str | None = None) -> bool:
    """Send one due clip for ``account`` (None: clips made before account sets) on ``platform``.

    Each account set has its own login, daily cap and spacing per platform.
    """
    if platform == "instagram" and account in config.runtime.instagram_browser_accounts:
        return True
    database = config.paths.database
    owner = account or ""
    now = int(time.time())
    settings = config.schedule[platform]
    with connect(database) as db:
        pacing = db.execute(
            """SELECT MAX(sent_at) AS last,
               COUNT(CASE WHEN date(sent_at, 'unixepoch', 'localtime') = date('now', 'localtime')
                     THEN 1 END) AS today
               FROM uploads JOIN clips ON clips.id = uploads.clip_id
               WHERE uploads.platform=? AND sent_at IS NOT NULL AND COALESCE(clips.account, '')=?""",
            (platform, owner),
        ).fetchone()
        if pacing["today"] >= settings.daily_limit:
            return True
        if pacing["last"] and now < pacing["last"] + spacing_seconds(settings, platform, owner, pacing["last"]):
            return True
        job = db.execute(
            """SELECT clips.*, uploads.attempts, uploads.retry_at, uploads.checkpoint,
                      uploads.visibility, uploads.made_for_kids
               FROM uploads JOIN clips ON clips.id = uploads.clip_id
               WHERE uploads.platform=? AND uploads.state='queued' AND clips.state='approved'
                     AND uploads.retry_at <= ? AND COALESCE(clips.account, '')=?
               ORDER BY clips.created, clips.id LIMIT 1""", (platform, now, owner),
        ).fetchone()
    if job is None or job["retry_at"] > now:
        return True

    checkpoint = json.loads(job["checkpoint"])

    def persist(updated: dict):
        with connect(database) as db:
            db.execute("UPDATE uploads SET checkpoint=? WHERE clip_id=? AND platform=?",
                       (json.dumps(updated), job["id"], platform))

    attempts = job["attempts"]
    try:
        path = path_for(config, job)
        if not path.is_file() or path.stat().st_size == 0:
            raise UploadError("Approved clip file is missing or empty")
        if platform == "instagram":
            path = instagram.prepare(job, path, INSTAGRAM_WORK(config))
        token, login = access_token(database, platform, account)
        authorization_id = login.get("authorization_id")
        bound = checkpoint.get("authorization_id")
        if bound and bound != authorization_id:
            raise UploadError("The authorized account changed after approval", held=True)
        if not bound and authorization_id:
            checkpoint["authorization_id"] = authorization_id
            persist(checkpoint)
        result = SENDERS[platform](Upload(job, path, token, login, checkpoint, persist))
        attempts = 0
    except UploadError as exc:
        attempts += 1
        delay = max(exc.delay, min(3600, 60 * 2 ** min(attempts - 1, 6)))
        state = "held" if exc.held else "queued" if exc.retry else "failed"
        result = Result(state, None, str(exc), delay if state == "queued" else 0)
    except (requests.ConnectionError, requests.Timeout):
        attempts += 1
        result = Result("queued", None, "Unable to reach the OAuth provider",
                        min(3600, 60 * 2 ** min(attempts - 1, 6)))
    except RuntimeError as exc:
        attempts += 1
        result = Result("queued", None, str(exc), 300)
    except Exception as exc:
        attempts += 1
        result = Result("held", None, f"Unexpected {type(exc).__name__}. Check the destination.")

    now = int(time.time())
    sent = now if result.state in ("draft", "posted") else None
    detail = result.detail
    with connect(database) as db:
        db.execute(
            """UPDATE uploads SET state=?, attempts=?, retry_at=?, sent_at=?,
               remote_id=COALESCE(?, remote_id), detail=?, checkpoint=?
               WHERE clip_id=? AND platform=?""",
            (result.state, attempts, now + result.delay, sent, result.remote_id, detail,
             "{}" if sent else json.dumps(checkpoint), job["id"], platform),
        )
    if platform == "instagram" and result.state in ("posted", "failed"):
        for leftover in INSTAGRAM_WORK(config).glob(f"{job['id']}-*.mp4"):
            leftover.unlink(missing_ok=True)
    log = logging.warning if result.state in ("failed", "held") or attempts else logging.info
    log("%s%s %s %s | %s", platform, f"@{account}" if account else "", job["id"], result.state, detail)
    return not attempts


def publish_due(config: Config) -> int:
    ok = True
    with worker_lock(config.paths.data_dir, "publish"):
        owners = [None, *(account.name for account in config.accounts)]
        for platform, settings in config.schedule.items():
            if settings.daily_limit > 0:
                for owner in owners:
                    ok = _run_one(config, platform, owner) and ok
    return int(not ok)


def retry_failed(config: Config, platform: str, clip_id: str):
    with worker_lock(config.paths.data_dir, "publish"):
        with connect(config.paths.database) as db:
            changed = db.execute(
                """UPDATE uploads SET state='queued', retry_at=0, attempts=0, detail=NULL
                   WHERE clip_id=? AND platform=? AND state='failed'""", (clip_id, platform),
            ).rowcount
            if not changed:
                raise ValueError("Only failed uploads can be retried with this command")
