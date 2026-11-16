"""Send approved videos to the creator inbox using TikTok's draft API."""

from .common import Result, Upload, UploadError, check, request, retry_after, safe_code, verify_url

BASE = "https://open.tiktokapis.com/v2/post/publish"


def _api(upload: Upload, path: str, data: dict) -> dict:
    response = check(request("POST", f"{BASE}/{path}/", headers=upload.auth, json=data), 200)
    payload = response.json()
    code = payload.get("error", {}).get("code", "ok")
    if code != "ok":
        slow = code == "spam_risk_too_many_pending_share"
        raise UploadError(f"TikTok API: {safe_code(code)}",
                          retry=slow or code in {"rate_limit_exceeded", "internal_error"},
                          delay=max(retry_after(response), 3600 if slow else 0))
    return payload["data"]


def send(upload: Upload) -> Result:
    size = upload.file.stat().st_size
    chunk_size = size if size <= 64_000_000 else 16_000_000
    count = size // chunk_size
    publish_id = upload.checkpoint.get("publish_id")
    offset = 0
    if publish_id:
        state = _api(upload, "status/fetch", {"publish_id": publish_id})
        status = state["status"]
        if status == "SEND_TO_USER_INBOX":
            return Result("draft", publish_id, "Open the TikTok inbox to finish and publish")
        if status == "PUBLISH_COMPLETE":
            return Result("posted", publish_id, "TikTok confirmed the creator published the draft")
        if status == "FAILED":
            reason = safe_code(state.get("fail_reason"))
            upload.save(publish_id=None, upload_url=None)
            raise UploadError(f"TikTok draft failed ({reason})", retry=reason == "internal_error")
        offset = int(state.get("uploaded_bytes", 0))
    else:
        created = _api(upload, "inbox/video/init", {"source_info": {
            "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
            "total_chunk_count": count,
        }})
        publish_id = created["publish_id"]
        upload.save(publish_id=publish_id, upload_url=created["upload_url"])

    if offset < size:
        url = verify_url(upload.checkpoint["upload_url"], "tiktokapis.com")
        with upload.file.open("rb") as file:
            file.seek(offset)
            while offset < size:
                remaining = size - offset
                chunk = file.read(remaining if remaining < 2 * chunk_size else chunk_size)
                end = offset + len(chunk) - 1
                response = request("PUT", url, headers={
                    "Content-Type": "video/mp4", "Content-Range": f"bytes {offset}-{end}/{size}",
                }, data=chunk)
                if response.status_code in (403, 404):
                    raise UploadError("TikTok upload URL expired. Check the TikTok inbox.", held=True)
                if response.status_code == 416:
                    return Result("queued", publish_id, "Checking TikTok byte position", 60)
                check(response, 201 if end == size - 1 else 206)
                offset = end + 1
    return Result("queued", publish_id, "Waiting for TikTok to deliver the draft", 60)
