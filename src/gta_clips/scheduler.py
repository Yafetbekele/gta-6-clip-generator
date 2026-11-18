"""Send approved clips within each destination's local daily cap and spacing."""

from collections.abc import Callable
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

SENDERS: dict[str, Callable[[Upload], Result]] = {
    "youtube": youtube.send,
    "instagram": instagram.send,
    "tiktok": tiktok.send,
}


def _run_one(config: Config, platform: str) -> bool:
    database = config.paths.database
    now = int(time.time())
    settings = config.schedule[platform]
    with connect(database) as db:
        pacing = db.execute(
            """SELECT MAX(sent_at) AS last,
               COUNT(CASE WHEN date(sent_at, 'unixepoch', 'localtime') = date('now', 'localtime')
                     THEN 1 END) AS today
               FROM uploads WHERE platform=? AND sent_at IS NOT NULL""",
            (platform,),
        ).fetchone()
        if pacing["today"] >= settings.daily_limit:
            return True
        if pacing["last"] and now < pacing["last"] + settings.min_spacing_minutes * 60:
            return True
        job = db.execute(
            """SELECT clips.*, uploads.attempts, uploads.retry_at, uploads.checkpoint,
                      uploads.visibility, uploads.made_for_kids
               FROM uploads JOIN clips ON clips.id = uploads.clip_id
               WHERE uploads.platform=? AND uploads.state='queued' AND clips.state='approved'
                     AND uploads.retry_at <= ?
               ORDER BY clips.created, clips.id LIMIT 1""", (platform, now),
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
        token, account = access_token(database, platform)
        authorization_id = account.get("authorization_id")
        bound = checkpoint.get("authorization_id")
        if bound and bound != authorization_id:
            raise UploadError("The authorized account changed after approval", held=True)
        if not bound and authorization_id:
            checkpoint["authorization_id"] = authorization_id
            persist(checkpoint)
        result = SENDERS[platform](Upload(job, path, token, account, checkpoint, persist))
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
    log = logging.warning if result.state in ("failed", "held") or attempts else logging.info
    log("%s %s %s | %s", platform, job["id"], result.state, detail)
    return not attempts


def publish_due(config: Config) -> int:
    ok = True
    with worker_lock(config.paths.data_dir, "publish"):
        for platform, settings in config.schedule.items():
            if settings.daily_limit > 0:
                ok = _run_one(config, platform) and ok
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
