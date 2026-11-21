"""Instagram tokens pasted from Meta's Unified login are checked before saving and never refreshed."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
import json
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips import oauth
from gta_clips.db import connect


def reply(status, body):
    response = MagicMock(ok=200 <= status < 300, status_code=status, content=b"x")
    response.json.return_value = body
    return response


class UnifiedTokenTest(unittest.TestCase):
    def authorize(self, handle, responses):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = Path(folder.name) / "db.sqlite3"
        with patch("builtins.input", return_value="17841458053099788"), \
                patch.object(oauth, "getpass", return_value="EAAtest"), \
                patch.object(oauth.requests, "get", side_effect=responses) as get, \
                patch("builtins.print"):
            oauth.authorize(database, "instagram", "irl-en", handle)
        return database, get

    def test_checks_account_and_saves_without_refresh(self):
        database, get = self.authorize("irlstreamclipshq", [
            reply(200, {"id": "17841458053099788", "username": "irlstreamclipshq"}),
            reply(200, {"data": [{"quota_usage": 0}]}),
        ])
        self.assertTrue(get.call_args_list[0].args[0].startswith("https://graph.facebook.com/v"))
        with patch.object(oauth, "_token_request") as refresh, patch.object(oauth.time, "time",
                                                                          return_value=time.time() + 20 * 365 * 86400):
            token, login = oauth.access_token(database, "instagram", "irl-en")
        refresh.assert_not_called()
        self.assertEqual(token, "EAAtest")
        self.assertEqual((login["user_id"], login["login"]), ("17841458053099788", "unified"))

    def test_rejects_token_for_another_account(self):
        with self.assertRaises(RuntimeError) as caught:
            self.authorize("cs2clipsdailyhq", [reply(200, {"id": "1", "username": "irlstreamclipshq"})])
        self.assertIn("not @cs2clipsdailyhq", str(caught.exception))

    def test_rejected_token_is_not_saved(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = Path(folder.name) / "db.sqlite3"
        with patch("builtins.input", return_value="17841458053099788"), \
                patch.object(oauth, "getpass", return_value="bad"), \
                patch.object(oauth.requests, "get", return_value=reply(400, {"error": {"code": 190}})):
            with self.assertRaises(RuntimeError) as caught:
                oauth.authorize(database, "instagram", "irl-en", None)
        self.assertNotIn("bad", str(caught.exception))
        self.assertEqual(oauth.connected(database), set())


if __name__ == "__main__":
    unittest.main()
