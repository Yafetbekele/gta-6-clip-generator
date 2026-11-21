"""Publish Instagram Reels through Meta's Unified login, uploading the file straight to Meta.

Meta's resumable upload takes the MP4 bytes directly, so no public URL or S3 bucket is needed:
create a REELS container with upload_type=resumable, POST the file to the rupload URL it returns,
wait for the container to finish processing, then publish it.
"""

from ..oauth import INSTAGRAM_GRAPH, env, instagram_version
from .common import Result, Upload, UploadError, check, request, safe_code, verify_url

MAX_BYTES = 300 * 1024 * 1024  # Meta's Reel file size limit


def _upload_file(upload: Upload, uri: str):
    """Send the whole file. After an interruption it is sent again from the start of the same container."""
    size = upload.file.stat().st_size
    if size > MAX_BYTES:
        raise UploadError("Clip is larger than Instagram's 300 MB Reel limit")
    with upload.file.open("rb") as body:
        response = request("POST", uri, data=body, headers={
            "Authorization": f"OAuth {upload.token}", "offset": "0", "file_size": str(size),
        })
    check(response, 200, 201)
    try:
        accepted = response.json().get("success") is True
    except ValueError:
        accepted = False
    if not accepted:
        raise UploadError("Instagram did not confirm the video upload", retry=True)


def send(upload: Upload) -> Result:
    media_id = upload.checkpoint.get("media_id")
    if media_id:
        return Result("posted", media_id, "Instagram Reel published")
    try:
        version = instagram_version()
    except ValueError as exc:
        raise UploadError(str(exc)) from None
    base = f"{INSTAGRAM_GRAPH}/{version}"
    account_id = str(upload.account.get("user_id") or env("INSTAGRAM_USER_ID"))
    if not account_id.isdecimal():
        raise UploadError("Invalid Instagram professional account ID")

    container = upload.checkpoint.get("container")
    if not container:
        response = check(request("POST", f"{base}/{account_id}/media", headers=upload.auth,
                                 params={"upload_type": "resumable"},
                                 data={"media_type": "REELS", "caption": upload.caption}), 200, 201)
        body = response.json()
        container, uri = body["id"], verify_url(body.get("uri", ""), "facebook.com")
        upload.save(container=container, upload_uri=uri, uploaded=False)
    if not upload.checkpoint.get("uploaded"):
        _upload_file(upload, upload.checkpoint["upload_uri"])
        upload.save(uploaded=True)
        return Result("queued", container, "Instagram is processing the Reel", 60)

    response = check(request("GET", f"{base}/{container}", headers=upload.auth,
                             params={"fields": "status_code"}), 200)
    state = response.json().get("status_code")
    if state == "PUBLISHED":
        return Result("posted", container, "Instagram confirmed publication (container ID)")
    if state in ("ERROR", "EXPIRED"):
        upload.save(container=None, upload_uri=None, uploaded=False, publish_started=False, publish_polls=0)
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
    return Result("posted", media_id, "Instagram Reel published")
