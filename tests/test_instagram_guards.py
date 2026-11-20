import tempfile
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from gta_clips.browser_upload import (InstagramRestricted, assert_instagram_available,
    verify_saved_account, ledger, account_block, instagram, Clip)
from gta_clips.browser_schedule import readiness


class InstagramGuardTests(unittest.TestCase):
    def test_verification_urls_and_visible_prompts_stop_upload_before_file_send(self):
        for url in ('https://www.instagram.com/accounts/suspended/',
                    'https://www.instagram.com/challenge/123/',
                    'https://www.instagram.com/checkpoint/'):
            page = MagicMock()
            page.url = url
            with self.assertRaises(InstagramRestricted):
                instagram(page, Clip(Path('clip.mp4'), 'id', 'hash', 'Title', 'Description'), True)
            page.locator.assert_not_called()
        page = MagicMock()
        page.url = 'https://www.instagram.com/'
        page.get_by_text.return_value.first.is_visible.return_value = True
        with self.assertRaises(InstagramRestricted):
            assert_instagram_available(page)
        page.get_by_text.return_value.first.is_visible.return_value = False
        assert_instagram_available(page)

    def test_appeal_pause_persists_until_successful_account_verification(self):
        with tempfile.TemporaryDirectory() as directory, closing(ledger(Path(directory) / 'posts.sqlite3')) as db:
            target = {'handle': 'example', 'auto_post': True}
            with patch('gta_clips.browser_upload.verify_account', side_effect=InstagramRestricted('Appeal required')):
                with self.assertRaises(InstagramRestricted):
                    verify_saved_account(None, 'instagram', 'test', target, db)
            self.assertEqual(account_block(db, 'instagram', 'test', target), 'Appeal required')
            self.assertEqual(readiness(db, 'test', 'instagram', target, 10000), 'paused: account verification / appeal required')
            self.assertIsNone(account_block(db, 'youtube', 'test', target))
            self.assertEqual(db.execute('SELECT count(*) FROM posts').fetchone()[0], 0)
            with patch('gta_clips.browser_upload.verify_account'):
                verify_saved_account(None, 'instagram', 'test', target, db)
            self.assertIsNone(account_block(db, 'instagram', 'test', target))

    def test_late_redirect_is_detected_after_navigation_timeout(self):
        with tempfile.TemporaryDirectory() as directory, closing(ledger(Path(directory) / 'posts.sqlite3')) as db:
            page = MagicMock()
            page.url = 'https://www.instagram.com/accounts/suspended/'
            with patch('gta_clips.browser_upload.verify_account', side_effect=RuntimeError('Navigation timed out')):
                with self.assertRaises(InstagramRestricted):
                    verify_saved_account(page, 'instagram', 'test', {'handle': 'example'}, db)
            self.assertIsNotNone(account_block(db, 'instagram', 'test', {'handle': 'example'}))
