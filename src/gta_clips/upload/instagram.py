"""Publish Instagram Reels through Instagram Login and private S3 staging."""

import logging
import os
import re
import secrets

from ..oauth import INSTAGRAM_GRAPH, env
from .common import Result, Upload, UploadError, check, request, safe_code


def _s3():
    import boto3
    return boto3.client("s3", region_name=os.getenv("AWS_REGION")), env("INSTAGRAM_S3_BUCKET")


def _stage(upload: Upload) -> str:
    client, bucket = _s3()
    key = upload.checkpoint.get("s3_key")
    if not key:
        key = f"gta-clips/{upload.clip['id']}-{secrets.token_hex(12)}.mp4"
        upload.save(s3_key=key)
    client.upload_file(str(upload.file), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    return client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                         ExpiresIn=6 * 3600)


def _cleanup(upload: Upload):
    key = upload.checkpoint.get("s3_key")
    if key:
        try:
            client, bucket = _s3()
            client.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            logging.warning("S3 staging cleanup failed (%s)", type(exc).__name__)


def send(upload: Upload) -> Result:
    media_id = upload.checkpoint.get("media_id")
    if media_id:
        _cleanup(upload)
        return Result("posted", media_id, "Instagram Reel published")
    version = env("INSTAGRAM_GRAPH_VERSION")
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise UploadError("INSTAGRAM_GRAPH_VERSION must look like v27.0")
    base = f"{INSTAGRAM_GRAPH}/{version}"
    account_id = str(upload.account.get("user_id") or env("INSTAGRAM_USER_ID"))
    if not account_id.isdecimal():
        raise UploadError("Invalid Instagram professional account ID")
    container = upload.checkpoint.get("container")
    if not container:
        try:
            video_url = _stage(upload)
        except (ImportError, RuntimeError) as exc:
            raise UploadError(f"Instagram staging setup failed ({type(exc).__name__})") from None
        except Exception as exc:
            raise UploadError(f"Instagram staging failed ({type(exc).__name__})", retry=True) from None
        response = check(request("POST", f"{base}/{account_id}/media", headers=upload.auth, data={
            "media_type": "REELS", "video_url": video_url, "caption": upload.caption,
        }), 200, 201)
        container = response.json()["id"]
        upload.save(container=container)
        return Result("queued", container, "Instagram is processing the Reel", 60)

    response = check(request("GET", f"{base}/{container}", headers=upload.auth,
                             params={"fields": "status_code"}), 200)
    state = response.json().get("status_code")
    if state == "PUBLISHED":
        _cleanup(upload)
        return Result("posted", container, "Instagram confirmed publication (container ID)")
    if state in ("ERROR", "EXPIRED"):
        _cleanup(upload)
        upload.save(container=None, publish_started=False, publish_polls=0)
        raise UploadError(f"Instagram container {safe_code(state)}", retry=state == "EXPIRED")
    if state != "FINISHED":
        return Result("queued", container, f"Instagram container {safe_code(state)}", 60)
    if upload.checkpoint.get("publish_started"):
        polls = upload.checkpoint.get("publish_polls", 0)
        if polls >= 3:
            raise UploadError("Instagram publishing result is uncertain. Check your Reels.", held=True)
        upload.save(publish_polls=polls + 1)
        return Result("queued", container, "Checking Instagram publish result", 60)

    upload.save(publish_started=True)
    response = request("POST", f"{base}/{account_id}/media_publish", headers=upload.auth,
                       data={"creation_id": container})
    if response.status_code in (400, 401, 403, 429):
        upload.save(publish_started=False)
    check(response, 200, 201)
    media_id = response.json()["id"]
    upload.save(media_id=media_id)
    _cleanup(upload)
    return Result("posted", media_id, "Instagram Reel published")
