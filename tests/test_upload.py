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
        self.clip.checkpoint.update(container="123", uploaded=True)
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

    def test_instagram_uploads_the_file_to_meta_without_a_public_url(self):
        self.clip.account["user_id"] = "42"
        uri = "https://rupload.facebook.com/ig-api-upload/v25.0/123"
        with patch("gta_clips.upload.instagram.request", side_effect=[
            response(200, {"id": "123", "uri": uri}), response(200, {"success": True}),
        ]) as request:
            result = instagram.send(self.clip)
        self.assertEqual((result.state, result.remote_id), ("queued", "123"))
        create, send = request.call_args_list
        self.assertEqual(create.args[1], "https://graph.facebook.com/v25.0/42/media")
        self.assertEqual(create.kwargs["params"], {"upload_type": "resumable"})
        self.assertEqual(create.kwargs["data"]["media_type"], "REELS")
        self.assertNotIn("video_url", create.kwargs["data"])
        self.assertEqual(send.args[1], uri)
        self.assertEqual(send.kwargs["headers"], {"Authorization": "OAuth fixture-token", "offset": "0",
                                                  "file_size": str(len(b"example video"))})
        self.assertEqual(self.clip.checkpoint["uploaded"], True)

    def test_instagram_resends_the_file_after_an_interrupted_upload(self):
        self.clip.account["user_id"] = "42"
        uri = "https://rupload.facebook.com/ig-api-upload/v25.0/123"
        with patch("gta_clips.upload.instagram.request", side_effect=[
            response(200, {"id": "123", "uri": uri}), UploadError("Timed out", retry=True),
            response(200, {"success": True}),
        ]) as request:
            with self.assertRaises(UploadError):
                instagram.send(self.clip)
            instagram.send(self.clip)
        self.assertEqual([call.args[1] for call in request.call_args_list],
                         ["https://graph.facebook.com/v25.0/42/media", uri, uri])

    def test_instagram_refuses_an_upload_address_off_facebook(self):
        self.clip.account["user_id"] = "42"
        with patch("gta_clips.upload.instagram.request",
                   return_value=response(200, {"id": "123", "uri": "https://example.com/upload"})):
            with self.assertRaises(UploadError) as caught:
                instagram.send(self.clip)
        self.assertTrue(caught.exception.held)

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
