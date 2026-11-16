"""YouTube Data API resumable video uploads."""

import re

from .common import Result, Upload, UploadError, check, request, verify_url

ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024


def _result(response) -> Result:
    video_id = response.json().get("id")
    if not video_id:
        raise UploadError("YouTube completed the upload without a video ID", held=True)
    return Result("posted", video_id, f"https://www.youtube.com/shorts/{video_id}")


def _offset(response) -> int:
    match = re.fullmatch(r"bytes=0-(\d+)", response.headers.get("Range", ""))
    return int(match.group(1)) + 1 if match else 0


def send(upload: Upload) -> Result:
    size = upload.file.stat().st_size
    headers = upload.auth
    session = upload.checkpoint.get("session")
    if session:
        session = verify_url(session, "googleapis.com")
        response = request("PUT", session, headers={
            **headers, "Content-Length": "0", "Content-Range": f"bytes */{size}",
        })
        if response.status_code in (200, 201):
            return _result(response)
        if response.status_code in (404, 410):
            if upload.checkpoint.get("sent_final_chunk"):
                raise UploadError("YouTube session expired after the final chunk. Check YouTube Studio.", held=True)
            session = None
        else:
            check(response, 308)
            offset = _offset(response)
    if not session:
        response = check(request(
            "POST", ENDPOINT, params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**headers, "X-Upload-Content-Type": "video/mp4",
                     "X-Upload-Content-Length": str(size)},
            json={
                "snippet": {
                    "title": upload.clip["title"],
                    "description": f'{upload.clip["description"]}\n\n{upload.clip["hashtags"]}'
                    .replace("<", "‹").replace(">", "›"),
                    "categoryId": "20",
                },
                "status": {"privacyStatus": upload.clip["visibility"],
                           "selfDeclaredMadeForKids": bool(upload.clip["made_for_kids"])},
            },
        ), 200, 201)
        session = verify_url(response.headers.get("Location", ""), "googleapis.com")
        upload.save(session=session, sent_final_chunk=False)
        offset = 0
    with upload.file.open("rb") as file:
        while offset < size:
            file.seek(offset)
            chunk = file.read(CHUNK)
            end = offset + len(chunk) - 1
            if end == size - 1 and not upload.checkpoint.get("sent_final_chunk"):
                upload.save(sent_final_chunk=True)
            response = request("PUT", session, headers={
                **headers, "Content-Type": "video/mp4", "Content-Range": f"bytes {offset}-{end}/{size}",
            }, data=chunk)
            if response.status_code in (200, 201):
                return _result(response)
            check(response, 308)
            next_offset = _offset(response)
            if next_offset <= offset:
                raise UploadError("YouTube has not acknowledged the latest chunk", retry=True)
            offset = next_offset
    return Result("queued", None, "Waiting for YouTube to finalize the upload", 60)
