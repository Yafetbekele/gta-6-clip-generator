import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from gta_clips.browser_upload import allowed, claim, exists, finish, ledger, scan, verify_account, open_normal_chrome, chrome_profile, main
from gta_clips.browser_upload import upload_slot, wait_youtube_checks, confirm_tiktok_post, ensure_tiktok_everyone, instagram, Clip
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
    def test_instagram_legacy_and_icon_editor_paths(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                page = MagicMock()
                page.get_by_role.return_value.is_visible.return_value = legacy
                notice = page.get_by_role.return_value.filter.return_value
                notice.is_visible.return_value = not legacy
                clip = Clip(self.video, 'clip', 'digest', 'Title', 'Credit')
                instagram(page, clip, True)
                if not legacy:
                    page.locator.assert_any_call('svg[aria-label="Post"]')
                    page.locator.assert_any_call('svg[aria-label="Select crop"]')
                    notice.get_by_role.assert_called_once_with('button', name='OK', exact=True)
                captions = [c.kwargs['name'] for c in page.get_by_role.call_args_list if c.args == ('textbox',)]
                self.assertTrue(captions[0].fullmatch('Add a caption...'))
                self.assertTrue(captions[0].fullmatch('Write a caption...'))

    def test_instagram_icon_sidebar_verifies_username_and_rejects_wrong_one(self):
        for actual, passes in [('joethegoat2334', True), ('other_account', False)]:
            page = MagicMock()
            page.get_by_role.return_value.is_visible.return_value = False
            profile = page.locator.return_value.locator.return_value.locator.return_value.filter.return_value.first
            profile.get_attribute.return_value = '/'+actual+'/'
            if passes:
                verify_account(page, 'instagram', {'handle': 'joethegoat2334'})
            else:
                with self.assertRaisesRegex(RuntimeError, 'does not match'):
                    verify_account(page, 'instagram', {'handle': 'joethegoat2334'})

    def test_tiktok_audience_changes_private_to_everyone(self):
        page = MagicMock()
        field = page.get_by_text.return_value.locator.return_value
        everyone, private = MagicMock(), MagicMock()
        everyone.is_visible.return_value = False
        private.count.return_value = 0
        field.get_by_text.side_effect = lambda name, **kw: everyone if name == 'Everyone' else private
        ensure_tiktok_everyone(page)
        private.click.assert_called_once()
        page.get_by_text.assert_any_call('Everyone', exact=True)
        page.get_by_text.return_value.click.assert_called_once()
        everyone.wait_for.assert_called_once_with(state='visible')

    def test_tiktok_ambiguous_audience_blocks_publication(self):
        page = MagicMock()
        field = page.get_by_text.return_value.locator.return_value
        field.get_by_text.return_value.is_visible.return_value = True
        field.get_by_text.return_value.count.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'could not be confirmed'):
            ensure_tiktok_everyone(page)

    def test_tiktok_navigation_anchor_with_nested_listitem(self):
        page = MagicMock()
        page.url = 'https://www.tiktok.com/foryou'
        page.get_by_role.return_value.is_visible.return_value = False
        page.locator.return_value.is_visible.return_value = True
        page.locator.return_value.get_attribute.return_value = '/@gta_6_3_clips'
        verify_account(page, 'tiktok', {'handle': 'gta_6_3_clips'})
        page.locator.assert_any_call('a[data-e2e="nav-profile"]')
        self.assertEqual(page.goto.call_count, 2)

    def test_tiktok_profile_button_checks_resulting_account_url(self):
        for handle, passes in [('gta_6_3_clips', True), ('wrong_account', False)]:
            page = MagicMock()
            page.url = 'https://www.tiktok.com/foryou'
            link, control = MagicMock(), MagicMock()
            link.is_visible.return_value = False
            control.is_visible.return_value = True
            page.get_by_role.side_effect = lambda role, **kw: control if role == 'button' else link
            page.locator.return_value.is_visible.return_value = False
            control.click.side_effect = lambda: setattr(page, 'url', 'https://www.tiktok.com/@'+handle)
            if passes:
                verify_account(page, 'tiktok', {'handle': 'gta_6_3_clips'})
                self.assertEqual(page.goto.call_count, 2)
            else:
                with self.assertRaisesRegex(RuntimeError, 'does not match'):
                    verify_account(page, 'tiktok', {'handle': 'gta_6_3_clips'})
                self.assertEqual(page.goto.call_count, 1)

    def test_tiktok_collapsed_avatar_menu(self):
        page = MagicMock()
        page.url = 'https://www.tiktok.com/foryou'
        page.get_by_role.return_value.is_visible.return_value = False
        page.locator.return_value.is_visible.return_value = False
        page.get_by_role.return_value.get_attribute.return_value = '/@gta_clip83'
        verify_account(page, 'tiktok', {'handle': 'Gta_clip83'})
        page.locator.assert_any_call('svg[class*="StyledAvatar"]')
        page.locator.return_value.hover.assert_called_once()

    def test_tiktok_pending_check_confirmation_clicked_once(self):
        page = MagicMock()
        success = MagicMock()
        success.is_visible.side_effect = [False, False, True]
        modal = MagicMock()
        modal.is_visible.return_value = True
        review = MagicMock()
        review.is_visible.return_value = False
        page.get_by_text.side_effect = lambda text, **kw: (review if not isinstance(text, str) else modal if text == 'Continue to post?' else success)
        confirm_tiktok_post(page)
        page.get_by_role.assert_called_once_with('button', name='Post now', exact=True)
        page.get_by_role.return_value.click.assert_called_once()

    def test_tiktok_without_confirmation_modal_waits_for_success(self):
        page = MagicMock()
        page.get_by_text.return_value.is_visible.return_value = True
        confirm_tiktok_post(page)
        page.get_by_role.assert_not_called()

    def test_tiktok_matching_post_under_review_completes_submission(self):
        page = MagicMock()
        absent, entry = MagicMock(), MagicMock()
        absent.is_visible.return_value = False
        entry.count.return_value = 1
        entry.is_visible.return_value = True
        row = entry.locator.return_value
        row.count.return_value = 1
        row.get_by_text.return_value.is_visible.return_value = True
        page.get_by_text.side_effect = lambda text, **kw: entry if text == 'Unique clip title' else absent
        self.assertEqual(confirm_tiktok_post(page, title='Unique clip title'), 'under_review')
        page.get_by_role.assert_not_called()

    def test_tiktok_under_review_notice_completes_without_reposting(self):
        page = MagicMock()
        success, review = MagicMock(), MagicMock()
        success.is_visible.return_value = False
        review.is_visible.return_value = True
        page.get_by_text.side_effect = lambda text, **kw: success if isinstance(text, str) else review
        self.assertEqual(confirm_tiktok_post(page), 'under_review')
        page.get_by_role.assert_not_called()

    def test_tiktok_verifies_on_main_site_then_returns_to_studio(self):
        page = MagicMock()
        page.url = 'https://www.tiktok.com/foryou'
        page.get_by_role.return_value.get_attribute.return_value = '/@Gta_clip80'
        verify_account(page, 'tiktok', {'handle': 'Gta_clip80'})
        self.assertEqual([c.args[0] for c in page.goto.call_args_list],
                         ['https://www.tiktok.com/foryou', 'https://www.tiktok.com/tiktokstudio/upload'])
        page.locator.return_value.first.wait_for.assert_called_once_with(state='attached')

    def test_tiktok_wrong_account_never_opens_upload_page(self):
        page = MagicMock()
        page.url = 'https://www.tiktok.com/foryou'
        page.get_by_role.return_value.get_attribute.return_value = '/@someone_else'
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            verify_account(page, 'tiktok', {'handle': 'Gta_clip80'})
        self.assertEqual(page.goto.call_count, 1)

    def test_tiktok_about_redirect_is_not_verified(self):
        page = MagicMock()
        page.url = 'https://www.tiktok.com/about?lang=en'
        with self.assertRaisesRegex(RuntimeError, 'redirected verification to About'):
            verify_account(page, 'tiktok', {'handle': 'Gta_clip80'})
        page.get_by_role.assert_not_called()

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
