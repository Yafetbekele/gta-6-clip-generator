"""Exercise the upload adapters with official API response shapes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

import requests

from gta_clips.upload import instagram, tiktok, youtube
from gta_clips.upload.common import Upload, UploadError


def response(status, body=None, **headers):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers)
    result._content = json.dumps(body or {}).encode()
    return result


class UploadTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "clip.mp4"
        source.write_bytes(b"example video")
        self.clip = Upload(
            {"id": "12-1", "title": "Clip", "description": "Streamer credit", "hashtags": "#Gaming",
             "visibility": "private", "made_for_kids": False}, source, "fixture-token", {}, {}, lambda _: None,
        )

    def test_youtube_saves_session_before_final_put_and_probes_after_timeout(self):
        endpoint = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=fixture"
        with patch("gta_clips.upload.youtube.request", side_effect=[
            response(200, Location=endpoint), UploadError("Timed out", retry=True),
        ]):
            with self.assertRaises(UploadError):
                youtube.send(self.clip)
        self.assertEqual(self.clip.checkpoint, {"session": endpoint, "sent_final_chunk": True})
        with patch("gta_clips.upload.youtube.request", return_value=response(200, {"id": "abc"})) as request:
            result = youtube.send(self.clip)
        self.assertEqual(result.remote_id, "abc")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["headers"]["Content-Range"], "bytes */13")

    def test_instagram_reconciles_a_lost_publish_response(self):
        self.clip.account["user_id"] = "42"
        self.clip.checkpoint["container"] = "123"
        with patch.dict("os.environ", {"INSTAGRAM_GRAPH_VERSION": "v27.0"}), \
                patch("gta_clips.upload.instagram.request", side_effect=[
                    response(200, {"status_code": "FINISHED"}), UploadError("Timed out", retry=True),
                    response(200, {"status_code": "PUBLISHED"}),
                ]) as request:
            with self.assertRaises(UploadError):
                instagram.send(self.clip)
            result = instagram.send(self.clip)
        self.assertEqual(result.state, "posted")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "POST", "GET"])

    def test_tiktok_sends_video_bytes_then_checks_inbox_status(self):
        url = "https://open-upload.tiktokapis.com/video/?upload_id=fixture"
        with patch("gta_clips.upload.tiktok.request", side_effect=[
            response(200, {"data": {"publish_id": "abc", "upload_url": url}}),
            response(201),
            response(200, {"data": {"status": "SEND_TO_USER_INBOX"}}),
        ]) as request:
            self.assertEqual(tiktok.send(self.clip).state, "queued")
            self.assertEqual(tiktok.send(self.clip).state, "draft")
        put = request.call_args_list[1]
        self.assertEqual(put.args, ("PUT", url))
        self.assertEqual(put.kwargs["data"], b"example video")
        self.assertEqual(put.kwargs["headers"]["Content-Range"], "bytes 0-12/13")
