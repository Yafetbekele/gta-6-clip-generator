import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from gta_clips.browser_upload import allowed, claim, exists, finish, ledger, scan, verify_account, open_normal_chrome, chrome_profile, main
from gta_clips.browser_upload import upload_slot, wait_youtube_checks
from gta_clips.db import worker_lock, WorkerBusy


NOTE = '''Channel: Test (test)
Post to:
  YouTube: test, on private@example.com
Sign-in email: secret@example.com

Title:
A funny moment | @streamer

Description:
A gaming moment.
@streamer on Twitch
https://www.twitch.tv/streamer

Hashtags:
#Gaming #Shorts

Clip ID: test-clip-1
'''


class BrowserUploadTests(unittest.TestCase):
    def test_new_checks_require_both_visible_success_results(self):
        for count, visible, passes in ((2, True, True), (1, True, False), (2, False, False)):
            with self.subTest(count=count, visible=visible):
                page = MagicMock()
                legacy = MagicMock()
                legacy.is_visible.return_value = False
                results = MagicMock()
                results.count.return_value = count
                results.nth.return_value.is_visible.return_value = visible
                page.get_by_text.side_effect = lambda text, **kw: legacy if text.startswith('Checks complete') else results
                page.get_by_role.return_value.is_visible.return_value = True
                with patch('gta_clips.browser_upload.time.monotonic', side_effect=[0, 0, 2]):
                    if passes:
                        wait_youtube_checks(page, timeout=1)
                    else:
                        with self.assertRaises(RuntimeError):
                            wait_youtube_checks(page, timeout=1)

    def test_legacy_checks_still_accepted(self):
        page = MagicMock()
        page.get_by_text.return_value.is_visible.return_value = True
        wait_youtube_checks(page)
        page.get_by_role.assert_not_called()

    def test_two_upload_slots_and_release(self):
        with upload_slot(self.root):
            with upload_slot(self.root):
                with self.assertRaises(WorkerBusy):
                    with upload_slot(self.root):
                        self.fail('Third concurrent upload was allowed')
            with upload_slot(self.root):
                pass

    def test_profile_locks_are_independent(self):
        with worker_lock(self.root, 'browser-upload-youtube-first'):
            with worker_lock(self.root, 'browser-upload-youtube-second'):
                pass
            with self.assertRaises(WorkerBusy):
                with worker_lock(self.root, 'browser-upload-youtube-first'):
                    self.fail('Same profile was allowed twice')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / '2026-11-19' / 'test'
        self.folder.mkdir(parents=True)
        self.video = self.folder / 'clip.mp4'
        self.video.write_bytes(b'fake video')
        self.video.with_suffix('.txt').write_text(NOTE, encoding='utf-8')
        self.db = ledger(self.root / 'posts.sqlite3')
        self.addCleanup(self.db.close)

    def test_only_public_sections_are_uploaded(self):
        clip, = scan(self.root, 'test')
        self.assertNotIn('example.com', clip.description)
        self.assertNotIn('Sign-in', clip.description)
        self.assertIn('https://www.twitch.tv/streamer', clip.description)
        self.assertEqual(scan(self.root, 'other'), [])

    def test_login_opens_normal_chrome_in_isolated_profile(self):
        with patch('gta_clips.browser_upload.chrome_executable', return_value=Path('chrome.exe')), \
                patch('gta_clips.browser_upload.subprocess.Popen') as launch:
            open_normal_chrome(self.root, 'youtube', 'test')
        self.assertEqual(launch.call_args.args[0], [
            'chrome.exe', f'--user-data-dir={chrome_profile(self.root, "youtube", "test").resolve()}',
            '--new-window', 'https://studio.youtube.com/',
        ])

    def test_login_before_identity_setup_but_upload_still_blocked(self):
        config = self.root / 'browser-upload.json'
        config.write_text(json.dumps({
            'ready_folder': str(self.root),
            'accounts': {'test': {'youtube': {'channel_id': ''}}},
        }), encoding='utf-8')
        args = ['--settings', str(config), '--account', 'test', '--platform', 'youtube']
        with patch('gta_clips.browser_upload.open_normal_chrome') as launch:
            self.assertEqual(main(args + ['login']), 0)
            launch.assert_called_once()
        for action in ('verify-login', 'run'):
            with self.subTest(action=action), self.assertRaises(SystemExit) as stopped:
                main(args + [action])
            self.assertEqual(stopped.exception.code, 1)

    def test_missing_credit_and_bad_metadata_stop(self):
        self.video.with_suffix('.txt').write_text(NOTE.replace('https://www.twitch.tv/streamer', ''), encoding='utf-8')
        with self.assertRaises(ValueError):
            scan(self.root, 'test')

    def test_claim_survives_crash_and_blocks_renamed_file(self):
        clip, = scan(self.root, 'test')
        claim(self.db, 'youtube', 'test', clip)
        self.assertTrue(exists(self.db, 'youtube', 'test', clip))
        self.video.rename(self.folder / 'renamed.mp4')
        self.video.with_suffix('.txt').rename(self.folder / 'renamed.txt')
        renamed, = scan(self.root, 'test')
        renamed.clip_id = 'different-id-same-bytes'
        self.assertTrue(exists(self.db, 'youtube', 'test', renamed))
        self.assertFalse(exists(self.db, 'tiktok', 'test', renamed))
        self.assertEqual(self.db.execute('select state from posts').fetchone()[0], 'held')

    def test_posted_state_retains_receipt(self):
        clip, = scan(self.root, 'test')
        claim(self.db, 'youtube', 'test', clip)
        finish(self.db, 'youtube', 'test', clip, 'posted', 'https://youtube.com/shorts/example')
        row = self.db.execute('select * from posts').fetchone()
        self.assertEqual(row['state'], 'posted')
        self.assertIn('/shorts/', row['url'])

    def test_spacing_and_rolling_daily_limit_include_held(self):
        clip, = scan(self.root, 'test')
        claim(self.db, 'youtube', 'test', clip)
        self.db.execute('update posts set started=100000')
        self.db.commit()
        self.assertFalse(allowed(self.db, 'youtube', 'test', 4, 3600, 100001))
        self.assertTrue(allowed(self.db, 'youtube', 'test', 4, 3600, 103601))
        self.assertFalse(allowed(self.db, 'youtube', 'test', 1, 0, 103601))
        self.assertTrue(allowed(self.db, 'youtube', 'test', 1, 0, 186401))
        self.assertFalse(allowed(self.db, 'youtube', 'test', 0, 0, 186401))
        self.assertTrue(allowed(self.db, 'youtube', 'test', None, 0, 100001))

    def test_wrong_youtube_channel_is_rejected(self):
        class Page:
            url = 'https://studio.youtube.com/channel/WRONG'
            def goto(self, *args, **kwargs): pass
            def wait_for_url(self, *args, **kwargs): pass
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            verify_account(Page(), 'youtube', {'channel_id': 'EXPECTED'})


if __name__ == '__main__':
    unittest.main()
